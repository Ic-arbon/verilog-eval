"""Stable unnumbered predicate for one committed Agent sample bundle."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping, Optional


_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAMPLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_RESERVED_KEYS = frozenset(
    {
        "schema_version",  # owned-version-negative-guard
        "profile",
        "profile_id",  # owned-version-negative-guard
    }
)
_LEAKY_KEY = re.compile(
    r"(?:api[_-]?key|secret|credential|token_value|host[_-]?path|run[_-]?config|hidden|reference)",
    re.IGNORECASE,
)
HOST_BUDGET_EXIT_CODES = {"max_turns": 86, "max_tool_calls": 87}
EXECUTION_STATUSES = frozenset({"completed", "timeout", "error"})
SUBMISSION_STATUSES = frozenset({"published", "missing", "invalid"})
USAGE_SOURCES = frozenset({"trajectory", "unavailable"})


class ResultContractError(ValueError):
    """A sample manifest does not satisfy the stable result predicate."""


def _mapping(
    value: Any,
    name: str,
    *,
    exact_fields: Optional[set[str]] = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResultContractError(f"{name} must be an object")
    if exact_fields is not None and set(value) != exact_fields:
        raise ResultContractError(f"{name} has invalid fields")
    return value


def _string(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
    ):
        raise ResultContractError(f"{name} must be a non-empty single-line string")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _string(value, name)
    if _SHA256.fullmatch(value) is None:
        raise ResultContractError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _integer(
    value: Any,
    name: str,
    *,
    optional: bool = False,
    positive: bool = False,
) -> Optional[int]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultContractError(f"{name} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ResultContractError(f"{name} must be {qualifier}")
    return value


def _duration(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ResultContractError("execution.duration_seconds is invalid")
    return float(value)


def _validate_execution(execution: dict[str, Any]) -> None:
    status = execution["status"]
    exit_code = execution["exit_code"]
    reason = execution["termination_reason"]
    if status not in EXECUTION_STATUSES:
        raise ResultContractError("execution.status is invalid")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ResultContractError("execution.exit_code is invalid")
    _duration(execution["duration_seconds"])

    if status == "completed":
        valid = exit_code == 0 and reason is None
    elif status == "timeout":
        valid = exit_code == 124 and reason == "timeout"
    elif reason in HOST_BUDGET_EXIT_CODES:
        valid = exit_code == HOST_BUDGET_EXIT_CODES[reason]
    else:
        valid = (
            reason is None
            and exit_code is not None
            and exit_code != 0
            and exit_code not in HOST_BUDGET_EXIT_CODES.values()
            and exit_code != 124
        )
    if not valid:
        raise ResultContractError("execution fields are inconsistent")


def _validate_extensions(value: Any, *, depth: int = 0, budget: list[int]) -> None:
    if depth > 8:
        raise ResultContractError("manifest extension is too deeply nested")
    budget[0] += 1
    if budget[0] > 1000:
        raise ResultContractError("manifest extension has too many values")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise ResultContractError("manifest extension integer is out of range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResultContractError("manifest extension number is not finite")
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 4096 or "\x00" in value:
            raise ResultContractError("manifest extension string is invalid")
        if value.startswith(("/", "file://")) or "../" in value:
            raise ResultContractError("manifest extension contains a host path")
        return
    if isinstance(value, list):
        for child in value:
            _validate_extensions(child, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ResultContractError("manifest extension key is invalid")
            if key in _RESERVED_KEYS or _LEAKY_KEY.search(key):
                raise ResultContractError("manifest extension contains a reserved field")
            _validate_extensions(child, depth=depth + 1, budget=budget)
        return
    raise ResultContractError("manifest extension contains an unsafe type")


def validate_result_manifest(
    value: Any,
    *,
    expected_sample_id: Optional[str] = None,
    expected_run_config_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Validate required result semantics while safely bounding extensions."""

    manifest = _mapping(value, "manifest")
    required = {
        "sample_id",
        "producer",
        "execution",
        "limits",
        "submission",
        "usage",
        "runtime",
        "artifacts",
    }
    if not required.issubset(manifest):
        raise ResultContractError("manifest is missing required fields")
    if _RESERVED_KEYS.intersection(manifest):
        raise ResultContractError("manifest contains a reserved old field")

    sample_id = _string(manifest["sample_id"], "sample_id")
    if _SAMPLE_ID.fullmatch(sample_id) is None:
        raise ResultContractError("sample_id is unsafe")
    if expected_sample_id is not None and sample_id != expected_sample_id:
        raise ResultContractError("sample_id does not match the expected target")

    producer = _mapping(
        manifest["producer"],
        "producer",
        exact_fields={"kind", "agent", "model", "run_config_sha256"},
    )
    if producer["kind"] != "agent":
        raise ResultContractError("producer.kind must be agent")
    _string(producer["agent"], "producer.agent")
    _string(producer["model"], "producer.model")
    config_digest = _sha256(
        producer["run_config_sha256"], "producer.run_config_sha256"
    )
    if (
        expected_run_config_sha256 is not None
        and config_digest != expected_run_config_sha256
    ):
        raise ResultContractError("producer run config does not match the run")

    execution = _mapping(
        manifest["execution"],
        "execution",
        exact_fields={
            "status",
            "exit_code",
            "duration_seconds",
            "termination_reason",
        },
    )
    _validate_execution(execution)

    limits = _mapping(
        manifest["limits"],
        "limits",
        exact_fields={
            "timeout_seconds",
            "max_turns",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
        },
    )
    for name, limit in limits.items():
        _integer(limit, f"limits.{name}", positive=True)

    submission = _mapping(
        manifest["submission"],
        "submission",
        exact_fields={"status", "source_sha256", "source_size_bytes"},
    )
    status = submission["status"]
    if status not in SUBMISSION_STATUSES:
        raise ResultContractError("submission.status is invalid")
    if status == "published":
        _sha256(submission["source_sha256"], "submission.source_sha256")
        _integer(
            submission["source_size_bytes"],
            "submission.source_size_bytes",
            positive=True,
        )
    elif (
        submission["source_sha256"] is not None
        or submission["source_size_bytes"] is not None
    ):
        raise ResultContractError("non-published source identity must be null")

    usage = _mapping(
        manifest["usage"],
        "usage",
        exact_fields={
            "input_tokens",
            "output_tokens",
            "turns",
            "tool_calls",
            "usage_source",
        },
    )
    if usage["usage_source"] not in USAGE_SOURCES:
        raise ResultContractError("usage.usage_source is invalid")
    if usage["usage_source"] == "unavailable":
        if any(usage[field] is not None for field in ("input_tokens", "output_tokens", "turns", "tool_calls")):
            raise ResultContractError("unavailable usage values must all be null")
    else:
        _integer(usage["input_tokens"], "usage.input_tokens", optional=True)
        _integer(usage["output_tokens"], "usage.output_tokens", optional=True)
        _integer(usage["turns"], "usage.turns")
        _integer(usage["tool_calls"], "usage.tool_calls")

    runtime = _mapping(
        manifest["runtime"],
        "runtime",
        exact_fields={
            "source_revision",
            "docker_image_id",
            "docker_daemon_identity",
            "agent_tools_content_sha256",
            "endpoint_base_url",
            "endpoint_evidence_sha256",
        },
    )
    source_revision = _string(runtime["source_revision"], "runtime.source_revision")
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_revision) is None:
        raise ResultContractError("runtime.source_revision is invalid")
    image_id = _string(runtime["docker_image_id"], "runtime.docker_image_id")
    if not image_id.startswith("sha256:") or _SHA256.fullmatch(image_id[7:]) is None:
        raise ResultContractError("runtime.docker_image_id is invalid")
    _string(runtime["docker_daemon_identity"], "runtime.docker_daemon_identity")
    _sha256(
        runtime["agent_tools_content_sha256"],
        "runtime.agent_tools_content_sha256",
    )
    endpoint = _string(runtime["endpoint_base_url"], "runtime.endpoint_base_url")
    if not endpoint.startswith(("http://", "https://")):
        raise ResultContractError("runtime.endpoint_base_url is invalid")
    _sha256(
        runtime["endpoint_evidence_sha256"],
        "runtime.endpoint_evidence_sha256",
    )

    artifacts = _mapping(
        manifest["artifacts"],
        "artifacts",
        exact_fields={"candidate", "trajectory", "stderr"},
    )
    for name in ("candidate", "trajectory", "stderr"):
        artifact = _mapping(
            artifacts[name],
            f"artifacts.{name}",
            exact_fields={"sha256", "size_bytes"},
        )
        _sha256(artifact["sha256"], f"artifacts.{name}.sha256")
        _integer(artifact["size_bytes"], f"artifacts.{name}.size_bytes")

    for key, extension in manifest.items():
        if key not in required:
            if key in _RESERVED_KEYS or _LEAKY_KEY.search(key):
                raise ResultContractError("manifest extension key is reserved")
            _validate_extensions(extension, budget=[0])
    return manifest


def canonical_manifest_bytes(value: Mapping[str, Any]) -> bytes:
    """Return validated canonical bytes; the manifest never identifies itself."""

    validate_result_manifest(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ResultContractError(f"manifest is not canonical JSON: {error}") from error
    return (text + "\n").encode("utf-8")
