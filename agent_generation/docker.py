"""Hardened one-container executor for formal Agent evaluation samples."""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional

from agent_generation.contracts import (
    BUDGET_EVENT_KINDS,
    AgentProcessSpec,
    AgentUsage,
    ProcessResult,
)
from agent_generation.result_contract import HOST_BUDGET_EXIT_CODES


CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}")
CONTAINER_NAME_PATTERN = re.compile(r"verilog-eval-[0-9a-f]{32}")


class DockerInfrastructureError(RuntimeError):
    """The formal sandbox could not be started or safely terminated."""


def _decode_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _normalize_trajectory_line(
    line: str,
    normalizer: Optional[Callable[[str], str]],
) -> str:
    if normalizer is None:
        return line
    normalized = normalizer(line)
    if not isinstance(normalized, str):
        raise DockerInfrastructureError(
            "Agent trajectory normalizer must return text"
        )
    return normalized


def _normalize_trajectory(
    value: object,
    normalizer: Optional[Callable[[str], str]],
) -> str:
    trajectory = _decode_output(value)
    return "".join(
        _normalize_trajectory_line(line, normalizer)
        for line in trajectory.splitlines(keepends=True)
    )


def _mount_source(path: Path, name: str) -> str:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise DockerInfrastructureError(f"{name} directory does not exist: {resolved}")
    if any(character in str(resolved) for character in (",", "\n", "\x00")):
        raise DockerInfrastructureError(f"{name} path cannot be represented safely")
    return str(resolved)


class DockerExecutor:
    """Run one external Agent with no repository or dataset mount."""

    def __init__(
        self,
        *,
        docker_path: str,
        image: str,
        agent_tools: Path,
        uid: int,
        gid: int,
        runner: Callable = subprocess.run,
        host_environment: Optional[Mapping[str, str]] = None,
        ownership_changer: Callable = os.chown,
    ) -> None:
        if not docker_path or "\x00" in docker_path:
            raise ValueError("docker_path must not be empty")
        if not image or any(character.isspace() for character in image):
            raise ValueError("image must be a non-empty Docker reference")
        if uid < 0 or gid < 0:
            raise ValueError("uid and gid must not be negative")

        self.docker_path = docker_path
        self.image = image
        self.agent_tools = agent_tools
        self.host_uid = uid
        self.host_gid = gid
        self.uid = 65534 if uid == 0 else uid
        self.gid = 65534 if gid == 0 else gid
        self.runner = runner
        self.ownership_changer = ownership_changer
        self.host_environment = dict(
            os.environ if host_environment is None else host_environment
        )

    def _prepare_workspace_ownership(self, workspace: Path) -> None:
        if (self.host_uid, self.host_gid) == (self.uid, self.gid):
            return
        if self.host_uid != 0:
            raise DockerInfrastructureError(
                "only a root host process can remap workspace ownership"
            )

        paths = [workspace]
        for current_root, directory_names, file_names in os.walk(
            workspace, followlinks=False
        ):
            current = Path(current_root)
            paths.extend(current / name for name in directory_names)
            paths.extend(current / name for name in file_names)
        try:
            for path in paths:
                self.ownership_changer(
                    path,
                    self.uid,
                    self.gid,
                    follow_symlinks=False,
                )
        except OSError as error:
            raise DockerInfrastructureError(
                f"failed to transfer workspace ownership: {error}"
            ) from error

    def _validate_inherited_environment(self, spec: AgentProcessSpec) -> None:
        missing = [
            name
            for name in spec.environment.inherit
            if not self.host_environment.get(name)
        ]
        if missing:
            names = ", ".join(sorted(missing))
            raise DockerInfrastructureError(
                f"required inherited environment is missing: {names}"
            )

    def build_command(
        self,
        spec: AgentProcessSpec,
        cidfile: Path,
        *,
        container_name: Optional[str] = None,
    ) -> tuple[str, ...]:
        """Build an argv-only Docker command with an explicit mount allowlist."""

        self._validate_inherited_environment(spec)
        workspace = _mount_source(spec.workspace, "workspace")
        agent_tools = _mount_source(self.agent_tools, "agent_tools")
        name = container_name or f"verilog-eval-{uuid.uuid4().hex}"
        if CONTAINER_NAME_PATTERN.fullmatch(name) is None:
            raise DockerInfrastructureError("invalid generated container name")

        command = [
            self.docker_path,
            "run",
            "--rm",
            "--init",
            "--cidfile",
            str(cidfile),
            f"--name={name}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=4g",
            f"--user={self.uid}:{self.gid}",
            "--workdir=/workspace",
            "--network=host",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={agent_tools},dst=/agent-tools,readonly",
        ]
        for name, value in spec.environment.variables:
            command.extend(("--env", f"{name}={value}"))
        for name in spec.environment.inherit:
            command.extend(("--env", name))
        command.append(self.image)
        command.extend(spec.command)
        return tuple(command)

    def _container_identifier(self, cidfile: Path) -> str:
        try:
            container_id = cidfile.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise DockerInfrastructureError(
                "Docker did not confirm container launch"
            ) from error
        if CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
            raise DockerInfrastructureError("Docker returned an invalid container ID")
        return container_id

    def _confirm_removed(self, identifier: str) -> None:
        try:
            completed = self.runner(
                (self.docker_path, "container", "inspect", identifier),
                capture_output=True,
                text=True,
                timeout=30,
                env=self.host_environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DockerInfrastructureError(
                f"cannot confirm container cleanup: {error}"
            ) from error
        message = _decode_output(completed.stderr).lower()
        if completed.returncode == 0 or not (
            "no such" in message or "not found" in message
        ):
            raise DockerInfrastructureError("container cleanup was not confirmed")

    def _stop_and_remove(self, identifier: str) -> None:
        """Give the Agent a bounded TERM window before forced cleanup."""

        try:
            completed = self.runner(
                (self.docker_path, "stop", "--time=3", identifier),
                capture_output=True,
                text=True,
                timeout=10,
                env=self.host_environment,
            )
        except (OSError, subprocess.SubprocessError):
            self._force_remove(identifier)
            return
        message = _decode_output(completed.stderr).lower()
        if completed.returncode != 0 and not (
            "no such" in message or "not found" in message
        ):
            self._force_remove(identifier)
            return
        try:
            self._confirm_removed(identifier)
        except DockerInfrastructureError:
            self._force_remove(identifier)

    def _force_remove(self, identifier: str) -> None:
        try:
            completed = self.runner(
                (self.docker_path, "rm", "--force", identifier),
                capture_output=True,
                text=True,
                timeout=30,
                env=self.host_environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DockerInfrastructureError(
                f"failed to remove terminated container: {error}"
            ) from error
        if completed.returncode != 0:
            message = _decode_output(completed.stderr).strip()
            raise DockerInfrastructureError(
                f"failed to remove terminated container: {message or completed.returncode}"
            )

    @staticmethod
    def _collect_stream(label: str, stream, events: queue.Queue) -> None:
        try:
            for line in iter(stream.readline, ""):
                events.put((label, line))
        finally:
            events.put((label, None))
            stream.close()

    @staticmethod
    def _stop_client_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)

    @staticmethod
    def _drain_events(
        events: queue.Queue,
        stdout_lines: list[str],
        stderr_lines: list[str],
        *,
        include_stdout: bool,
        trajectory_normalizer: Optional[Callable[[str], str]],
    ) -> None:
        while True:
            try:
                label, line = events.get_nowait()
            except queue.Empty:
                return
            if line is None:
                continue
            if label == "stdout":
                if include_stdout:
                    stdout_lines.append(
                        _normalize_trajectory_line(line, trajectory_normalizer)
                    )
            else:
                stderr_lines.append(line)

    def _run_streaming(
        self,
        spec: AgentProcessSpec,
        command: tuple[str, ...],
        cidfile: Path,
        container_name: str,
        started: float,
    ) -> ProcessResult:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=self.host_environment,
            )
        except OSError as error:
            raise DockerInfrastructureError(
                f"failed to execute Docker: {error}"
            ) from error
        if process.stdout is None or process.stderr is None:
            self._stop_client_process(process)
            raise DockerInfrastructureError("Docker output pipes were not created")

        events: queue.Queue = queue.Queue()
        readers = (
            threading.Thread(
                target=self._collect_stream,
                args=("stdout", process.stdout, events),
                daemon=True,
            ),
            threading.Thread(
                target=self._collect_stream,
                args=("stderr", process.stderr, events),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        ended_streams = 0
        turns = 0
        tool_calls = 0
        termination_reason: Optional[str] = None
        deadline = started + spec.timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                termination_reason = "timeout"
                break
            if process.poll() is not None and ended_streams == 2 and events.empty():
                break
            try:
                label, line = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                ended_streams += 1
                continue
            if label == "stdout":
                try:
                    stdout_lines.append(
                        _normalize_trajectory_line(line, spec.trajectory_normalizer)
                    )
                except BaseException as error:
                    identifier = self._container_identifier(cidfile)
                    try:
                        self._stop_and_remove(identifier)
                    finally:
                        self._stop_client_process(process)
                    raise DockerInfrastructureError(
                        "Agent trajectory normalization failed"
                    ) from error
                if spec.event_classifier is not None:
                    try:
                        event_kind = spec.event_classifier(line)
                    except BaseException as error:
                        identifier = self._container_identifier(cidfile)
                        try:
                            self._stop_and_remove(identifier)
                        finally:
                            self._stop_client_process(process)
                        raise DockerInfrastructureError(
                            "Agent budget event classifier failed"
                        ) from error
                    if event_kind is not None and event_kind not in BUDGET_EVENT_KINDS:
                        identifier = self._container_identifier(cidfile)
                        try:
                            self._stop_and_remove(identifier)
                        finally:
                            self._stop_client_process(process)
                        raise DockerInfrastructureError(
                            "Agent budget event classifier returned an invalid kind"
                        )
                    if event_kind == "turn":
                        turns += 1
                        if spec.max_turns is not None and turns >= spec.max_turns:
                            termination_reason = "max_turns"
                            break
                    elif event_kind == "tool":
                        tool_calls += 1
                        if (
                            spec.max_tool_calls is not None
                            and tool_calls >= spec.max_tool_calls
                        ):
                            termination_reason = "max_tool_calls"
                            break
            else:
                stderr_lines.append(line)

        if termination_reason is not None:
            identifier = self._container_identifier(cidfile)
            try:
                self._stop_and_remove(identifier)
            finally:
                self._stop_client_process(process)
        else:
            process.wait()
            identifier = self._container_identifier(cidfile)
            if process.returncode in {125, 126, 127}:
                raise DockerInfrastructureError(
                    f"Docker control plane exited {process.returncode}"
                )
            self._confirm_removed(identifier)

        for reader in readers:
            reader.join(timeout=1)
        self._drain_events(
            events,
            stdout_lines,
            stderr_lines,
            include_stdout=termination_reason in {None, "timeout"},
            trajectory_normalizer=spec.trajectory_normalizer,
        )

        if termination_reason == "timeout":
            return ProcessResult(
                status="timeout",
                exit_code=124,
                duration_seconds=time.monotonic() - started,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                usage=AgentUsage.unavailable(),
                termination_reason="timeout",
            )
        if termination_reason is not None:
            exit_code = HOST_BUDGET_EXIT_CODES[termination_reason]
            stderr_lines.append(
                f"VERILOG_EVAL_AGENT_TERMINATED: {termination_reason}\n"
            )
            return ProcessResult(
                status="error",
                exit_code=exit_code,
                duration_seconds=time.monotonic() - started,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                usage=AgentUsage.unavailable(),
                termination_reason=termination_reason,
            )
        return ProcessResult(
            status="completed" if process.returncode == 0 else "error",
            exit_code=process.returncode,
            duration_seconds=time.monotonic() - started,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
            usage=AgentUsage.unavailable(),
        )

    def _run_buffered(
        self,
        spec: AgentProcessSpec,
        command: tuple[str, ...],
        cidfile: Path,
        container_name: str,
        started: float,
    ) -> ProcessResult:
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                env=self.host_environment,
            )
        except subprocess.TimeoutExpired as error:
            identifier = self._container_identifier(cidfile)
            self._stop_and_remove(identifier)
            return ProcessResult(
                status="timeout",
                exit_code=124,
                duration_seconds=time.monotonic() - started,
                stdout=_normalize_trajectory(
                    error.output,
                    spec.trajectory_normalizer,
                ),
                stderr=_decode_output(error.stderr),
                usage=AgentUsage.unavailable(),
                termination_reason="timeout",
            )
        except OSError as error:
            raise DockerInfrastructureError(
                f"failed to execute Docker: {error}"
            ) from error

        identifier = self._container_identifier(cidfile)
        if completed.returncode in {125, 126, 127}:
            raise DockerInfrastructureError(
                f"Docker control plane exited {completed.returncode}"
            )
        self._confirm_removed(identifier)
        return ProcessResult(
            status="completed" if completed.returncode == 0 else "error",
            exit_code=completed.returncode,
            duration_seconds=time.monotonic() - started,
            stdout=_normalize_trajectory(
                completed.stdout,
                spec.trajectory_normalizer,
            ),
            stderr=_decode_output(completed.stderr),
            usage=AgentUsage.unavailable(),
        )

    def run(self, spec: AgentProcessSpec) -> ProcessResult:
        """Run one container and guarantee timeout or budget cleanup."""

        self._validate_inherited_environment(spec)
        self._prepare_workspace_ownership(spec.workspace)
        with tempfile.TemporaryDirectory(prefix="verilog-eval-container-") as tmp:
            cidfile = Path(tmp) / "container.cid"
            container_name = f"verilog-eval-{uuid.uuid4().hex}"
            command = self.build_command(
                spec,
                cidfile,
                container_name=container_name,
            )
            started = time.monotonic()
            if self.runner is subprocess.run:
                return self._run_streaming(
                    spec,
                    command,
                    cidfile,
                    container_name,
                    started,
                )
            return self._run_buffered(
                spec,
                command,
                cidfile,
                container_name,
                started,
            )
