"""Toolchain profiles and deterministic Docker preflight checks."""

import subprocess
from typing import Callable, Dict, Tuple


TOOLCHAIN_COMMANDS = {
    "base": ("iverilog",),
    "minimal-rtl": (
        "iverilog",
        "verilator",
        "yosys",
        "abc",
        "sby",
        "slang",
        "surelog",
        "sv2v",
    ),
}


def required_commands(profile: str) -> Tuple[str, ...]:
    try:
        return TOOLCHAIN_COMMANDS[profile]
    except KeyError as error:
        raise ValueError(f"unknown Agent toolchain: {profile}") from error


def verify_docker_toolchain(
    docker_path: str,
    image: str,
    profile: str,
    run: Callable = subprocess.run,
) -> Dict[str, str]:
    """Resolve every required command inside the selected immutable image."""
    commands = required_commands(profile)
    script = """
set -eu
for tool in "$@"; do
  path="$(command -v "$tool" || true)"
  if [ -z "$path" ]; then
    echo "missing: $tool" >&2
    exit 1
  fi
  printf '%s=%s\n' "$tool" "$path"
done
""".strip()
    completed = run(
        [
            docker_path,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            image,
            "bash",
            "-lc",
            script,
            "toolchain-preflight",
            *commands,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(f"Agent toolchain preflight failed: {detail}")

    resolved = {}
    for line in completed.stdout.splitlines():
        name, separator, path = line.partition("=")
        if separator and name in commands:
            resolved[name] = path
    missing = [name for name in commands if name not in resolved]
    if missing:
        raise RuntimeError(
            "Agent toolchain preflight returned no path for: " + ", ".join(missing)
        )
    return resolved
