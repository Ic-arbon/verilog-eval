"""Deterministic, non-secret provenance for explicit Agent tool prefixes."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


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

    digest = hashlib.sha256(b"verilog-eval-agent-tools-v1\0")
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
