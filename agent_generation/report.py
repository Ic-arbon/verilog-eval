"""Exact correctness join and recoverable Agent report publication."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from agent_generation.sample_result import SampleInfrastructureError, validate_sample_bundle


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
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise ReportTransactionError(f"report dependency is not bounded owned regular: {path.name}")
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
        existing = validate_report_pair(
            summary_path,
            marker_path,
            human_path,
            run_config_sha256,
        )
        expected_payload = dict(existing)
        expected_payload.pop("evidence", None)
        if expected_payload != dict(report) or _read_regular(
            human_path, 16 * 1024 * 1024
        ) != text.encode("utf-8"):
            raise ReportTransactionError("existing report does not match requested payload")
        return existing

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
        os.link(
            marker_temp,
            marker_path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        os.unlink(marker_temp, dir_fd=directory_descriptor)
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


USAGE_FIELDS = ("input_tokens", "output_tokens", "turns", "tool_calls")


class ReportError(ValueError):
    """Canonical grading rows or committed sample bundles are inconsistent."""


def _usage_aggregate(values: Iterable[Optional[int]]) -> dict[str, Optional[int]]:
    materialized = list(values)
    known = [value for value in materialized if value is not None]
    unknown_samples = len(materialized) - len(known)
    known_sum = sum(known)
    return {
        "value": known_sum if unknown_samples == 0 else None,
        "known_sum": known_sum,
        "known_samples": len(known),
        "unknown_samples": unknown_samples,
    }


def _sorted_counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _canonical_rows(summary_csv: Path) -> list[tuple[str, int, int, str]]:
    try:
        summary_file = Path(summary_csv).open(newline="", encoding="utf-8")
    except OSError as error:
        raise ReportError(f"cannot read canonical summary: {error}") from error
    rows: list[tuple[str, int, int, str]] = []
    seen: set[str] = set()
    with summary_file:
        for line_number, row in enumerate(csv.reader(summary_file), 1):
            if len(row) != 5:
                raise ReportError(f"invalid canonical summary row {line_number}")
            problem, passed_text, sample_count_text, rate_text, statuses = row
            if (
                not problem
                or Path(problem).name != problem
                or problem in {".", ".."}
                or problem in seen
            ):
                raise ReportError(f"invalid or duplicate problem on row {line_number}")
            seen.add(problem)
            try:
                passed = int(passed_text)
                sample_count = int(sample_count_text)
                canonical_rate = float(rate_text)
            except ValueError as error:
                raise ReportError(f"invalid canonical counts on row {line_number}") from error
            if (
                passed < 0
                or sample_count <= 0
                or len(statuses) != sample_count
                or passed != statuses.count(".")
            ):
                raise ReportError(f"inconsistent canonical row {line_number}")
            expected_rate = int((passed / sample_count) * 100) / 100.0
            if abs(canonical_rate - expected_rate) > 1e-9:
                raise ReportError(f"canonical pass rate mismatch on row {line_number}")
            rows.append((problem, passed, sample_count, statuses))
    if not rows:
        raise ReportError("canonical summary contains no samples")
    return rows


def build_agent_report(
    summary_csv: Path,
    *,
    run_config_sha256: str,
    expected_problems: Optional[Sequence[str]] = None,
    expected_samples_per_problem: Optional[int] = None,
    expected_config: Optional[dict] = None,
    endpoint_evidence_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Join exact Icarus rows with hash-valid orthogonal Agent bundles."""

    summary_path = Path(summary_csv)
    root = summary_path.parent
    rows = _canonical_rows(summary_path)
    if expected_problems is not None and [row[0] for row in rows] != list(expected_problems):
        raise ReportError("canonical summary problem set/order does not match run config")
    samples: list[dict[str, Any]] = []
    common_identity = None
    for problem, _passed, sample_count, statuses in rows:
        if (
            expected_samples_per_problem is not None
            and sample_count != expected_samples_per_problem
        ):
            raise ReportError("canonical summary sample count does not match run config")
        for sample_number, grader_status in enumerate(statuses, 1):
            sample_id = f"{problem}_sample{sample_number:02d}"
            output = root / problem / f"{sample_id}.sv"
            try:
                manifest = validate_sample_bundle(output, run_config_sha256)
            except SampleInfrastructureError as error:
                raise ReportError(f"invalid Sample Bundle {sample_id}: {error}") from error
            identity = {
                "producer": manifest["producer"],
                "limits": manifest["limits"],
                "runtime": manifest["runtime"],
            }
            if common_identity is None:
                common_identity = identity
            elif identity != common_identity:
                raise ReportError("Sample Bundles contain mixed producer/runtime identities")
            if expected_config is not None:
                expected_limits = {
                    "timeout_seconds": expected_config["limits"]["timeout_seconds"],
                    "max_turns": expected_config["limits"]["max_turns"],
                    "max_tool_calls": expected_config["limits"]["max_tool_calls"],
                    "max_input_tokens": expected_config["limits"]["max_input_tokens"],
                    "max_output_tokens": expected_config["limits"]["max_output_tokens"],
                }
                expected_runtime = {
                    "source_revision": expected_config["runtime"]["source_commit"],
                    "docker_image_id": expected_config["runtime"]["docker_image_id"],
                    "docker_daemon_identity": expected_config["runtime"]["docker_daemon_identity"],
                    "agent_tools_content_sha256": expected_config["runtime"]["agent_tools"]["content_sha256"],
                    "endpoint_base_url": expected_config["endpoint"]["base_url"],
                    "endpoint_evidence_sha256": endpoint_evidence_sha256,
                }
                if (
                    manifest["producer"]["agent"] != expected_config["agent"]["name"]
                    or manifest["producer"]["model"] != expected_config["agent"]["model"]
                    or manifest["limits"] != expected_limits
                    or manifest["runtime"] != expected_runtime
                ):
                    raise ReportError("Sample Bundle identity does not match run config")
            samples.append(
                {
                    "sample_id": sample_id,
                    "problem": problem,
                    "sample_number": sample_number,
                    "correctness": {
                        "passed": grader_status == ".",
                        "grader_status": grader_status,
                    },
                    "producer": dict(manifest["producer"]),
                    "execution": dict(manifest["execution"]),
                    "limits": dict(manifest["limits"]),
                    "submission": dict(manifest["submission"]),
                    "usage": dict(manifest["usage"]),
                    "runtime": dict(manifest["runtime"]),
                    "manifest": str(
                        output.with_name(f"{sample_id}-generation.json").relative_to(root)
                    ),
                }
            )
    if expected_problems is not None and expected_samples_per_problem is not None:
        expected_total = len(expected_problems) * expected_samples_per_problem
        if len(samples) != expected_total:
            raise ReportError("canonical summary does not contain the exact configured set")

    passed_samples = sum(sample["correctness"]["passed"] for sample in samples)
    published = [
        sample for sample in samples if sample["submission"]["status"] == "published"
    ]
    conditional_passed = sum(sample["correctness"]["passed"] for sample in published)
    duration_sum = sum(sample["execution"]["duration_seconds"] for sample in samples)
    usage = {
        field: _usage_aggregate(sample["usage"][field] for sample in samples)
        for field in USAGE_FIELDS
    }
    usage["total_tokens"] = _usage_aggregate(
        (
            sample["usage"]["input_tokens"] + sample["usage"]["output_tokens"]
            if sample["usage"]["input_tokens"] is not None
            and sample["usage"]["output_tokens"] is not None
            else None
        )
        for sample in samples
    )
    usage["source_counts"] = _sorted_counts(
        sample["usage"]["usage_source"] for sample in samples
    )
    sample_total = len(samples)
    return {
        "run_config_sha256": run_config_sha256,
        "correctness": {
            "samples": sample_total,
            "passed": passed_samples,
            "failed": sample_total - passed_samples,
            "pass_rate": passed_samples / sample_total,
            "grader_status_counts": _sorted_counts(
                sample["correctness"]["grader_status"] for sample in samples
            ),
        },
        "execution": {
            "status_counts": _sorted_counts(
                sample["execution"]["status"] for sample in samples
            ),
            "duration_seconds_sum": duration_sum,
            "duration_seconds_mean": duration_sum / sample_total,
        },
        "submission": {
            "status_counts": _sorted_counts(
                sample["submission"]["status"] for sample in samples
            ),
            "conditional_passed": conditional_passed,
            "conditional_pass_rate": (
                conditional_passed / len(published) if published else None
            ),
        },
        "usage": usage,
        "samples": samples,
    }


def _format_counts(counts: dict[str, int]) -> str:
    return " ".join(f"{name}={count}" for name, count in sorted(counts.items()))


def _format_usage(label: str, aggregate: dict[str, Optional[int]]) -> str:
    if aggregate["value"] is not None:
        return f"{label}: {aggregate['value']}"
    return (
        f"{label}: unavailable "
        f"(known_sum={aggregate['known_sum']} "
        f"known_samples={aggregate['known_samples']} "
        f"unknown_samples={aggregate['unknown_samples']})"
    )


def format_agent_report(report: dict[str, Any]) -> str:
    correctness = report["correctness"]
    lines = [
        "Agent evaluation",
        (
            f"Verilog Pass@1: {correctness['passed']}/{correctness['samples']} "
            f"({100 * correctness['pass_rate']:.2f}%)"
        ),
        f"Execution: {_format_counts(report['execution']['status_counts'])}",
        f"Submission: {_format_counts(report['submission']['status_counts'])}",
        _format_usage("Input tokens", report["usage"]["input_tokens"]),
        _format_usage("Output tokens", report["usage"]["output_tokens"]),
        _format_usage("Total tokens", report["usage"]["total_tokens"]),
        "",
    ]
    return "\n".join(lines)


def write_agent_report(
    report: dict[str, Any],
    *,
    summary_csv: Path,
    run_config_sha256: str,
    json_path: Path,
    text_path: Path,
) -> dict:
    """Publish text then JSON marker after exact report construction."""

    return commit_report_pair(
        report,
        text=format_agent_report(report),
        summary_csv=summary_csv,
        run_config_sha256=run_config_sha256,
        json_path=json_path,
        text_path=text_path,
    )


__all__ = [
    "ReportError",
    "ReportTransactionError",
    "build_agent_report",
    "format_agent_report",
    "validate_report_pair",
    "write_agent_report",
]
