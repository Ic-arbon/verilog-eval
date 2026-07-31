"""Join canonical Verilog correctness with orthogonal Agent run metadata."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from agent_generation.manifest import atomic_write_text


REPORT_SCHEMA_VERSION = "agent-evaluation/v1"
GENERATION_SCHEMA_VERSION = "agent-generation/v1"
EXECUTION_STATUSES = frozenset({"completed", "timeout", "error"})
SUBMISSION_STATUSES = frozenset({"published", "missing", "invalid"})
USAGE_FIELDS = ("input_tokens", "output_tokens", "turns", "tool_calls")
LIMIT_FIELDS = (
    "timeout_seconds",
    "max_turns",
    "max_tool_calls",
    "max_input_tokens",
    "per_call_max_tokens",
)
RUNTIME_FIELDS = (
    "source_revision",
    "source_diff_sha256",
    "docker_image",
    "docker_image_id",
    "agent_tools_versions",
    "agent_tools_lock_sha256",
    "agent_tools_content_sha256",
    "api_base_url",
)


class ReportError(ValueError):
    """A canonical result or Agent manifest violated the report contract."""


def _integer(value: object, name: str, *, optional: bool = False) -> Optional[int]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError(f"{name} must be a non-negative integer")
    return value


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportError(f"{name} must be an object")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ReportError(f"{name} must be a non-negative number")
    return float(value)


def _read_manifest(path: Path, expected_sample_id: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportError(f"cannot read Agent manifest {path}: {error}") from error
    manifest = _mapping(manifest, str(path))
    if manifest.get("schema_version") != GENERATION_SCHEMA_VERSION:
        raise ReportError(f"unsupported generation manifest schema in {path}")
    if manifest.get("sample_id") != expected_sample_id:
        raise ReportError(f"sample ID mismatch in {path}")

    producer = _mapping(manifest.get("producer"), f"producer in {path}")
    if producer.get("kind") != "agent":
        raise ReportError(f"manifest is not an Agent producer: {path}")
    for field in ("agent", "profile", "model"):
        if not isinstance(producer.get(field), str) or not producer[field]:
            raise ReportError(f"producer.{field} is invalid in {path}")

    execution = _mapping(manifest.get("execution"), f"execution in {path}")
    if execution.get("status") not in EXECUTION_STATUSES:
        raise ReportError(f"invalid execution status in {path}")
    exit_code = execution.get("exit_code")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ReportError(f"invalid execution exit code in {path}")
    _number(execution.get("duration_seconds"), f"duration in {path}")

    limits = _mapping(manifest.get("limits"), f"limits in {path}")
    for field in LIMIT_FIELDS:
        if _integer(limits.get(field), f"limits.{field} in {path}") == 0:
            raise ReportError(f"limits.{field} must be positive in {path}")

    submission = _mapping(manifest.get("submission"), f"submission in {path}")
    if submission.get("status") not in SUBMISSION_STATUSES:
        raise ReportError(f"invalid submission status in {path}")

    usage = _mapping(manifest.get("usage"), f"usage in {path}")
    for field in USAGE_FIELDS:
        _integer(usage.get(field), f"usage.{field} in {path}", optional=True)
    if not isinstance(usage.get("usage_source"), str) or not usage["usage_source"]:
        raise ReportError(f"invalid usage source in {path}")

    runtime = manifest.get("runtime")
    if runtime is not None:
        runtime = _mapping(runtime, f"runtime in {path}")
        for field in RUNTIME_FIELDS:
            value = runtime.get(field)
            if value is not None and (
                not isinstance(value, str)
                or not value
                or any(character in value for character in "\r\n\x00")
            ):
                raise ReportError(f"invalid runtime.{field} in {path}")
    return manifest


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


def build_agent_report(summary_csv: Path) -> dict[str, Any]:
    """Build one report without conflating execution, submission, and grading."""

    summary_csv = Path(summary_csv)
    root = summary_csv.parent
    try:
        summary_file = summary_csv.open(newline="", encoding="utf-8")
    except OSError as error:
        raise ReportError(f"cannot read canonical summary: {error}") from error

    samples: list[dict[str, Any]] = []
    with summary_file:
        for line_number, row in enumerate(csv.reader(summary_file), 1):
            if len(row) != 5:
                raise ReportError(f"invalid canonical summary row {line_number}")
            problem, passed_text, sample_count_text, rate_text, statuses = row
            if not problem or Path(problem).name != problem or problem in {".", ".."}:
                raise ReportError(f"invalid problem name on row {line_number}")
            try:
                passed = int(passed_text)
                sample_count = int(sample_count_text)
                canonical_rate = float(rate_text)
            except ValueError as error:
                raise ReportError(
                    f"invalid canonical counts on row {line_number}"
                ) from error
            if passed < 0 or sample_count <= 0 or len(statuses) != sample_count:
                raise ReportError(f"inconsistent canonical row {line_number}")
            if passed != statuses.count("."):
                raise ReportError(f"canonical pass count mismatch on row {line_number}")
            expected_rate = passed / sample_count
            if abs(canonical_rate - expected_rate) > 1e-5:
                raise ReportError(f"canonical pass rate mismatch on row {line_number}")

            for sample_number, grader_status in enumerate(statuses, 1):
                sample_id = f"{problem}_sample{sample_number:02d}"
                manifest_path = root / problem / f"{sample_id}-generation.json"
                if not manifest_path.is_file():
                    raise ReportError(f"missing Agent manifest: {manifest_path}")
                manifest = _read_manifest(manifest_path, sample_id)
                samples.append(
                    {
                        "sample_id": sample_id,
                        "problem": problem,
                        "sample_number": sample_number,
                        "correctness": {
                            "passed": grader_status == ".",
                            "grader_status": grader_status,
                        },
                        "producer": manifest["producer"],
                        "execution": manifest["execution"],
                        "limits": manifest["limits"],
                        "submission": manifest["submission"],
                        "usage": manifest["usage"],
                        "runtime": manifest.get("runtime"),
                        "manifest": str(manifest_path.relative_to(root)),
                    }
                )

    if not samples:
        raise ReportError("canonical summary contains no samples")

    passed_samples = sum(sample["correctness"]["passed"] for sample in samples)
    published_samples = [
        sample for sample in samples if sample["submission"]["status"] == "published"
    ]
    conditional_passed = sum(
        sample["correctness"]["passed"] for sample in published_samples
    )
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
        "schema_version": REPORT_SCHEMA_VERSION,
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
                conditional_passed / len(published_samples)
                if published_samples
                else None
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


def write_agent_report(
    report: dict[str, Any],
    *,
    json_path: Path,
    text_path: Path,
) -> None:
    """Atomically write machine-readable and concise human-readable reports."""

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
    atomic_write_text(
        Path(json_path),
        json.dumps(report, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(Path(text_path), "\n".join(lines))
