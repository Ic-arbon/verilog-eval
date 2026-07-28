import json
from pathlib import Path


def write_agent_configs(workspace: Path, base_url: str, model: str) -> None:
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
                        "maxTokens": 8192,
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
                "temperature": 0.6,
                "top_p": 0.95,
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
                        "limit": {"context": 262144, "output": 8192},
                    }
                },
            }
        },
    }
    (workspace / "opencode.json").write_text(
        json.dumps(opencode_config, indent=2) + "\n"
    )
