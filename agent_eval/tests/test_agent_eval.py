import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_eval.adapters import create_adapter
from agent_eval.config import write_agent_configs
from agent_eval.grader import grade_submission
from agent_eval.metrics import parse_trajectory
from agent_eval.models import AgentRequest
from agent_eval.runner import sandbox_environment
from agent_eval.sandbox import (
    build_docker_command,
    build_sandbox_command,
    select_sandbox_backend,
)
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
        self.assertIn("Do not narrate", command[-1])

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
            self.assertEqual(opencode_config["agent"]["build"]["temperature"], 0)
            self.assertEqual(opencode_config["agent"]["build"]["top_p"], 0.01)


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


class GraderTests(unittest.TestCase):
    def test_passing_simulation_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("TopModule.sv", "ref.sv", "test.sv"):
                (root / name).write_text("module placeholder; endmodule")

            grader_environments = []

            def fake_run(command, **kwargs):
                grader_environments.append(kwargs["env"])
                if command[0] == "iverilog":
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(
                    command, 0, "Mismatches: 0 in 10 samples\n", ""
                )

            with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/poisoned/glibc"}):
                result = grade_submission(
                    candidate=root / "TopModule.sv",
                    reference=root / "ref.sv",
                    testbench=root / "test.sv",
                    output_dir=root / "grade",
                    run=fake_run,
                )

            self.assertTrue(result.passed)
            self.assertEqual(result.status, "passed")
            self.assertTrue(grader_environments)
            self.assertTrue(
                all("LD_LIBRARY_PATH" not in env for env in grader_environments)
            )

    def test_missing_submission_is_reported_without_running_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = grade_submission(
                candidate=Path(tmp) / "TopModule.sv",
                reference=Path(tmp) / "ref.sv",
                testbench=Path(tmp) / "test.sv",
                output_dir=Path(tmp),
            )
            self.assertEqual(result.status, "missing_submission")
            self.assertFalse(result.passed)


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
