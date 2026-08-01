from __future__ import annotations

import http.server
import json
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_generation.endpoint import (
    EndpointError,
    normalize_openai_base_url,
    preflight_endpoint,
)


SECRET = "endpoint-secret-canary"


class ThreadedServer:
    def __init__(self, handler):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def json_handler(payload, *, status=200, delay=0.0):
    encoded = json.dumps(payload).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        received_authorization = None

        def do_GET(self):
            type(self).received_authorization = self.headers.get("Authorization")
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    return Handler


class RawAmbiguousHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(65536)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 24\r\n"
            b"Content-Length: 25\r\n\r\n"
            b'{"data":[{"id":"m"}]}'
        )


class EndpointGrammarTests(unittest.TestCase):
    def test_base_normalization_preserves_safe_prefix_and_one_v1(self):
        self.assertEqual(
            normalize_openai_base_url("https://example.test/prefix/v1/"),
            "https://example.test/prefix/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("http://[::1]:58000/v1"),
            "http://[::1]:58000/v1",
        )

    def test_unsafe_or_non_loopback_plain_http_bases_are_rejected(self):
        invalid = (
            "http://example.test/v1",
            "https://user@example.test/v1",
            "https://example.test/v1?x=1",
            "https://example.test/not-v1",
            "https://example.test/%2e%2e/v1",
            "https://example.test/a//v1",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(EndpointError):
                normalize_openai_base_url(value)


class EndpointPreflightTests(unittest.TestCase):
    def test_exact_model_is_verified_without_exposing_key(self):
        handler = json_handler(
            {"object": "list", "data": [{"id": "qwen3.6-coder", "owned_by": "local"}]}
        )
        with ThreadedServer(handler) as base:
            evidence = preflight_endpoint(
                base_url=base,
                model="qwen3.6-coder",
                api_key=SECRET,
                timeout_seconds=3,
            )

        self.assertEqual(handler.received_authorization, f"Bearer {SECRET}")
        self.assertEqual(evidence["base_url"], base)
        self.assertEqual(evidence["model"], "qwen3.6-coder")
        self.assertEqual(evidence["model_count"], 1)
        self.assertRegex(evidence["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(SECRET, json.dumps(evidence))

    def test_missing_model_redirect_and_continuation_fail_closed(self):
        cases = (
            (json_handler({"data": [{"id": "other"}]}), "not advertised"),
            (json_handler({"data": [{"id": "qwen3.6-coder"}]}, status=302), "status"),
            (
                json_handler(
                    {"data": [{"id": "qwen3.6-coder"}], "has_more": True}
                ),
                "pagination",
            ),
        )
        for handler, message in cases:
            with self.subTest(message=message), ThreadedServer(handler) as base:
                with self.assertRaisesRegex(EndpointError, message):
                    preflight_endpoint(
                        base_url=base,
                        model="qwen3.6-coder",
                        api_key=SECRET,
                        timeout_seconds=3,
                    )

    def test_ambiguous_length_framing_is_rejected(self):
        with ThreadedServer(RawAmbiguousHandler) as base:
            with self.assertRaisesRegex(EndpointError, "length"):
                preflight_endpoint(
                    base_url=base,
                    model="m",
                    api_key=SECRET,
                    timeout_seconds=3,
                )

    def test_parent_total_deadline_terminates_stalled_helper(self):
        handler = json_handler({"data": [{"id": "m"}]}, delay=3)
        with ThreadedServer(handler) as base:
            started = time.monotonic()
            with self.assertRaisesRegex(EndpointError, "deadline"):
                preflight_endpoint(
                    base_url=base,
                    model="m",
                    api_key=SECRET,
                    timeout_seconds=0.25,
                )
            self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
