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
from agent_generation.drivers.base import ARTIFACT_INSTRUCTION


PI_BINARY = "/agent-tools/node_modules/.bin/pi"
PI_PROVIDER = "vllm-local"
PI_CONFIG_DIR = "/workspace/.agent-config/pi"


@dataclass(frozen=True)
class PiDriver:
    """Translate the shared Agent request into Pi config and argv."""

    base_url: str
    api_key_environment: str = "OPENAI_API_KEY"
    thinking_enabled: bool = True
    context_window: int = 262144

    def __post_init__(self) -> None:
        validate_base_url(self.base_url)
        validate_environment_name(self.api_key_environment)
        if self.context_window <= 0:
            raise ValueError("context_window must be a positive integer")

    @property
    def profile_id(self) -> str:
        suffix = "thinking" if self.thinking_enabled else "no-thinking"
        return f"pi-artifact-{suffix}-v1"

    def write_config(self, request: AgentRunRequest) -> tuple[Path, ...]:
        config_path = request.workspace / ".agent-config" / "pi" / "models.json"
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
                            "contextWindow": self.context_window,
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
        return (write_json(config_path, config),)

    def build_command(self, request: AgentRunRequest) -> tuple[str, ...]:
        thinking_level = "high" if self.thinking_enabled else "off"
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
            "--tools",
            "read,write,edit,bash",
            ARTIFACT_INSTRUCTION,
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
