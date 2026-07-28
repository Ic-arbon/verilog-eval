import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


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


def build_sandbox_command(
    workspace: Path,
    agent_tools: Path,
    agent_command: Sequence[str],
    store_paths: Iterable[Path],
    sandbox_path: str,
    bash_path: str,
    env_path: str,
    environment: Dict[str, str],
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
