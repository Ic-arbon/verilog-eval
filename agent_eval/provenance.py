"""Freeze local Agent tools and record reproducible source provenance."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


@dataclass(frozen=True)
class AgentToolsSnapshot:
    path: Path
    digest: str


def _hash_paths(root: Path, paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise ValueError(f"unsupported file in Agent source: {path}")
        digest.update(b"\0")
    return digest.hexdigest()


def directory_digest(root: Path) -> str:
    root = root.resolve()
    paths = [
        path for path in root.rglob("*") if path.is_symlink() or not path.is_dir()
    ]
    return _hash_paths(root, paths)


def _validate_internal_symlinks(root: Path) -> None:
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        resolved_target = path.resolve(strict=False)
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError as error:
            raise ValueError(
                f"symlink escapes Agent tools root: {path} -> {os.readlink(path)}"
            ) from error


def snapshot_agent_tools(
    source: Path,
    destination: Path,
    agent: str,
) -> AgentToolsSnapshot:
    """Copy one local tools prefix so a run cannot observe later source edits."""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Agent tools directory not found: {source}")
    if agent not in {"pi", "opencode"}:
        raise ValueError(f"unknown agent backend: {agent}")
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("Agent tools snapshot cannot be created inside its source")

    _validate_internal_symlinks(source)
    executable = source / "node_modules/.bin" / agent
    if not executable.is_file():
        raise FileNotFoundError(f"local Agent executable not found: {executable}")

    source_digest = directory_digest(source)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    snapshot_digest = directory_digest(destination)
    if snapshot_digest != source_digest:
        shutil.rmtree(destination)
        raise RuntimeError("Agent tools changed while the frozen snapshot was created")

    return AgentToolsSnapshot(path=destination, digest=snapshot_digest)


def _git(source: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _git_paths(repository: Path, *arguments: str) -> List[str]:
    output = subprocess.run(
        ["git", "-C", str(repository), *arguments, "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return sorted(
        item.decode(errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    )


def _git_source_digest(repository: Path, paths: Iterable[str]) -> str:
    files = [repository / path for path in paths]
    return _hash_paths(repository, files)


def _lockfile_digests(repository: Path) -> dict:
    result = {}
    for name in ("package-lock.json", "bun.lock", "bun.lockb", "pnpm-lock.yaml", "yarn.lock"):
        path = repository / name
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_agent_source_provenance(
    source: Path,
    output_dir: Path,
    tools_digest: str,
) -> dict:
    """Record the exact local Git state associated with a frozen tools build."""
    source = source.resolve()
    repository = Path(_git(source, "rev-parse", "--show-toplevel").strip()).resolve()
    resolved_commit = _git(repository, "rev-parse", "HEAD").strip()
    status_output = _git(repository, "status", "--porcelain", "--untracked-files=all")
    tracked_and_untracked = _git_paths(
        repository,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    untracked = _git_paths(
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    patch = _git(repository, "diff", "--binary", "HEAD")
    if patch:
        (output_dir / "agent-source.patch").write_text(patch)
    if untracked:
        with tarfile.open(output_dir / "agent-source-untracked.tar", "w") as archive:
            for relative in untracked:
                archive.add(repository / relative, arcname=relative, recursive=False)

    metadata = {
        "mode": "local",
        "source": str(source),
        "repository": str(repository),
        "resolved_commit": resolved_commit,
        "dirty": bool(status_output.strip()),
        "source_digest": _git_source_digest(repository, tracked_and_untracked),
        "tools_digest": tools_digest,
        "lockfile_digests": _lockfile_digests(repository),
        "untracked_files": untracked,
    }
    (output_dir / "agent-source.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    return metadata
