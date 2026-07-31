"""Structured sidecar serialization for one Agent generation sample."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from agent_generation.contracts import (
    AgentGenerationResult,
    AgentRunRequest,
    RuntimeProvenance,
)


SCHEMA_VERSION = "agent-generation/v1"


def sidecar_path(output_path: Path, suffix: str) -> Path:
    return output_path.with_name(f"{output_path.stem}{suffix}")


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_generation_sidecars(
    *,
    output_path: Path,
    request: AgentRunRequest,
    profile_id: str,
    runtime_provenance: RuntimeProvenance,
    result: AgentGenerationResult,
) -> None:
    """Persist normalized metadata without retaining the ephemeral workspace."""

    process = result.process
    submission = result.submission
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": request.sample_id,
        "producer": {
            "kind": "agent",
            "agent": request.agent_name,
            "profile": profile_id,
            "model": request.model,
        },
        "execution": {
            "status": process.status,
            "exit_code": process.exit_code,
            "duration_seconds": process.duration_seconds,
            "termination_reason": process.termination_reason,
        },
        "limits": {
            "timeout_seconds": request.timeout_seconds,
            "max_turns": request.max_turns,
            "max_tool_calls": request.max_tool_calls,
            "max_input_tokens": request.max_input_tokens,
            "per_call_max_tokens": request.per_call_max_tokens,
        },
        "submission": {
            "status": submission.status,
            "sha256": submission.sha256,
            "size_bytes": submission.size_bytes,
        },
        "usage": asdict(process.usage),
        "runtime": asdict(runtime_provenance),
    }

    atomic_write_text(
        sidecar_path(output_path, "-generation.json"),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(
        sidecar_path(output_path, "-trajectory.jsonl"),
        process.stdout,
    )
    atomic_write_text(
        sidecar_path(output_path, "-stderr.log"),
        process.stderr,
    )
