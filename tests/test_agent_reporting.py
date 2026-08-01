from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_generation.contracts import AgentUsage, ProcessResult
from agent_generation.report import ReportError, build_agent_report, write_agent_report
from agent_generation.sample_result import commit_sample_bundle
from tests.test_agent_sample_result import limits, runtime


DIGEST = "a" * 64


def process(status: str, sample_number: int, *, known_usage: bool) -> ProcessResult:
    if status == "completed":
        exit_code, reason = 0, None
    elif status == "timeout":
        exit_code, reason = 124, "timeout"
    else:
        exit_code, reason = 9, None
    usage = (
        AgentUsage(
            input_tokens=sample_number * 100,
            output_tokens=sample_number * 10,
            turns=sample_number,
            tool_calls=sample_number - 1,
            usage_source="trajectory",
        )
        if known_usage
        else AgentUsage.unavailable()
    )
    return ProcessResult(
        status=status,
        exit_code=exit_code,
        duration_seconds=float(sample_number),
        stdout="trajectory\n",
        stderr="",
        usage=usage,
        termination_reason=reason,
    )


def make_mixed_run(root: Path) -> Path:
    summary = root / "summary.csv"
    summary.write_text("Prob001_zero,1,3,0.33,.CS\n")
    for number, (status, candidate, known) in enumerate(
        (
            ("completed", "module TopModule; endmodule\n", True),
            ("timeout", "module TopModule; endmodule\n", True),
            ("error", None, False),
        ),
        1,
    ):
        workspace = root / f"workspace-{number}"
        workspace.mkdir()
        if candidate is not None:
            (workspace / "TopModule.sv").write_text(candidate)
        sample_id = f"Prob001_zero_sample{number:02d}"
        commit_sample_bundle(
            workspace=workspace,
            output_path=root / "Prob001_zero" / f"{sample_id}.sv",
            sample_id=sample_id,
            agent="pi",
            model="qwen3.6-coder",
            run_config_sha256=DIGEST,
            process=process(status, number, known_usage=known),
            limits=limits(),
            runtime=runtime(),
        )
    return summary


class AgentReportingTests(unittest.TestCase):
    def test_report_keeps_correctness_execution_and_submission_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_agent_report(
                make_mixed_run(root),
                run_config_sha256=DIGEST,
                expected_problems=("Prob001_zero",),
                expected_samples_per_problem=3,
            )

            self.assertNotIn("schema_version", report)
            self.assertEqual(report["run_config_sha256"], DIGEST)
            self.assertEqual(report["correctness"]["samples"], 3)
            self.assertEqual(report["correctness"]["passed"], 1)
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
            self.assertFalse(report["samples"][1]["correctness"]["passed"])
            self.assertEqual(report["samples"][1]["submission"]["status"], "published")

    def test_unknown_usage_is_explicit_and_never_fabricated_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_agent_report(
                make_mixed_run(Path(tmp)), run_config_sha256=DIGEST
            )
            input_tokens = report["usage"]["input_tokens"]
            total_tokens = report["usage"]["total_tokens"]
            self.assertIsNone(input_tokens["value"])
            self.assertEqual(input_tokens["known_sum"], 300)
            self.assertEqual(input_tokens["known_samples"], 2)
            self.assertEqual(input_tokens["unknown_samples"], 1)
            self.assertIsNone(total_tokens["value"])
            self.assertEqual(total_tokens["known_sum"], 330)

    def test_report_writes_text_first_and_json_completion_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = make_mixed_run(root)
            report = build_agent_report(summary, run_config_sha256=DIGEST)
            json_path = root / "agent-summary.json"
            text_path = root / "agent-summary.txt"

            committed = write_agent_report(
                report,
                summary_csv=summary,
                run_config_sha256=DIGEST,
                json_path=json_path,
                text_path=text_path,
            )

            self.assertEqual(json.loads(json_path.read_text()), committed)
            self.assertEqual(committed["evidence"]["run_config_sha256"], DIGEST)
            text = text_path.read_text()
            self.assertIn("Verilog Pass@1: 1/3 (33.33%)", text)
            self.assertIn("Input tokens: unavailable", text)

    def test_exact_expected_problem_set_and_bundle_hashes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = make_mixed_run(root)
            with self.assertRaises(ReportError):
                build_agent_report(
                    summary,
                    run_config_sha256=DIGEST,
                    expected_problems=("Prob999_extra",),
                )
            candidate = root / "Prob001_zero/Prob001_zero_sample01.sv"
            candidate.write_text("tampered\n")
            with self.assertRaises(ReportError):
                build_agent_report(summary, run_config_sha256=DIGEST)


if __name__ == "__main__":
    unittest.main()
