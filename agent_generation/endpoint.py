"""Bounded OpenAI-compatible model-list preflight in an isolated helper."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import SplitResult, urlsplit, urlunsplit


_UNRESERVED_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_MAX_STATUS_BYTES = 8192
_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_COUNT = 100
_MAX_BODY_BYTES = 1024 * 1024
_MAX_WIRE_BYTES = 2 * 1024 * 1024
_MAX_MODELS = 10_000
_MAX_ID_BYTES = 4096
_MAX_JSON_ITEMS = 100_000
_MAX_JSON_DEPTH = 32


class EndpointError(RuntimeError):
    """The endpoint grammar, transport, or advertised model is invalid."""


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _parse_base_url(base_url: str) -> SplitResult:
    if not isinstance(base_url, str) or not base_url or any(
        character in base_url for character in "\x00\r\n"
    ):
        raise EndpointError("base URL must be non-empty single-line text")
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise EndpointError(f"base URL authority is invalid: {error}") from error
    if parsed.scheme not in {"http", "https"} or hostname is None:
        raise EndpointError("base URL must be absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointError("base URL userinfo is forbidden")
    if parsed.query or parsed.fragment:
        raise EndpointError("base URL query and fragment are forbidden")
    if "%" in parsed.path or "//" in parsed.path:
        raise EndpointError("base URL path contains ambiguous encoding")
    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    segments = path.split("/")[1:] if path.startswith("/") else []
    if not segments or segments[-1] != "v1" or any(
        _UNRESERVED_SEGMENT.fullmatch(segment) is None for segment in segments
    ):
        raise EndpointError("base URL must end in an unreserved /v1 path")
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise EndpointError("plain HTTP is allowed only for loopback endpoints")
    if port is not None and not 1 <= port <= 65535:
        raise EndpointError("base URL port is invalid")
    return parsed._replace(path=path)


def normalize_openai_base_url(base_url: str) -> str:
    """Validate and normalize one OpenAI-compatible `/v1` base URL."""

    return urlunsplit(_parse_base_url(base_url))


def _models_path(parsed: SplitResult) -> str:
    return f"{parsed.path}/models"


def _remaining_timeout(timeout_seconds: float) -> float:
    return max(0.05, min(float(timeout_seconds), 30.0))


def _connect(parsed: SplitResult, timeout_seconds: float, ca_bundle: Optional[str]):
    hostname = parsed.hostname
    if hostname is None:
        raise EndpointError("endpoint hostname is missing")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as error:
        raise EndpointError(f"endpoint DNS resolution failed: {error}") from error
    last_error: Optional[OSError] = None
    for family, socktype, protocol, _canonical_name, address in addresses:
        connection = socket.socket(family, socktype, protocol)
        connection.settimeout(_remaining_timeout(timeout_seconds))
        try:
            connection.connect(address)
            if parsed.scheme == "https":
                context = ssl.create_default_context(cafile=ca_bundle)
                context.set_alpn_protocols(["http/1.1"])
                wrapped = context.wrap_socket(connection, server_hostname=hostname)
                negotiated = wrapped.selected_alpn_protocol()
                if negotiated not in {None, "http/1.1"}:
                    wrapped.close()
                    raise EndpointError("endpoint did not negotiate HTTP/1.1")
                return wrapped
            return connection
        except (OSError, ssl.SSLError) as error:
            last_error = error
            connection.close()
    raise EndpointError(f"endpoint connection failed: {last_error}")


def _read_wire(connection) -> bytes:
    wire = bytearray()
    while True:
        try:
            block = connection.recv(64 * 1024)
        except socket.timeout as error:
            raise EndpointError("endpoint transport deadline expired") from error
        if not block:
            break
        wire.extend(block)
        if len(wire) > _MAX_WIRE_BYTES:
            raise EndpointError("endpoint response exceeds wire-byte bound")
    return bytes(wire)


def _parse_headers(raw: bytes) -> tuple[int, dict[str, list[str]], bytes]:
    boundary = raw.find(b"\r\n\r\n")
    if boundary < 0 or boundary > _MAX_HEADER_BYTES:
        raise EndpointError("endpoint response headers are missing or oversized")
    head = raw[:boundary]
    body = raw[boundary + 4 :]
    lines = head.split(b"\r\n")
    if not lines or len(lines[0]) > _MAX_STATUS_BYTES:
        raise EndpointError("endpoint status line is invalid")
    try:
        status_parts = lines[0].decode("ascii").split(" ", 2)
        if len(status_parts) < 2 or status_parts[0] != "HTTP/1.1":
            raise ValueError
        status = int(status_parts[1])
    except (UnicodeDecodeError, ValueError) as error:
        raise EndpointError("endpoint requires a valid HTTP/1.1 status line") from error
    if len(lines) - 1 > _MAX_HEADER_COUNT:
        raise EndpointError("endpoint response has too many headers")
    headers: dict[str, list[str]] = {}
    for raw_line in lines[1:]:
        if not raw_line or raw_line[:1] in {b" ", b"\t"} or b":" not in raw_line:
            raise EndpointError("endpoint response contains malformed headers")
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise EndpointError("endpoint response headers must be ASCII") from error
        if not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name):
            raise EndpointError("endpoint response header name is invalid")
        headers.setdefault(name, []).append(value)
    return status, headers, body


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    offset = 0
    chunks = 0
    while True:
        line_end = body.find(b"\r\n", offset)
        if line_end < 0 or line_end - offset > 128:
            raise EndpointError("endpoint chunk framing is malformed")
        size_text = body[offset:line_end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError as error:
            raise EndpointError("endpoint chunk size is malformed") from error
        offset = line_end + 2
        chunks += 1
        if chunks > 10_000:
            raise EndpointError("endpoint response has too many chunks")
        if size == 0:
            if body[offset:] not in {b"", b"\r\n"}:
                raise EndpointError("endpoint trailers are unsupported")
            break
        end = offset + size
        if end + 2 > len(body) or body[end : end + 2] != b"\r\n":
            raise EndpointError("endpoint chunk framing is truncated")
        decoded.extend(body[offset:end])
        if len(decoded) > _MAX_BODY_BYTES:
            raise EndpointError("endpoint body exceeds decoded-byte bound")
        offset = end + 2
    return bytes(decoded)


def _response_body(raw: bytes) -> tuple[int, bytes]:
    status, headers, body = _parse_headers(raw)
    if status != 200:
        raise EndpointError(f"endpoint returned status {status}; expected 200")
    if "location" in headers:
        raise EndpointError("endpoint redirects are forbidden")
    encodings = headers.get("content-encoding", [])
    if len(encodings) > 1 or (encodings and encodings[0].lower() not in {"", "identity"}):
        raise EndpointError("endpoint compression is forbidden")
    lengths = headers.get("content-length", [])
    transfers = headers.get("transfer-encoding", [])
    if len(lengths) > 1:
        raise EndpointError("endpoint has ambiguous content length")
    if len(transfers) > 1 or (lengths and transfers):
        raise EndpointError("endpoint has ambiguous length framing")
    if transfers:
        if transfers[0].lower() != "chunked":
            raise EndpointError("endpoint transfer encoding is unsupported")
        decoded = _decode_chunked(body)
    elif lengths:
        try:
            expected = int(lengths[0])
        except ValueError as error:
            raise EndpointError("endpoint content length is invalid") from error
        if expected < 0 or expected > _MAX_BODY_BYTES or len(body) != expected:
            raise EndpointError("endpoint content length does not match body")
        decoded = body
    else:
        decoded = body
    if len(decoded) > _MAX_BODY_BYTES:
        raise EndpointError("endpoint body exceeds decoded-byte bound")
    return status, decoded


def _json_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise EndpointError(f"endpoint JSON has duplicate key: {key}")
        value[key] = child
    return value


def _bound_json(value: Any, *, depth: int = 0, counter: list[int]) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise EndpointError("endpoint JSON is too deeply nested")
    counter[0] += 1
    if counter[0] > _MAX_JSON_ITEMS:
        raise EndpointError("endpoint JSON has too many values")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > _MAX_ID_BYTES:
                raise EndpointError("endpoint JSON key is invalid")
            _bound_json(child, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for child in value:
            _bound_json(child, depth=depth + 1, counter=counter)
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > 64 * 1024:
            raise EndpointError("endpoint JSON string is oversized")
    elif isinstance(value, float) and not math.isfinite(value):
        raise EndpointError("endpoint JSON contains a non-finite number")
    elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**63 - 1:
        raise EndpointError("endpoint JSON integer is out of range")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise EndpointError("endpoint JSON contains an unsupported value")


def _validate_models(body: bytes, model: str) -> int:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_json_without_duplicates)
    except (UnicodeDecodeError, ValueError) as error:
        raise EndpointError(f"endpoint model list is invalid JSON: {error}") from error
    _bound_json(value, counter=[0])
    if not isinstance(value, dict):
        raise EndpointError("endpoint model list must be an object")
    allowed = {"data", "object", "has_more", "next", "next_page"}
    unknown = set(value) - allowed
    if unknown:
        if any("next" in key.lower() or "page" in key.lower() or "more" in key.lower() for key in unknown):
            raise EndpointError("endpoint uses unknown pagination metadata")
        raise EndpointError("endpoint model list has unknown top-level fields")
    if "object" in value and not isinstance(value["object"], str):
        raise EndpointError("endpoint object metadata is invalid")
    for key in ("has_more", "next", "next_page"):
        if key in value and not (
            value[key] is False or value[key] is None or value[key] == ""
        ):
            raise EndpointError("endpoint pagination indicates continuation")
    models = value.get("data")
    if not isinstance(models, list) or len(models) > _MAX_MODELS:
        raise EndpointError("endpoint data must be a bounded model list")
    identifiers: list[str] = []
    for entry in models:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise EndpointError("endpoint model entry has no string id")
        identifier = entry["id"]
        if not identifier or len(identifier.encode("utf-8")) > _MAX_ID_BYTES:
            raise EndpointError("endpoint model id is invalid")
        identifiers.append(identifier)
    if model not in identifiers:
        raise EndpointError(f"configured model {model!r} is not advertised")
    return len(models)


def _perform_preflight(
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    ca_bundle: Optional[str],
) -> dict[str, Any]:
    normalized = normalize_openai_base_url(base_url)
    parsed = _parse_base_url(normalized)
    if not model or any(character in model for character in "\x00\r\n"):
        raise EndpointError("model ID is invalid")
    if not api_key or any(character in api_key for character in "\x00\r\n"):
        raise EndpointError("API credential is invalid")
    connection = _connect(parsed, timeout_seconds, ca_bundle)
    try:
        path = _models_path(parsed)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Accept: application/json\r\n"
            "Accept-Encoding: identity\r\n"
            f"Authorization: Bearer {api_key}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        connection.sendall(request)
        wire = _read_wire(connection)
    finally:
        connection.close()
    _status, body = _response_body(wire)
    model_count = _validate_models(body, model)
    return {
        "base_url": normalized,
        "models_url": normalized + "/models",
        "model": model,
        "model_count": model_count,
        "response_sha256": hashlib.sha256(body).hexdigest(),
    }


def preflight_endpoint(
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float = 10.0,
    ca_bundle: Optional[Path] = None,
) -> dict[str, Any]:
    """Run bounded DNS/TLS/HTTP work in a killable helper process."""

    normalized = normalize_openai_base_url(base_url)
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise EndpointError("preflight deadline must be positive")
    command = [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--helper",
        "--base-url",
        normalized,
        "--model",
        model,
        "--timeout-seconds",
        str(float(timeout_seconds)),
    ]
    if ca_bundle is not None:
        command.extend(("--ca-bundle", str(Path(ca_bundle))))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
    except OSError as error:
        raise EndpointError(f"cannot start endpoint helper: {error}") from error
    try:
        stdout, stderr = process.communicate(
            api_key.encode("utf-8"),
            timeout=float(timeout_seconds),
        )
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=2)
        raise EndpointError("endpoint preflight total deadline expired") from error
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        raise EndpointError(message or "endpoint preflight helper failed")
    try:
        evidence = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EndpointError("endpoint helper returned invalid evidence") from error
    if not isinstance(evidence, dict):
        raise EndpointError("endpoint helper evidence must be an object")
    return evidence


def _helper_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--helper", action="store_true")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--ca-bundle")
    return parser


def _helper_main(argv: list[str]) -> int:
    arguments = _helper_parser().parse_args(argv)
    if not arguments.helper:
        return 2
    key = sys.stdin.buffer.read(64 * 1024 + 1)
    if len(key) > 64 * 1024:
        print("API credential exceeds helper bound", file=sys.stderr)
        return 2
    try:
        evidence = _perform_preflight(
            arguments.base_url,
            arguments.model,
            key.decode("utf-8"),
            arguments.timeout_seconds,
            arguments.ca_bundle,
        )
    except (EndpointError, UnicodeDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper_main(sys.argv[1:]))
