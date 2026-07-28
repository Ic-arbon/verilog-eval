import json
import tempfile
import unittest
from pathlib import Path

from agent_eval.adapters import create_adapter
from agent_eval.metrics import parse_trajectory
from agent_eval.models import AgentRequest
from agent_eval.workspace import prepare_workspace


class WorkspaceTests(unittest.TestCase):
    def test_prepare_workspace_exposes_prompt_but_not_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset_spec-to-rtl"
            dataset.mkdir()
            (dataset / "Prob001_zero_prompt.txt").write_text("Implement TopModule.")
            (dataset / "Prob001_zero_ref.sv").write_text("secret reference")
            (dataset / "Prob001_zero_test.sv").write_text("secret test")

            workspace = prepare_workspace(
                repo_root=root,
                run_root=root / "runs",
                task="spec-to-rtl",
                problem="Prob001_zero",
            )

            self.assertIn("Implement TopModule.", (workspace / "TASK.md").read_text())
            self.assertTrue((workspace / "AGENT_INSTRUCTIONS.md").exists())
            self.assertFalse(any(workspace.rglob("*_ref.sv")))
            self.assertFalse(any(workspace.rglob("*_test.sv")))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.request = AgentRequest(
            problem_id="Prob001_zero",
            workspace=Path("/workspace"),
            model="qwen3.6-coder",
            timeout_seconds=180,
        )

    def test_pi_uses_json_mode_and_ephemeral_session(self):
        command = create_adapter("pi").agent_command(self.request)
        self.assertEqual(command[0], "/agent-tools/node_modules/.bin/pi")
        self.assertIn("--mode", command)
        self.assertIn("json", command)
        self.assertIn("--no-session", command)
        self.assertIn("read,write,edit,bash", command)

    def test_opencode_uses_json_mode_and_isolated_directory(self):
        command = create_adapter("opencode").agent_command(self.request)
        self.assertEqual(command[0], "/agent-tools/node_modules/.bin/opencode")
        self.assertIn("--format", command)
        self.assertIn("json", command)
        self.assertIn("--pure", command)
        self.assertIn("/workspace", command)

    def test_unknown_adapter_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown agent"):
            create_adapter("unknown")


class MetricsTests(unittest.TestCase):
    def test_pi_jsonl_metrics_count_turns_tools_and_tokens(self):
        lines = [
            json.dumps({"type": "turn_end"}),
            json.dumps({"type": "tool_execution_start", "toolName": "bash"}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 120, "output": 30},
                    },
                }
            ),
        ]

        metrics = parse_trajectory("pi", lines)

        self.assertEqual(metrics.turns, 1)
        self.assertEqual(metrics.tool_calls, 1)
        self.assertEqual(metrics.input_tokens, 120)
        self.assertEqual(metrics.output_tokens, 30)

    def test_invalid_json_lines_are_preserved_as_parse_errors(self):
        metrics = parse_trajectory("opencode", ["not-json"])
        self.assertEqual(metrics.parse_errors, 1)


if __name__ == "__main__":
    unittest.main()
