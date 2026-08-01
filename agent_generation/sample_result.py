"""Candidate inspection and recoverable candidate-last Sample Bundle commit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from agent_generation.contracts import ProcessResult
from agent_generation.manifest import canonical_generation_manifest
from agent_generation.result_contract import (
    ResultContractError,
    validate_result_manifest,
)


INVALID_CANDIDATE_BYTES = (
    b"// VERILOG_EVAL_INVALID_SUBMISSION\n"
    b"VERILOG_EVAL_INVALID_SUBMISSION\n"
)
FORMAL_SUBMISSION_NAME = "TopModule.sv"
MAX_CANDIDATE_BYTES = 1024 * 1024
FaultHook = Optional[Callable[[str], None]]


class SampleInfrastructureError(RuntimeError):
    """A sample bundle could not be safely classified, committed, or recovered."""


def sample_sidecar_paths(output_path: Path) -> dict[str, Path]:
    output = Path(output_path)
    return {
        "manifest": output.with_name(f"{output.stem}-generation.json"),
        "trajectory": output.with_name(f"{output.stem}-trajectory.jsonl"),
        "stderr": output.with_name(f"{output.stem}-stderr.log"),
    }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bounded_regular(path: Path, maximum: int) -> Optional[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
        ):
            return None
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if not content or len(content) > maximum:
            return None
        return bytes(content)
    finally:
        os.close(descriptor)


def _inspect_candidate(
    workspace: Path,
    *,
    starter_sha256: Optional[str],
    secret_values: Iterable[str],
) -> tuple[str, bytes, Optional[str], Optional[int]]:
    candidate_path = Path(workspace) / FORMAL_SUBMISSION_NAME
    try:
        metadata = candidate_path.lstat()
    except FileNotFoundError:
        return "missing", INVALID_CANDIDATE_BYTES, None, None
    except OSError:
        return "invalid", INVALID_CANDIDATE_BYTES, None, None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return "invalid", INVALID_CANDIDATE_BYTES, None, None
    content = _read_bounded_regular(candidate_path, MAX_CANDIDATE_BYTES)
    if content is None:
        return "invalid", INVALID_CANDIDATE_BYTES, None, None
    digest = _sha256(content)
    if starter_sha256 is not None and digest == starter_sha256:
        return "missing", INVALID_CANDIDATE_BYTES, None, None
    for secret in secret_values:
        if secret and secret.encode("utf-8") in content:
            return "invalid", INVALID_CANDIDATE_BYTES, None, None
    return "published", content, digest, len(content)


def _scrub(value: str, secrets: Iterable[str]) -> bytes:
    scrubbed = value
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed.encode("utf-8")


def _artifact(content: bytes) -> dict[str, object]:
    return {"sha256": _sha256(content), "size_bytes": len(content)}


def _manifest(
    *,
    sample_id: str,
    agent: str,
    model: str,
    run_config_sha256: str,
    process: ProcessResult,
    limits: Mapping[str, int],
    runtime: Mapping[str, str],
    submission_status: str,
    source_sha256: Optional[str],
    source_size_bytes: Optional[int],
    candidate: bytes,
    trajectory: bytes,
    stderr: bytes,
) -> dict:
    manifest = {
        "sample_id": sample_id,
        "producer": {
            "kind": "agent",
            "agent": agent,
            "model": model,
            "run_config_sha256": run_config_sha256,
        },
        "execution": {
            "status": process.status,
            "exit_code": process.exit_code,
            "duration_seconds": process.duration_seconds,
            "termination_reason": process.termination_reason,
        },
        "limits": dict(limits),
        "submission": {
            "status": submission_status,
            "source_sha256": source_sha256,
            "source_size_bytes": source_size_bytes,
        },
        "usage": asdict(process.usage),
        "runtime": dict(runtime),
        "artifacts": {
            "candidate": _artifact(candidate),
            "trajectory": _artifact(trajectory),
            "stderr": _artifact(stderr),
        },
    }
    validate_result_manifest(
        manifest,
        expected_sample_id=sample_id,
        expected_run_config_sha256=run_config_sha256,
    )
    return manifest


def _read_regular_bytes(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SampleInfrastructureError(f"cannot read bundle artifact: {path.name}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise SampleInfrastructureError(
                f"bundle artifact is not bounded owned regular: {path.name}"
            )
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > maximum:
            raise SampleInfrastructureError(f"bundle artifact is oversized: {path.name}")
        return bytes(content)
    finally:
        os.close(descriptor)


def validate_sample_bundle(
    output_path: Path,
    expected_run_config_sha256: str,
) -> dict:
    """Validate candidate plus all sidecars by canonical bytes and exact hashes."""

    output = Path(output_path)
    paths = sample_sidecar_paths(output)
    candidate = _read_regular_bytes(output, MAX_CANDIDATE_BYTES)
    trajectory = _read_regular_bytes(paths["trajectory"], 256 * 1024 * 1024)
    stderr = _read_regular_bytes(paths["stderr"], 64 * 1024 * 1024)
    manifest_bytes = _read_regular_bytes(paths["manifest"], 4 * 1024 * 1024)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        validate_result_manifest(
            manifest,
            expected_sample_id=output.stem,
            expected_run_config_sha256=expected_run_config_sha256,
        )
        if canonical_generation_manifest(manifest) != manifest_bytes:
            raise ResultContractError("manifest bytes are not canonical")
    except (UnicodeDecodeError, json.JSONDecodeError, ResultContractError) as error:
        raise SampleInfrastructureError(f"sample manifest is invalid: {error}") from error
    contents = {
        "candidate": candidate,
        "trajectory": trajectory,
        "stderr": stderr,
    }
    for name, content in contents.items():
        expected = manifest["artifacts"][name]
        if expected != _artifact(content):
            raise SampleInfrastructureError(f"sample {name} hash or size mismatch")
    if manifest["submission"]["status"] != "published" and candidate != INVALID_CANDIDATE_BYTES:
        raise SampleInfrastructureError("invalid submission does not use frozen placeholder")
    return manifest


def _recover_before_generation(output_path: Path, config_digest: str) -> Optional[dict]:
    output = Path(output_path)
    sidecars = sample_sidecar_paths(output)
    try:
        candidate_metadata = output.lstat()
    except FileNotFoundError:
        candidate_metadata = None
    if candidate_metadata is not None:
        if (
            not stat.S_ISREG(candidate_metadata.st_mode)
            or candidate_metadata.st_uid != os.geteuid()
            or candidate_metadata.st_nlink != 1
        ):
            raise SampleInfrastructureError("candidate completion marker is unsafe")
        return validate_sample_bundle(output, config_digest)

    present: list[Path] = []
    for path in sidecars.values():
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise SampleInfrastructureError("partial sample sidecar is suspicious")
        present.append(path)
    if present:
        directory_descriptor = os.open(
            output.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            for path in present:
                os.unlink(path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        except OSError as error:
            raise SampleInfrastructureError("cannot recover partial sample sidecars") from error
        finally:
            os.close(directory_descriptor)
    return None


def _write_temp(directory_descriptor: int, final_name: str, content: bytes) -> str:
    temporary_name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return temporary_name


def _fault(hook: FaultHook, event: str) -> None:
    if hook is not None:
        hook(event)


def commit_sample_bundle(
    *,
    workspace: Path,
    output_path: Path,
    sample_id: str,
    agent: str,
    model: str,
    run_config_sha256: str,
    process: ProcessResult,
    limits: Mapping[str, int],
    runtime: Mapping[str, str],
    secret_values: Iterable[str] = (),
    starter_sha256: Optional[str] = None,
    fault: FaultHook = None,
) -> dict:
    """Inspect `/workspace/TopModule.sv` and commit sidecars then candidate."""

    output = Path(output_path)
    if output.stem != sample_id:
        raise SampleInfrastructureError("sample ID does not match output target")
    output.parent.mkdir(parents=True, exist_ok=True)
    parent_metadata = output.parent.lstat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
    ):
        raise SampleInfrastructureError("sample output directory is unsafe")
    lock_path = output.parent / f".{sample_id}.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        lock_metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise SampleInfrastructureError("sample lock is unsafe")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SampleInfrastructureError("sample target is already active") from error
        existing = _recover_before_generation(output, run_config_sha256)
        if existing is not None:
            return existing

        secrets = tuple(secret_values)
        status, candidate, source_sha, source_size = _inspect_candidate(
            Path(workspace),
            starter_sha256=starter_sha256,
            secret_values=secrets,
        )
        trajectory = _scrub(process.stdout, secrets)
        stderr = _scrub(process.stderr, secrets)
        manifest = _manifest(
            sample_id=sample_id,
            agent=agent,
            model=model,
            run_config_sha256=run_config_sha256,
            process=process,
            limits=limits,
            runtime=runtime,
            submission_status=status,
            source_sha256=source_sha,
            source_size_bytes=source_size,
            candidate=candidate,
            trajectory=trajectory,
            stderr=stderr,
        )
        manifest_bytes = canonical_generation_manifest(manifest)
        paths = sample_sidecar_paths(output)
        payloads = {
            paths["trajectory"].name: trajectory,
            paths["stderr"].name: stderr,
            paths["manifest"].name: manifest_bytes,
            output.name: candidate,
        }
        directory_descriptor = os.open(
            output.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        temporaries: dict[str, str] = {}
        candidate_renamed = False
        try:
            for final_name, content in payloads.items():
                temporaries[final_name] = _write_temp(
                    directory_descriptor,
                    final_name,
                    content,
                )
            _fault(fault, "temps_synced")
            for path in (paths["trajectory"], paths["stderr"], paths["manifest"]):
                os.replace(
                    temporaries.pop(path.name),
                    path.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
            os.fsync(directory_descriptor)
            _fault(fault, "sidecars_synced")
            candidate_temporary = temporaries.pop(output.name)
            os.link(
                candidate_temporary,
                output.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            os.unlink(candidate_temporary, dir_fd=directory_descriptor)
            candidate_renamed = True
            _fault(fault, "candidate_renamed")
            os.fsync(directory_descriptor)
            _fault(fault, "candidate_synced")
        except BaseException as error:
            if candidate_renamed:
                try:
                    os.unlink(output.name, dir_fd=directory_descriptor)
                    os.fsync(directory_descriptor)
                except OSError as cleanup_error:
                    raise SampleInfrastructureError(
                        "candidate rollback failed; run is corrupt"
                    ) from cleanup_error
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SampleInfrastructureError(f"sample bundle commit failed: {error}") from error
        finally:
            for temporary in temporaries.values():
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
            os.close(directory_descriptor)
        return manifest
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
