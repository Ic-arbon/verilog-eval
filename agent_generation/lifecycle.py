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


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    """Hold one content-addressed run exclusively without waiting."""

    run = Path(run_dir)
    try:
        metadata = run.lstat()
    except OSError as error:
        raise LifecycleError(f"cannot inspect run directory: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LifecycleError("run directory must be a real directory")
    directory_descriptor = os.open(
        run,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    lock_descriptor = -1
    try:
        lock_descriptor = os.open(
            ".run.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            raise LifecycleError("run lock must be regular")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LifecycleError("run is already active") from error
        yield
    finally:
        if lock_descriptor >= 0:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        os.close(directory_descriptor)


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


def _receipt_content(config_digest: str, *, acknowledged: bool) -> bytes:
    import json

    value = {
        "acknowledged": acknowledged,
        "config_path": f"{config_digest}/run-config.json",
        "config_sha256": config_digest,
        "state": "new_run",
    }
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _receipts_directory(root_descriptor: int, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(".recoveries", mode=0o700, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
        except FileExistsError:
            pass
    try:
        return os.open(
            ".recoveries",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
    except FileNotFoundError:
        return -1


def _write_receipt_unlocked(
    build_root: Path,
    config_digest: str,
    *,
    acknowledged: bool,
    replace: bool,
) -> Path:
    if _RUN_ROOT.fullmatch(config_digest) is None:
        raise LifecycleError("recovery config digest is invalid")
    root, root_descriptor = _open_build_root(build_root, create=True)
    receipts_descriptor = -1
    temporary_name = f".{config_digest}.{uuid.uuid4().hex}.tmp"
    final_name = f"{config_digest}.json"
    content = _receipt_content(config_digest, acknowledged=acknowledged)
    try:
        receipts_descriptor = _receipts_directory(root_descriptor, create=True)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=receipts_descriptor,
        )
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if replace:
            try:
                metadata = os.stat(
                    final_name,
                    dir_fd=receipts_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(metadata.st_mode):
                    raise LifecycleError("recovery receipt is not regular")
            except FileNotFoundError:
                pass
            os.replace(
                temporary_name,
                final_name,
                src_dir_fd=receipts_descriptor,
                dst_dir_fd=receipts_descriptor,
            )
        else:
            try:
                os.link(
                    temporary_name,
                    final_name,
                    src_dir_fd=receipts_descriptor,
                    dst_dir_fd=receipts_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = _read_receipt_descriptor(receipts_descriptor, final_name)
                if existing["config_sha256"] != config_digest:
                    raise LifecycleError("occupied recovery receipt is invalid")
        os.fsync(receipts_descriptor)
        return root / ".recoveries" / final_name
    except OSError as error:
        raise LifecycleError(f"cannot publish recovery receipt: {error}") from error
    finally:
        if receipts_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=receipts_descriptor)
                os.fsync(receipts_descriptor)
            except FileNotFoundError:
                pass
            os.close(receipts_descriptor)
        os.close(root_descriptor)


def _read_receipt_descriptor(directory_descriptor: int, name: str) -> dict:
    import json

    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024:
            raise LifecycleError("recovery receipt must be a bounded regular file")
        content = os.read(descriptor, 16 * 1024 + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleError(f"invalid recovery receipt: {error}") from error
    expected_fields = {"acknowledged", "config_path", "config_sha256", "state"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise LifecycleError("recovery receipt fields are invalid")
    digest = value["config_sha256"]
    if (
        not isinstance(digest, str)
        or _RUN_ROOT.fullmatch(digest) is None
        or name != f"{digest}.json"
        or value["config_path"] != f"{digest}/run-config.json"
        or value["state"] != "new_run"
        or not isinstance(value["acknowledged"], bool)
    ):
        raise LifecycleError("recovery receipt identity is invalid")
    if content != _receipt_content(digest, acknowledged=value["acknowledged"]):
        raise LifecycleError("recovery receipt bytes are not canonical")
    return value


def _list_receipts_unlocked(build_root: Path) -> list[dict]:
    _root, root_descriptor = _open_build_root(build_root, create=True)
    receipts_descriptor = -1
    try:
        receipts_descriptor = _receipts_directory(root_descriptor, create=False)
        if receipts_descriptor < 0:
            return []
        names = sorted(os.listdir(receipts_descriptor))
        values = []
        for name in names:
            if name.startswith(".") and name.endswith(".tmp"):
                continue
            values.append(_read_receipt_descriptor(receipts_descriptor, name))
        return values
    finally:
        if receipts_descriptor >= 0:
            os.close(receipts_descriptor)
        os.close(root_descriptor)


def publish_recovery_receipt(build_root: Path, config_digest: str) -> Path:
    """Publish an unacknowledged nonce-run recovery receipt without overwrite."""

    with lifecycle_lock(build_root, exclusive=True):
        return _write_receipt_unlocked(
            build_root,
            config_digest,
            acknowledged=False,
            replace=False,
        )


def acknowledge_recovery(build_root: Path, config_digest: str) -> Path:
    """Durably mark a recovery path as delivered to the caller."""

    with lifecycle_lock(build_root, exclusive=True):
        receipts = {
            value["config_sha256"]: value
            for value in _list_receipts_unlocked(build_root)
        }
        if config_digest not in receipts:
            raise LifecycleError("recovery receipt does not exist")
        return _write_receipt_unlocked(
            build_root,
            config_digest,
            acknowledged=True,
            replace=True,
        )


def list_recovery_receipts(build_root: Path) -> list[dict]:
    """Return canonical receipts without changing acknowledgement state."""

    with lifecycle_lock(build_root, exclusive=False):
        return _list_receipts_unlocked(build_root)


def synthesize_orphan_recovery_receipts(build_root: Path) -> list[Path]:
    """Close the config/receipt crash gap for every canonical nonce run."""

    from agent_generation.run_config import load_run_config

    created: list[Path] = []
    with lifecycle_lock(build_root, exclusive=True):
        inventory = inventory_build_root(build_root)
        existing = {
            value["config_sha256"]
            for value in _list_receipts_unlocked(build_root)
        }
        for digest in inventory["run_roots"]:
            if digest in existing:
                continue
            config_path = Path(build_root) / digest / "run-config.json"
            try:
                config = load_run_config(config_path)
            except ValueError:
                continue
            if config["nonce"] is None:
                continue
            created.append(
                _write_receipt_unlocked(
                    build_root,
                    digest,
                    acknowledged=False,
                    replace=False,
                )
            )
    return created


def abandon_recovery(build_root: Path, config_digest: str) -> Path:
    """Quarantine one incomplete nonce run and its receipt under exclusive lock."""

    if _RUN_ROOT.fullmatch(config_digest) is None:
        raise LifecycleError("recovery config digest is invalid")
    root = Path(build_root)
    with lifecycle_lock(root, exclusive=True):
        receipts = {
            value["config_sha256"]: value
            for value in _list_receipts_unlocked(root)
        }
        if config_digest not in receipts:
            raise LifecycleError("recovery receipt does not exist")
        run = root / config_digest
        completion = run / "agent-summary.json"
        if completion.exists() or completion.is_symlink():
            raise LifecycleError("a completed run cannot be abandoned")
        destination = quarantine_entry(root, config_digest)

        _root, root_descriptor = _open_build_root(root, create=False)
        receipts_descriptor = -1
        quarantine_descriptor = -1
        try:
            receipts_descriptor = _receipts_directory(root_descriptor, create=False)
            if receipts_descriptor < 0:
                raise LifecycleError("recovery receipt directory disappeared")
            receipt_name = f"{config_digest}.json"
            _read_receipt_descriptor(receipts_descriptor, receipt_name)
            quarantine_descriptor = os.open(
                "quarantine",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            destination_name = f"{destination.name}.recovery.json"
            os.rename(
                receipt_name,
                destination_name,
                src_dir_fd=receipts_descriptor,
                dst_dir_fd=quarantine_descriptor,
            )
            os.fsync(receipts_descriptor)
            os.fsync(quarantine_descriptor)
            os.fsync(root_descriptor)
        except OSError as error:
            raise LifecycleError(f"cannot quarantine recovery receipt: {error}") from error
        finally:
            if quarantine_descriptor >= 0:
                os.close(quarantine_descriptor)
            if receipts_descriptor >= 0:
                os.close(receipts_descriptor)
            os.close(root_descriptor)
        return destination
