"""Deterministic, non-secret provenance for explicit Agent tool prefixes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Callable, Mapping


class AgentToolsError(ValueError):
    """An Agent tools prefix cannot be safely identified or mounted."""


def _update_field(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, byteorder="big"))
    digest.update(value)


def digest_agent_tools(root: Path) -> str:
    """Hash paths, file bytes, executable bits, and contained symlink targets."""

    try:
        resolved_root = Path(root).resolve(strict=True)
    except OSError as error:
        raise AgentToolsError(f"Agent tools prefix does not exist: {root}") from error
    if not resolved_root.is_dir():
        raise AgentToolsError(f"Agent tools prefix is not a directory: {root}")

    entries = []
    for current_root, directory_names, file_names in os.walk(
        resolved_root, followlinks=False
    ):
        current = Path(current_root)
        entries.extend(current / name for name in directory_names)
        entries.extend(current / name for name in file_names)
    entries.sort(key=lambda path: path.relative_to(resolved_root).as_posix())

    digest = hashlib.sha256(b"verilog-eval-agent-tools\0")
    for path in entries:
        relative = path.relative_to(resolved_root).as_posix().encode("utf-8")
        metadata = path.lstat()
        executable = b"1" if metadata.st_mode & 0o111 else b"0"
        _update_field(digest, relative)
        _update_field(digest, executable)

        if stat.S_ISDIR(metadata.st_mode):
            _update_field(digest, b"directory")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            resolved_target = (path.parent / target).resolve(strict=False)
            try:
                resolved_target.relative_to(resolved_root)
            except ValueError as error:
                raise AgentToolsError(
                    f"Agent tools symlink escapes prefix: {path} -> {target}"
                ) from error
            _update_field(digest, b"symlink")
            _update_field(digest, target.encode("utf-8"))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AgentToolsError(f"unsupported Agent tools file type: {path}")

        _update_field(digest, b"file")
        _update_field(digest, metadata.st_size.to_bytes(8, byteorder="big"))
        with path.open("rb") as input_file:
            while True:
                block = input_file.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)

    return digest.hexdigest()


def _regular_file_bytes(path: Path, *, maximum_bytes: int = 512 * 1024 * 1024) -> tuple[bytes, os.stat_result]:
    try:
        resolved = Path(path).resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise AgentToolsError(f"identity file is unavailable: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
        raise AgentToolsError(f"identity file must be a bounded regular file: {path}")
    try:
        content = resolved.read_bytes()
    except OSError as error:
        raise AgentToolsError(f"cannot read identity file: {path}") from error
    return content, metadata


def executable_identity(name: str, path: Path) -> dict[str, str]:
    """Return a locator-free content/mode identity for one host executable."""

    if not name or any(character in name for character in "\x00\r\n"):
        raise AgentToolsError("executable identity name is invalid")
    content, metadata = _regular_file_bytes(Path(path))
    digest = hashlib.sha256()
    _update_field(digest, content)
    _update_field(digest, (metadata.st_mode & 0o111).to_bytes(2, "big"))
    return {"name": name, "identity": f"sha256:{digest.hexdigest()}"}


def support_file_identity(name: str, path: Path) -> dict[str, object]:
    """Return stable bytes/size identity for one admitted support file."""

    if not name or any(character in name for character in "\x00\r\n"):
        raise AgentToolsError("support identity name is invalid")
    content, metadata = _regular_file_bytes(Path(path), maximum_bytes=64 * 1024 * 1024)
    return {
        "name": name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": metadata.st_size,
    }


def docker_daemon_identity(
    docker_path: str,
    *,
    environment: Mapping[str, str],
    runner: Callable = subprocess.run,
) -> str:
    """Hash one bounded canonical Docker server record using an exact environment."""

    command = (docker_path, "version", "--format", "{{json .Server}}")
    try:
        completed = runner(
            command,
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AgentToolsError(f"cannot query Docker daemon identity: {error}") from error
    if completed.returncode != 0:
        raise AgentToolsError("Docker daemon identity query failed")
    output = completed.stdout
    if not isinstance(output, str) or len(output.encode("utf-8")) > 1024 * 1024:
        raise AgentToolsError("Docker daemon identity response is oversized")
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise AgentToolsError("Docker daemon identity is not JSON") from error
    if not isinstance(value, dict) or not value:
        raise AgentToolsError("Docker daemon identity must be a non-empty object")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
