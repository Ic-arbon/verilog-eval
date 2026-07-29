#!/usr/bin/env python3
"""Run VerilogEval with an external Agent as the generation backend."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.provenance import (
    snapshot_agent_tools,
    write_agent_source_provenance,
)
from agent_eval.sandbox import (
    nix_store_closure,
    required_store_roots,
    select_sandbox_backend,
)


def build_evaluation_commands(
    repo_root: Path,
    build_dir: Path,
    artifact_root: Path,
    agent: str,
    task: str,
    model: str,
    problems_file: Optional[Path],
    jobs: int,
    timeout: int,
    max_tokens: int,
    temperature: float,
    top_p: float,
    bash_path: str,
    sandbox_backend: str,
) -> Tuple[List[str], List[str]]:
    """Build the original configure/make flow with only the generator replaced."""
    configure = [
        str(repo_root / "configure"),
        f"--with-model=agent-{agent}-{model}",
        f"--with-task={task}",
        "--with-samples=1",
        f"--with-max-tokens={max_tokens}",
        f"--with-temperature={temperature}",
        f"--with-top-p={top_p}",
    ]
    if problems_file is not None:
        configure.append(f"--with-problems={problems_file}")

    generator_flags = shlex.join(
        [
            f"--agent={agent}",
            f"--model={model}",
            f"--task={task}",
            f"--timeout={timeout}",
            f"--max-tokens={max_tokens}",
            f"--temperature={temperature}",
            f"--top-p={top_p}",
            f"--sandbox-backend={sandbox_backend}",
            f"--artifact-root={artifact_root}",
        ]
    )
    make = [
        "make",
        f"--jobs={jobs}",
        f"SHELL={bash_path}",
        "ECHO=echo",
        f"GENERATE_VERILOG={repo_root / 'agent_eval/generate.py'}",
        f"GENERATE_FLAGS={generator_flags}",
        "sv-iv-analyze",
    ]
    return configure, make


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate an external Agent through VerilogEval's generator ABI"
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--agent", choices=["pi", "opencode", "all"], default="all")
    parser.add_argument(
        "--task",
        "--with-task",
        choices=["spec-to-rtl", "code-complete-iccad2023"],
        default="spec-to-rtl",
    )
    parser.add_argument("--model", "--with-model", default="qwen3.6-coder")
    parser.add_argument("--samples", "--with-samples", type=int, default=1)
    parser.add_argument("--max-tokens", "--with-max-tokens", type=int, default=8192)
    parser.add_argument(
        "--temperature", "--with-temperature", type=float, default=0.6
    )
    parser.add_argument("--top-p", "--with-top-p", type=float, default=0.95)
    parser.add_argument("--base-url", default="http://127.0.0.1:58000/v1")
    parser.add_argument(
        "--agent-tools",
        type=Path,
        help="Local built Agent tools prefix to freeze and mount read-only",
    )
    parser.add_argument(
        "--agent-source",
        type=Path,
        help="Local Git source associated with --agent-tools",
    )
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=180)
    problems = parser.add_mutually_exclusive_group()
    problems.add_argument("--problems", nargs="*", help="Problem IDs")
    problems.add_argument(
        "--with-problems",
        dest="problems_file",
        type=Path,
        help="File containing one problem ID per line",
    )
    parser.add_argument("--run-root", type=Path)
    parser.add_argument(
        "--sandbox", choices=["auto", "bwrap", "docker"], default="auto"
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def check_vllm(base_url: str) -> None:
    health_url = base_url.rstrip("/")
    if health_url.endswith("/v1"):
        health_url = health_url[:-3]
    health_url += "/health"
    with urllib.request.urlopen(health_url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"vLLM health check returned HTTP {response.status}")


def ensure_docker_image(docker_path: str, image: str, archive: Path) -> None:
    inspect = subprocess.run(
        [docker_path, "image", "inspect", image],
        capture_output=True,
        text=True,
    )
    if inspect.returncode == 0:
        return
    loaded = subprocess.run(
        [docker_path, "load", "--input", str(archive)],
        capture_output=True,
        text=True,
    )
    if loaded.returncode != 0:
        raise RuntimeError(f"failed to load sandbox image: {loaded.stderr.strip()}")


def write_problem_file(
    run_root: Path,
    requested: Sequence[str],
    supplied_file: Optional[Path],
) -> Optional[Path]:
    if supplied_file is not None:
        path = supplied_file.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"problems file not found: {path}")
        return path
    if not requested:
        return None

    values = []
    for item in requested:
        values.extend(part for part in item.split(",") if part)
    path = run_root / "problems.txt"
    path.write_text("".join(f"{problem}\n" for problem in values))
    return path


def run_with_tee(command: Sequence[str], cwd: Path, log_path: Path, env: dict) -> int:
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log.write(line)
        return process.wait()


def validate_args(args: argparse.Namespace) -> None:
    if args.jobs < 1:
        raise SystemExit("--jobs must be a positive integer")
    if args.timeout < 1:
        raise SystemExit("--timeout must be a positive integer")
    if args.samples != 1:
        raise SystemExit("Agent Pass@1 evaluation requires --with-samples=1")
    if args.max_tokens < 1:
        raise SystemExit("--with-max-tokens must be a positive integer")
    if not 0 <= args.temperature <= 2:
        raise SystemExit("--with-temperature must be between 0 and 2")
    if not 0 < args.top_p <= 1:
        raise SystemExit("--with-top-p must be greater than 0 and at most 1")
    if args.agent_tools is not None and args.agent == "all":
        raise SystemExit("--agent-tools requires one explicit --agent")
    if args.agent_source is not None and args.agent_tools is None:
        raise SystemExit("--agent-source requires --agent-tools")


def main() -> int:
    args = parse_args()
    validate_args(args)
    repo_root = args.repo_root.resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = (
        args.run_root or repo_root / "runs" / f"agent-eval-{timestamp}"
    ).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    problems_file = write_problem_file(
        run_root, args.problems or [], args.problems_file
    )

    if args.dry_run:
        sandbox_backend = "docker" if args.sandbox == "auto" else args.sandbox
    else:
        check_vllm(args.base_url)
        sandbox_backend = select_sandbox_backend(
            requested=args.sandbox,
            bwrap_path=os.environ.get("AGENT_EVAL_BWRAP", "bwrap"),
            docker_path=os.environ.get("AGENT_EVAL_DOCKER", "docker"),
            true_path=os.environ.get("AGENT_EVAL_TRUE", "true"),
        )
        if sandbox_backend == "docker":
            ensure_docker_image(
                docker_path=os.environ.get("AGENT_EVAL_DOCKER", "docker"),
                image=os.environ["AGENT_EVAL_DOCKER_IMAGE"],
                archive=Path(os.environ["AGENT_EVAL_DOCKER_IMAGE_ARCHIVE"]),
            )

    environment = os.environ.copy()
    environment["AGENT_EVAL_BASE_URL"] = args.base_url
    if sandbox_backend == "bwrap":
        store_paths = nix_store_closure(required_store_roots())
        environment["AGENT_EVAL_STORE_PATHS"] = "\n".join(map(str, store_paths))

    agents = ["pi", "opencode"] if args.agent == "all" else [args.agent]
    print(f"Sandbox backend: {sandbox_backend}")
    print(
        f"Running original VerilogEval with Agent generator(s): {', '.join(agents)}"
    )

    for agent in agents:
        agent_root = run_root / agent
        build_dir = agent_root / "verilog-eval"
        build_dir.mkdir(parents=True, exist_ok=True)
        agent_environment = environment.copy()
        if args.agent_tools is not None:
            snapshot = snapshot_agent_tools(
                source=args.agent_tools,
                destination=agent_root / "agent-tools-snapshot",
                agent=agent,
            )
            agent_environment["AGENT_EVAL_AGENT_TOOLS"] = str(snapshot.path)
            if args.agent_source is not None:
                write_agent_source_provenance(
                    source=args.agent_source,
                    output_dir=agent_root,
                    tools_digest=snapshot.digest,
                )
            else:
                (agent_root / "agent-source.json").write_text(
                    json.dumps(
                        {
                            "mode": "local-tools",
                            "tools_digest": snapshot.digest,
                            "tools_path": str(args.agent_tools.resolve()),
                        },
                        indent=2,
                    )
                    + "\n"
                )
        configure, make = build_evaluation_commands(
            repo_root=repo_root,
            build_dir=build_dir,
            artifact_root=run_root,
            agent=agent,
            task=args.task,
            model=args.model,
            problems_file=problems_file,
            jobs=args.jobs,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            bash_path=os.environ.get("AGENT_EVAL_BASH", "/bin/bash"),
            sandbox_backend=sandbox_backend,
        )
        (agent_root / "commands.json").write_text(
            json.dumps({"configure": configure, "make": make}, indent=2) + "\n"
        )
        if args.dry_run:
            print(f"[{agent}] dry-run artifacts: {agent_root}")
            continue

        configured = subprocess.run(
            configure,
            cwd=build_dir,
            capture_output=True,
            text=True,
            env=agent_environment,
        )
        (agent_root / "configure.log").write_text(
            (configured.stdout or "") + (configured.stderr or "")
        )
        if configured.returncode != 0:
            print(f"[{agent}] configure failed: {agent_root / 'configure.log'}")
            return 1

        returncode = run_with_tee(
            make, build_dir, agent_root / "make.log", agent_environment
        )
        if returncode != 0:
            print(f"[{agent}] make failed: {agent_root / 'make.log'}")
            return 1
        for summary_name in ("summary.csv", "summary.txt"):
            shutil.copyfile(build_dir / summary_name, agent_root / summary_name)
        print(f"[{agent}] canonical summary: {agent_root / 'summary.txt'}")

    print(f"Artifacts: {run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
