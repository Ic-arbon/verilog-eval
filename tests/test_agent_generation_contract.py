from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

from agent_generation.contracts import AgentRunRequest, AgentUsage


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


if __name__ == "__main__":
    unittest.main()
