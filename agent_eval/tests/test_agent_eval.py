import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_eval.adapters import create_adapter
from agent_eval.canonical import (
    canonical_commands,
    parse_verilog_eval_summary,
    run_canonical_evaluation,
    stage_agent_result,
)
from agent_eval.config import write_agent_configs
from agent_eval.metrics import parse_trajectory
from agent_eval.models import AgentRequest, AgentResult, TrajectoryMetrics
from agent_eval.runner import load_problems, parse_args, sandbox_environment
from agent_eval.sandbox import (
    build_docker_command,
    build_sandbox_command,
    select_sandbox_backend,
)
from agent_eval.workspace import prepare_workspace


class CliTests(unittest.TestCase):
    def test_configure_style_options_map_to_agent_settings(self):
        argv = [
            "agent-eval",
            "--with-task=code-complete-iccad2023",
            "--with-model=qwen3.6-coder",
            "--with-samples=1",
            "--with-max-tokens=4096",
            "--with-temperature=0.2",
            "--with-top-p=0.8",
            "--with-problems=/tmp/problems.txt",
        ]
        with patch.object(sys, "argv", argv):
            args = parse_args()

        self.assertEqual(args.task, "code-complete-iccad2023")
        self.assertEqual(args.model, "qwen3.6-coder")
        self.assertEqual(args.samples, 1)
        self.assertEqual(args.max_tokens, 4096)
        self.assertEqual(args.temperature, 0.2)
        self.assertEqual(args.top_p, 0.8)
        self.assertEqual(args.problems_file, Path("/tmp/problems.txt"))

    def test_with_problems_reads_the_same_problem_file_as_configure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            problems_file = root / "problems.txt"
            problems_file.write_text("Prob001_zero\nProb002_m2014_q4i\n")

            problems = load_problems(
                repo_root=root,
                task="spec-to-rtl",
                requested=[],
                problems_file=problems_file,
            )

        self.assertEqual(problems, ["Prob001_zero", "Prob002_m2014_q4i"])


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
        self.assertIn("Do not narrate", command[-1])
        self.assertIn("<tool_call>", command[-1])
        self.assertIn("<function=read>", command[-1])

    def test_unknown_adapter_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown agent"):
            create_adapter("unknown")


class ConfigTests(unittest.TestCase):
    def test_configs_point_both_agents_at_the_same_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_agent_configs(
                workspace,
                base_url="http://127.0.0.1:58000/v1",
                model="qwen3.6-coder",
            )

            pi_config = json.loads((workspace / ".pi-agent/models.json").read_text())
            opencode_config = json.loads((workspace / "opencode.json").read_text())

            self.assertEqual(
                pi_config["providers"]["vllm-local"]["models"][0]["id"],
                "qwen3.6-coder",
            )
            self.assertIn(
                "qwen3.6-coder",
                opencode_config["provider"]["vllm-local"]["models"],
            )
            self.assertTrue(
                opencode_config["provider"]["vllm-local"]["models"]
                ["qwen3.6-coder"]["tool_call"]
            )
            self.assertEqual(opencode_config["agent"]["build"]["temperature"], 0.6)
            self.assertEqual(opencode_config["agent"]["build"]["top_p"], 0.95)


class SandboxTests(unittest.TestCase):
    def test_auto_backend_falls_back_to_docker(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                1 if "bwrap" in command[0] else 0,
                "",
                "uid map denied" if "bwrap" in command[0] else "",
            )

        backend = select_sandbox_backend(
            requested="auto",
            bwrap_path="/nix/store/bwrap/bin/bwrap",
            docker_path="docker",
            true_path="/nix/store/coreutils/bin/true",
            run=fake_run,
        )

        self.assertEqual(backend, "docker")
        self.assertEqual(len(calls), 2)

    def test_unavailable_backends_fail_before_trajectories_start(self):
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "denied")

        with self.assertRaisesRegex(RuntimeError, "No usable sandbox backend"):
            select_sandbox_backend(
                requested="auto",
                bwrap_path="bwrap",
                docker_path="docker",
                true_path="true",
                run=fake_run,
            )

    def test_agent_uses_sandbox_library_path_not_host_grader_path(self):
        with patch.dict(
            os.environ,
            {
                "LD_LIBRARY_PATH": "/host/compiler-libs",
                "AGENT_EVAL_SANDBOX_LD_LIBRARY_PATH": "/sandbox/glibc",
            },
        ):
            environment = sandbox_environment("opencode")
        self.assertEqual(environment["LD_LIBRARY_PATH"], "/sandbox/glibc")

    def test_agent_caches_stay_inside_opt_backed_workspace(self):
        environment = sandbox_environment("opencode")
        self.assertEqual(environment["HOME"], "/workspace/.home")
        self.assertEqual(environment["XDG_CACHE_HOME"], "/workspace/.cache")
        self.assertEqual(environment["npm_config_cache"], "/workspace/.cache/npm")

    def test_docker_command_is_read_only_and_drops_privileges(self):
        command = build_docker_command(
            workspace=Path("/run/workspace"),
            agent_tools=Path("/run/agent-tools"),
            agent_command=["/agent-tools/node_modules/.bin/opencode", "run"],
            image="verilog-eval-agent-sandbox:1",
            sandbox_path="/agent-tools/node_modules/.bin:/nix/store/tools/bin",
            environment={"HOME": "/home/agent"},
            docker_path="docker",
            uid=1000,
            gid=1000,
        )

        joined = " ".join(command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("--security-opt no-new-privileges", joined)
        self.assertIn("--network host", joined)
        self.assertIn("/run/workspace:/workspace:rw", joined)
        self.assertIn("/run/agent-tools:/agent-tools:ro", joined)
        self.assertIn("--user 1000:1000", joined)
        self.assertEqual(command[-3:], ["verilog-eval-agent-sandbox:1", "/agent-tools/node_modules/.bin/opencode", "run"])

    def test_bwrap_mounts_only_selected_store_paths(self):
        command = build_sandbox_command(
            workspace=Path("/run/workspace"),
            agent_tools=Path("/run/agent-tools"),
            agent_command=["/agent-tools/node_modules/.bin/pi", "--mode", "json"],
            store_paths=[Path("/nix/store/aaa-node"), Path("/nix/store/bbb-bash")],
            sandbox_path="/agent-tools/node_modules/.bin:/nix/store/aaa-node/bin",
            bash_path="/nix/store/bbb-bash/bin/bash",
            env_path="/nix/store/ccc-coreutils/bin/env",
            environment={"PI_OFFLINE": "1"},
        )

        joined = " ".join(map(str, command))
        self.assertNotIn("--ro-bind /nix/store /nix/store", joined)
        self.assertIn("--ro-bind /nix/store/aaa-node /nix/store/aaa-node", joined)
        self.assertIn("--bind /run/workspace /workspace", joined)
        self.assertEqual(
            command[-3:],
            ["/agent-tools/node_modules/.bin/pi", "--mode", "json"],
        )


class CanonicalEvaluationTests(unittest.TestCase):
    def test_agent_result_is_staged_as_original_pregen_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "TopModule.sv"
            candidate.write_text("module TopModule(output zero); assign zero=0; endmodule")
            result = AgentResult(
                agent="opencode",
                status="completed",
                exit_code=0,
                final_sv=candidate,
                trajectory=root / "trajectory.jsonl",
                stderr_log=root / "stderr.log",
                duration_seconds=1.0,
                metrics=TrajectoryMetrics(
                    turns=2,
                    tool_calls=3,
                    input_tokens=120,
                    output_tokens=30,
                ),
            )

            sample, generate_log = stage_agent_result(
                pregen_root=root / "pregen",
                problem="Prob001_zero",
                result=result,
            )

            self.assertEqual(
                sample.name,
                "Prob001_zero_sample01.sv",
            )
            self.assertEqual(sample.read_text(), candidate.read_text())
            self.assertIn("prompt_tokens = 120", generate_log.read_text())
            self.assertIn("resp_tokens = 30", generate_log.read_text())
            self.assertIn("agent_status = completed", generate_log.read_text())

    def test_missing_submission_stages_compile_failure_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = AgentResult(
                agent="opencode",
                status="missing_submission",
                exit_code=0,
                final_sv=None,
                trajectory=root / "trajectory.jsonl",
                stderr_log=root / "stderr.log",
                duration_seconds=1.0,
                metrics=TrajectoryMetrics(),
            )

            sample, _ = stage_agent_result(
                pregen_root=root / "pregen",
                problem="Prob001_zero",
                result=result,
            )

            self.assertIn("AGENT_EVAL_NO_SUBMISSION", sample.read_text())

    def test_original_analyzer_handles_python3_sample_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            problem_dir = root / "Prob001_zero"
            problem_dir.mkdir()
            (problem_dir / "Prob001_zero_sample01-sv-generate.log").write_text(
                "prompt_tokens = 10\nresp_tokens = 5\ncost = 0.0\n"
            )
            (problem_dir / "Prob001_zero_sample01-sv-iv-test.log").write_text(
                "Mismatches: 0 in 20 samples\n"
            )
            (problem_dir / "Prob001_zero_sample01.sv").write_text(
                "module TopModule; endmodule\n"
            )
            repo_root = Path(__file__).resolve().parents[2]

            completed = subprocess.run(
                [
                    sys.executable,
                    str(repo_root / "scripts/sv-iv-analyze"),
                    "--csv=summary.csv",
                    "Prob001_zero",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                (root / "summary.csv").read_text(),
                "Prob001_zero,1,1,1.0,.\n",
            )

    def test_original_summary_symbols_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "summary.csv"
            summary.write_text(
                "Prob001_zero,1,1,1.0,.\n"
                "Prob002_m2014_q4i,0,1,0.0,m\n"
            )

            results = parse_verilog_eval_summary(summary, root)

            self.assertTrue(results["Prob001_zero"].passed)
            self.assertEqual(results["Prob001_zero"].symbol, ".")
            self.assertFalse(results["Prob002_m2014_q4i"].passed)
            self.assertEqual(results["Prob002_m2014_q4i"].symbol, "m")

    def test_canonical_runner_publishes_original_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "TopModule.sv"
            candidate.write_text("module TopModule(output zero); endmodule")
            result = AgentResult(
                agent="opencode",
                status="completed",
                exit_code=0,
                final_sv=candidate,
                trajectory=root / "trajectory.jsonl",
                stderr_log=root / "stderr.log",
                duration_seconds=1.0,
                metrics=TrajectoryMetrics(),
            )
            calls = []

            def fake_run(command, cwd, **_kwargs):
                calls.append(command)
                if "sv-iv-analyze" in command:
                    (cwd / "summary.csv").write_text(
                        "Prob001_zero,1,1,1.0,.\n"
                    )
                    (cwd / "summary.txt").write_text("pass_rate = 100.00\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            grades, canonical_root = run_canonical_evaluation(
                repo_root=Path("/repo"),
                agent_root=root / "opencode",
                task="spec-to-rtl",
                problems=["Prob001_zero"],
                agent_results={"Prob001_zero": result},
                jobs=1,
                bash_path="/nix/store/bash/bin/bash",
                run=fake_run,
            )

            self.assertTrue(grades["Prob001_zero"].passed)
            self.assertTrue((canonical_root / "summary.csv").is_file())
            self.assertTrue((canonical_root / "summary.txt").is_file())
            self.assertEqual(len(calls), 2)

    def test_canonical_commands_use_pregen_and_original_analyzer(self):
        configure, make = canonical_commands(
            repo_root=Path("/repo"),
            build_dir=Path("/run/build"),
            pregen_root=Path("/run/pregen"),
            problems_file=Path("/run/problems.txt"),
            task="spec-to-rtl",
            jobs=2,
            bash_path="/nix/store/bash/bin/bash",
        )

        self.assertIn("--with-pregen=/run/pregen", configure)
        self.assertIn("--with-problems=/run/problems.txt", configure)
        self.assertIn("sv-iv-analyze", make)
        self.assertIn("--jobs=2", make)


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

    def test_opencode_nested_step_tokens_are_counted(self):
        lines = [
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"tokens": {"input": 7430, "output": 109}},
                }
            )
        ]
        metrics = parse_trajectory("opencode", lines)
        self.assertEqual(metrics.turns, 1)
        self.assertEqual(metrics.input_tokens, 7430)
        self.assertEqual(metrics.output_tokens, 109)

    def test_invalid_json_lines_are_preserved_as_parse_errors(self):
        metrics = parse_trajectory("opencode", ["not-json"])
        self.assertEqual(metrics.parse_errors, 1)


if __name__ == "__main__":
    unittest.main()
