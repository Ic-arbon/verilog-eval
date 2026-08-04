"""Safe staging of one frozen DCD Pi resource bundle per sample workspace."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tarfile
import uuid
from pathlib import Path, PurePosixPath


_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_ENTRY_BYTES = 4 * 1024 * 1024
_MAX_ENTRIES = 1024
_EXPECTED_AGENT_COUNT = 16
_EXPECTED_SKILL_COUNT = 17
_EXPECTED_EXTENSION_FILES = frozenset(
    {
        "agents.ts",
        "eda-tools.ts",
        "front-end-flow.ts",
        "index.ts",
        "process-supervisor.mjs",
        "run-agent.ts",
    }
)


class DcdPiBundleError(ValueError):
    """A DCD Pi bundle is unsafe, incomplete, or not the current product surface."""


def _member_name(member: tarfile.TarInfo) -> str:
    name = member.name.rstrip("/")
    if not name or "\x00" in name or "\\" in name:
        raise DcdPiBundleError("DCD bundle contains an invalid member name")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DcdPiBundleError("DCD bundle member escapes its root")
    canonical = path.as_posix()
    if canonical != name:
        raise DcdPiBundleError("DCD bundle member name is not canonical")
    return canonical


def _validate_inventory(members: list[tarfile.TarInfo]) -> dict[str, tarfile.TarInfo]:
    if not members or len(members) > _MAX_ENTRIES:
        raise DcdPiBundleError("DCD bundle has an invalid entry count")

    files: dict[str, tarfile.TarInfo] = {}
    directories: set[str] = set()
    total_bytes = 0
    seen: set[str] = set()
    for member in members:
        name = _member_name(member)
        if name in seen:
            raise DcdPiBundleError("DCD bundle contains duplicate members")
        seen.add(name)
        if member.isdir():
            directories.add(name)
            continue
        if not member.isfile():
            raise DcdPiBundleError("DCD bundle contains links or special files")
        if member.size < 0 or member.size > _MAX_ENTRY_BYTES:
            raise DcdPiBundleError("DCD bundle entry exceeds its size bound")
        total_bytes += member.size
        if total_bytes > _MAX_BUNDLE_BYTES:
            raise DcdPiBundleError("DCD bundle payload exceeds its size bound")
        files[name] = member

    agents = {
        name
        for name in files
        if name.startswith("agents/")
        and len(PurePosixPath(name).parts) == 2
        and name.endswith(".md")
    }
    skills = {
        name
        for name in files
        if name.startswith("skills/")
        and len(PurePosixPath(name).parts) == 3
        and PurePosixPath(name).name == "SKILL.md"
    }
    extension_prefix = "extensions/digital-chip-design-agents/"
    extension_files = {
        name.removeprefix(extension_prefix)
        for name in files
        if name.startswith(extension_prefix)
        and len(PurePosixPath(name).parts) == 3
    }
    expected_files = agents | skills | {
        extension_prefix + name for name in extension_files
    }
    if set(files) != expected_files:
        raise DcdPiBundleError("DCD bundle contains resources outside the exact Pi inventory")
    if len(agents) != _EXPECTED_AGENT_COUNT or not any(
        name.endswith("/front-end-design-orchestrator.md") for name in agents
    ):
        raise DcdPiBundleError("DCD bundle Agent inventory is incomplete")
    if len(skills) != _EXPECTED_SKILL_COUNT or not any(
        name.endswith("/chip-front-end-design/SKILL.md") for name in skills
    ):
        raise DcdPiBundleError("DCD bundle Skill inventory is incomplete")
    if extension_files != _EXPECTED_EXTENSION_FILES:
        raise DcdPiBundleError("DCD bundle Extension inventory is incomplete")

    for directory in directories:
        prefix = directory + "/"
        if not any(name.startswith(prefix) for name in files):
            raise DcdPiBundleError("DCD bundle contains an unrelated directory")
    return files


def _open_bundle(
    path: Path,
    *,
    expected_sha256: str | None,
    expected_size_bytes: int | None,
):
    if (expected_sha256 is None) != (expected_size_bytes is None):
        raise DcdPiBundleError("DCD bundle expected identity must include hash and size")
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or expected_sha256.lower() != expected_sha256
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or expected_size_bytes <= 0
    ):
        raise DcdPiBundleError("DCD bundle expected identity is invalid")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DcdPiBundleError(f"cannot open DCD Pi bundle: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_BUNDLE_BYTES
        ):
            raise DcdPiBundleError("DCD Pi bundle must be a bounded nonempty regular file")
        content = bytearray()
        while len(content) <= _MAX_BUNDLE_BYTES:
            block = os.read(
                descriptor,
                min(1024 * 1024, _MAX_BUNDLE_BYTES + 1 - len(content)),
            )
            if not block:
                break
            content.extend(block)
        if len(content) > _MAX_BUNDLE_BYTES:
            raise DcdPiBundleError("DCD Pi bundle payload exceeds its size bound")
        if len(content) != metadata.st_size:
            raise DcdPiBundleError("DCD Pi bundle changed while taking its byte snapshot")
        snapshot = bytes(content)
        if expected_size_bytes is not None and len(snapshot) != expected_size_bytes:
            raise DcdPiBundleError("DCD Pi bundle material identity changed")
        if (
            expected_sha256 is not None
            and hashlib.sha256(snapshot).hexdigest() != expected_sha256
        ):
            raise DcdPiBundleError("DCD Pi bundle material identity changed")
        return io.BytesIO(snapshot)
    finally:
        os.close(descriptor)


def stage_dcd_pi_bundle(
    bundle: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
) -> Path:
    """Validate and atomically stage exact DCD resources into a writable Pi directory."""

    bundle_path = Path(bundle)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise DcdPiBundleError("DCD Pi destination must not already exist")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)

    try:
        try:
            with _open_bundle(
                bundle_path,
                expected_sha256=expected_sha256,
                expected_size_bytes=expected_size_bytes,
            ) as input_file, tarfile.open(fileobj=input_file, mode="r:") as archive:
                files = _validate_inventory(archive.getmembers())
                for name, member in sorted(files.items()):
                    output_path = temporary.joinpath(*PurePosixPath(name).parts)
                    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = archive.extractfile(member)
                    if source is None:
                        raise DcdPiBundleError("DCD bundle file payload is missing")
                    descriptor = os.open(
                        output_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    try:
                        remaining = member.size
                        while remaining:
                            block = source.read(min(1024 * 1024, remaining))
                            if not block:
                                raise DcdPiBundleError("DCD bundle file payload is truncated")
                            offset = 0
                            while offset < len(block):
                                offset += os.write(descriptor, block[offset:])
                            remaining -= len(block)
                        if source.read(1):
                            raise DcdPiBundleError("DCD bundle file exceeds its declared size")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                        source.close()
        except (OSError, tarfile.TarError) as error:
            if isinstance(error, DcdPiBundleError):
                raise
            raise DcdPiBundleError(f"cannot read DCD Pi bundle: {error}") from error

        try:
            os.rename(temporary, target)
        except OSError as error:
            raise DcdPiBundleError(f"cannot publish DCD Pi resources: {error}") from error
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return target
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
