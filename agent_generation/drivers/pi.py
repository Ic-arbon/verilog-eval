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
PI_DCD_DISPATCH_BINARY = "/dcd-dispatch"
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
This is an artifact-only benchmark. To submit, you MUST invoke the write or edit tool
and create /workspace/TopModule.sv. A shell command or code block written in chat text
is never executed, and chat text is never a submission. Do not finish until you have
used a tool to create the file and then verified the file exists.
"""


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
        if self.entry not in {None, "rtl-module"}:
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
            },
            # The host owns the sample deadline. Pi's five-minute HTTP idle
            # default can otherwise terminate a long reasoning stream early.
            "httpIdleTimeoutMs": 0,
        }
        return (
            write_json(config_path, config),
            write_json(settings_path, settings),
        )

    def build_command(self, request: AgentRunRequest) -> tuple[str, ...]:
        thinking_level = "high" if self.thinking_enabled else "off"
        inline_task = INLINE_ARTIFACT_INSTRUCTION + request.prompt_text
        pi_command = (
            PI_BINARY,
            "--mode",
            "json",
            "--no-session",
            "--no-approve",
            "--offline",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--provider",
            PI_PROVIDER,
            "--model",
            request.model,
            "--thinking",
            thinking_level,
            "--system-prompt",
            PI_ARTIFACT_SYSTEM_PROMPT,
            "--tools",
            "read,write,edit,bash",
            inline_task,
        )
        if self.entry is None:
            return pi_command
        return (
            PI_DCD_DISPATCH_BINARY,
            "--entry",
            self.entry,
            "--",
            *pi_command,
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
        return parse_json_object(line)

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

        event = self.parse_event(line)
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
        return json.dumps(
            compact_event,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
