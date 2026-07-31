from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_generation.report import ReportError, build_agent_report, write_agent_report


SCHEMA_VERSION = "agent-generation/v1"


def write_manifest(
    root: Path,
    *,
    problem: str,
    sample_number: int,
    execution_status: str,
    submission_status: str,
    input_tokens,
    output_tokens,
    turns,
    tool_calls,
) -> None:
    sample_id = f"{problem}_sample{sample_number:02d}"
    path = root / problem / f"{sample_id}-generation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "sample_id": sample_id,
                "producer": {
                    "kind": "agent",
                    "agent": "fake",
                    "profile": "fake-v1",
                    "model": "qwen3.6-coder",
                },
                "execution": {
                    "status": execution_status,
                    "exit_code": 0 if execution_status == "completed" else 124,
                    "duration_seconds": float(sample_number),
                },
                "submission": {
                    "status": submission_status,
                    "sha256": "a" * 64 if submission_status == "published" else None,
                    "size_bytes": 64 if submission_status == "published" else None,
                },
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "turns": turns,
                    "tool_calls": tool_calls,
                    "usage_source": (
                        "trajectory" if input_tokens is not None else "unavailable"
                    ),
                },
            }
        )
        + "\n"
    )


class AgentReportingTests(unittest.TestCase):
    def make_mixed_run(self, root: Path) -> Path:
        summary = root / "summary.csv"
        summary.write_text("Prob001_zero,1,3,0.333333, .CS\n".replace(" ", ""))
        write_manifest(
            root,
            problem="Prob001_zero",
            sample_number=1,
            execution_status="completed",
            submission_status="published",
            input_tokens=100,
            output_tokens=20,
            turns=2,
            tool_calls=1,
        )
        write_manifest(
            root,
            problem="Prob001_zero",
            sample_number=2,
            execution_status="timeout",
            submission_status="published",
            input_tokens=200,
            output_tokens=30,
            turns=3,
            tool_calls=2,
        )
        write_manifest(
            root,
            problem="Prob001_zero",
            sample_number=3,
            execution_status="error",
            submission_status="missing",
            input_tokens=None,
            output_tokens=None,
            turns=None,
            tool_calls=None,
        )
        return summary

    def test_report_keeps_correctness_execution_and_submission_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_agent_report(self.make_mixed_run(root))

            self.assertEqual(report["schema_version"], "agent-evaluation/v1")
            self.assertEqual(report["correctness"]["samples"], 3)
            self.assertEqual(report["correctness"]["passed"], 1)
            self.assertAlmostEqual(report["correctness"]["pass_rate"], 1 / 3)
            self.assertEqual(
                report["execution"]["status_counts"],
                {"completed": 1, "error": 1, "timeout": 1},
            )
            self.assertEqual(
                report["submission"]["status_counts"],
                {"missing": 1, "published": 2},
            )
            self.assertEqual(report["submission"]["conditional_passed"], 1)
            self.assertEqual(report["submission"]["conditional_pass_rate"], 0.5)
            self.assertEqual(report["samples"][1]["execution"]["status"], "timeout")
            self.assertFalse(report["samples"][1]["correctness"]["passed"])
            self.assertEqual(report["samples"][1]["submission"]["status"], "published")

    def test_unknown_usage_is_explicit_and_never_fabricated_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_agent_report(self.make_mixed_run(root))

            input_tokens = report["usage"]["input_tokens"]
            total_tokens = report["usage"]["total_tokens"]
            self.assertIsNone(input_tokens["value"])
            self.assertEqual(input_tokens["known_sum"], 300)
            self.assertEqual(input_tokens["known_samples"], 2)
            self.assertEqual(input_tokens["unknown_samples"], 1)
            self.assertIsNone(total_tokens["value"])
            self.assertEqual(total_tokens["known_sum"], 350)

    def test_report_writes_machine_and_human_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_agent_report(self.make_mixed_run(root))
            json_path = root / "agent-summary.json"
            text_path = root / "agent-summary.txt"

            write_agent_report(report, json_path=json_path, text_path=text_path)

            self.assertEqual(json.loads(json_path.read_text()), report)
            text = text_path.read_text()
            self.assertIn("Verilog Pass@1: 1/3 (33.33%)", text)
            self.assertIn("Execution: completed=1 error=1 timeout=1", text)
            self.assertIn("Submission: missing=1 published=2", text)
            self.assertIn("Input tokens: unavailable", text)

    def test_missing_or_inconsistent_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "summary.csv"
            summary.write_text("Prob001_zero,1,1,1.0,.\n")

            with self.assertRaises(ReportError):
                build_agent_report(summary)

            write_manifest(
                root,
                problem="Prob001_zero",
                sample_number=1,
                execution_status="completed",
                submission_status="published",
                input_tokens=1,
                output_tokens=1,
                turns=1,
                tool_calls=1,
            )
            manifest = next(root.glob("*/*-generation.json"))
            data = json.loads(manifest.read_text())
            data["sample_id"] = "wrong-sample"
            manifest.write_text(json.dumps(data))

            with self.assertRaises(ReportError):
                build_agent_report(summary)


if __name__ == "__main__":
    unittest.main()
