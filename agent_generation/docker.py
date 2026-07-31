"""Hardened one-container executor for formal Agent evaluation samples."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional

from agent_generation.contracts import AgentProcessSpec, AgentUsage, ProcessResult


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

    def _container_identifier(self, cidfile: Path, container_name: str) -> str:
        try:
            container_id = cidfile.read_text(encoding="utf-8").strip()
        except OSError:
            container_id = ""
        if CONTAINER_ID_PATTERN.fullmatch(container_id):
            return container_id
        return container_name

    def _force_remove(self, identifier: str) -> None:
        try:
            completed = self.runner(
                (self.docker_path, "rm", "--force", identifier),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DockerInfrastructureError(
                f"failed to remove timed-out container: {error}"
            ) from error
        if completed.returncode != 0:
            message = _decode_output(completed.stderr).strip()
            raise DockerInfrastructureError(
                f"failed to remove timed-out container: {message or completed.returncode}"
            )

    def run(self, spec: AgentProcessSpec) -> ProcessResult:
        """Run one container and guarantee timeout cleanup before returning."""

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
            try:
                completed = self.runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=spec.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                identifier = self._container_identifier(cidfile, container_name)
                self._force_remove(identifier)
                return ProcessResult(
                    status="timeout",
                    exit_code=124,
                    duration_seconds=time.monotonic() - started,
                    stdout=_decode_output(error.output),
                    stderr=_decode_output(error.stderr),
                    usage=AgentUsage.unavailable(),
                )
            except OSError as error:
                raise DockerInfrastructureError(
                    f"failed to execute Docker: {error}"
                ) from error

            return ProcessResult(
                status="completed" if completed.returncode == 0 else "error",
                exit_code=completed.returncode,
                duration_seconds=time.monotonic() - started,
                stdout=_decode_output(completed.stdout),
                stderr=_decode_output(completed.stderr),
                usage=AgentUsage.unavailable(),
            )
