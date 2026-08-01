"""Shared validation and serialization helpers for external Agent drivers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from agent_generation.contracts import AgentEnvironment


ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")


def validate_environment_name(name: str) -> None:
    if ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("api_key_environment is not a valid environment name")


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def parse_json_object(line: str) -> Optional[dict]:
    try:
        value = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def workspace_environment(
    *,
    extra: tuple[tuple[str, str], ...],
    api_key_environment: str,
) -> AgentEnvironment:
    common = (
        ("HOME", "/workspace/.home"),
        ("XDG_CACHE_HOME", "/workspace/.cache"),
        ("XDG_CONFIG_HOME", "/workspace/.config"),
        ("XDG_DATA_HOME", "/workspace/.local/share"),
        ("XDG_STATE_HOME", "/workspace/.local/state"),
    )
    return AgentEnvironment(
        variables=common + extra,
        inherit=(api_key_environment,),
    )
