import json
from pathlib import Path


def write_agent_configs(
    workspace: Path,
    base_url: str,
    model: str,
    max_tokens: int = 8192,
    temperature: float = 0.6,
    top_p: float = 0.95,
) -> None:
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

    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {
            "build": {
                "temperature": temperature,
                "top_p": top_p,
                "steps": 20,
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
                        "tool_call": True,
                        "limit": {"context": 262144, "output": max_tokens},
                    }
                },
            }
        },
    }
    (workspace / "opencode.json").write_text(
        json.dumps(opencode_config, indent=2) + "\n"
    )
