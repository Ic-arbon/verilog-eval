from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_generation.report import (
    ReportTransactionError,
    commit_report_pair,
    validate_report_pair,
)


DIGEST = "a" * 64


def report() -> dict:
    return {
        "correctness": {"samples": 1, "passed": 1, "failed": 0, "pass_rate": 1.0},
        "execution": {"status_counts": {"completed": 1}},
        "submission": {"status_counts": {"published": 1}},
        "usage": {"input_tokens": {"value": None, "unknown_samples": 1}},
        "samples": [{"sample_id": "Prob001_zero_sample01"}],
    }


class ReportTransactionTests(unittest.TestCase):
    def make_paths(self, root: Path):
        summary = root / "summary.csv"
        summary.write_text("Prob001_zero,1,1,1.0,.\n")
        return summary, root / "agent-summary.json", root / "agent-summary.txt"

    def test_text_is_durable_before_json_completion_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, json_path, text_path = self.make_paths(root)
            text = "Agent evaluation\nVerilog Pass@1: 1/1 (100.00%)\n"
            events = []

            committed = commit_report_pair(
                report(),
                text=text,
                summary_csv=summary,
                run_config_sha256=DIGEST,
                json_path=json_path,
                text_path=text_path,
                fault=events.append,
            )

            self.assertEqual(text_path.read_text(), text)
            self.assertEqual(json.loads(json_path.read_text()), committed)
            self.assertLess(events.index("text_synced"), events.index("json_renamed"))
            evidence = committed["evidence"]
            self.assertEqual(evidence["run_config_sha256"], DIGEST)
            self.assertEqual(
                evidence["canonical_summary_sha256"],
                hashlib.sha256(summary.read_bytes()).hexdigest(),
            )
            self.assertEqual(evidence["text_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(validate_report_pair(summary, json_path, text_path, DIGEST), committed)
            self.assertNotIn("schema_version", committed)

    def test_text_only_partial_is_removed_and_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, json_path, text_path = self.make_paths(root)
            text_path.write_text("partial")

            committed = commit_report_pair(
                report(),
                text="complete\n",
                summary_csv=summary,
                run_config_sha256=DIGEST,
                json_path=json_path,
                text_path=text_path,
            )

            self.assertEqual(text_path.read_text(), "complete\n")
            self.assertEqual(json.loads(json_path.read_text()), committed)

    def test_existing_valid_pair_returns_without_rewrite_and_tamper_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, json_path, text_path = self.make_paths(root)
            first = commit_report_pair(
                report(),
                text="complete\n",
                summary_csv=summary,
                run_config_sha256=DIGEST,
                json_path=json_path,
                text_path=text_path,
            )
            first_mtime = json_path.stat().st_mtime_ns
            second = commit_report_pair(
                report(),
                text="complete\n",
                summary_csv=summary,
                run_config_sha256=DIGEST,
                json_path=json_path,
                text_path=text_path,
            )
            self.assertEqual(first, second)
            self.assertEqual(json_path.stat().st_mtime_ns, first_mtime)
            with self.assertRaises(ReportTransactionError):
                commit_report_pair(
                    report(),
                    text="different text must not replace a completed pair\n",
                    summary_csv=summary,
                    run_config_sha256=DIGEST,
                    json_path=json_path,
                    text_path=text_path,
                )

            text_path.write_text("tampered\n")
            with self.assertRaises(ReportTransactionError):
                validate_report_pair(summary, json_path, text_path, DIGEST)
            with self.assertRaises(ReportTransactionError):
                commit_report_pair(
                    report(),
                    text="new\n",
                    summary_csv=summary,
                    run_config_sha256=DIGEST,
                    json_path=json_path,
                    text_path=text_path,
                )

    def test_catchable_failure_after_json_rename_removes_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, json_path, text_path = self.make_paths(root)

            def fail(event):
                if event == "json_renamed":
                    raise OSError("injected")

            with self.assertRaises(ReportTransactionError):
                commit_report_pair(
                    report(),
                    text="text\n",
                    summary_csv=summary,
                    run_config_sha256=DIGEST,
                    json_path=json_path,
                    text_path=text_path,
                    fault=fail,
                )
            self.assertFalse(json_path.exists())


if __name__ == "__main__":
    unittest.main()
