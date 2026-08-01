"""Canonical immutable configuration and content-addressed run publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_LOGICAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+:-]{0,255}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_ALLOWED_AGENTS = frozenset({"pi", "opencode"})
_ALLOWED_TASKS = frozenset({"spec-to-rtl", "code-complete-iccad2023"})
_INPUT_KINDS = frozenset(
    {
        "problem_list",
        "prompt",
        "public_starter",
        "example",
        "rules",
        "hidden_test",
        "hidden_reference",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {"agent", "benchmark", "endpoint", "limits", "runtime", "jobs", "nonce"}
)


class RunConfigError(ValueError):
    """Run configuration bytes or publication violate the immutable contract."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, name: str, fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunConfigError(f"{name} must be an object")
    expected = frozenset(fields)
    actual = frozenset(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        detail = []
        if unknown:
            detail.append(f"unknown={','.join(unknown)}")
        if missing:
            detail.append(f"missing={','.join(missing)}")
        raise RunConfigError(f"{name} fields are invalid ({'; '.join(detail)})")
    return value


def _string(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
    ):
        raise RunConfigError(f"{name} must be a non-empty single-line string")
    return value


def _positive_integer(value: Any, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise RunConfigError(f"{name} must be a {qualifier} integer")
    return value


def _digest(value: Any, name: str) -> str:
    value = _string(value, name)
    if _SHA256.fullmatch(value) is None:
        raise RunConfigError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _logical_name(value: Any, name: str) -> str:
    value = _string(value, name)
    if _SAFE_LOGICAL_NAME.fullmatch(value) is None or value in {".", ".."}:
        raise RunConfigError(f"{name} must be a safe locator-free logical name")
    return value


def _validate_endpoint(value: Any) -> None:
    endpoint = _mapping(
        value,
        "endpoint",
        {"base_url", "api_key_environment"},
    )
    base_url = _string(endpoint["base_url"], "endpoint.base_url")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RunConfigError("endpoint.base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RunConfigError("endpoint.base_url contains forbidden URL components")
    if _ENVIRONMENT.fullmatch(
        _string(endpoint["api_key_environment"], "endpoint.api_key_environment")
    ) is None:
        raise RunConfigError("endpoint.api_key_environment is invalid")


def _validate_benchmark(value: Any) -> None:
    benchmark = _mapping(
        value,
        "benchmark",
        {"task", "samples", "examples", "rules", "problems", "inputs"},
    )
    if benchmark["task"] not in _ALLOWED_TASKS:
        raise RunConfigError("benchmark.task is unsupported")
    _positive_integer(benchmark["samples"], "benchmark.samples")
    _positive_integer(benchmark["examples"], "benchmark.examples", allow_zero=True)
    if not isinstance(benchmark["rules"], bool):
        raise RunConfigError("benchmark.rules must be boolean")
    problems = benchmark["problems"]
    if not isinstance(problems, list) or not problems:
        raise RunConfigError("benchmark.problems must be a non-empty ordered list")
    validated_problems = [
        _logical_name(problem, "benchmark.problems[]") for problem in problems
    ]
    if len(set(validated_problems)) != len(validated_problems):
        raise RunConfigError("benchmark.problems must not contain duplicates")
    inputs = benchmark["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise RunConfigError("benchmark.inputs must be a non-empty ordered list")
    identities: set[tuple[str, str]] = set()
    for index, raw_input in enumerate(inputs):
        item = _mapping(
            raw_input,
            f"benchmark.inputs[{index}]",
            {"kind", "name", "sha256", "size_bytes"},
        )
        kind = _string(item["kind"], f"benchmark.inputs[{index}].kind")
        if kind not in _INPUT_KINDS:
            raise RunConfigError(f"benchmark.inputs[{index}].kind is unsupported")
        logical_name = _logical_name(
            item["name"], f"benchmark.inputs[{index}].name"
        )
        _digest(item["sha256"], f"benchmark.inputs[{index}].sha256")
        _positive_integer(
            item["size_bytes"],
            f"benchmark.inputs[{index}].size_bytes",
            allow_zero=True,
        )
        identity = (kind, logical_name)
        if identity in identities:
            raise RunConfigError("benchmark.inputs contains a duplicate identity")
        identities.add(identity)


def _validate_runtime(value: Any) -> None:
    runtime = _mapping(
        value,
        "runtime",
        {
            "source_commit",
            "docker_image_id",
            "docker_daemon_identity",
            "agent_tools",
            "toolchain",
            "support_files",
        },
    )
    source_commit = _string(runtime["source_commit"], "runtime.source_commit")
    if _COMMIT.fullmatch(source_commit) is None:
        raise RunConfigError("runtime.source_commit must be a Git object ID")
    image_id = _string(runtime["docker_image_id"], "runtime.docker_image_id")
    if not image_id.startswith("sha256:") or _SHA256.fullmatch(image_id[7:]) is None:
        raise RunConfigError("runtime.docker_image_id must be a Docker content ID")
    _string(runtime["docker_daemon_identity"], "runtime.docker_daemon_identity")

    tools = _mapping(
        runtime["agent_tools"],
        "runtime.agent_tools",
        {
            "content_sha256",
            "source_content_sha256",
            "lock_sha256",
            "versions",
        },
    )
    _digest(tools["content_sha256"], "runtime.agent_tools.content_sha256")
    _digest(
        tools["source_content_sha256"],
        "runtime.agent_tools.source_content_sha256",
    )
    _digest(tools["lock_sha256"], "runtime.agent_tools.lock_sha256")
    versions = tools["versions"]
    if not isinstance(versions, dict) or not versions:
        raise RunConfigError("runtime.agent_tools.versions must be a non-empty object")
    for name, version in versions.items():
        _logical_name(name, "runtime.agent_tools.versions key")
        _string(version, f"runtime.agent_tools.versions.{name}")

    toolchain = runtime["toolchain"]
    if not isinstance(toolchain, list) or not toolchain:
        raise RunConfigError("runtime.toolchain must be a non-empty ordered list")
    names: set[str] = set()
    for index, raw_tool in enumerate(toolchain):
        tool = _mapping(
            raw_tool,
            f"runtime.toolchain[{index}]",
            {"name", "identity"},
        )
        name = _logical_name(tool["name"], f"runtime.toolchain[{index}].name")
        _string(tool["identity"], f"runtime.toolchain[{index}].identity")
        if name in names:
            raise RunConfigError("runtime.toolchain contains a duplicate name")
        names.add(name)

    support_files = runtime["support_files"]
    if not isinstance(support_files, list):
        raise RunConfigError("runtime.support_files must be an ordered list")
    support_names: set[str] = set()
    for index, raw_support in enumerate(support_files):
        support = _mapping(
            raw_support,
            f"runtime.support_files[{index}]",
            {"name", "sha256", "size_bytes"},
        )
        name = _logical_name(
            support["name"], f"runtime.support_files[{index}].name"
        )
        _digest(support["sha256"], f"runtime.support_files[{index}].sha256")
        _positive_integer(
            support["size_bytes"],
            f"runtime.support_files[{index}].size_bytes",
            allow_zero=True,
        )
        if name in support_names:
            raise RunConfigError("runtime.support_files contains a duplicate name")
        support_names.add(name)


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def validate_run_config(
    value: Any,
    *,
    forbidden_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate one fully resolved, locator-free material configuration."""

    config = _mapping(value, "run configuration", _TOP_LEVEL_FIELDS)
    agent = _mapping(
        config["agent"],
        "agent",
        {"name", "model", "thinking", "toolset"},
    )
    if agent["name"] not in _ALLOWED_AGENTS:
        raise RunConfigError("agent.name is unsupported")
    _string(agent["model"], "agent.model")
    if not isinstance(agent["thinking"], bool):
        raise RunConfigError("agent.thinking must be boolean")
    if agent["toolset"] not in {"standard", "rtl"}:
        raise RunConfigError("agent.toolset is unsupported")
    _validate_benchmark(config["benchmark"])
    _validate_endpoint(config["endpoint"])

    limits = _mapping(
        config["limits"],
        "limits",
        {
            "timeout_seconds",
            "max_turns",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
        },
    )
    for name, limit in limits.items():
        _positive_integer(limit, f"limits.{name}")
    _validate_runtime(config["runtime"])
    _positive_integer(config["jobs"], "jobs")
    nonce = config["nonce"]
    if nonce is not None and (
        not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None
    ):
        raise RunConfigError("nonce must be null or 32 lowercase hex characters")

    forbidden = tuple(value for value in forbidden_values if value)
    for text in _walk_strings(config):
        if any(secret in text for secret in forbidden):
            raise RunConfigError("run configuration contains a forbidden secret value")
    return config


def canonical_run_config(
    value: Mapping[str, Any],
    *,
    forbidden_values: Iterable[str] = (),
) -> bytes:
    """Return deterministic canonical UTF-8 JSON bytes for one run."""

    validate_run_config(value, forbidden_values=forbidden_values)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise RunConfigError(f"run configuration is not canonical JSON: {error}") from error
    return (encoded + "\n").encode("utf-8")


def run_config_sha256(canonical_bytes: bytes) -> str:
    """Return the content address of exact canonical configuration bytes."""

    if not isinstance(canonical_bytes, bytes):
        raise TypeError("canonical_bytes must be bytes")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _load_json_bytes(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunConfigError(f"invalid run configuration JSON: {error}") from error
    return validate_run_config(value)


def load_run_config(
    path: Path,
    *,
    require_parent_digest: bool = True,
) -> dict[str, Any]:
    """Load and verify exact canonical bytes without following a final symlink."""

    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunConfigError(f"cannot open run configuration: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024 * 1024:
            raise RunConfigError("run configuration must be a bounded regular file")
        raw = b""
        while len(raw) <= 16 * 1024 * 1024:
            block = os.read(descriptor, min(1024 * 1024, 16 * 1024 * 1024 + 1 - len(raw)))
            if not block:
                break
            raw += block
    finally:
        os.close(descriptor)
    value = _load_json_bytes(raw)
    if canonical_run_config(value) != raw:
        raise RunConfigError("run configuration bytes are not canonical")
    digest = run_config_sha256(raw)
    if require_parent_digest and path.parent.name != digest:
        raise RunConfigError("run configuration digest does not match its parent")
    return value


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def publish_run_config(build_root: Path, value: Mapping[str, Any]) -> Path:
    """Publish immutable config bytes at their SHA-256 run root without overwrite."""

    canonical = canonical_run_config(value)
    digest = run_config_sha256(canonical)
    build_root = Path(build_root)
    build_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if build_root.is_symlink() or not build_root.is_dir():
        raise RunConfigError("build root must be a real directory")

    root_descriptor = os.open(build_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    run_descriptor = -1
    temporary_name = f".run-config.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    try:
        try:
            os.mkdir(digest, mode=0o700, dir_fd=root_descriptor)
            _fsync_directory(root_descriptor)
        except FileExistsError:
            pass
        run_descriptor = os.open(
            digest,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=run_descriptor,
            )
            try:
                written = 0
                while written < len(canonical):
                    written += os.write(descriptor, canonical[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

            try:
                os.link(
                    temporary_name,
                    "run-config.json",
                    src_dir_fd=run_descriptor,
                    dst_dir_fd=run_descriptor,
                    follow_symlinks=False,
                )
                _fsync_directory(run_descriptor)
            except FileExistsError:
                existing = build_root / digest / "run-config.json"
                loaded = load_run_config(existing)
                if canonical_run_config(loaded) != canonical:
                    raise RunConfigError("occupied run configuration is not identical")
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=run_descriptor)
                    _fsync_directory(run_descriptor)
                except FileNotFoundError:
                    pass
        except OSError as error:
            if isinstance(error, RunConfigError):
                raise
            raise RunConfigError(f"cannot publish run configuration: {error}") from error
    finally:
        if run_descriptor >= 0:
            os.close(run_descriptor)
        os.close(root_descriptor)
    return build_root / digest / "run-config.json"
