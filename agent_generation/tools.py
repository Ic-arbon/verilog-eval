"""Lock-derived minimal read-only Agent tools projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


_AGENT_PACKAGES = {
    "pi": ("@earendil-works/pi-coding-agent", "pi"),
    "opencode": ("opencode-ai", "opencode"),
}
_BANNED_TOP_LEVEL = frozenset({".npmrc", ".npm", ".cache", "cache", "logs"})
_BANNED_FILE = re.compile(
    r"(?:^\.npmrc$|^\.env(?:\.|$)|(?:api[-_]?key|auth[-_]?token|credential|secret)(?:\.|$)|\.log$)",
    re.IGNORECASE,
)


class ToolsProjectionError(ValueError):
    """The explicit tools prefix cannot produce a safe selected projection."""


@dataclass(frozen=True)
class ToolsProjection:
    path: Path
    content_sha256: str
    source_content_sha256: str
    lock_sha256: str
    versions: dict[str, str]


def _lock_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ToolsProjectionError(f"package lock contains duplicate key: {key}")
        value[key] = child
    return value


def _load_lock(path: Path) -> tuple[dict[str, Any], bytes]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024 * 1024:
            raise ToolsProjectionError("package lock must be a bounded regular file")
        content = bytearray()
        while len(content) <= 16 * 1024 * 1024:
            block = os.read(
                descriptor,
                min(1024 * 1024, 16 * 1024 * 1024 + 1 - len(content)),
            )
            if not block:
                break
            content.extend(block)
        if len(content) > 16 * 1024 * 1024:
            raise ToolsProjectionError("package lock exceeds bound")
        raw = bytes(content)
        lock = json.loads(raw.decode("utf-8"), object_pairs_hook=_lock_object)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        if isinstance(error, ToolsProjectionError):
            raise
        raise ToolsProjectionError(f"cannot read package lock: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    content = raw
    if (
        not isinstance(lock, dict)
        or lock.get("lockfileVersion") not in {2, 3}
        or not isinstance(lock.get("packages"), dict)
    ):
        raise ToolsProjectionError("package lock must use the npm packages format")
    return lock, content


def _package_name(package_path: str) -> str:
    marker = "node_modules/"
    if marker not in package_path:
        raise ToolsProjectionError(f"invalid package-lock path: {package_path}")
    return package_path.rsplit(marker, 1)[1]


def _resolve_dependency(
    packages: dict[str, Any],
    parent: str,
    dependency: str,
) -> str:
    candidates = [f"{parent}/node_modules/{dependency}"]
    segments = parent.split("/node_modules/")
    while len(segments) > 1:
        segments.pop()
        prefix = "/node_modules/".join(segments)
        candidates.append(f"{prefix}/node_modules/{dependency}")
    candidates.append(f"node_modules/{dependency}")
    for candidate in candidates:
        candidate = candidate.lstrip("./")
        if candidate in packages:
            return candidate
    raise ToolsProjectionError(
        f"installed dependency {dependency!r} for {parent!r} is absent from lock"
    )


def _selected_packages(
    packages: dict[str, Any],
    agent: str,
) -> tuple[list[str], str]:
    if agent not in _AGENT_PACKAGES:
        raise ToolsProjectionError("Agent must be pi or opencode")
    package_name, binary_name = _AGENT_PACKAGES[agent]
    root = packages.get("")
    if not isinstance(root, dict) or package_name not in root.get("dependencies", {}):
        raise ToolsProjectionError("selected Agent is not a root production dependency")
    start = f"node_modules/{package_name}"
    if start not in packages:
        raise ToolsProjectionError("selected Agent package is not installed")

    selected: list[str] = []
    pending = [start]
    seen: set[str] = set()
    while pending:
        package_path = pending.pop(0)
        if package_path in seen:
            continue
        seen.add(package_path)
        metadata = packages.get(package_path)
        if not isinstance(metadata, dict) or metadata.get("dev") is True:
            raise ToolsProjectionError("selected dependency is missing or development-only")
        selected.append(package_path)
        dependency_requirements: dict[str, bool] = {}
        peer_metadata = metadata.get("peerDependenciesMeta", {})
        if not isinstance(peer_metadata, dict):
            raise ToolsProjectionError(f"{package_path} has malformed peer metadata")
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            dependencies = metadata.get(field, {})
            if dependencies is None:
                continue
            if not isinstance(dependencies, dict):
                raise ToolsProjectionError(f"{package_path} has malformed dependencies")
            for dependency in dependencies:
                peer_optional = (
                    field == "peerDependencies"
                    and isinstance(peer_metadata.get(dependency), dict)
                    and peer_metadata[dependency].get("optional") is True
                )
                optional = field == "optionalDependencies" or peer_optional
                dependency_requirements[dependency] = (
                    dependency_requirements.get(dependency, True) and optional
                )
        for dependency, optional in sorted(dependency_requirements.items()):
            try:
                resolved = _resolve_dependency(packages, package_path, dependency)
            except ToolsProjectionError:
                if optional:
                    continue
                raise
            if resolved not in seen:
                pending.append(resolved)
    return sorted(selected), binary_name


def _reject_prefix_state(source: Path) -> None:
    for name in _BANNED_TOP_LEVEL:
        if (source / name).exists() or (source / name).is_symlink():
            raise ToolsProjectionError(f"tools prefix contains forbidden state: {name}")


def _is_within(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _copy_tree(source: Path, destination: Path, selected_roots: tuple[Path, ...]) -> None:
    destination.mkdir(mode=0o700)
    try:
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as error:
        raise ToolsProjectionError(f"cannot scan package tree: {error}") from error
    for entry in entries:
        if _BANNED_FILE.search(entry.name):
            raise ToolsProjectionError(f"selected package contains forbidden file: {entry.name}")
        source_path = Path(entry.path)
        destination_path = destination / entry.name
        metadata = entry.stat(follow_symlinks=False)
        if entry.name == "node_modules" and stat.S_ISDIR(metadata.st_mode):
            # Nested packages are copied only by their own lock-selected root.
            continue
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(source_path)
            resolved = (source_path.parent / target).resolve(strict=False)
            if not _is_within(resolved, selected_roots):
                raise ToolsProjectionError(
                    f"selected package symlink escapes projection: {source_path}"
                )
            destination_path.symlink_to(target)
        elif stat.S_ISDIR(metadata.st_mode):
            _copy_tree(source_path, destination_path, selected_roots)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ToolsProjectionError(
                    f"selected package contains a hard-linked file: {source_path}"
                )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source_path, flags)
            try:
                verified = os.fstat(descriptor)
                if not stat.S_ISREG(verified.st_mode):
                    raise ToolsProjectionError("package file changed type during copy")
                output = os.open(
                    destination_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o700 if verified.st_mode & 0o111 else 0o600,
                )
                try:
                    while True:
                        block = os.read(descriptor, 1024 * 1024)
                        if not block:
                            break
                        offset = 0
                        while offset < len(block):
                            offset += os.write(output, block[offset:])
                    os.fsync(output)
                finally:
                    os.close(output)
            finally:
                os.close(descriptor)
        else:
            raise ToolsProjectionError(
                f"selected package contains unsupported file type: {source_path}"
            )


def _digest_field(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256(b"verilog-eval-agent-tools\0")
    entries: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        entries.extend(current_path / name for name in directories)
        entries.extend(current_path / name for name in files)
    for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.lstat()
        _digest_field(digest, relative)
        _digest_field(digest, (metadata.st_mode & 0o777).to_bytes(2, "big"))
        if stat.S_ISDIR(metadata.st_mode):
            _digest_field(digest, b"directory")
        elif stat.S_ISLNK(metadata.st_mode):
            _digest_field(digest, b"symlink")
            _digest_field(digest, os.readlink(path).encode("utf-8"))
        elif stat.S_ISREG(metadata.st_mode):
            _digest_field(digest, b"file")
            with path.open("rb") as input_file:
                for block in iter(lambda: input_file.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            raise ToolsProjectionError("projection contains unsupported file type")
    return digest.hexdigest()


def _make_read_only(root: Path) -> None:
    paths: list[Path] = [root]
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        paths.extend(current_path / name for name in directories)
        paths.extend(current_path / name for name in files)
    for path in reversed(paths):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o555)
        else:
            path.chmod(0o555 if metadata.st_mode & 0o111 else 0o444)


def _bin_map(metadata: dict[str, Any], package_name: str) -> dict[str, str]:
    bins = metadata.get("bin", {})
    if isinstance(bins, str):
        return {package_name.rsplit("/", 1)[-1]: bins}
    if not isinstance(bins, dict):
        raise ToolsProjectionError(f"package {package_name} has malformed bin metadata")
    result: dict[str, str] = {}
    for name, target in bins.items():
        if not isinstance(name, str) or not isinstance(target, str):
            raise ToolsProjectionError("package bin metadata must contain strings")
        result[name] = target
    return result


def _remove_private_tree(path: Path) -> None:
    if not path.exists():
        return
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            child = current_path / name
            if not child.is_symlink():
                child.chmod(0o600)
        for name in directories:
            child = current_path / name
            if not child.is_symlink():
                child.chmod(0o700)
        current_path.chmod(0o700)
    shutil.rmtree(path)


def validate_tools_projection(path: Path, expected_sha256: str) -> None:
    """Verify a reused projection's exact tree digest and read-only modes."""

    projection = Path(path)
    if not projection.is_dir() or projection.is_symlink():
        raise ToolsProjectionError("tools projection must be a real directory")
    if _digest_tree(projection) != expected_sha256:
        raise ToolsProjectionError("tools projection content digest mismatch")
    for current, directories, files in os.walk(projection, followlinks=False):
        current_path = Path(current)
        for child in [current_path, *(current_path / name for name in directories + files)]:
            metadata = child.lstat()
            if not stat.S_ISLNK(metadata.st_mode) and metadata.st_mode & 0o222:
                raise ToolsProjectionError("tools projection is unexpectedly writable")


def project_agent_tools(
    source_prefix: Path,
    projection_root: Path,
    agent: str,
) -> ToolsProjection:
    """Copy the selected lock-derived production closure into a private tree."""

    source = Path(source_prefix).resolve(strict=True)
    if not source.is_dir():
        raise ToolsProjectionError("tools prefix must be a directory")
    _reject_prefix_state(source)
    lock, lock_bytes = _load_lock(source / "package-lock.json")
    packages = lock["packages"]
    selected, expected_binary = _selected_packages(packages, agent)
    selected_roots = tuple((source / package_path).resolve() for package_path in selected)
    for package_path, package_root in zip(selected, selected_roots):
        if not package_root.is_dir():
            raise ToolsProjectionError(f"installed package is absent: {package_path}")
        try:
            package_root.relative_to(source)
        except ValueError as error:
            raise ToolsProjectionError("installed package escapes tools prefix") from error

    destination_root = Path(projection_root)
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination_root / f".projection.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    versions: dict[str, str] = {}
    try:
        for package_path, source_root in zip(selected, selected_roots):
            metadata = packages[package_path]
            package_name = _package_name(package_path)
            version = metadata.get("version")
            if not isinstance(version, str) or not version:
                raise ToolsProjectionError(f"package {package_name} has no version")
            versions[package_name] = version
            destination = temporary / PurePosixPath(package_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_tree(source_root, destination, selected_roots)

        package_name, _binary = _AGENT_PACKAGES[agent]
        agent_path = f"node_modules/{package_name}"
        bins = _bin_map(packages[agent_path], package_name)
        if expected_binary not in bins:
            raise ToolsProjectionError("selected Agent declares no expected binary")
        binary_target = PurePosixPath(agent_path) / bins[expected_binary]
        target_path = temporary / binary_target
        if not target_path.is_file():
            raise ToolsProjectionError("selected Agent binary target is absent")
        bin_directory = temporary / "node_modules" / ".bin"
        bin_directory.mkdir(parents=True, exist_ok=True)
        link_path = bin_directory / expected_binary
        relative_target = os.path.relpath(target_path, bin_directory)
        link_path.symlink_to(relative_target)

        source_digest = _digest_tree(temporary)
        _make_read_only(temporary)
        content_digest = _digest_tree(temporary)
        destination = destination_root / content_digest
        if destination.exists() or destination.is_symlink():
            if (
                destination.is_symlink()
                or not destination.is_dir()
                or _digest_tree(destination) != content_digest
            ):
                raise ToolsProjectionError("occupied projection digest is invalid")
            _remove_private_tree(temporary)
        else:
            try:
                os.rename(temporary, destination)
            except OSError:
                if (
                    destination.is_symlink()
                    or not destination.is_dir()
                    or _digest_tree(destination) != content_digest
                ):
                    raise ToolsProjectionError("projection publication race is invalid")
                _remove_private_tree(temporary)
        directory = os.open(destination_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return ToolsProjection(
            path=destination,
            content_sha256=content_digest,
            source_content_sha256=source_digest,
            lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
            versions=dict(sorted(versions.items())),
        )
    except BaseException:
        if temporary.exists():
            _remove_private_tree(temporary)
        raise
