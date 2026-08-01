from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_generation.run_config import (
    RunConfigError,
    canonical_run_config,
    load_run_config,
    publish_run_config,
    run_config_sha256,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def valid_config() -> dict:
    return {
        "agent": {
            "name": "pi",
            "model": "qwen3.6-coder",
            "thinking": True,
        },
        "benchmark": {
            "task": "spec-to-rtl",
            "samples": 1,
            "examples": 0,
            "rules": False,
            "problems": ["Prob001_zero"],
            "inputs": [
                {
                    "kind": "problem_list",
                    "name": "problems.txt",
                    "sha256": DIGEST_A,
                    "size_bytes": 13,
                },
                {
                    "kind": "hidden_test",
                    "name": "Prob001_zero_test.sv",
                    "sha256": DIGEST_B,
                    "size_bytes": 42,
                },
            ],
        },
        "endpoint": {
            "base_url": "http://127.0.0.1:58000/v1",
            "api_key_environment": "OPENAI_API_KEY",
        },
        "limits": {
            "timeout_seconds": 300,
            "max_turns": 20,
            "max_tool_calls": 50,
            "max_input_tokens": 16384,
            "max_output_tokens": 16384,
        },
        "runtime": {
            "source_commit": "1" * 40,
            "docker_image_id": "sha256:" + "2" * 64,
            "docker_daemon_identity": "linux/amd64@daemon",
            "agent_tools": {
                "content_sha256": DIGEST_C,
                "lock_sha256": DIGEST_A,
                "versions": {"opencode-ai": "1.18.7"},
            },
            "toolchain": [
                {"name": "python", "identity": "/nix/store/python-3.11"},
                {"name": "make", "identity": "/nix/store/make-4.4"},
            ],
            "support_files": [
                {"name": "ca-bundle", "sha256": DIGEST_B, "size_bytes": 10}
            ],
        },
        "jobs": 4,
        "nonce": None,
    }


class CanonicalRunConfigTests(unittest.TestCase):
    def test_canonical_bytes_are_stable_utf8_and_define_digest(self):
        config = valid_config()
        canonical = canonical_run_config(config)

        self.assertTrue(canonical.endswith(b"\n"))
        self.assertEqual(
            canonical,
            (json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        self.assertEqual(run_config_sha256(canonical), __import__("hashlib").sha256(canonical).hexdigest())

    def test_locator_changes_cannot_enter_material_identity(self):
        config = valid_config()
        for forbidden in (
            "source_path",
            "build_dir",
            "dataset_dir",
            "docker_path",
            "tools_path",
            "certificate_path",
            "broker_path",
        ):
            with self.subTest(forbidden=forbidden):
                changed = json.loads(json.dumps(config))
                changed[forbidden] = "/machine/local/path"
                with self.assertRaises(RunConfigError):
                    canonical_run_config(changed)

    def test_unknown_sampling_secret_and_malformed_fields_fail_closed(self):
        cases = []
        for field, value in (("temperature", 0.6), ("top_p", 0.95), ("api_key", "secret")):
            changed = valid_config()
            changed["agent"][field] = value
            cases.append(changed)
        changed = valid_config()
        changed["jobs"] = True
        cases.append(changed)
        changed = valid_config()
        changed["benchmark"]["inputs"][0]["name"] = "../hidden"
        cases.append(changed)
        changed = valid_config()
        changed["nonce"] = "not-a-nonce"
        cases.append(changed)

        for config in cases:
            with self.subTest(config=config), self.assertRaises(RunConfigError):
                canonical_run_config(config)

    def test_duplicate_json_keys_are_rejected_when_loading(self):
        raw = b'{"jobs":4,"jobs":8}\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-config.json"
            path.write_bytes(raw)
            with self.assertRaises(RunConfigError):
                load_run_config(path, require_parent_digest=False)

    def test_known_secret_value_is_rejected_before_publication(self):
        config = valid_config()
        config["agent"]["model"] = "model-secret-canary"
        with self.assertRaises(RunConfigError):
            canonical_run_config(config, forbidden_values=("secret-canary",))


class RunConfigPublicationTests(unittest.TestCase):
    def test_publish_uses_digest_root_and_is_idempotent_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = canonical_run_config(valid_config())
            digest = run_config_sha256(canonical)

            first = publish_run_config(root, valid_config())
            second = publish_run_config(root, valid_config())

            self.assertEqual(first, root / digest / "run-config.json")
            self.assertEqual(second, first)
            self.assertEqual(first.read_bytes(), canonical)
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(first.parent.glob("*.tmp")), [])
            self.assertEqual(load_run_config(first), valid_config())

    def test_occupied_or_truncated_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = canonical_run_config(valid_config())
            digest = run_config_sha256(canonical)
            run = root / digest
            run.mkdir()
            config_path = run / "run-config.json"
            config_path.write_bytes(canonical[:17])
            os.chmod(config_path, 0o600)

            with self.assertRaises(RunConfigError):
                publish_run_config(root, valid_config())
            self.assertEqual(config_path.read_bytes(), canonical[:17])

    def test_parent_digest_mismatch_is_rejected_on_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("0" * 64) / "run-config.json"
            path.parent.mkdir()
            path.write_bytes(canonical_run_config(valid_config()))
            os.chmod(path, 0o600)

            with self.assertRaises(RunConfigError):
                load_run_config(path)


if __name__ == "__main__":
    unittest.main()
