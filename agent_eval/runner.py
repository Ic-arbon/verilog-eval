#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.adapters import create_adapter
from agent_eval.canonical import VerilogEvalResult, run_canonical_evaluation
from agent_eval.config import write_agent_configs
from agent_eval.metrics import parse_trajectory
from agent_eval.models import AgentRequest, AgentResult
from agent_eval.sandbox import (
    build_docker_command,
    build_sandbox_command,
    nix_store_closure,
    required_store_roots,
    select_sandbox_backend,
)
from agent_eval.workspace import prepare_workspace


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate external coding agents")
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
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=180)
    problems = parser.add_mutually_exclusive_group()
    problems.add_argument(
        "--problems",
        nargs="*",
        help="Problem IDs; defaults to the full dataset",
    )
    problems.add_argument(
        "--with-problems",
        dest="problems_file",
        type=Path,
        help="File containing one problem ID per line",
    )
    parser.add_argument("--run-root", type=Path)
    parser.add_argument(
        "--sandbox",
        choices=["auto", "bwrap", "docker"],
        default="auto",
        help="Isolation backend; auto tries Bubblewrap then Docker",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_problems(
    repo_root: Path,
    task: str,
    requested: Sequence[str],
    problems_file: Optional[Path] = None,
) -> List[str]:
    if problems_file is not None:
        return [
            line.strip()
            for line in problems_file.read_text().splitlines()
            if line.strip()
        ]
    if requested:
        values: List[str] = []
        for item in requested:
            values.extend(part for part in item.split(",") if part)
        return values
    problems_file = repo_root / f"dataset_{task}" / "problems.txt"
    return [line.strip() for line in problems_file.read_text().splitlines() if line.strip()]


def check_vllm(base_url: str) -> None:
    health_url = base_url.rstrip("/")
    if health_url.endswith("/v1"):
        health_url = health_url[:-3]
    health_url += "/health"
    with urllib.request.urlopen(health_url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"vLLM health check returned HTTP {response.status}")


def ensure_docker_image(docker_path: str, image: str, archive: Path) -> None:
    loaded = subprocess.run(
        [docker_path, "load", "--input", str(archive)],
        capture_output=True,
        text=True,
    )
    if loaded.returncode != 0:
        raise RuntimeError(f"failed to load sandbox image: {loaded.stderr.strip()}")
    inspect = subprocess.run(
        [docker_path, "image", "inspect", image],
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0:
        raise RuntimeError(f"sandbox image was not loaded as {image}")


def sandbox_environment(agent: str) -> Dict[str, str]:
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


def run_agent(
    agent: str,
    problem: str,
    args: argparse.Namespace,
    run_root: Path,
    store_paths: Sequence[Path],
) -> AgentResult:
    problem_root = run_root / agent / problem
    workspace = prepare_workspace(
        repo_root=args.repo_root,
        run_root=run_root / agent,
        task=args.task,
        problem=problem,
    )
    write_agent_configs(
        workspace,
        args.base_url,
        args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
    )

    request = AgentRequest(
        problem_id=problem,
        workspace=Path("/workspace"),
        model=args.model,
        timeout_seconds=args.timeout,
    )
    adapter = create_adapter(agent)
    agent_command = adapter.agent_command(request)

    tools_path = Path(os.environ["AGENT_EVAL_AGENT_TOOLS"])
    if args.sandbox_backend == "docker":
        sandbox_command = build_docker_command(
            workspace=workspace.resolve(),
            agent_tools=tools_path.resolve(),
            agent_command=agent_command,
            image=os.environ["AGENT_EVAL_DOCKER_IMAGE"],
            sandbox_path=os.environ["AGENT_EVAL_SANDBOX_PATH"],
            environment=sandbox_environment(agent),
            docker_path=os.environ.get("AGENT_EVAL_DOCKER", "docker"),
            uid=os.getuid(),
            gid=os.getgid(),
        )
    else:
        sandbox_command = build_sandbox_command(
            workspace=workspace.resolve(),
            agent_tools=tools_path.resolve(),
            agent_command=agent_command,
            store_paths=store_paths,
            sandbox_path=os.environ["AGENT_EVAL_SANDBOX_PATH"],
            bash_path=os.environ["AGENT_EVAL_BASH"],
            env_path=os.environ["AGENT_EVAL_ENV"],
            environment=sandbox_environment(agent),
            bwrap_path=os.environ.get("AGENT_EVAL_BWRAP", "bwrap"),
        )

    trajectory_path = problem_root / "trajectory.jsonl"
    stderr_path = problem_root / "stderr.log"
    problem_root.mkdir(parents=True, exist_ok=True)
    (problem_root / "command.json").write_text(
        json.dumps(sandbox_command, indent=2) + "\n"
    )

    if args.dry_run:
        trajectory_path.write_text("")
        stderr_path.write_text("")
        result = AgentResult(
            agent=agent,
            status="dry_run",
            exit_code=0,
            final_sv=None,
            trajectory=trajectory_path,
            stderr_log=stderr_path,
            duration_seconds=0.0,
            metrics=parse_trajectory(agent, []),
        )
    else:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                sandbox_command,
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

        trajectory_path.write_text(stdout)
        stderr_path.write_text(stderr)
        lines = stdout.splitlines()
        final_sv = workspace / "TopModule.sv"
        if status == "completed" and not final_sv.is_file():
            status = "missing_submission"
        result = AgentResult(
            agent=agent,
            status=status,
            exit_code=exit_code,
            final_sv=final_sv if final_sv.is_file() else None,
            trajectory=trajectory_path,
            stderr_log=stderr_path,
            duration_seconds=time.monotonic() - started,
            metrics=parse_trajectory(agent, lines),
        )

    record = result.to_dict()
    record["problem"] = problem
    record["sandbox"] = args.sandbox_backend
    record["grade"] = None
    (problem_root / "metrics.json").write_text(json.dumps(record, indent=2) + "\n")
    return result


def attach_canonical_grade(
    run_root: Path,
    agent: str,
    problem: str,
    grade: VerilogEvalResult,
) -> None:
    problem_root = run_root / agent / problem
    metrics_path = problem_root / "metrics.json"
    record = json.loads(metrics_path.read_text())
    grade_log = problem_root / "grade.log"
    grade_log.write_text(grade.compile_log.read_text())
    record["grade"] = {
        "source": "verilog-eval/sv-iv-analyze",
        "status": grade.status,
        "passed": grade.passed,
        "symbol": grade.symbol,
        "num_passed": grade.num_passed,
        "num_samples": grade.num_samples,
        "pass_rate": grade.pass_rate,
        "compile_log": str(grade.compile_log),
        "canonical_candidate": str(grade.candidate),
        "log_path": str(grade_log),
    }
    metrics_path.write_text(json.dumps(record, indent=2) + "\n")


def write_summary(
    run_root: Path,
    records: List[Tuple[str, str, AgentResult]],
    grades: Dict[str, Dict[str, VerilogEvalResult]],
    sandbox_backend: str,
) -> None:
    summary = []
    for agent, problem, result in sorted(records):
        grade = grades.get(agent, {}).get(problem)
        summary.append(
            {
                "agent": agent,
                "problem": problem,
                "agent_status": result.status,
                "sandbox": sandbox_backend,
                "passed": bool(grade and grade.passed),
                "grade_status": grade.status if grade else "not_graded",
                "verilog_eval_symbol": grade.symbol if grade else "",
                "duration_seconds": round(result.duration_seconds, 3),
                **result.metrics.to_dict(),
            }
        )

    (run_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (run_root / "summary.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary[0]) if summary else ["agent"])
        writer.writeheader()
        writer.writerows(summary)

    for agent in sorted({row["agent"] for row in summary}):
        rows = [row for row in summary if row["agent"] == agent]
        passed = sum(row["passed"] for row in rows)
        print(f"{agent}: {passed}/{len(rows)} passed")
    print(f"Artifacts: {run_root}")


def main() -> int:
    args = parse_args()
    args.repo_root = args.repo_root.resolve()
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

    if not args.dry_run:
        check_vllm(args.base_url)
        try:
            args.sandbox_backend = select_sandbox_backend(
                requested=args.sandbox,
                bwrap_path=os.environ.get("AGENT_EVAL_BWRAP", "bwrap"),
                docker_path=os.environ.get("AGENT_EVAL_DOCKER", "docker"),
                true_path=os.environ.get("AGENT_EVAL_TRUE", "true"),
            )
            if args.sandbox_backend == "docker":
                ensure_docker_image(
                    docker_path=os.environ.get("AGENT_EVAL_DOCKER", "docker"),
                    image=os.environ["AGENT_EVAL_DOCKER_IMAGE"],
                    archive=Path(os.environ["AGENT_EVAL_DOCKER_IMAGE_ARCHIVE"]),
                )
        except (KeyError, RuntimeError) as error:
            raise SystemExit(str(error))
    else:
        args.sandbox_backend = "bwrap" if args.sandbox == "auto" else args.sandbox
    print(f"Sandbox backend: {args.sandbox_backend}")

    problems = load_problems(
        args.repo_root,
        args.task,
        args.problems or [],
        args.problems_file,
    )
    agents = ["pi", "opencode"] if args.agent == "all" else [args.agent]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = (args.run_root or args.repo_root / "runs" / f"agent-eval-{timestamp}").resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    store_paths = (
        nix_store_closure(required_store_roots())
        if args.sandbox_backend == "bwrap"
        else []
    )
    work = [(agent, problem) for agent in agents for problem in problems]
    print(f"Running {len(work)} trajectories with {args.jobs} parallel jobs")

    records: List[Tuple[str, str, AgentResult]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(run_agent, agent, problem, args, run_root, store_paths): (agent, problem)
            for agent, problem in work
        }
        for future in as_completed(futures):
            agent, problem = futures[future]
            try:
                result = future.result()
                records.append((agent, problem, result))
                print(f"[{agent}] {problem}: agent {result.status}")
            except Exception as error:
                print(f"[{agent}] {problem}: ERROR: {error}", file=sys.stderr)

    if len(records) != len(work):
        write_summary(run_root, records, {}, args.sandbox_backend)
        return 1

    canonical_grades: Dict[str, Dict[str, VerilogEvalResult]] = {}
    if not args.dry_run:
        for agent in agents:
            agent_results = {
                problem: result
                for record_agent, problem, result in records
                if record_agent == agent
            }
            grades, canonical_root = run_canonical_evaluation(
                repo_root=args.repo_root,
                agent_root=run_root / agent,
                task=args.task,
                problems=problems,
                agent_results=agent_results,
                jobs=args.jobs,
                bash_path=os.environ["AGENT_EVAL_BASH"],
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                top_p=args.top_p,
            )
            canonical_grades[agent] = grades
            for problem, grade in grades.items():
                attach_canonical_grade(run_root, agent, problem, grade)
                if grade.passed:
                    outcome = "PASS"
                elif agent_results[problem].status != "completed":
                    outcome = (
                        f"{agent_results[problem].status} "
                        f"(VerilogEval {grade.symbol})"
                    )
                else:
                    outcome = f"FAIL (VerilogEval {grade.symbol})"
                print(f"[{agent}] {problem}: {outcome}")

            print(f"\nCanonical VerilogEval results ({agent}):")
            print((canonical_root / "summary.txt").read_text(), end="")

    write_summary(run_root, records, canonical_grades, args.sandbox_backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
