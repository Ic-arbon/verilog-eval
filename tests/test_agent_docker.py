from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from agent_generation.contracts import AgentEnvironment, AgentProcessSpec
from agent_generation.docker import DockerExecutor, DockerInfrastructureError


class RecordingRunner:
    def __init__(self, returncode=0, stdout="ok\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        if len(command) > 1 and command[1] == "run":
            cidfile = Path(command[command.index("--cidfile") + 1])
            cidfile.write_text("abcdef1234567890\n")
        if tuple(command[1:3]) == ("container", "inspect"):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class TimeoutRunner:
    def __init__(self):
        self.calls = []
        self.removed_before_return = False

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        if len(command) > 1 and command[1] == "run":
            cidfile = Path(command[command.index("--cidfile") + 1])
            cidfile.write_text("abcdef1234567890\n")
            raise subprocess.TimeoutExpired(
                command,
                kwargs["timeout"],
                output="partial trajectory\n",
                stderr="deadline\n",
            )
        self.removed_before_return = tuple(command[1:3]) == ("rm", "--force")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class DockerExecutorTests(unittest.TestCase):
    def make_fixture(self, root: Path, runner=None):
        workspace = root / "runtime" / "sample"
        tools = root / "agent-tools"
        workspace.mkdir(parents=True)
        tools.mkdir()
        executor = DockerExecutor(
            docker_path="/nix/store/docker/bin/docker",
            image="verilog-eval-agent:clean-v1",
            agent_tools=tools,
            uid=1000,
            gid=1000,
            runner=runner or RecordingRunner(),
            host_environment={"VLLM_API_KEY": "host-secret-not-in-argv"},
        )
        spec = AgentProcessSpec(
            command=("/agent-tools/node_modules/.bin/fake", "run"),
            workspace=workspace,
            timeout_seconds=30,
            environment=AgentEnvironment(
                variables=(
                    ("HOME", "/workspace/.home"),
                    ("OPENCODE_CONFIG", "/workspace/.agent-config/opencode.json"),
                ),
                inherit=("VLLM_API_KEY",),
            ),
        )
        return executor, spec, tools

    def test_command_has_required_isolation_and_only_expected_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor, spec, tools = self.make_fixture(root)
            cidfile = root / "container.cid"

            command = executor.build_command(spec, cidfile)
            command_text = " ".join(command)

            self.assertEqual(command[:2], ("/nix/store/docker/bin/docker", "run"))
            self.assertIn("--rm", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn("--security-opt=no-new-privileges", command)
            self.assertIn("--pids-limit=256", command)
            self.assertIn("--user=1000:1000", command)
            self.assertIn("--workdir=/workspace", command)
            self.assertIn("--network=host", command)
            self.assertIn("--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m", command)
            self.assertIn(
                f"type=bind,src={spec.workspace.resolve()},dst=/workspace",
                command,
            )
            self.assertIn(
                f"type=bind,src={tools.resolve()},dst=/agent-tools,readonly",
                command,
            )
            self.assertEqual(command.count("--mount"), 2)
            self.assertIn("HOME=/workspace/.home", command)
            self.assertIn("VLLM_API_KEY", command)
            self.assertNotIn("host-secret-not-in-argv", command_text)
            self.assertNotIn("_ref.sv", command_text)
            self.assertNotIn("_test.sv", command_text)
            self.assertNotIn("docker.sock", command_text)
            self.assertEqual(
                command[-3:],
                (
                    "verilog-eval-agent:clean-v1",
                    "/agent-tools/node_modules/.bin/fake",
                    "run",
                ),
            )

    def test_completed_and_nonzero_processes_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for returncode, expected_status in ((0, "completed"), (7, "error")):
                with self.subTest(returncode=returncode):
                    runner = RecordingRunner(
                        returncode=returncode,
                        stdout="trajectory\n",
                        stderr="diagnostic\n",
                    )
                    executor, spec, _tools = self.make_fixture(
                        root / str(returncode), runner=runner
                    )

                    result = executor.run(spec)

                    self.assertEqual(result.status, expected_status)
                    self.assertEqual(result.exit_code, returncode)
                    self.assertEqual(result.stdout, "trajectory\n")
                    self.assertEqual(result.stderr, "diagnostic\n")
                    self.assertIsNone(result.usage.input_tokens)
                    self.assertEqual(len(runner.calls), 2)
                    self.assertEqual(runner.calls[1][0][1:3], ("container", "inspect"))
                    self.assertFalse(
                        Path(
                            runner.calls[0][0][runner.calls[0][0].index("--cidfile") + 1]
                        ).exists()
                    )

    def test_buffered_execution_normalizes_trajectory_before_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = RecordingRunner(stdout="large cumulative event\ncomplete event\n")
            executor, spec, _tools = self.make_fixture(Path(tmp), runner=runner)
            spec = replace(
                spec,
                trajectory_normalizer=lambda line: (
                    "compact event\n" if "cumulative" in line else line
                ),
            )

            result = executor.run(spec)

            self.assertEqual(result.stdout, "compact event\ncomplete event\n")

    def test_timeout_force_removes_container_before_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = TimeoutRunner()
            executor, spec, _tools = self.make_fixture(Path(tmp), runner=runner)

            result = executor.run(spec)

            self.assertEqual(result.status, "timeout")
            self.assertEqual(result.exit_code, 124)
            self.assertEqual(result.stdout, "partial trajectory\n")
            self.assertEqual(result.stderr, "deadline\n")
            self.assertTrue(runner.removed_before_return)
            self.assertEqual(runner.calls[1][0][1:4], ("rm", "--force", "abcdef1234567890"))

    def test_host_terminates_process_when_streamed_turn_budget_is_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            tools = root / "tools"
            workspace.mkdir()
            tools.mkdir()
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = rm ]; then exit 0; fi\n"
                "if [ \"$1\" = container ]; then echo 'not found' >&2; exit 1; fi\n"
                "previous=\n"
                "for argument in \"$@\"; do\n"
                "  if [ \"$previous\" = --cidfile ]; then printf 'abcdef1234567890\\n' >\"$argument\"; fi\n"
                "  previous=\"$argument\"\n"
                "done\n"
                "printf '{\"type\":\"turn_end\"}\\n'\n"
                "printf '{\"type\":\"turn_end\"}\\n'\n"
                "sleep 30\n"
            )
            fake_docker.chmod(0o755)
            executor = DockerExecutor(
                docker_path=str(fake_docker),
                image="image:tag",
                agent_tools=tools,
                uid=1000,
                gid=1000,
                host_environment={},
            )
            spec = AgentProcessSpec(
                command=("fake-agent",),
                workspace=workspace,
                timeout_seconds=20,
                max_turns=2,
                max_tool_calls=5,
                event_classifier=lambda line: (
                    "turn" if '"type":"turn_end"' in line else None
                ),
                trajectory_normalizer=lambda _line: '{"type":"compact"}\n',
            )

            started = time.monotonic()
            result = executor.run(spec)

            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.exit_code, 86)
            self.assertEqual(result.termination_reason, "max_turns")
            self.assertEqual(result.stdout, '{"type":"compact"}\n' * 2)

    def test_invalid_budget_classifier_result_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            tools = root / "tools"
            workspace.mkdir()
            tools.mkdir()
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = rm ]; then exit 0; fi\n"
                "previous=\n"
                "for argument in \"$@\"; do\n"
                "  if [ \"$previous\" = --cidfile ]; then printf 'abcdef1234567890\\n' >\"$argument\"; fi\n"
                "  previous=\"$argument\"\n"
                "done\n"
                "printf '{\"type\":\"unexpected\"}\\n'\n"
                "sleep 30\n"
            )
            fake_docker.chmod(0o755)
            executor = DockerExecutor(
                docker_path=str(fake_docker),
                image="image:tag",
                agent_tools=tools,
                uid=1000,
                gid=1000,
                host_environment={},
            )
            spec = AgentProcessSpec(
                command=("fake-agent",),
                workspace=workspace,
                timeout_seconds=20,
                max_turns=2,
                max_tool_calls=2,
                event_classifier=lambda _line: "invalid-kind",
            )

            with self.assertRaises(DockerInfrastructureError):
                executor.run(spec)

    def test_docker_control_plane_exit_is_infrastructure_not_sample_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = RecordingRunner(returncode=125, stderr="daemon failure")
            executor, spec, _tools = self.make_fixture(Path(tmp), runner=runner)

            with self.assertRaises(DockerInfrastructureError):
                executor.run(spec)

    def test_missing_inherited_secret_fails_before_docker_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = RecordingRunner()
            executor, spec, tools = self.make_fixture(Path(tmp), runner=runner)
            executor = DockerExecutor(
                docker_path="docker",
                image="image:tag",
                agent_tools=tools,
                uid=1000,
                gid=1000,
                runner=runner,
                host_environment={},
            )

            with self.assertRaises(DockerInfrastructureError):
                executor.run(spec)

            self.assertEqual(runner.calls, [])

    def test_root_host_workspace_is_transferred_to_non_root_container_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingRunner()
            _executor, spec, tools = self.make_fixture(root, runner=runner)
            config_file = spec.workspace / ".agent-config" / "config.json"
            config_file.parent.mkdir()
            config_file.write_text("{}\n")
            ownership_changes = []

            def change_owner(path, uid, gid, *, follow_symlinks):
                ownership_changes.append((Path(path), uid, gid, follow_symlinks))

            executor = DockerExecutor(
                docker_path="docker",
                image="image:tag",
                agent_tools=tools,
                uid=0,
                gid=0,
                runner=runner,
                host_environment={"VLLM_API_KEY": "secret"},
                ownership_changer=change_owner,
            )

            result = executor.run(spec)

            self.assertEqual(result.status, "completed")
            changed_paths = {item[0] for item in ownership_changes}
            self.assertIn(spec.workspace, changed_paths)
            self.assertIn(config_file, changed_paths)
            self.assertTrue(
                all(
                    uid == 65534 and gid == 65534 and follow_symlinks is False
                    for _path, uid, gid, follow_symlinks in ownership_changes
                )
            )
            command = runner.calls[0][0]
            self.assertIn("--user=65534:65534", command)


if __name__ == "__main__":
    unittest.main()
