"""Safely publish the formal Agent artifact to VerilogEval's output path."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Optional

from agent_generation.contracts import SubmissionResult


DEFAULT_MAX_CANDIDATE_BYTES = 1024 * 1024
FORMAL_SUBMISSION_NAME = "TopModule.sv"


def _failure_candidate(reason: str) -> bytes:
    return (
        f"// VERILOG_EVAL_GENERATION_FAILED: {reason}\n"
        "VERILOG_EVAL_GENERATION_FAILED\n"
    ).encode("utf-8")


def _atomic_write(output_path: Path, content: bytes) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_regular_candidate(
    candidate_path: Path,
    max_candidate_bytes: int,
) -> Optional[bytes]:
    if candidate_path.is_symlink():
        return None

    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    try:
        file_descriptor = os.open(candidate_path, open_flags)
    except (FileNotFoundError, OSError):
        return None

    try:
        file_status = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            return None
        if file_status.st_size <= 0 or file_status.st_size > max_candidate_bytes:
            return None
        with os.fdopen(file_descriptor, "rb", closefd=False) as candidate_file:
            content = candidate_file.read(max_candidate_bytes + 1)
        if not content or len(content) > max_candidate_bytes:
            return None
        return content
    finally:
        os.close(file_descriptor)


def _publish_failure(
    output_path: Path,
    status: str,
    reason: str,
) -> SubmissionResult:
    _atomic_write(output_path, _failure_candidate(reason))
    return SubmissionResult(
        status=status,
        output_path=output_path,
        sha256=None,
        size_bytes=None,
    )


def publish_submission(
    workspace: Path,
    output_path: Path,
    *,
    starter_sha256: Optional[str] = None,
    max_candidate_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES,
) -> SubmissionResult:
    """Publish `/workspace/TopModule.sv` without following untrusted symlinks."""

    if max_candidate_bytes <= 0:
        raise ValueError("max_candidate_bytes must be a positive integer")
    if not workspace.is_dir():
        raise FileNotFoundError(f"workspace does not exist: {workspace}")

    candidate_path = workspace / FORMAL_SUBMISSION_NAME
    if not candidate_path.exists() and not candidate_path.is_symlink():
        return _publish_failure(output_path, "missing", "missing_submission")

    candidate = _read_regular_candidate(candidate_path, max_candidate_bytes)
    if candidate is None:
        return _publish_failure(output_path, "invalid", "invalid_submission")

    content = candidate
    candidate_sha256 = hashlib.sha256(content).hexdigest()
    if starter_sha256 is not None and candidate_sha256 == starter_sha256:
        return _publish_failure(output_path, "missing", "unchanged_starter")

    _atomic_write(output_path, content)
    return SubmissionResult(
        status="published",
        output_path=output_path,
        sha256=candidate_sha256,
        size_bytes=len(content),
    )
