"""Ephemeral nonsecret transport for machine-local runtime locators."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}")
_BINDINGS_NAME = "runtime-bindings.json"
_TOP_FIELDS = {
    "run_config_sha256",
    "source_root",
    "dataset_dir",
    "problems_file",
    "build_dir",
    "docker",
    "tools_projection",
    "toolchain",
    "support_files",
    "credential_broker",
}


class RuntimeBindingError(ValueError):
    """Runtime bindings are malformed, stale, or unsafe to consume."""


def runtime_bindings_path(run_config_path: Path) -> Path:
    path = Path(run_config_path)
    if path.name != "run-config.json":
        raise RuntimeBindingError("run config must use the canonical filename")
    return path.with_name(_BINDINGS_NAME)


def _exact_mapping(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeBindingError(f"{name} has invalid fields")
    return value


def _text(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
    ):
        raise RuntimeBindingError(f"{name} must be non-empty single-line text")
    return value


def _absolute_path(value: Any, name: str) -> str:
    text = _text(value, name)
    if not Path(text).is_absolute():
        raise RuntimeBindingError(f"{name} must be an absolute locator")
    return text


def validate_runtime_bindings(
    value: Any,
    *,
    expected_config_digest: str,
) -> dict[str, Any]:
    """Validate syntax and config ownership; material identities are checked by runner."""

    bindings = _exact_mapping(value, "runtime bindings", _TOP_FIELDS)
    digest = _text(bindings["run_config_sha256"], "run_config_sha256")
    if _SHA256.fullmatch(digest) is None or digest != expected_config_digest:
        raise RuntimeBindingError("runtime bindings do not match the run config")
    for field in (
        "source_root",
        "dataset_dir",
        "problems_file",
        "build_dir",
        "tools_projection",
    ):
        _absolute_path(bindings[field], field)

    docker = _exact_mapping(
        bindings["docker"],
        "docker",
        {"client", "daemon", "image", "archive"},
    )
    _absolute_path(docker["client"], "docker.client")
    _text(docker["daemon"], "docker.daemon")
    _text(docker["image"], "docker.image")
    _absolute_path(docker["archive"], "docker.archive")

    toolchain = bindings["toolchain"]
    if not isinstance(toolchain, dict) or not toolchain:
        raise RuntimeBindingError("toolchain must be a non-empty mapping")
    for name, path in toolchain.items():
        _text(name, "toolchain key")
        _absolute_path(path, f"toolchain.{name}")

    support_files = bindings["support_files"]
    if not isinstance(support_files, dict):
        raise RuntimeBindingError("support_files must be a mapping")
    for name, path in support_files.items():
        _text(name, "support_files key")
        _absolute_path(path, f"support_files.{name}")

    if bindings["credential_broker"] != ".credential.sock":
        raise RuntimeBindingError("credential_broker must be the fixed relative name")
    return bindings


def _canonical(value: Mapping[str, Any], *, digest: str) -> bytes:
    validate_runtime_bindings(value, expected_config_digest=digest)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeBindingError(f"runtime bindings are not JSON: {error}") from error
    return (text + "\n").encode("utf-8")


def _run_digest(run_config_path: Path) -> str:
    path = Path(run_config_path)
    if path.name != "run-config.json" or _SHA256.fullmatch(path.parent.name) is None:
        raise RuntimeBindingError("run config path has no content-addressed parent")
    return path.parent.name


def _open_run_directory(run_config_path: Path) -> int:
    try:
        descriptor = os.open(
            Path(run_config_path).parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise RuntimeBindingError(f"cannot open run directory: {error}") from error
    return descriptor


def publish_runtime_bindings(
    run_config_path: Path,
    value: Mapping[str, Any],
) -> Path:
    """Atomically replace stale nonsecret locators under the already-held run lock."""

    digest = _run_digest(run_config_path)
    content = _canonical(value, digest=digest)
    run_descriptor = _open_run_directory(run_config_path)
    temporary_name = f".{_BINDINGS_NAME}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    try:
        try:
            existing = os.stat(
                _BINDINGS_NAME,
                dir_fd=run_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
            or existing.st_nlink != 1
        ):
            raise RuntimeBindingError("existing runtime bindings are unsafe")

        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=run_descriptor,
        )
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary_name,
            _BINDINGS_NAME,
            src_dir_fd=run_descriptor,
            dst_dir_fd=run_descriptor,
        )
        os.fsync(run_descriptor)
    except OSError as error:
        raise RuntimeBindingError(f"cannot publish runtime bindings: {error}") from error
    finally:
        try:
            os.unlink(temporary_name, dir_fd=run_descriptor)
        except FileNotFoundError:
            pass
        os.close(run_descriptor)
    return runtime_bindings_path(run_config_path)


def load_runtime_bindings(run_config_path: Path) -> dict[str, Any]:
    """Read canonical bindings from the fixed sibling without following symlinks."""

    digest = _run_digest(run_config_path)
    run_descriptor = _open_run_directory(run_config_path)
    descriptor = -1
    try:
        descriptor = os.open(
            _BINDINGS_NAME,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=run_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > 1024 * 1024
        ):
            raise RuntimeBindingError(
                "runtime bindings must be a bounded owned mode-0600 regular file"
            )
        content = b""
        while len(content) <= 1024 * 1024:
            block = os.read(descriptor, min(65536, 1024 * 1024 + 1 - len(content)))
            if not block:
                break
            content += block
    except OSError as error:
        raise RuntimeBindingError(f"cannot read runtime bindings: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(run_descriptor)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeBindingError(f"invalid runtime bindings JSON: {error}") from error
    validated = validate_runtime_bindings(value, expected_config_digest=digest)
    if _canonical(validated, digest=digest) != content:
        raise RuntimeBindingError("runtime bindings bytes are not canonical")
    return validated


def remove_runtime_bindings(run_config_path: Path) -> None:
    """Durably remove ephemeral bindings; reject suspicious filesystem entries."""

    _run_digest(run_config_path)
    run_descriptor = _open_run_directory(run_config_path)
    try:
        try:
            metadata = os.stat(
                _BINDINGS_NAME,
                dir_fd=run_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise RuntimeBindingError("runtime bindings removal refused unsafe entry")
        os.unlink(_BINDINGS_NAME, dir_fd=run_descriptor)
        os.fsync(run_descriptor)
    except OSError as error:
        raise RuntimeBindingError(f"cannot remove runtime bindings: {error}") from error
    finally:
        os.close(run_descriptor)
