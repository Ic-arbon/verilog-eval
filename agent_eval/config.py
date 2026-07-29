import json
from pathlib import Path


def write_agent_configs(
    workspace: Path,
    agent: str,
    base_url: str,
    model: str,
    max_tokens: int = 8192,
    temperature: float = 0.6,
    top_p: float = 0.95,
    opencode_harness: bool = False,
    opencode_thinking: bool = True,
) -> None:
    if agent == "pi":
        pi_dir = workspace / ".pi-agent"
        pi_dir.mkdir(parents=True, exist_ok=True)
        pi_config = {
            "providers": {
                "vllm-local": {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": "local",
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                        "maxTokensField": "max_tokens",
                        "thinkingFormat": "qwen-chat-template",
                    },
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "reasoning": True,
                            "contextWindow": 262144,
                            "maxTokens": max_tokens,
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
        (pi_dir / "models.json").write_text(json.dumps(pi_config, indent=2) + "\n")
        return

    if agent != "opencode":
        raise ValueError(f"unknown agent backend: {agent}")

    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {
            "benchmark": {
                "description": "Complete isolated workspace artifact tasks.",
                "mode": "primary",
                "temperature": temperature,
                "top_p": top_p,
                "steps": 20,
                "prompt": (
                    "You are a coding agent operating in an isolated workspace. "
                    "Complete implementation tasks by using the available file "
                    "tools to create or modify requested workspace artifacts. "
                    "Workspace files are the deliverables; a text response does "
                    "not satisfy a file-creation request. Before finishing, verify "
                    "that the requested artifact exists."
                ),
                "permission": {
                    "*": "deny",
                    "read": "allow",
                    "edit": "allow",
                    "bash": "allow",
                    "task": (
                        {"*": "deny", "chip-*": "allow"}
                        if opencode_harness
                        else "deny"
                    ),
                    "skill": "deny",
                },
            }
        },
        "provider": {
            "vllm-local": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "vLLM Local",
                "options": {"baseURL": base_url, "apiKey": "local"},
                "models": {
                    model: {
                        "name": model,
                        "temperature": True,
                        "reasoning": True,
                        "tool_call": True,
                        "interleaved": {"field": "reasoning_content"},
                        "options": {
                            "chat_template_kwargs": {
                                "enable_thinking": opencode_thinking,
                            }
                        },
                        "limit": {"context": 262144, "output": max_tokens},
                    }
                },
            }
        },
    }
    (workspace / "opencode.json").write_text(
        json.dumps(opencode_config, indent=2) + "\n"
    )
