"""Pi CLI driver for artifact-only VerilogEval samples."""

from __future__ import annotations

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
PI_PROVIDER = "vllm-local"
PI_CONFIG_DIR = "/workspace/.agent-config/pi"
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

    def __post_init__(self) -> None:
        validate_base_url(self.base_url)
        validate_environment_name(self.api_key_environment)

    @property
    def profile_id(self) -> str:
        suffix = "thinking" if self.thinking_enabled else "no-thinking"
        return f"pi-minimal-system-inline-artifact-{suffix}-v1"

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
        return (
            write_json(config_path, config),
            write_json(settings_path, settings),
        )

    def build_command(self, request: AgentRunRequest) -> tuple[str, ...]:
        thinking_level = "high" if self.thinking_enabled else "off"
        inline_task = INLINE_ARTIFACT_INSTRUCTION + request.prompt_text
        return (
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
