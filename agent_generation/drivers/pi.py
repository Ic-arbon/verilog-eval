"""Pi CLI driver for artifact-only VerilogEval samples."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_generation.contracts import AgentEnvironment, AgentRunRequest
from agent_generation.drivers._common import (
    parse_json_object,
    validate_base_url,
    validate_environment_name,
    workspace_environment,
    write_json,
)
from agent_generation.drivers.base import INLINE_ARTIFACT_INSTRUCTION


PI_BINARY = "/agent-tools/node_modules/.bin/pi"
PI_DCD_FRONT_END_COMMAND = "/dcd-front-end"
PI_PROVIDER = "openai-compatible"
PI_CONFIG_DIR = "/workspace/.agent-config/pi"
PI_MESSAGE_UPDATE_FIELDS = (
    "type",
    "contentIndex",
    "delta",
    "content",
    "toolCall",
)
PI_ARTIFACT_SYSTEM_PROMPT = """\
At completion, the sole formal submission is /workspace/TopModule.sv.
It must be a non-empty regular SystemVerilog file satisfying the public specification.
Chat output is not a submission.
"""


def _dcd_child_envelope(event: object) -> Optional[tuple[dict, dict]]:
    if not isinstance(event, dict) or event.get("type") != "entry_appended":
        return None
    entry = event.get("entry")
    if not isinstance(entry, dict) or entry.get("customType") != "dcd_child_event":
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    agent = data.get("agent")
    depth = data.get("depth")
    child = data.get("event")
    if (
        not isinstance(agent, str)
        or not agent
        or isinstance(depth, bool)
        or not isinstance(depth, int)
        or not 1 <= depth <= 4
        or not isinstance(child, dict)
        or not isinstance(child.get("type"), str)
    ):
        return None
    return data, child


@dataclass(frozen=True)
class PiDriver:
    """Translate the shared Agent request into Pi config and argv."""

    base_url: str
    api_key_environment: str = "OPENAI_API_KEY"
    thinking_enabled: bool = True
    entry: Optional[str] = None

    def __post_init__(self) -> None:
        validate_base_url(self.base_url)
        validate_environment_name(self.api_key_environment)
        if self.entry not in {None, "front-end", "native"}:
            raise ValueError(f"unsupported Pi entry: {self.entry}")

    def write_config(self, request: AgentRunRequest) -> tuple[Path, ...]:
        config_dir = request.workspace / ".agent-config" / "pi"
        config_path = config_dir / "models.json"
        settings_path = config_dir / "settings.json"
        context_window = request.max_input_tokens + request.per_call_max_tokens
        config = {
            "providers": {
                PI_PROVIDER: {
                    "baseUrl": self.base_url,
                    "api": "openai-completions",
                    "apiKey": f"${self.api_key_environment}",
                    "authHeader": True,
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                        "maxTokensField": "max_tokens",
                        "thinkingFormat": "qwen-chat-template",
                    },
                    "models": [
                        {
                            "id": request.model,
                            "name": request.model,
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": context_window,
                            "maxTokens": request.per_call_max_tokens,
                            "cost": {
                                "input": 0,
                                "output": 0,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                            },
                        }
                    ],
                }
            }
        }
        settings = {
            "compaction": {
                "enabled": True,
                "reserveTokens": request.per_call_max_tokens,
                "keepRecentTokens": max(1, request.max_input_tokens // 2),
            }
        }
        if self.entry in {"front-end", "native"}:
            # Child subagents spawned by the DCD Extension inherit these defaults
            # from the same PI_CODING_AGENT_DIR settings.json.
            settings.update(
                {
                    "defaultProvider": PI_PROVIDER,
                    "defaultModel": request.model,
                }
            )
        return (
            write_json(config_path, config),
            write_json(settings_path, settings),
        )

    def build_command(self, request: AgentRunRequest) -> tuple[str, ...]:
        thinking_level = "high" if self.thinking_enabled else "off"
        common = (
            PI_BINARY,
            "--mode",
            "json",
            "--no-session",
            "--no-approve",
            "--offline",
            "--no-context-files",
        )
        model = (
            "--provider",
            PI_PROVIDER,
            "--model",
            request.model,
            "--thinking",
            thinking_level,
            "--system-prompt",
            PI_ARTIFACT_SYSTEM_PROMPT,
        )
        if self.entry is None:
            inline_task = INLINE_ARTIFACT_INSTRUCTION + request.prompt_text
            return (
                *common,
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                *model,
                "--tools",
                "read,write,edit,bash",
                inline_task,
            )

        if self.entry == "native":
            # Full automatic discovery: the complete frozen DCD directory is
            # mounted as PI_CODING_AGENT_DIR and Pi discovers every Skill,
            # Agent, and Extension itself. The model selects the Skill and
            # Orchestrator; no front-end command is injected.
            task = request.prompt_text
            if request.rules_text:
                task += "\n\nPublic benchmark rules:\n" + request.rules_text
            return (
                *common,
                "--no-prompt-templates",
                *model,
                task,
            )

        task = request.prompt_text
        if request.rules_text:
            task += "\n\nPublic benchmark rules:\n" + request.rules_text
        payload = {
            "task": task,
            "completion_contract": {
                "scope": "module",
                "required_artifact": "/workspace/TopModule.sv",
                "required_gates": [
                    "interface_contract",
                    "elaboration",
                    "functional_verification",
                ],
                "max_repair_iterations": 3,
            },
        }
        command = PI_DCD_FRONT_END_COMMAND + " " + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            *common,
            "--no-prompt-templates",
            *model,
            command,
        )

    def environment(self, request: AgentRunRequest) -> AgentEnvironment:
        del request
        return workspace_environment(
            extra=(
                ("PI_CODING_AGENT_DIR", PI_CONFIG_DIR),
                ("PI_OFFLINE", "1"),
                ("PI_TELEMETRY", "0"),
                ("PI_SKIP_VERSION_CHECK", "1"),
            ),
            api_key_environment=self.api_key_environment,
        )

    def parse_event(self, line: str) -> Optional[object]:
        event = parse_json_object(line)
        envelope = _dcd_child_envelope(event)
        return envelope[1] if envelope else event

    def classify_budget_event(self, line: str) -> Optional[str]:
        event = self.parse_event(line)
        if not isinstance(event, dict):
            return None
        if event.get("type") == "turn_end":
            return "turn"
        if event.get("type") == "tool_execution_end":
            return "tool"
        return None

    def normalize_trajectory_line(self, line: str) -> str:
        """Remove cumulative Pi snapshots while preserving incremental events."""

        raw_event = parse_json_object(line)
        envelope = _dcd_child_envelope(raw_event)
        event = envelope[1] if envelope else raw_event
        if not isinstance(event, dict) or event.get("type") != "message_update":
            return line
        update = event.get("assistantMessageEvent")
        if not isinstance(update, dict) or not isinstance(update.get("type"), str):
            return line

        compact_update = {
            name: update[name]
            for name in PI_MESSAGE_UPDATE_FIELDS
            if name in update
        }
        compact_event = {
            "type": "message_update",
            "assistantMessageEvent": compact_update,
        }
        if envelope:
            compact_wrapper = dict(raw_event)
            compact_entry = dict(compact_wrapper["entry"])
            compact_data = dict(envelope[0])
            compact_data["event"] = compact_event
            compact_entry["data"] = compact_data
            compact_wrapper["entry"] = compact_entry
            compact_event = compact_wrapper
        return json.dumps(
            compact_event,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
