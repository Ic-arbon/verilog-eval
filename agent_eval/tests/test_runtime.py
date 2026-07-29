import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_eval.config import write_agent_configs
from agent_eval.generate import run_agent_process
from agent_eval.metrics import parse_trajectory
from agent_eval.sandbox import (
    build_docker_command,
    build_sandbox_command,
    select_sandbox_backend,
)


class ConfigTests(unittest.TestCase):
    def test_pi_workspace_contains_only_pi_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_agent_configs(
                workspace,
                agent="pi",
                base_url="http://127.0.0.1:58000/v1",
                model="qwen3.6-coder",
                max_tokens=4096,
                temperature=0.2,
                top_p=0.8,
            )

            pi = json.loads((workspace / ".pi-agent/models.json").read_text())
            self.assertEqual(
                pi["providers"]["vllm-local"]["models"][0]["maxTokens"], 4096
            )
            self.assertFalse((workspace / "opencode.json").exists())

    def test_opencode_workspace_contains_only_opencode_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_agent_configs(
                workspace,
                agent="opencode",
                base_url="http://127.0.0.1:58000/v1",
                model="qwen3.6-coder",
                max_tokens=4096,
                temperature=0.2,
                top_p=0.8,
            )

            self.assertFalse((workspace / ".pi-agent").exists())
            opencode = json.loads((workspace / "opencode.json").read_text())
            benchmark = opencode["agent"]["benchmark"]
            model = opencode["provider"]["vllm-local"]["models"]["qwen3.6-coder"]

            self.assertEqual(benchmark["temperature"], 0.2)
            self.assertEqual(benchmark["top_p"], 0.8)
            self.assertEqual(benchmark["mode"], "primary")
            self.assertEqual(benchmark["permission"]["edit"], "allow")
            self.assertEqual(benchmark["permission"]["task"], "deny")
            self.assertIn("Workspace files are the deliverables", benchmark["prompt"])
            self.assertNotIn("tool_call", benchmark["prompt"])
            self.assertTrue(model["temperature"])
            self.assertTrue(model["reasoning"])
            self.assertTrue(model["tool_call"])
            self.assertEqual(model["interleaved"], {"field": "reasoning_content"})


class MetricsTests(unittest.TestCase):
    def test_pi_metrics_count_turns_tools_and_tokens(self):
        lines = [
            json.dumps({"type": "turn_end"}),
            json.dumps({"type": "tool_execution_start"}),
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

    def test_opencode_metrics_read_nested_step_tokens(self):
        lines = [
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"tokens": {"input": 80, "output": 20}},
                }
            ),
            json.dumps({"type": "tool_use"}),
        ]
        metrics = parse_trajectory("opencode", lines)
        self.assertEqual(metrics.turns, 1)
        self.assertEqual(metrics.tool_calls, 1)
        self.assertEqual(metrics.input_tokens, 80)
        self.assertEqual(metrics.output_tokens, 20)


class SandboxTests(unittest.TestCase):
    def test_docker_is_read_only_and_drops_privileges(self):
        command = build_docker_command(
            workspace=Path("/run/workspace"),
            agent_tools=Path("/run/tools"),
            agent_command=["pi"],
            image="agent-sandbox:1",
            sandbox_path="/agent-tools/bin:/usr/bin",
            environment={},
            cidfile=Path("/run/artifacts/container.cid"),
        )
        self.assertIn("--read-only", command)
        self.assertEqual(
            command[command.index("--cidfile") + 1],
            "/run/artifacts/container.cid",
        )
        self.assertIn("ALL", command)
        self.assertIn("no-new-privileges", command)
        self.assertNotIn("/var/run/docker.sock", " ".join(command))

    def test_bwrap_mounts_workspace_but_not_repository(self):
        command = build_sandbox_command(
            workspace=Path("/run/workspace"),
            agent_tools=Path("/run/tools"),
            agent_command=["pi"],
            store_paths=[Path("/nix/store/tool")],
            sandbox_path="/nix/store/tool/bin",
            bash_path="/nix/store/bash/bin/bash",
            env_path="/nix/store/coreutils/bin/env",
            environment={},
        )
        rendered = " ".join(command)
        self.assertIn("/run/workspace /workspace", rendered)
        self.assertNotIn("dataset_spec-to-rtl", rendered)
        self.assertNotIn("_ref.sv", rendered)

    def test_timeout_forces_docker_container_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cidfile = Path(tmp) / "container.cid"
            cidfile.write_text("abc123\n")

            def time_out(_command, **_kwargs):
                raise subprocess.TimeoutExpired(["docker", "run"], 10)

            cleanup_calls = []

            result = run_agent_process(
                command=["docker", "run"],
                timeout=10,
                cidfile=cidfile,
                docker_path="docker",
                run=time_out,
                cleanup=lambda path, docker: cleanup_calls.append((path, docker)),
            )

            self.assertEqual(result.status, "timeout")
            self.assertEqual(cleanup_calls, [(cidfile, "docker")])

    def test_auto_backend_falls_back_to_docker(self):
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                1 if "bwrap" in command[0] else 0,
                "",
                "uid map denied" if "bwrap" in command[0] else "",
            )

        backend = select_sandbox_backend(
            requested="auto",
            bwrap_path="bwrap",
            docker_path="docker",
            true_path="true",
            run=fake_run,
        )
        self.assertEqual(backend, "docker")


if __name__ == "__main__":
    unittest.main()
