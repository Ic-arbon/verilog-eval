from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_generation.lifecycle import (
    LifecycleError,
    inventory_build_root,
    lifecycle_lock,
    quarantine_entry,
)


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
