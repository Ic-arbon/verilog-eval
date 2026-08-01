"""Private run-local Unix credential broker for Agent sample adapters."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import socket
import stat
import struct
import threading
from pathlib import Path
from typing import Iterable, Optional


_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAMPLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOCKET_NAME = ".credential.sock"
_MAX_MESSAGE = 16 * 1024
_CWD_LOCK = threading.RLock()


class CredentialError(RuntimeError):
    """Credential handoff failed without exposing the credential value."""


def _validate_identity(
    config_digest: str,
    sample_id: str,
    environment_name: str,
) -> None:
    if _SHA256.fullmatch(config_digest) is None:
        raise CredentialError("credential request config digest is invalid")
    if _SAMPLE_ID.fullmatch(sample_id) is None:
        raise CredentialError("credential request sample ID is invalid")
    if _ENVIRONMENT.fullmatch(environment_name) is None:
        raise CredentialError("credential environment name is invalid")


def _peer_uid(connection: socket.socket) -> Optional[int]:
    if hasattr(socket, "SO_PEERCRED"):
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            return uid
        except OSError:
            return None
    return None


def _read_request(connection: socket.socket) -> dict:
    content = bytearray()
    while b"\n" not in content:
        block = connection.recv(4096)
        if not block:
            break
        content.extend(block)
        if len(content) > _MAX_MESSAGE:
            raise CredentialError("credential request exceeds bound")
    if not content.endswith(b"\n"):
        raise CredentialError("credential request is incomplete")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialError("credential request is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "config_sha256",
        "sample_id",
        "environment_name",
        "uid",
    }:
        raise CredentialError("credential request fields are invalid")
    return value


def _serve_connection(
    connection: socket.socket,
    *,
    config_digest: str,
    environment_name: str,
    secret: str,
    expected_sample_ids: frozenset[str],
    expected_uid: int,
) -> None:
    try:
        value = _read_request(connection)
        _validate_identity(
            value["config_sha256"],
            value["sample_id"],
            value["environment_name"],
        )
        peer_uid = _peer_uid(connection)
        valid = (
            value["config_sha256"] == config_digest
            and value["sample_id"] in expected_sample_ids
            and value["environment_name"] == environment_name
            and value["uid"] == expected_uid
            and (peer_uid is None or peer_uid == expected_uid)
        )
        if not valid:
            raise CredentialError("credential request identity does not match")
        response = json.dumps(
            {"ok": True, "value": secret},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except CredentialError:
        response = b'{"ok":false,"error":"credential request rejected"}\n'
    try:
        connection.sendall(response)
    finally:
        connection.close()


def _broker_process(
    run_dir: str,
    config_digest: str,
    environment_name: str,
    secret: str,
    expected_sample_ids: tuple[str, ...],
    expected_uid: int,
    stop_event,
    ready_connection,
) -> None:
    listener: Optional[socket.socket] = None
    workers: list[threading.Thread] = []
    try:
        os.chdir(run_dir)
        directory = Path(".").stat()
        if directory.st_mode & 0o077:
            raise CredentialError("run directory must not be accessible to other users")
        try:
            metadata = os.lstat(_SOCKET_NAME)
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not stat.S_ISSOCK(metadata.st_mode):
                raise CredentialError("stale broker path is not a socket")
            os.unlink(_SOCKET_NAME)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(_SOCKET_NAME)
        os.chmod(_SOCKET_NAME, 0o600)
        listener.listen(128)
        listener.settimeout(0.1)
        ready_connection.send((True, ""))
        allowed_samples = frozenset(expected_sample_ids)
        while not stop_event.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            worker = threading.Thread(
                target=_serve_connection,
                kwargs={
                    "connection": connection,
                    "config_digest": config_digest,
                    "environment_name": environment_name,
                    "secret": secret,
                    "expected_sample_ids": allowed_samples,
                    "expected_uid": expected_uid,
                },
                daemon=True,
            )
            worker.start()
            workers.append(worker)
        for worker in workers:
            worker.join(timeout=2)
    except BaseException as error:
        try:
            ready_connection.send((False, str(error)))
        except (BrokenPipeError, EOFError):
            pass
    finally:
        ready_connection.close()
        if listener is not None:
            listener.close()
        try:
            metadata = os.lstat(_SOCKET_NAME)
            if stat.S_ISSOCK(metadata.st_mode):
                os.unlink(_SOCKET_NAME)
        except FileNotFoundError:
            pass


class CredentialBroker:
    """Own one short-lived credential server while a run lock is held."""

    def __init__(
        self,
        *,
        run_dir: Path,
        config_digest: str,
        environment_name: str,
        secret: str,
        expected_sample_ids: Iterable[str],
    ) -> None:
        _validate_identity(config_digest, "validation_sample", environment_name)
        samples = tuple(expected_sample_ids)
        if not samples:
            raise CredentialError("credential broker requires expected sample IDs")
        for sample in samples:
            if _SAMPLE_ID.fullmatch(sample) is None:
                raise CredentialError("credential broker sample ID is invalid")
        if not isinstance(secret, str) or not secret or "\x00" in secret:
            raise CredentialError("credential value is invalid")
        if len(secret.encode("utf-8")) > 64 * 1024:
            raise CredentialError("credential value exceeds broker bound")
        self.run_dir = Path(run_dir)
        self.config_digest = config_digest
        self.environment_name = environment_name
        self._secret = secret
        self._samples = samples
        self._context = multiprocessing.get_context("spawn")
        self._stop_event = self._context.Event()
        self._process = None

    @property
    def socket_path(self) -> Path:
        return self.run_dir / _SOCKET_NAME

    def start(self) -> None:
        if self._process is not None:
            raise CredentialError("credential broker is already started")
        try:
            metadata = self.run_dir.lstat()
        except OSError as error:
            raise CredentialError(f"cannot inspect run directory: {error}") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CredentialError("run directory must be a real directory")
        if metadata.st_mode & 0o077:
            raise CredentialError("run directory mode must be 0700")
        parent_connection, child_connection = self._context.Pipe(duplex=False)
        self._process = self._context.Process(
            target=_broker_process,
            args=(
                str(self.run_dir),
                self.config_digest,
                self.environment_name,
                self._secret,
                self._samples,
                os.getuid(),
                self._stop_event,
                child_connection,
            ),
            daemon=True,
        )
        original_environment = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(
                {
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            self._process.start()
        finally:
            os.environ.clear()
            os.environ.update(original_environment)
        child_connection.close()
        try:
            if not parent_connection.poll(5):
                raise CredentialError("credential broker did not become ready")
            ready, message = parent_connection.recv()
            if not ready:
                raise CredentialError(message or "credential broker failed to start")
        finally:
            parent_connection.close()
        if not self._process.is_alive():
            raise CredentialError("credential broker exited during startup")

    def _remove_stale_socket(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode):
            raise CredentialError("broker cleanup refused non-socket path")
        self.socket_path.unlink()
        directory = os.open(
            self.run_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def stop(self) -> None:
        process = self._process
        if process is not None:
            self._stop_event.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                raise CredentialError("credential broker could not be stopped")
            self._process = None
        self._remove_stale_socket()

    def terminate_for_test(self) -> None:
        if self._process is None:
            raise CredentialError("credential broker is not running")
        self._process.terminate()
        self._process.join(timeout=2)

    def __enter__(self) -> "CredentialBroker":
        self.start()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.stop()


def request_credential(
    *,
    run_dir: Path,
    config_digest: str,
    sample_id: str,
    environment_name: str,
) -> str:
    """Retrieve one credential using a short relative AF_UNIX path."""

    _validate_identity(config_digest, sample_id, environment_name)
    request = json.dumps(
        {
            "config_sha256": config_digest,
            "sample_id": sample_id,
            "environment_name": environment_name,
            "uid": os.getuid(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with _CWD_LOCK:
        original = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        run_descriptor = -1
        connection: Optional[socket.socket] = None
        try:
            run_descriptor = os.open(
                Path(run_dir),
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            os.fchdir(run_descriptor)
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(5)
            connection.connect(_SOCKET_NAME)
            connection.sendall(request)
            response = bytearray()
            while b"\n" not in response:
                block = connection.recv(4096)
                if not block:
                    break
                response.extend(block)
                if len(response) > 128 * 1024:
                    raise CredentialError("credential response exceeds bound")
        except (OSError, socket.timeout) as error:
            raise CredentialError(f"credential broker request failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()
            os.fchdir(original)
            if run_descriptor >= 0:
                os.close(run_descriptor)
            os.close(original)
    try:
        value = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialError("credential broker returned invalid JSON") from error
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise CredentialError("credential broker rejected request")
    secret = value.get("value")
    if not isinstance(secret, str) or not secret:
        raise CredentialError("credential broker returned invalid value")
    return secret
