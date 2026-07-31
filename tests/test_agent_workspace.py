from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_generation.contracts import AgentUsage, ProcessResult
from agent_generation.workspace import staged_workspace


class StagedWorkspaceTests(unittest.TestCase):
    def test_spec_task_contains_only_public_task_and_selected_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "runtime"

            with staged_workspace(
                work_root=work_root,
                task="spec-to-rtl",
                prompt_text="Build TopModule from this public specification.\n",
                rules_text="Use synthesizable SystemVerilog.\n",
            ) as prepared:
                names = {path.name for path in prepared.root.iterdir()}
                self.assertEqual(names, {"TASK.md", "RULES.md"})
                self.assertEqual(
                    (prepared.root / "TASK.md").read_text(),
                    "Build TopModule from this public specification.\n",
                )
                self.assertEqual(
                    (prepared.root / "RULES.md").read_text(),
                    "Use synthesizable SystemVerilog.\n",
                )
                self.assertFalse((prepared.root / "TopModule.sv").exists())
                self.assertIsNone(prepared.starter_sha256)
                self.assertFalse(any("_ref.sv" in name for name in names))
                self.assertFalse(any("_test.sv" in name for name in names))

    def test_code_completion_stages_only_the_public_starter(self):
        starter = "module TopModule(input logic a, output logic y);\n"
        with tempfile.TemporaryDirectory() as tmp:
            with staged_workspace(
                work_root=Path(tmp),
                task="code-complete-iccad2023",
                prompt_text="Complete the module.\n",
                starter_text=starter,
            ) as prepared:
                self.assertEqual(
                    {path.name for path in prepared.root.iterdir()},
                    {"TASK.md", "TopModule.sv"},
                )
                self.assertEqual(
                    (prepared.root / "TopModule.sv").read_text(), starter
                )
                self.assertEqual(
                    prepared.starter_sha256,
                    hashlib.sha256(starter.encode()).hexdigest(),
                )

    def test_task_and_starter_combinations_are_validated(self):
        invalid_cases = (
            {"task": "unknown", "starter_text": None},
            {"task": "code-complete-iccad2023", "starter_text": None},
            {"task": "spec-to-rtl", "starter_text": "module TopModule;"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            for case in invalid_cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    with staged_workspace(
                        work_root=Path(tmp),
                        prompt_text="Public prompt",
                        **case,
                    ):
                        self.fail("invalid workspace must not be yielded")

    def test_workspace_is_unique_and_removed_after_use_or_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "runtime"
            observed_roots = []

            for should_raise in (False, True):
                with self.subTest(should_raise=should_raise):
                    try:
                        with staged_workspace(
                            work_root=work_root,
                            task="spec-to-rtl",
                            prompt_text="Public prompt",
                        ) as prepared:
                            observed_roots.append(prepared.root)
                            (prepared.root / ".cache").mkdir()
                            (prepared.root / ".cache" / "state").write_text("runtime")
                            if should_raise:
                                raise RuntimeError("fake executor failed")
                    except RuntimeError:
                        if not should_raise:
                            raise
                    self.assertFalse(observed_roots[-1].exists())

            self.assertEqual(len(set(observed_roots)), 2)
            self.assertEqual(list(work_root.iterdir()), [])


class ProcessResultTests(unittest.TestCase):
    def test_process_result_keeps_status_and_nullable_usage_separate(self):
        result = ProcessResult(
            status="timeout",
            exit_code=124,
            duration_seconds=30.0,
            stdout="partial trajectory",
            stderr="",
            usage=AgentUsage.unavailable(),
        )

        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.usage.input_tokens)

    def test_process_result_rejects_unknown_status_and_negative_duration(self):
        invalid_values = (
            {"status": "missing", "duration_seconds": 1.0},
            {"status": "completed", "duration_seconds": -0.1},
        )

        for overrides in invalid_values:
            values = {
                "status": "completed",
                "exit_code": 0,
                "duration_seconds": 1.0,
                "stdout": "",
                "stderr": "",
                "usage": AgentUsage.unavailable(),
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                ProcessResult(**values)


if __name__ == "__main__":
    unittest.main()
