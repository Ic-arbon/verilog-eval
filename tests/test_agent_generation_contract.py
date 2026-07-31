from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

from agent_generation.contracts import AgentRunRequest, AgentUsage
from agent_generation.submission import (
    DEFAULT_MAX_CANDIDATE_BYTES,
    publish_submission,
)


class AgentRunRequestTests(unittest.TestCase):
    def make_request(self, **overrides):
        values = {
            "sample_id": "Prob001_zero_sample01",
            "agent_name": "fake",
            "model": "qwen3.6-coder",
            "task": "spec-to-rtl",
            "prompt_text": "Implement TopModule.",
            "rules_text": None,
            "workspace": Path("/tmp/public-workspace"),
            "timeout_seconds": 30,
            "max_turns": 10,
            "max_tool_calls": 20,
            "max_input_tokens": 16384,
            "per_call_max_tokens": 8192,
        }
        values.update(overrides)
        return AgentRunRequest(**values)

    def test_request_contains_no_hidden_grader_paths(self):
        request_fields = {field.name for field in fields(AgentRunRequest)}

        self.assertTrue(
            {
                "dataset_dir",
                "reference_path",
                "testbench_path",
                "build_dir",
                "grader_command",
            }.isdisjoint(request_fields)
        )

    def test_request_is_immutable(self):
        request = self.make_request()

        with self.assertRaises(FrozenInstanceError):
            request.model = "other-model"

    def test_request_rejects_sample_ids_that_can_change_paths(self):
        for sample_id in ("../secret", "nested/sample", "", ".", ".."):
            with self.subTest(sample_id=sample_id), self.assertRaises(ValueError):
                self.make_request(sample_id=sample_id)

    def test_request_rejects_invalid_task_and_non_positive_budgets(self):
        invalid_values = {
            "task": "unknown-task",
            "timeout_seconds": 0,
            "max_turns": 0,
            "max_tool_calls": -1,
            "max_input_tokens": 0,
            "per_call_max_tokens": 0,
        }

        for field_name, value in invalid_values.items():
            with self.subTest(field_name=field_name), self.assertRaises(ValueError):
                self.make_request(**{field_name: value})

    def test_unknown_usage_is_null_instead_of_zero(self):
        usage = AgentUsage.unavailable()

        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.output_tokens)
        self.assertIsNone(usage.turns)
        self.assertIsNone(usage.tool_calls)
        self.assertEqual(usage.usage_source, "unavailable")


class SubmissionPublicationTests(unittest.TestCase):
    def test_regular_candidate_is_published_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            output = root / "build" / "Prob001_zero_sample01.sv"
            workspace.mkdir()
            candidate = workspace / "TopModule.sv"
            candidate.write_text("module TopModule(output zero); assign zero=0; endmodule\n")

            result = publish_submission(workspace, output)

            expected = candidate.read_bytes()
            self.assertEqual(result.status, "published")
            self.assertEqual(result.output_path, output)
            self.assertEqual(result.sha256, hashlib.sha256(expected).hexdigest())
            self.assertEqual(result.size_bytes, len(expected))
            self.assertEqual(output.read_bytes(), expected)
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_missing_candidate_publishes_deterministic_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            output = root / "candidate.sv"
            workspace.mkdir()

            result = publish_submission(workspace, output)

            self.assertEqual(result.status, "missing")
            self.assertIsNone(result.sha256)
            self.assertEqual(
                output.read_text(),
                "// VERILOG_EVAL_GENERATION_FAILED: missing_submission\n"
                "VERILOG_EVAL_GENERATION_FAILED\n",
            )

    def test_unchanged_starter_is_not_a_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            output = root / "candidate.sv"
            workspace.mkdir()
            candidate = workspace / "TopModule.sv"
            candidate.write_text("module TopModule;\n")
            starter_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()

            result = publish_submission(
                workspace,
                output,
                starter_sha256=starter_sha256,
            )

            self.assertEqual(result.status, "missing")
            self.assertIn("unchanged_starter", output.read_text())

    def test_symlink_candidate_is_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            output = root / "candidate.sv"
            secret = root / "secret.sv"
            workspace.mkdir()
            secret.write_text("module TopModule; endmodule\n")
            (workspace / "TopModule.sv").symlink_to(secret)

            result = publish_submission(workspace, output)

            self.assertEqual(result.status, "invalid")
            self.assertIn("invalid_submission", output.read_text())
            self.assertNotIn("module TopModule", output.read_text())

    def test_empty_and_oversized_candidates_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            output = root / "candidate.sv"
            workspace.mkdir()
            candidate = workspace / "TopModule.sv"

            for content in (b"", b"x" * (DEFAULT_MAX_CANDIDATE_BYTES + 1)):
                with self.subTest(size=len(content)):
                    candidate.write_bytes(content)
                    result = publish_submission(workspace, output)
                    self.assertEqual(result.status, "invalid")
                    self.assertIn("invalid_submission", output.read_text())


if __name__ == "__main__":
    unittest.main()
