from __future__ import annotations

import copy
import json
import unittest

from agent_generation.result_contract import (
    HOST_BUDGET_EXIT_CODES,
    ResultContractError,
    canonical_manifest_bytes,
    validate_result_manifest,
)


D = "d" * 64


def valid_manifest() -> dict:
    return {
        "sample_id": "Prob001_zero_sample01",
        "producer": {
            "kind": "agent",
            "agent": "pi",
            "model": "qwen3.6-coder",
            "run_config_sha256": "a" * 64,
        },
        "execution": {
            "status": "completed",
            "exit_code": 0,
            "duration_seconds": 1.25,
            "termination_reason": None,
        },
        "limits": {
            "timeout_seconds": 300,
            "max_turns": 20,
            "max_tool_calls": 50,
            "max_input_tokens": 16384,
            "max_output_tokens": 16384,
        },
        "submission": {
            "status": "published",
            "source_sha256": "b" * 64,
            "source_size_bytes": 64,
        },
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "turns": 2,
            "tool_calls": 1,
            "usage_source": "trajectory",
        },
        "runtime": {
            "source_revision": "1" * 40,
            "docker_image_id": "sha256:" + "2" * 64,
            "docker_daemon_identity": "linux/amd64@daemon",
            "agent_tools_content_sha256": "3" * 64,
            "endpoint_base_url": "http://127.0.0.1:58000/v1",
            "endpoint_evidence_sha256": "4" * 64,
        },
        "artifacts": {
            "candidate": {"sha256": "5" * 64, "size_bytes": 64},
            "trajectory": {"sha256": "6" * 64, "size_bytes": 120},
            "stderr": {"sha256": "7" * 64, "size_bytes": 0},
        },
    }


class ResultContractTests(unittest.TestCase):
    def test_valid_manifest_has_canonical_unnumbered_bytes(self):
        manifest = valid_manifest()
        validated = validate_result_manifest(
            manifest,
            expected_sample_id="Prob001_zero_sample01",
            expected_run_config_sha256="a" * 64,
        )

        self.assertEqual(validated, manifest)
        encoded = canonical_manifest_bytes(manifest)
        self.assertEqual(
            encoded,
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        self.assertNotIn(b"schema_version", encoded)
        self.assertNotIn(b"profile", encoded)
        self.assertNotIn(b'"manifest"', encoded)

    def test_closed_execution_matrix(self):
        rows = (
            ("completed", 0, None),
            ("timeout", 124, "timeout"),
            ("error", 9, None),
            ("error", HOST_BUDGET_EXIT_CODES["max_turns"], "max_turns"),
            ("error", HOST_BUDGET_EXIT_CODES["max_tool_calls"], "max_tool_calls"),
        )
        for status, exit_code, reason in rows:
            with self.subTest(status=status, reason=reason):
                manifest = valid_manifest()
                manifest["execution"].update(
                    status=status,
                    exit_code=exit_code,
                    termination_reason=reason,
                )
                validate_result_manifest(manifest)

        invalid = valid_manifest()
        invalid["execution"].update(status="completed", exit_code=1)
        with self.assertRaises(ResultContractError):
            validate_result_manifest(invalid)
        invalid = valid_manifest()
        invalid["execution"].update(status="error", exit_code=9, termination_reason="timeout")
        with self.assertRaises(ResultContractError):
            validate_result_manifest(invalid)

    def test_unavailable_usage_requires_only_null_values(self):
        manifest = valid_manifest()
        manifest["usage"] = {
            "input_tokens": None,
            "output_tokens": None,
            "turns": None,
            "tool_calls": None,
            "usage_source": "unavailable",
        }
        validate_result_manifest(manifest)

        manifest["usage"]["turns"] = 0
        with self.assertRaises(ResultContractError):
            validate_result_manifest(manifest)

    def test_submission_status_is_orthogonal_and_source_identity_is_conditional(self):
        for status in ("missing", "invalid"):
            with self.subTest(status=status):
                manifest = valid_manifest()
                manifest["submission"] = {
                    "status": status,
                    "source_sha256": None,
                    "source_size_bytes": None,
                }
                validate_result_manifest(manifest)

        invalid = valid_manifest()
        invalid["submission"]["source_sha256"] = None
        with self.assertRaises(ResultContractError):
            validate_result_manifest(invalid)

    def test_manifest_never_hashes_itself(self):
        for key in ("manifest", "manifest_sha256", "manifest_size_bytes"):
            with self.subTest(key=key):
                manifest = valid_manifest()
                manifest["artifacts"][key] = {"sha256": D, "size_bytes": 1}
                with self.assertRaises(ResultContractError):
                    validate_result_manifest(manifest)

    def test_reserved_old_fields_and_leaky_extensions_fail(self):
        mutations = []
        for key, value in (
            ("schema_version", "agent-generation/v1"),
            ("profile", "legacy-v1"),
            ("profile_id", "legacy-v1"),
        ):
            manifest = valid_manifest()
            manifest[key] = value
            mutations.append(manifest)
        for key, value in (
            ("api_key", "secret"),
            ("host_path", "/opt/agent/private"),
            ("run_config", {"agent": "pi"}),
            ("hidden_reference_sha256", D),
        ):
            manifest = valid_manifest()
            manifest["extension"] = {key: value}
            mutations.append(manifest)

        for manifest in mutations:
            with self.subTest(manifest=manifest), self.assertRaises(ResultContractError):
                validate_result_manifest(manifest)

    def test_bounded_safe_additive_fields_are_tolerated(self):
        manifest = valid_manifest()
        manifest["extension"] = {"retry_count": 1, "note": "provider metadata"}
        validate_result_manifest(manifest)

    def test_expected_identity_mismatch_fails(self):
        with self.assertRaises(ResultContractError):
            validate_result_manifest(valid_manifest(), expected_sample_id="other")
        with self.assertRaises(ResultContractError):
            validate_result_manifest(
                valid_manifest(), expected_run_config_sha256="f" * 64
            )


if __name__ == "__main__":
    unittest.main()
