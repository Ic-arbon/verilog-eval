from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_generation.cli import (
    AgentGeneratorConfig,
    _argument_parser,
    main,
    run_agent_generation,
)
from agent_generation.contracts import (
    AgentEnvironment,
    AgentUsage,
    ProcessResult,
    RuntimeProvenance,
)


class FakeDriver:
    profile_id = "fake-artifact-v1"

    def __init__(self):
        self.requests = []

    def write_config(self, request):
        self.requests.append(request)
        config_dir = request.workspace / ".agent-config"
        config_dir.mkdir()
        config_path = config_dir / "fake.json"
        config_path.write_text('{"fake": true}\n')
        return (config_path,)

    def build_command(self, request):
        return ("fake-agent", "--workspace", "/workspace")

    def environment(self, request):
        return AgentEnvironment(
            variables=(("HOME", "/workspace/.home"),),
            inherit=("VLLM_API_KEY",),
        )

    def parse_event(self, line):
        return None

    def classify_budget_event(self, line):
        return "turn" if line == "turn" else None


class FakeExecutor:
    def __init__(self, result, candidate=None):
        self.result = result
        self.candidate = candidate
        self.specs = []
        self.workspace_paths = []
        self.workspace_snapshots = []

    def run(self, spec):
        self.specs.append(spec)
        self.workspace_paths.append(spec.workspace)
        self.workspace_snapshots.append(
            {
                path.name: path.read_text()
                for path in spec.workspace.iterdir()
                if path.is_file()
            }
        )
        if self.candidate is not None:
            (spec.workspace / "TopModule.sv").write_text(self.candidate)
        return self.result


def completed_result(stdout="trajectory\n", stderr=""):
    return ProcessResult(
        status="completed",
        exit_code=0,
        duration_seconds=1.25,
        stdout=stdout,
        stderr=stderr,
        usage=AgentUsage(
            input_tokens=120,
            output_tokens=30,
            turns=2,
            tool_calls=3,
            usage_source="fake_events",
        ),
    )


class AgentGeneratorCliTests(unittest.TestCase):
    def test_thinking_agent_defaults_to_16k_output_budget(self):
        parser = _argument_parser()
        args = parser.parse_args(
            [
                "--agent=pi",
                "--model=qwen3.6-coder",
                "--task=spec-to-rtl",
                "--output=TopModule.sv",
                "prompt.txt",
            ]
        )

        self.assertEqual(args.max_tokens, 16384)

    def test_script_is_executable_generator_program(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "sv-agent-generate"

        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK))

    def test_cli_runs_one_sample_with_injected_runtime_and_public_rules(self):
        candidate = "module TopModule(output zero); assign zero=1'b0; endmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            output = root / "build" / "Prob001_zero_sample01.sv"
            prompt.write_text("Produce a constant-zero TopModule.\n")
            driver = FakeDriver()
            executor = FakeExecutor(completed_result(), candidate=candidate)
            argv = [
                "--agent=pi",
                "--model=qwen3.6-coder",
                "--task=spec-to-rtl",
                "--examples=0",
                "--rules",
                "--max-tokens=8192",
                "--temperature=0.6",
                "--top-p=0.95",
                "--agent-thinking=off",
                "--agent-timeout=30",
                "--agent-max-turns=10",
                "--agent-max-tool-calls=20",
                "--agent-max-input-tokens=16384",
                f"--work-root={root / 'runtime'}",
                f"--output={output}",
                str(prompt),
            ]

            with redirect_stdout(io.StringIO()):
                exit_code = main(argv, driver=driver, executor=executor)

            self.assertEqual(exit_code, 0)
            self.assertEqual(output.read_text(), candidate)
            self.assertIn("TASK.md", executor.workspace_snapshots[0])
            self.assertIn("RULES.md", executor.workspace_snapshots[0])
            self.assertIn(
                "synchronous reset",
                executor.workspace_snapshots[0]["RULES.md"],
            )
            self.assertNotIn("TopModule.sv", executor.workspace_snapshots[0])
            self.assertEqual(driver.requests[0].max_input_tokens, 16384)


class AgentGeneratorVerticalSliceTests(unittest.TestCase):
    def make_config(self, root):
        prompt = root / "Prob001_zero_prompt.txt"
        prompt.write_text("Produce a constant-zero TopModule.\n")
        return AgentGeneratorConfig(
            sample_id="Prob001_zero_sample01",
            agent_name="fake",
            model="qwen3.6-coder",
            task="spec-to-rtl",
            prompt_path=prompt,
            output_path=root / "build" / "Prob001_zero_sample01.sv",
            work_root=root / "runtime",
            timeout_seconds=30,
            max_turns=10,
            max_tool_calls=20,
            max_input_tokens=16384,
            per_call_max_tokens=8192,
            rules_text=None,
        )

    def test_file_submission_is_published_with_manifest_and_sidecars(self):
        candidate = "module TopModule(output zero); assign zero=1'b0; endmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_config = self.make_config(root)
            config = AgentGeneratorConfig(
                **{
                    **base_config.__dict__,
                    "runtime_provenance": RuntimeProvenance(
                        source_revision="1" * 40,
                        source_diff_sha256="2" * 64,
                        docker_image="verilog-eval-agent-sandbox:v1",
                        docker_image_id="sha256:" + "3" * 64,
                        agent_tools_versions="pi=0.82.1 opencode=1.18.7",
                        agent_tools_content_sha256="5" * 64,
                    ),
                }
            )
            driver = FakeDriver()
            executor = FakeExecutor(
                completed_result(stderr="diagnostic\n"),
                candidate=candidate,
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                result = run_agent_generation(config, driver, executor)

            self.assertEqual(result.process.status, "completed")
            self.assertEqual(result.submission.status, "published")
            self.assertEqual(config.output_path.read_text(), candidate)
            self.assertEqual(len(driver.requests), 1)
            self.assertEqual(executor.specs[0].command[0], "fake-agent")
            self.assertEqual(executor.specs[0].timeout_seconds, 30)
            self.assertEqual(executor.specs[0].max_turns, 10)
            self.assertEqual(executor.specs[0].max_tool_calls, 20)
            self.assertEqual(executor.specs[0].event_classifier("turn"), "turn")
            self.assertEqual(
                executor.specs[0].environment.variables,
                (("HOME", "/workspace/.home"),),
            )
            self.assertEqual(
                executor.specs[0].environment.inherit,
                ("VLLM_API_KEY",),
            )
            self.assertFalse(executor.workspace_paths[0].exists())

            manifest_path = config.output_path.with_name(
                "Prob001_zero_sample01-generation.json"
            )
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["schema_version"], "agent-generation/v1")
            self.assertEqual(manifest["producer"]["kind"], "agent")
            self.assertEqual(manifest["producer"]["profile"], "fake-artifact-v1")
            self.assertEqual(manifest["execution"]["status"], "completed")
            self.assertIsNone(manifest["execution"]["termination_reason"])
            self.assertEqual(manifest["submission"]["status"], "published")
            self.assertEqual(
                manifest["limits"],
                {
                    "timeout_seconds": 30,
                    "max_turns": 10,
                    "max_tool_calls": 20,
                    "max_input_tokens": 16384,
                    "per_call_max_tokens": 8192,
                },
            )
            self.assertEqual(manifest["usage"]["input_tokens"], 120)
            self.assertEqual(manifest["runtime"]["source_revision"], "1" * 40)
            self.assertEqual(
                manifest["runtime"]["docker_image_id"], "sha256:" + "3" * 64
            )
            self.assertEqual(
                manifest["runtime"]["agent_tools_versions"],
                "pi=0.82.1 opencode=1.18.7",
            )
            self.assertEqual(
                manifest["runtime"]["agent_tools_content_sha256"], "5" * 64
            )
            self.assertNotIn("workspace", json.dumps(manifest))

            trajectory = config.output_path.with_name(
                "Prob001_zero_sample01-trajectory.jsonl"
            )
            stderr = config.output_path.with_name(
                "Prob001_zero_sample01-stderr.log"
            )
            self.assertEqual(trajectory.read_text(), "trajectory\n")
            self.assertEqual(stderr.read_text(), "diagnostic\n")
            self.assertIn("agent_status = completed", stdout.getvalue())
            self.assertIn("submission_status = published", stdout.getvalue())
            self.assertIn("prompt_tokens = 120", stdout.getvalue())
            self.assertIn("resp_tokens   = 30", stdout.getvalue())

    def test_chat_code_without_workspace_artifact_is_not_extracted(self):
        chat_code = "[BEGIN]\nmodule TopModule; endmodule\n[DONE]\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_config(root)
            executor = FakeExecutor(completed_result(stdout=chat_code))

            with redirect_stdout(io.StringIO()):
                result = run_agent_generation(config, FakeDriver(), executor)

            self.assertEqual(result.process.status, "completed")
            self.assertEqual(result.submission.status, "missing")
            output = config.output_path.read_text()
            self.assertIn("missing_submission", output)
            self.assertNotIn("module TopModule", output)
            trajectory = config.output_path.with_name(
                "Prob001_zero_sample01-trajectory.jsonl"
            )
            self.assertEqual(trajectory.read_text(), chat_code)

    def test_timeout_status_is_preserved_when_candidate_exists(self):
        candidate = "module TopModule; endmodule\n"
        timeout = ProcessResult(
            status="timeout",
            exit_code=124,
            duration_seconds=30.0,
            stdout="partial\n",
            stderr="deadline exceeded\n",
            usage=AgentUsage.unavailable(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_config(root)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = run_agent_generation(
                    config,
                    FakeDriver(),
                    FakeExecutor(timeout, candidate=candidate),
                )

            self.assertEqual(result.process.status, "timeout")
            self.assertEqual(result.submission.status, "published")
            self.assertEqual(config.output_path.read_text(), candidate)
            manifest = json.loads(
                config.output_path.with_name(
                    "Prob001_zero_sample01-generation.json"
                ).read_text()
            )
            self.assertEqual(manifest["execution"]["status"], "timeout")
            self.assertEqual(manifest["submission"]["status"], "published")
            self.assertIsNone(manifest["usage"]["input_tokens"])
            self.assertIn("prompt_tokens = unavailable", stdout.getvalue())
            self.assertIn("resp_tokens   = unavailable", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
