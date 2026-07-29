import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence


CommandRunner = Callable[..., subprocess.CompletedProcess]


def sandbox_identity(host_uid: int, host_gid: int) -> tuple:
    """Never propagate host root into the Docker sandbox."""
    if host_uid == 0:
        return 65534, 65534
    return host_uid, host_gid


def assign_workspace_ownership(workspace: Path, uid: int, gid: int) -> None:
    """Make a root-created workspace writable by its mapped sandbox user."""
    for path in [workspace, *workspace.rglob("*")]:
        os.chown(path, uid, gid, follow_symlinks=False)


def nix_store_closure(store_roots: Sequence[Path]) -> List[Path]:
    if not store_roots:
        return []
    result = subprocess.run(
        ["nix-store", "--query", "--requisites", *map(str, store_roots)],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted({Path(line) for line in result.stdout.splitlines() if line})


def select_sandbox_backend(
    requested: str,
    bwrap_path: str,
    docker_path: str,
    true_path: str,
    run: CommandRunner = subprocess.run,
) -> str:
    if requested not in {"auto", "bwrap", "docker"}:
        raise ValueError(f"unknown sandbox backend: {requested}")

    errors = []
    if requested in {"auto", "bwrap"}:
        try:
            probe = run(
                [
                    bwrap_path,
                    "--unshare-all",
                    "--share-net",
                    "--ro-bind",
                    "/",
                    "/",
                    "--",
                    true_path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                return "bwrap"
            errors.append(f"bubblewrap: {(probe.stderr or '').strip()}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"bubblewrap: {error}")
        if requested == "bwrap":
            raise RuntimeError("Bubblewrap sandbox is unavailable: " + "; ".join(errors))

    if requested in {"auto", "docker"}:
        try:
            probe = run(
                [docker_path, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                return "docker"
            errors.append(f"docker: {(probe.stderr or '').strip()}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"docker: {error}")
        if requested == "docker":
            raise RuntimeError("Docker sandbox is unavailable: " + "; ".join(errors))

    raise RuntimeError(
        "No usable sandbox backend. Enable unprivileged user namespaces or start "
        "Docker. " + "; ".join(errors)
    )


def build_docker_command(
    workspace: Path,
    agent_tools: Path,
    agent_command: Sequence[str],
    image: str,
    sandbox_path: str,
    environment: Dict[str, str],
    cidfile: Path,
    opencode_harness: Optional[Path] = None,
    docker_path: str = "docker",
    uid: int = 65534,
    gid: int = 65534,
) -> List[str]:
    command = [
        docker_path,
        "run",
        "--rm",
        "--init",
        "--cidfile",
        str(cidfile),
        "--network",
        "host",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--tmpfs",
        "/home/agent:rw,nosuid,nodev,size=512m,mode=1777",
        "--volume",
        f"{workspace}:/workspace:rw",
        "--volume",
        f"{agent_tools}:/agent-tools:ro",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/home/agent",
        "--env",
        f"PATH={sandbox_path}",
        "--env",
        "SHELL=/bin/bash",
    ]
    if opencode_harness is not None:
        command.extend(
            ["--volume", f"{opencode_harness}:/opencode-harness:ro"]
        )
    for key, value in sorted(environment.items()):
        command.extend(["--env", f"{key}={value}"])
    command.extend([image, *agent_command])
    return command


def build_sandbox_command(
    workspace: Path,
    agent_tools: Path,
    agent_command: Sequence[str],
    store_paths: Iterable[Path],
    sandbox_path: str,
    bash_path: str,
    env_path: str,
    environment: Dict[str, str],
    opencode_harness: Optional[Path] = None,
    bwrap_path: str = "bwrap",
) -> List[str]:
    command = [
        bwrap_path,
        "--die-with-parent",
        "--unshare-all",
        "--share-net",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/nix",
        "--dir",
        "/nix/store",
        "--dir",
        "/usr",
        "--dir",
        "/usr/bin",
        "--dir",
        "/bin",
        "--dir",
        "/home",
        "--dir",
        "/home/agent",
    ]

    for store_path in sorted(set(map(Path, store_paths))):
        command.extend(["--ro-bind", str(store_path), str(store_path)])

    # OpenCode ships a native executable from npm. On conventional Linux hosts
    # it needs the host dynamic loader, but no host project files are exposed.
    for system_path in (Path("/lib"), Path("/lib64")):
        if system_path.exists():
            command.extend(["--ro-bind", str(system_path), str(system_path)])

    if opencode_harness is not None:
        command.extend(
            ["--ro-bind", str(opencode_harness), "/opencode-harness"]
        )

    command.extend(
        [
            "--ro-bind",
            str(agent_tools),
            "/agent-tools",
            "--bind",
            str(workspace),
            "/workspace",
            "--ro-bind",
            env_path,
            "/usr/bin/env",
            "--ro-bind",
            bash_path,
            "/bin/bash",
            "--ro-bind",
            bash_path,
            "/bin/sh",
            "--chdir",
            "/workspace",
            "--setenv",
            "HOME",
            "/home/agent",
            "--setenv",
            "PATH",
            sandbox_path,
            "--setenv",
            "SHELL",
            "/bin/bash",
        ]
    )

    for key, value in sorted(environment.items()):
        command.extend(["--setenv", key, value])

    command.extend(["--", *agent_command])
    return command


def required_store_roots() -> List[Path]:
    raw = os.environ.get("AGENT_EVAL_STORE_ROOTS", "")
    return [Path(item) for item in raw.split() if item]
