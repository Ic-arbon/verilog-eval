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
from typing import Dict, List, Sequence, Tuple

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.adapters import create_adapter
from agent_eval.config import write_agent_configs
from agent_eval.grader import grade_submission
from agent_eval.metrics import parse_trajectory
from agent_eval.models import AgentRequest, AgentResult
from agent_eval.sandbox import (
    build_sandbox_command,
    nix_store_closure,
    required_store_roots,
)
from agent_eval.workspace import prepare_workspace


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate external coding agents")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--agent", choices=["pi", "opencode", "all"], default="all")
    parser.add_argument("--task", choices=["spec-to-rtl", "code-complete-iccad2023"], default="spec-to-rtl")
    parser.add_argument("--model", default="qwen3.6-coder")
    parser.add_argument("--base-url", default="http://127.0.0.1:58000/v1")
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--problems", nargs="*", help="Problem IDs; defaults to the full dataset")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_problems(repo_root: Path, task: str, requested: Sequence[str]) -> List[str]:
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


def sandbox_environment(agent: str) -> Dict[str, str]:
    environment = {
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
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
) -> Tuple[AgentResult, object]:
    problem_root = run_root / agent / problem
    workspace = prepare_workspace(
        repo_root=args.repo_root,
        run_root=run_root / agent,
        task=args.task,
        problem=problem,
    )
    write_agent_configs(workspace, args.base_url, args.model)

    request = AgentRequest(
        problem_id=problem,
        workspace=Path("/workspace"),
        model=args.model,
        timeout_seconds=args.timeout,
    )
    adapter = create_adapter(agent)
    agent_command = adapter.agent_command(request)

    tools_path = Path(os.environ["AGENT_EVAL_AGENT_TOOLS"])
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

    if args.dry_run:
        trajectory_path.write_text("")
        stderr_path.write_text("")
        (problem_root / "command.json").write_text(
            json.dumps(sandbox_command, indent=2) + "\n"
        )
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

    dataset = args.repo_root / f"dataset_{args.task}"
    grade = grade_submission(
        candidate=workspace / "TopModule.sv",
        reference=dataset / f"{problem}_ref.sv",
        testbench=dataset / f"{problem}_test.sv",
        output_dir=problem_root,
    ) if not args.dry_run else None

    record = result.to_dict()
    record["problem"] = problem
    record["grade"] = grade.to_dict() if grade else None
    (problem_root / "metrics.json").write_text(json.dumps(record, indent=2) + "\n")
    return result, grade


def write_summary(run_root: Path, records: List[Tuple[str, str, AgentResult, object]]) -> None:
    summary = []
    for agent, problem, result, grade in sorted(records):
        summary.append(
            {
                "agent": agent,
                "problem": problem,
                "agent_status": result.status,
                "passed": bool(grade and grade.passed),
                "grade_status": grade.status if grade else "dry_run",
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

    if not args.dry_run:
        check_vllm(args.base_url)

    problems = load_problems(args.repo_root, args.task, args.problems or [])
    agents = ["pi", "opencode"] if args.agent == "all" else [args.agent]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = (args.run_root or args.repo_root / "runs" / f"agent-eval-{timestamp}").resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    store_paths = nix_store_closure(required_store_roots())
    work = [(agent, problem) for agent in agents for problem in problems]
    print(f"Running {len(work)} trajectories with {args.jobs} parallel jobs")

    records: List[Tuple[str, str, AgentResult, object]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(run_agent, agent, problem, args, run_root, store_paths): (agent, problem)
            for agent, problem in work
        }
        for future in as_completed(futures):
            agent, problem = futures[future]
            try:
                result, grade = future.result()
                records.append((agent, problem, result, grade))
                outcome = "PASS" if grade and grade.passed else result.status
                print(f"[{agent}] {problem}: {outcome}")
            except Exception as error:
                print(f"[{agent}] {problem}: ERROR: {error}", file=sys.stderr)

    write_summary(run_root, records)
    return 0 if len(records) == len(work) else 1


if __name__ == "__main__":
    raise SystemExit(main())
