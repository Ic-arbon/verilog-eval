from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_generation.lifecycle import (
    LifecycleError,
    abandon_recovery,
    acknowledge_recovery,
    inventory_build_root,
    lifecycle_lock,
    list_recovery_receipts,
    publish_recovery_receipt,
    quarantine_entry,
    run_lock,
    synthesize_orphan_recovery_receipts,
)
from agent_generation.run_config import publish_run_config
from tests.test_agent_run_config import valid_config


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "quarantine-agent-runs"


class LifecycleTests(unittest.TestCase):
    def test_inventory_is_read_only_and_classifies_managed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            complete = root / ("a" * 64)
            partial = root / ("b" * 64)
            complete.mkdir()
            partial.mkdir()
            (complete / "run-config.json").write_text("{}\n")
            (root / ".recoveries").mkdir()
            (root / ".recoveries" / "receipt.json").write_text("{}\n")
            (root / ".lifecycle.lock").touch()

            before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            inventory = inventory_build_root(root)
            after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))

            self.assertEqual(before, after)
            self.assertEqual(inventory["run_roots"], ["a" * 64, "b" * 64])
            self.assertEqual(inventory["receipts"], ["receipt.json"])
            self.assertIn(".lifecycle.lock", inventory["control_entries"])

    def test_same_run_lock_is_nonblocking_and_nonce_roots_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / ("1" * 64)
            second = root / ("2" * 64)
            first.mkdir()
            second.mkdir()
            with run_lock(first):
                with self.assertRaises(LifecycleError):
                    with run_lock(first):
                        pass
                with run_lock(second):
                    pass

    def test_quarantine_renames_directory_without_recursive_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = "c" * 64
            run = root / name
            run.mkdir()
            evidence = run / "evidence"
            evidence.write_text("keep me")

            with lifecycle_lock(root, exclusive=True):
                destination = quarantine_entry(root, name)

            self.assertFalse(run.exists())
            self.assertTrue(destination.parent.samefile(root / "quarantine"))
            self.assertEqual((destination / "evidence").read_text(), "keep me")

    def test_symlink_and_path_traversal_are_never_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            (root / ("d" * 64)).symlink_to(outside, target_is_directory=True)

            for name in ("d" * 64, "../outside", "not-a-run"):
                with self.subTest(name=name), self.assertRaises(LifecycleError):
                    with lifecycle_lock(root, exclusive=True):
                        quarantine_entry(root, name)
            self.assertTrue(outside.is_dir())

    def test_recovery_receipt_closes_config_publication_crash_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = valid_config()
            config["nonce"] = "1" * 32
            config_path = publish_run_config(root, config)
            digest = config_path.parent.name

            created = synthesize_orphan_recovery_receipts(root)
            self.assertEqual(len(created), 1)
            receipts = list_recovery_receipts(root)
            self.assertEqual(receipts[0]["config_sha256"], digest)
            self.assertFalse(receipts[0]["acknowledged"])

            acknowledge_recovery(root, digest)
            self.assertTrue(list_recovery_receipts(root)[0]["acknowledged"])
            self.assertEqual(synthesize_orphan_recovery_receipts(root), [])

    def test_receipt_publication_is_idempotent_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = valid_config()
            config["nonce"] = "2" * 32
            config_path = publish_run_config(root, config)
            digest = config_path.parent.name

            first = publish_recovery_receipt(root, digest)
            second = publish_recovery_receipt(root, digest)
            self.assertEqual(first, second)
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)

    def test_abandon_quarantines_incomplete_run_and_receipt_but_refuses_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = valid_config()
            config["nonce"] = "3" * 32
            config_path = publish_run_config(root, config)
            digest = config_path.parent.name
            publish_recovery_receipt(root, digest)

            destination = abandon_recovery(root, digest)
            self.assertTrue(destination.is_dir())
            self.assertFalse(config_path.parent.exists())
            self.assertEqual(list_recovery_receipts(root), [])
            self.assertTrue(any((root / "quarantine").glob(f"{digest}*.recovery.json")))

            config["nonce"] = "4" * 32
            complete_path = publish_run_config(root, config)
            complete_digest = complete_path.parent.name
            publish_recovery_receipt(root, complete_digest)
            (complete_path.parent / "agent-summary.json").write_text("{}\n")
            with self.assertRaises(LifecycleError):
                abandon_recovery(root, complete_digest)

    def test_standalone_helper_self_test_and_json_inventory(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self_test = subprocess.run(
            (str(SCRIPT), "--self-test"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(self_test.returncode, 0, self_test.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ("e" * 64)).mkdir()
            result = subprocess.run(
                (str(SCRIPT), "--build-root", str(root), "--inventory"),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["run_roots"], ["e" * 64])


if __name__ == "__main__":
    unittest.main()
