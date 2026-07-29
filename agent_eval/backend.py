"""Thin command translation for external Agent CLIs."""

from typing import List


ADAPTER_PROFILES = {
    "pi": "pi-standard-v3",
    "opencode": "opencode-artifact-v4",
}


def adapter_profile(agent: str, opencode_harness: bool = False) -> str:
    if agent == "opencode" and opencode_harness:
        return "opencode-dcda-inline-v1"
    try:
        return ADAPTER_PROFILES[agent]
    except KeyError as error:
        raise ValueError(f"unknown agent backend: {agent}") from error


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
            "--print-logs",
            "--log-level",
            "DEBUG",
            "--pure",
            "run",
            "--format",
            "json",
            "--thinking",
            "--auto",
            "--dir",
            "/workspace",
            "--model",
            f"vllm-local/{model}",
            "--agent",
            "benchmark",
            prompt,
        ]
    raise ValueError(f"unknown agent backend: {agent}")
