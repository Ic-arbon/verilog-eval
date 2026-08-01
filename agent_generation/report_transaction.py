"""Recoverable text-first, JSON-last Agent report transaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional


_SHA256 = re.compile(r"[0-9a-f]{64}")
FaultHook = Optional[Callable[[str], None]]


class ReportTransactionError(RuntimeError):
    """A report dependency, marker, or filesystem transaction is invalid."""


def _read_regular(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReportTransactionError(f"cannot read report dependency: {path.name}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ReportTransactionError(f"report dependency is not bounded regular: {path.name}")
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > maximum:
            raise ReportTransactionError(f"report dependency is oversized: {path.name}")
        return bytes(content)
    finally:
        os.close(descriptor)


def _canonical_json(value: Mapping) -> bytes:
    if "schema_version" in value:  # owned-version-negative-guard
        raise ReportTransactionError("numbered report schema fields are forbidden")
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ReportTransactionError(f"report is not canonical JSON: {error}") from error
    return (text + "\n").encode("utf-8")


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_report_pair(
    summary_csv: Path,
    json_path: Path,
    text_path: Path,
    expected_run_config_sha256: str,
) -> dict:
    """Validate all report dependencies from exact persisted bytes."""

    if _SHA256.fullmatch(expected_run_config_sha256) is None:
        raise ReportTransactionError("expected run config digest is invalid")
    summary = _read_regular(Path(summary_csv), 16 * 1024 * 1024)
    text = _read_regular(Path(text_path), 16 * 1024 * 1024)
    marker = _read_regular(Path(json_path), 64 * 1024 * 1024)
    try:
        value = json.loads(marker.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportTransactionError("report marker is not valid JSON") from error
    if not isinstance(value, dict) or _canonical_json(value) != marker:
        raise ReportTransactionError("report marker bytes are not canonical")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "run_config_sha256",
        "canonical_summary_sha256",
        "text_sha256",
        "text_size_bytes",
    }:
        raise ReportTransactionError("report evidence fields are invalid")
    expected = {
        "run_config_sha256": expected_run_config_sha256,
        "canonical_summary_sha256": _hash(summary),
        "text_sha256": _hash(text),
        "text_size_bytes": len(text),
    }
    if evidence != expected:
        raise ReportTransactionError("report evidence does not match persisted bytes")
    return value


def _write_temp(directory_descriptor: int, final_name: str, content: bytes) -> str:
    name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        name,
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
    return name


def _fault(hook: FaultHook, event: str) -> None:
    if hook is not None:
        hook(event)


def _existing_type(path: Path) -> Optional[int]:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None


def commit_report_pair(
    report: Mapping,
    *,
    text: str,
    summary_csv: Path,
    run_config_sha256: str,
    json_path: Path,
    text_path: Path,
    fault: FaultHook = None,
) -> dict:
    """Commit human text first and canonical JSON last as completion marker."""

    summary_path = Path(summary_csv)
    marker_path = Path(json_path)
    human_path = Path(text_path)
    if marker_path.parent != human_path.parent or summary_path.parent != marker_path.parent:
        raise ReportTransactionError("summary and report outputs must share one directory")
    if _SHA256.fullmatch(run_config_sha256) is None:
        raise ReportTransactionError("run config digest is invalid")
    marker_mode = _existing_type(marker_path)
    if marker_mode is not None:
        if not stat.S_ISREG(marker_mode):
            raise ReportTransactionError("report completion marker is nonregular")
        return validate_report_pair(
            summary_path,
            marker_path,
            human_path,
            run_config_sha256,
        )

    summary = _read_regular(summary_path, 16 * 1024 * 1024)
    text_bytes = text.encode("utf-8")
    if len(text_bytes) > 16 * 1024 * 1024:
        raise ReportTransactionError("report text exceeds bound")
    text_mode = _existing_type(human_path)
    if text_mode is not None and not stat.S_ISREG(text_mode):
        raise ReportTransactionError("partial report text is nonregular")

    if "evidence" in report:
        raise ReportTransactionError("report payload already contains reserved evidence")
    committed = dict(report)
    committed["evidence"] = {
        "run_config_sha256": run_config_sha256,
        "canonical_summary_sha256": _hash(summary),
        "text_sha256": _hash(text_bytes),
        "text_size_bytes": len(text_bytes),
    }
    marker_bytes = _canonical_json(committed)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    directory_descriptor = os.open(
        marker_path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    temporaries: list[str] = []
    marker_renamed = False
    try:
        if text_mode is not None:
            os.unlink(human_path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        text_temp = _write_temp(directory_descriptor, human_path.name, text_bytes)
        temporaries.append(text_temp)
        marker_temp = _write_temp(directory_descriptor, marker_path.name, marker_bytes)
        temporaries.append(marker_temp)
        _fault(fault, "temps_synced")
        os.replace(
            text_temp,
            human_path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporaries.remove(text_temp)
        os.fsync(directory_descriptor)
        _fault(fault, "text_synced")
        os.replace(
            marker_temp,
            marker_path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporaries.remove(marker_temp)
        marker_renamed = True
        _fault(fault, "json_renamed")
        os.fsync(directory_descriptor)
        _fault(fault, "json_synced")
    except BaseException as error:
        if marker_renamed:
            try:
                os.unlink(marker_path.name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError as cleanup_error:
                raise ReportTransactionError(
                    "report marker rollback failed; run is corrupt"
                ) from cleanup_error
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, ReportTransactionError):
            raise
        raise ReportTransactionError(f"report transaction failed: {error}") from error
    finally:
        for temporary in temporaries:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        os.close(directory_descriptor)
    return committed
