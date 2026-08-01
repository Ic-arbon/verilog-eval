from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_generation.runtime_bindings import (
    RuntimeBindingError,
    load_runtime_bindings,
    publish_runtime_bindings,
    remove_runtime_bindings,
    runtime_bindings_path,
)


CONFIG_DIGEST = "a" * 64


def bindings(root: Path) -> dict:
    return {
        "run_config_sha256": CONFIG_DIGEST,
        "source_root": str(root / "source"),
        "dataset_dir": str(root / "source" / "dataset_spec-to-rtl"),
        "build_dir": str(root / "build"),
        "docker": {
            "client": str(root / "bin" / "docker"),
            "daemon": "unix:///var/run/docker.sock",
            "image": "verilog-eval-agent-sandbox:base",
            "archive": str(root / "image.tar"),
        },
        "tools_projection": str(root / "projection"),
        "toolchain": {
            "bash": str(root / "bin" / "bash"),
            "make": str(root / "bin" / "make"),
            "iverilog": str(root / "bin" / "iverilog"),
            "timeout": str(root / "bin" / "timeout"),
        },
        "support_files": {"ca_bundle": str(root / "ca-bundle.crt")},
        "credential_broker": ".credential.sock",
    }


class RuntimeBindingsTests(unittest.TestCase):
    def make_run(self, root: Path) -> Path:
        run = root / CONFIG_DIGEST
        run.mkdir()
        config = run / "run-config.json"
        config.write_text("{}\n", encoding="utf-8")
        return config

    def test_fixed_sibling_path_and_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_run(root)
            value = bindings(root)

            path = publish_runtime_bindings(config, value)

            self.assertEqual(path, config.with_name("runtime-bindings.json"))
            self.assertEqual(runtime_bindings_path(config), path)
            self.assertEqual(load_runtime_bindings(config), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_stale_binding_is_replaced_only_for_matching_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_run(root)
            stale = bindings(root)
            stale["tools_projection"] = str(root / "old")
            publish_runtime_bindings(config, stale)

            current = bindings(root)
            publish_runtime_bindings(config, current)

            self.assertEqual(load_runtime_bindings(config), current)

            mismatch = bindings(root)
            mismatch["run_config_sha256"] = "b" * 64
            with self.assertRaises(RuntimeBindingError):
                publish_runtime_bindings(config, mismatch)
            self.assertEqual(load_runtime_bindings(config), current)

    def test_secret_semantics_and_nonabsolute_locators_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_run(root)
            cases = []
            bad = bindings(root)
            bad["api_key"] = "secret"
            cases.append(bad)
            bad = bindings(root)
            bad["source_root"] = "relative/source"
            cases.append(bad)
            bad = bindings(root)
            bad["credential_broker"] = str(root / ".credential.sock")
            cases.append(bad)

            for value in cases:
                with self.subTest(value=value), self.assertRaises(RuntimeBindingError):
                    publish_runtime_bindings(config, value)

    def test_symlink_is_never_followed_and_removal_is_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_run(root)
            outside = root / "outside.json"
            outside.write_text("secret", encoding="utf-8")
            path = runtime_bindings_path(config)
            path.symlink_to(outside)

            with self.assertRaises(RuntimeBindingError):
                publish_runtime_bindings(config, bindings(root))
            self.assertEqual(outside.read_text(), "secret")
            path.unlink()

            publish_runtime_bindings(config, bindings(root))
            remove_runtime_bindings(config)
            self.assertFalse(path.exists())
            remove_runtime_bindings(config)


if __name__ == "__main__":
    unittest.main()
