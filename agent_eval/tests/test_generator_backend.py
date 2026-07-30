import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_eval.backend import adapter_profile, build_agent_command
from agent_eval.generate import main, prepare_generation_workspace, publish_candidate
from agent_eval.models import TrajectoryMetrics
from agent_eval.runner import build_evaluation_commands


class BackendContractTests(unittest.TestCase):
    def test_backends_receive_neutral_task_without_tool_syntax(self):
        prompt = "Implement TopModule and save the final candidate to TopModule.sv."

        for agent in ("pi", "opencode"):
            command = build_agent_command(agent, "qwen3.6-coder", prompt)
            self.assertEqual(command[-1], prompt)
            self.assertNotIn("<tool_call>", " ".join(command))
            self.assertNotIn("<function=read>", " ".join(command))

    def test_opencode_uses_artifact_agent_with_diagnostics(self):
        command = build_agent_command("opencode", "qwen3.6-coder", "task")

        self.assertEqual(command[1:6], [
            "--print-logs",
            "--log-level",
            "DEBUG",
            "--pure",
            "run",
        ])
        self.assertIn("--thinking", command)
        self.assertEqual(command[command.index("--agent") + 1], "benchmark")
        self.assertEqual(adapter_profile("opencode"), "opencode-artifact-v4")
        self.assertEqual(
            adapter_profile("opencode", opencode_harness=True),
            "opencode-dcda-inline-v1",
        )
        chip_command = build_agent_command(
            "opencode",
            "qwen3.6-coder",
            "task",
            opencode_primary_agent="chip-rtl",
        )
        self.assertEqual(
            chip_command[chip_command.index("--agent") + 1],
            "chip-rtl",
        )
        self.assertEqual(
            adapter_profile(
                "opencode",
                opencode_harness=True,
                opencode_primary_agent="chip-rtl",
            ),
            "opencode-dcda-chip-rtl-v1",
        )
        self.assertEqual(
            adapter_profile(
                "opencode",
                opencode_harness=True,
                opencode_primary_agent="chip-rtl",
                opencode_thinking=False,
            ),
            "opencode-dcda-chip-rtl-no-thinking-v1",
        )
        self.assertEqual(adapter_profile("pi"), "pi-standard-v3")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown agent backend"):
            build_agent_command("unknown", "qwen3.6-coder", "task")


class GenerationWorkspaceTests(unittest.TestCase):
    def test_spec_to_rtl_exposes_prompt_but_no_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            prompt.write_text("Implement TopModule with output zero.")
            (root / "Prob001_zero_ref.sv").write_text("secret reference")
            (root / "Prob001_zero_test.sv").write_text("secret test")

            prepared = prepare_generation_workspace(
                prompt_path=prompt,
                task="spec-to-rtl",
                problem="Prob001_zero",
                workspace=root / "workspace",
            )

            self.assertEqual((prepared.workspace / "TASK.md").read_text(), prompt.read_text())
            self.assertFalse((prepared.workspace / "TopModule.sv").exists())
            self.assertFalse(any(prepared.workspace.rglob("*_ref.sv")))
            self.assertFalse(any(prepared.workspace.rglob("*_test.sv")))
            self.assertIn(prompt.read_text(), prepared.agent_prompt)
            self.assertIn("TopModule.sv", prepared.agent_prompt)
            self.assertNotIn("tool_call", prepared.agent_prompt)

    def test_code_complete_starts_from_public_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            interface = root / "Prob001_zero_ifc.txt"
            prompt.write_text("Complete this module.")
            interface.write_text("module TopModule(output zero);\n")

            prepared = prepare_generation_workspace(
                prompt_path=prompt,
                task="code-complete-iccad2023",
                problem="Prob001_zero",
                workspace=root / "workspace",
            )

            self.assertEqual(
                (prepared.workspace / "TopModule.sv").read_text(),
                interface.read_text(),
            )
            self.assertIsNotNone(prepared.starter_digest)

    def test_missing_or_unchanged_candidate_becomes_explicit_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            interface = root / "Prob001_zero_ifc.txt"
            prompt.write_text("Complete this module.")
            interface.write_text("module TopModule(output zero);\n")
            prepared = prepare_generation_workspace(
                prompt_path=prompt,
                task="code-complete-iccad2023",
                problem="Prob001_zero",
                workspace=root / "workspace",
            )
            output = root / "Prob001_zero_sample01.sv"

            publication = publish_candidate(
                prepared=prepared,
                output_path=output,
                agent_status="completed",
            )

            self.assertFalse(publication.submitted)
            self.assertEqual(publication.status, "missing_submission")
            self.assertIn("AGENT_EVAL_NO_SUBMISSION", output.read_text())

    def test_modified_candidate_is_published_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            prompt.write_text("Implement TopModule.")
            prepared = prepare_generation_workspace(
                prompt_path=prompt,
                task="spec-to-rtl",
                problem="Prob001_zero",
                workspace=root / "workspace",
            )
            candidate = prepared.workspace / "TopModule.sv"
            candidate.write_text("module TopModule; endmodule\n")
            output = root / "Prob001_zero_sample01.sv"

            publication = publish_candidate(prepared, output, "timeout")

            self.assertTrue(publication.submitted)
            self.assertEqual(publication.status, "timeout")
            self.assertEqual(output.read_text(), candidate.read_text())


class GeneratorCliTests(unittest.TestCase):
    def test_generator_publishes_candidate_log_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            artifacts = root / "artifacts"
            prompt.write_text("Implement TopModule with output zero.")

            def fake_execute(_args, prepared, artifact):
                (prepared.workspace / "TopModule.sv").write_text(
                    "module TopModule(output zero); assign zero=0; endmodule\n"
                )
                npm_cache = prepared.workspace / ".cache/npm"
                npm_cache.mkdir(parents=True)
                (npm_cache / "duplicated-runtime-cache").write_bytes(b"cache")
                (artifact / "trajectory.jsonl").write_text('{"type":"step_finish"}\n')
                (artifact / "stderr.log").write_text("")
                return (
                    "completed",
                    0,
                    1.5,
                    TrajectoryMetrics(
                        turns=2,
                        tool_calls=1,
                        input_tokens=100,
                        output_tokens=20,
                    ),
                )

            argv = [
                "generate.py",
                "--agent=opencode",
                "--model=qwen3.6-coder",
                "--task=spec-to-rtl",
                "--timeout=180",
                "--max-tokens=8192",
                "--temperature=0.6",
                "--top-p=0.95",
                "--toolchain=base",
                "--opencode-primary-agent=benchmark",
                "--opencode-thinking=on",
                "--sandbox-backend=docker",
                f"--artifact-root={artifacts}",
                f"--output={output}",
                str(prompt),
            ]
            stdout = io.StringIO()
            with patch.object(sys, "argv", argv), patch(
                "agent_eval.generate.execute_agent", side_effect=fake_execute
            ), redirect_stdout(stdout):
                returncode = main()

            self.assertEqual(returncode, 0)
            self.assertIn("module TopModule", output.read_text())
            self.assertIn("agent_status = completed", stdout.getvalue())
            self.assertIn("prompt_tokens = 100", stdout.getvalue())
            sidecar = (
                artifacts
                / "opencode/Prob001_zero/sample01/agent.json"
            )
            self.assertTrue(sidecar.is_file())
            self.assertTrue((sidecar.parent / "trajectory.jsonl").is_file())
            self.assertFalse((sidecar.parent / "workspace").exists())
            record = json.loads(sidecar.read_text())
            self.assertIsNone(record["workspace"])
            self.assertEqual(record["workspace_policy"], "ephemeral-v1")


class MakeIntegrationContractTests(unittest.TestCase):
    def test_agent_replaces_only_make_generator(self):
        configure, make = build_evaluation_commands(
            repo_root=Path("/repo"),
            build_dir=Path("/run/verilog-eval"),
            artifact_root=Path("/run/agent-artifacts"),
            agent="opencode",
            task="spec-to-rtl",
            model="qwen3.6-coder",
            problems_file=Path("/run/problems.txt"),
            jobs=48,
            timeout=180,
            max_tokens=8192,
            temperature=0.6,
            top_p=0.95,
            toolchain="minimal-rtl",
            opencode_primary_agent="chip-rtl",
            opencode_thinking=False,
            bash_path="/nix/store/bash/bin/bash",
            sandbox_backend="docker",
        )

        self.assertIn("--with-task=spec-to-rtl", configure)
        self.assertNotIn("--with-pregen", " ".join(configure))
        self.assertIn("GENERATE_VERILOG=/repo/agent_eval/generate.py", make)
        self.assertTrue(any(item.startswith("GENERATE_FLAGS=") for item in make))
        self.assertIn("--toolchain=minimal-rtl", " ".join(make))
        self.assertIn("--opencode-primary-agent=chip-rtl", " ".join(make))
        self.assertIn("--opencode-thinking=off", " ".join(make))
        self.assertIn("sv-iv-analyze", make)
        self.assertIn("--jobs=48", make)


if __name__ == "__main__":
    unittest.main()
