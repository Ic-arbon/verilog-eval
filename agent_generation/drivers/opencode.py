"""OpenCode CLI driver for artifact-only VerilogEval samples."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_generation.contracts import AgentEnvironment, AgentRunRequest
from agent_generation.drivers._common import (
    parse_json_object,
    validate_base_url,
    validate_environment_name,
    validate_sampling,
    workspace_environment,
    write_json,
)
from agent_generation.drivers.base import (
    ARTIFACT_INSTRUCTION,
    INLINE_ARTIFACT_INSTRUCTION,
)


OPENCODE_BINARY = "/agent-tools/node_modules/.bin/opencode"
OPENCODE_PROVIDER = "vllm-local"
OPENCODE_CONFIG = "/workspace/.agent-config/opencode.json"


@dataclass(frozen=True)
class OpenCodeDriver:
    """Translate the shared Agent request into OpenCode config and argv."""

    base_url: str
    api_key_environment: str = "OPENAI_API_KEY"
    temperature: float = 0.6
    top_p: float = 0.95
    thinking_enabled: bool = True

    def __post_init__(self) -> None:
        validate_base_url(self.base_url)
        validate_environment_name(self.api_key_environment)
        validate_sampling(self.temperature, self.top_p)

    @property
    def profile_id(self) -> str:
        suffix = "thinking" if self.thinking_enabled else "no-thinking"
        return f"opencode-inline-artifact-{suffix}-v1"

    def write_config(self, request: AgentRunRequest) -> tuple[Path, ...]:
        config_path = request.workspace / ".agent-config" / "opencode.json"
        context_window = request.max_input_tokens + request.per_call_max_tokens
        config = {
            "$schema": "https://opencode.ai/config.json",
            "agent": {
                "benchmark": {
                    "description": "Complete one isolated artifact benchmark.",
                    "mode": "primary",
                    "model": f"{OPENCODE_PROVIDER}/{request.model}",
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "steps": request.max_turns,
                    "prompt": ARTIFACT_INSTRUCTION,
                    "permission": {
                        "*": "deny",
                        "read": "allow",
                        "edit": "allow",
                        "bash": "allow",
                        "task": "deny",
                        "skill": "deny",
                        "webfetch": "deny",
                        "websearch": "deny",
                        "external_directory": "deny",
                    },
                }
            },
            "provider": {
                OPENCODE_PROVIDER: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "vLLM Local",
                    "options": {
                        "baseURL": self.base_url,
                        "apiKey": f"{{env:{self.api_key_environment}}}",
                    },
                    "models": {
                        request.model: {
                            "name": request.model,
                            "reasoning": True,
                            "temperature": True,
                            "tool_call": True,
                            "interleaved": {"field": "reasoning_content"},
                            "limit": {
                                "context": context_window,
                                "output": request.per_call_max_tokens,
                            },
                            "options": {
                                "chat_template_kwargs": {
                                    "enable_thinking": self.thinking_enabled
                                }
                            },
                        }
                    },
                }
            },
            "mcp": {},
            "plugin": [],
        }
        return (write_json(config_path, config),)

    def build_command(self, request: AgentRunRequest) -> tuple[str, ...]:
        inline_task = INLINE_ARTIFACT_INSTRUCTION + request.prompt_text
        return (
            OPENCODE_BINARY,
            "--pure",
            "run",
            "--format",
            "json",
            "--thinking",
            "--dir",
            "/workspace",
            "--model",
            f"{OPENCODE_PROVIDER}/{request.model}",
            "--agent",
            "benchmark",
            inline_task,
        )

    def environment(self, request: AgentRunRequest) -> AgentEnvironment:
        del request
        return workspace_environment(
            extra=(
                ("OPENCODE_CONFIG", OPENCODE_CONFIG),
                ("OPENCODE_PURE", "1"),
                ("OPENCODE_DISABLE_EXTERNAL_SKILLS", "1"),
                ("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1"),
            ),
            api_key_environment=self.api_key_environment,
        )

    def parse_event(self, line: str) -> Optional[object]:
        return parse_json_object(line)

    def classify_budget_event(self, line: str) -> Optional[str]:
        event = self.parse_event(line)
        if not isinstance(event, dict) or event.get("type") != "tool_use":
            return None
        part = event.get("part")
        if not isinstance(part, dict):
            return None
        state = part.get("state")
        if isinstance(state, dict) and state.get("status") == "completed":
            return "tool"
        return None

    def normalize_trajectory_line(self, line: str) -> str:
        """Keep OpenCode events unchanged because they are already incremental."""

        return line
