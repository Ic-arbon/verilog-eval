"""Rollback-safe build-root locking, inventory, and no-follow quarantine."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_RUN_ROOT = re.compile(r"[0-9a-f]{64}")
_CONTROL_NAMES = frozenset({".lifecycle.lock", ".recoveries", "quarantine"})


class LifecycleError(RuntimeError):
    """Managed run storage cannot be inspected or changed safely."""


def _open_build_root(build_root: Path, *, create: bool) -> tuple[Path, int]:
    root = Path(build_root)
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise LifecycleError(f"cannot inspect build root: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LifecycleError("build root must be a real directory")
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise LifecycleError(f"cannot open build root: {error}") from error
    return root, descriptor


@contextmanager
def lifecycle_lock(build_root: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the build-root lifecycle lock, failing rather than waiting."""

    root, root_descriptor = _open_build_root(build_root, create=True)
    lock_descriptor = -1
    try:
        lock_descriptor = os.open(
            ".lifecycle.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(lock_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LifecycleError("lifecycle lock must be a regular file")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LifecycleError("build-root lifecycle lock is busy") from error
        yield
    finally:
        if lock_descriptor >= 0:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        os.close(root_descriptor)


def inventory_build_root(build_root: Path) -> dict[str, list[str]]:
    """Return a deterministic no-follow inventory without mutating storage."""

    root, descriptor = _open_build_root(build_root, create=False)
    os.close(descriptor)
    run_roots: list[str] = []
    receipts: list[str] = []
    control: list[str] = []
    other: list[str] = []
    try:
        for entry in os.scandir(root):
            name = entry.name
            if _RUN_ROOT.fullmatch(name):
                run_roots.append(name)
            elif name == ".recoveries":
                control.append(name)
                try:
                    receipt_entries = os.scandir(root / name)
                except OSError as error:
                    raise LifecycleError(f"cannot inspect recovery receipts: {error}") from error
                with receipt_entries:
                    receipts.extend(item.name for item in receipt_entries)
            elif name in _CONTROL_NAMES:
                control.append(name)
            else:
                other.append(name)
    except OSError as error:
        raise LifecycleError(f"cannot inventory build root: {error}") from error
    return {
        "run_roots": sorted(run_roots),
        "receipts": sorted(receipts),
        "control_entries": sorted(control),
        "other_entries": sorted(other),
    }


def quarantine_entry(build_root: Path, name: str) -> Path:
    """Atomically rename one validated real run directory into quarantine."""

    if _RUN_ROOT.fullmatch(name) is None:
        raise LifecycleError("quarantine source must be a 64-hex run root")
    root, root_descriptor = _open_build_root(build_root, create=False)
    quarantine_descriptor = -1
    try:
        try:
            metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        except OSError as error:
            raise LifecycleError(f"cannot inspect quarantine source: {error}") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise LifecycleError("quarantine source must be a real directory")
        try:
            os.mkdir("quarantine", mode=0o700, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
        except FileExistsError:
            pass
        quarantine_descriptor = os.open(
            "quarantine",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        destination_name = f"{name}.{uuid.uuid4().hex}"
        os.rename(
            name,
            destination_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=quarantine_descriptor,
        )
        os.fsync(quarantine_descriptor)
        os.fsync(root_descriptor)
        return root / "quarantine" / destination_name
    except OSError as error:
        if isinstance(error, LifecycleError):
            raise
        raise LifecycleError(f"cannot quarantine run root: {error}") from error
    finally:
        if quarantine_descriptor >= 0:
            os.close(quarantine_descriptor)
        os.close(root_descriptor)
