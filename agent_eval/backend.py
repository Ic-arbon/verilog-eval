"""Thin command translation for external Agent CLIs."""

from typing import List


def build_agent_command(agent: str, model: str, prompt: str) -> List[str]:
    """Translate a neutral benchmark task into one external CLI invocation."""
    if agent == "pi":
        return [
            "/agent-tools/node_modules/.bin/pi",
            "--mode",
            "json",
            "--no-session",
            "--approve",
            "--offline",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--provider",
            "vllm-local",
            "--model",
            model,
            "--api-key",
            "local",
            "--tools",
            "read,write,edit,bash",
            prompt,
        ]
    if agent == "opencode":
        return [
            "/agent-tools/node_modules/.bin/opencode",
            "run",
            "--format",
            "json",
            "--pure",
            "--auto",
            "--dir",
            "/workspace",
            "--model",
            f"vllm-local/{model}",
            "--agent",
            "build",
            prompt,
        ]
    raise ValueError(f"unknown agent backend: {agent}")
