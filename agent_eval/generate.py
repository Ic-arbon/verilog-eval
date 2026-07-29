#!/usr/bin/env python3
"""Generate one VerilogEval sample by invoking an external coding Agent."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.backend import adapter_profile, build_agent_command
from agent_eval.config import write_agent_configs
from agent_eval.metrics import parse_trajectory
from agent_eval.sandbox import build_docker_command, build_sandbox_command


SUBMISSION_CONTRACT = """\
Complete the benchmark task in the isolated workspace.
The benchmark submission must be saved as /workspace/TopModule.sv.
The file must contain the complete candidate that should be graded.
"""


@dataclass(frozen=True)
class PreparedWorkspace:
    workspace: Path
    agent_prompt: str
    starter_digest: Optional[str]


@dataclass(frozen=True)
class CandidatePublication:
    status: str
    submitted: bool
    output_path: Path


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_generation_workspace(
    prompt_path: Path,
    task: str,
    problem: str,
    workspace: Path,
) -> PreparedWorkspace:
    """Map public benchmark inputs into an isolated Agent workspace."""
    if task not in {"spec-to-rtl", "code-complete-iccad2023"}:
        raise ValueError(f"unsupported task: {task}")
    if not prompt_path.is_file():
        raise FileNotFoundError(f"prompt not found: {prompt_path}")

    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)

    benchmark_prompt = prompt_path.read_text()
    (workspace / "TASK.md").write_text(benchmark_prompt)

    starter_digest = None
    if task == "code-complete-iccad2023":
        interface_path = prompt_path.with_name(f"{problem}_ifc.txt")
        if not interface_path.is_file():
            raise FileNotFoundError(f"public interface not found: {interface_path}")
        candidate = workspace / "TopModule.sv"
        candidate.write_text(interface_path.read_text())
        starter_digest = file_digest(candidate)

    agent_prompt = benchmark_prompt.rstrip() + "\n\n" + SUBMISSION_CONTRACT
    return PreparedWorkspace(
        workspace=workspace,
        agent_prompt=agent_prompt,
        starter_digest=starter_digest,
    )


def publish_candidate(
    prepared: PreparedWorkspace,
    output_path: Path,
    agent_status: str,
) -> CandidatePublication:
    """Map the Agent workspace artifact back to the VerilogEval generator ABI."""
    candidate = prepared.workspace / "TopModule.sv"
    submitted = candidate.is_file()
    if submitted and prepared.starter_digest is not None:
        submitted = file_digest(candidate) != prepared.starter_digest

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if submitted:
        shutil.copyfile(candidate, output_path)
        status = agent_status
    else:
        status = (
            "missing_submission" if agent_status == "completed" else agent_status
        )
        output_path.write_text(
            f"// AGENT_EVAL_NO_SUBMISSION: {status}\n"
            "AGENT_EVAL_NO_SUBMISSION\n"
        )

    return CandidatePublication(
        status=status,
        submitted=submitted,
        output_path=output_path,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate one sample with an Agent")
    parser.add_argument("--agent", choices=["pi", "opencode"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--task",
        choices=["spec-to-rtl", "code-complete-iccad2023"],
        required=True,
    )
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--temperature", type=float, required=True)
    parser.add_argument("--top-p", type=float, required=True)
    parser.add_argument(
        "--sandbox-backend", choices=["bwrap", "docker"], required=True
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("prompt_filename", type=Path)
    return parser.parse_args()


def sandbox_environment(agent: str) -> dict:
    environment = {
        "HOME": "/workspace/.home",
        "XDG_CACHE_HOME": "/workspace/.cache",
        "XDG_CONFIG_HOME": "/workspace/.config",
        "XDG_DATA_HOME": "/workspace/.local/share",
        "XDG_STATE_HOME": "/workspace/.local/state",
        "npm_config_cache": "/workspace/.cache/npm",
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "LD_LIBRARY_PATH": os.environ.get(
            "AGENT_EVAL_SANDBOX_LD_LIBRARY_PATH",
            os.environ.get("LD_LIBRARY_PATH", ""),
        ),
    }
    if agent == "pi":
        environment["PI_CODING_AGENT_DIR"] = "/workspace/.pi-agent"
    return environment


def artifact_directory(
    artifact_root: Path,
    agent: str,
    problem: str,
    output_path: Path,
) -> Path:
    sample = output_path.stem.rsplit("_sample", 1)[-1]
    return artifact_root.resolve() / agent / problem / f"sample{sample}"


def execute_agent(args: argparse.Namespace, prepared: PreparedWorkspace, artifact: Path):
    write_agent_configs(
        prepared.workspace,
        os.environ.get("AGENT_EVAL_BASE_URL", "http://127.0.0.1:58000/v1"),
        args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
    )
    agent_command = build_agent_command(args.agent, args.model, prepared.agent_prompt)
    environment = sandbox_environment(args.agent)
    tools_path = Path(os.environ["AGENT_EVAL_AGENT_TOOLS"])

    if args.sandbox_backend == "docker":
        command = build_docker_command(
            workspace=prepared.workspace.resolve(),
            agent_tools=tools_path.resolve(),
            agent_command=agent_command,
            image=os.environ["AGENT_EVAL_DOCKER_IMAGE"],
            sandbox_path=os.environ["AGENT_EVAL_SANDBOX_PATH"],
            environment=environment,
            docker_path=os.environ.get("AGENT_EVAL_DOCKER", "docker"),
            uid=os.getuid(),
            gid=os.getgid(),
        )
    else:
        store_paths = [
            Path(item)
            for item in os.environ.get("AGENT_EVAL_STORE_PATHS", "").splitlines()
            if item
        ]
        command = build_sandbox_command(
            workspace=prepared.workspace.resolve(),
            agent_tools=tools_path.resolve(),
            agent_command=agent_command,
            store_paths=store_paths,
            sandbox_path=os.environ["AGENT_EVAL_SANDBOX_PATH"],
            bash_path=os.environ["AGENT_EVAL_BASH"],
            env_path=os.environ["AGENT_EVAL_ENV"],
            environment=environment,
            bwrap_path=os.environ.get("AGENT_EVAL_BWRAP", "bwrap"),
        )

    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.returncode
        status = "completed" if exit_code == 0 else "agent_error"
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        exit_code = 124
        status = "timeout"
    except OSError as error:
        stdout = ""
        stderr = str(error)
        exit_code = 127
        status = "agent_error"

    duration = time.monotonic() - started
    (artifact / "trajectory.jsonl").write_text(stdout)
    (artifact / "stderr.log").write_text(stderr)
    return status, exit_code, duration, parse_trajectory(args.agent, stdout.splitlines())


def main() -> int:
    args = parse_args()
    output_path = args.output.resolve()
    prompt_path = args.prompt_filename.resolve()
    problem = prompt_path.name.removesuffix("_prompt.txt")
    artifact = artifact_directory(args.artifact_root, args.agent, problem, output_path)
    prepared = prepare_generation_workspace(
        prompt_path=prompt_path,
        task=args.task,
        problem=problem,
        workspace=artifact / "workspace",
    )
    status, exit_code, duration, metrics = execute_agent(args, prepared, artifact)
    publication = publish_candidate(prepared, output_path, status)

    record = {
        "agent": args.agent,
        "adapter_profile": adapter_profile(args.agent),
        "problem": problem,
        "sample": output_path.stem,
        "status": publication.status,
        "submitted": publication.submitted,
        "exit_code": exit_code,
        "duration_seconds": duration,
        "candidate": str(output_path),
        "workspace": str(prepared.workspace),
        "metrics": asdict(metrics),
    }
    (artifact / "agent.json").write_text(json.dumps(record, indent=2) + "\n")

    print(f"problem = {problem}")
    print(f"model = {args.model}")
    print(f"agent = {args.agent}")
    print(f"adapter_profile = {adapter_profile(args.agent)}")
    print(f"agent_status = {publication.status}")
    print(f"submitted = {str(publication.submitted).lower()}")
    print(f"duration_seconds = {duration:.6f}")
    print(f"turns = {metrics.turns}")
    print(f"tool_calls = {metrics.tool_calls}")
    print(f"prompt_tokens = {metrics.input_tokens}")
    print(f"resp_tokens = {metrics.output_tokens}")
    print("cost = 0.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
