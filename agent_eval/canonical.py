import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Sequence, Tuple

from agent_eval.models import AgentResult


CommandRunner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class VerilogEvalResult:
    problem: str
    symbol: str
    passed: bool
    num_passed: int
    num_samples: int
    pass_rate: float
    compile_log: Path
    candidate: Path

    @property
    def status(self) -> str:
        return "passed" if self.passed else "failed"


def stage_agent_result(
    pregen_root: Path,
    problem: str,
    result: AgentResult,
) -> Tuple[Path, Path]:
    problem_dir = pregen_root / problem
    problem_dir.mkdir(parents=True, exist_ok=True)
    sample = problem_dir / f"{problem}_sample01.sv"
    generate_log = problem_dir / f"{problem}_sample01-sv-generate.log"

    if result.final_sv and result.final_sv.is_file():
        shutil.copyfile(result.final_sv, sample)
    else:
        sample.write_text(
            "// AGENT_EVAL_NO_SUBMISSION: " + result.status + "\n"
            "AGENT_EVAL_NO_SUBMISSION\n"
        )

    generate_log.write_text(
        f"agent = {result.agent}\n"
        f"agent_status = {result.status}\n"
        f"agent_exit_code = {result.exit_code}\n"
        f"duration_seconds = {result.duration_seconds:.6f}\n"
        f"turns = {result.metrics.turns}\n"
        f"tool_calls = {result.metrics.tool_calls}\n"
        f"prompt_tokens = {result.metrics.input_tokens}\n"
        f"resp_tokens = {result.metrics.output_tokens}\n"
        "cost = 0.0\n"
    )
    return sample, generate_log


def canonical_commands(
    repo_root: Path,
    build_dir: Path,
    pregen_root: Path,
    problems_file: Path,
    task: str,
    jobs: int,
    bash_path: str,
) -> Tuple[Sequence[str], Sequence[str]]:
    configure = [
        str(repo_root / "configure"),
        "--with-model=agent-pregen",
        f"--with-task={task}",
        "--with-samples=1",
        "--with-max-tokens=8192",
        "--with-temperature=0.6",
        "--with-top-p=0.95",
        f"--with-problems={problems_file}",
        f"--with-pregen={pregen_root}",
    ]
    make = [
        "make",
        f"--jobs={jobs}",
        f"SHELL={bash_path}",
        "ECHO=echo",
        "sv-iv-analyze",
    ]
    return configure, make


def parse_verilog_eval_summary(
    summary_path: Path,
    build_dir: Path,
) -> Dict[str, VerilogEvalResult]:
    results: Dict[str, VerilogEvalResult] = {}
    for raw_line in summary_path.read_text().splitlines():
        if not raw_line.strip():
            continue
        problem, num_passed, num_samples, pass_rate, symbol = raw_line.split(",", 4)
        results[problem] = VerilogEvalResult(
            problem=problem,
            symbol=symbol,
            passed=symbol == ".",
            num_passed=int(num_passed),
            num_samples=int(num_samples),
            pass_rate=float(pass_rate),
            compile_log=(
                build_dir
                / problem
                / f"{problem}_sample01-sv-iv-test.log"
            ),
            candidate=build_dir / problem / f"{problem}_sample01.sv",
        )
    return results


def run_canonical_evaluation(
    repo_root: Path,
    agent_root: Path,
    task: str,
    problems: Sequence[str],
    agent_results: Mapping[str, AgentResult],
    jobs: int,
    bash_path: str,
    run: CommandRunner = subprocess.run,
) -> Tuple[Dict[str, VerilogEvalResult], Path]:
    canonical_root = agent_root / "verilog-eval"
    pregen_root = canonical_root / "pregen"
    build_dir = canonical_root / "build"
    if canonical_root.exists():
        shutil.rmtree(canonical_root)
    pregen_root.mkdir(parents=True)
    build_dir.mkdir(parents=True)

    for problem in problems:
        result = agent_results.get(problem)
        if result is None:
            raise RuntimeError(f"agent result missing for canonical grading: {problem}")
        stage_agent_result(pregen_root, problem, result)

    problems_file = canonical_root / "problems.txt"
    problems_file.write_text("".join(f"{problem}\n" for problem in problems))
    configure, make = canonical_commands(
        repo_root=repo_root,
        build_dir=build_dir,
        pregen_root=pregen_root,
        problems_file=problems_file,
        task=task,
        jobs=jobs,
        bash_path=bash_path,
    )

    environment = os.environ.copy()
    environment.pop("LD_LIBRARY_PATH", None)
    environment.pop("LD_PRELOAD", None)

    configure_result = run(
        configure,
        cwd=build_dir,
        capture_output=True,
        text=True,
        env=environment,
    )
    (canonical_root / "configure.log").write_text(
        (configure_result.stdout or "") + (configure_result.stderr or "")
    )
    if configure_result.returncode != 0:
        raise RuntimeError(
            f"canonical VerilogEval configure failed for {agent_root.name}; "
            f"see {canonical_root / 'configure.log'}"
        )

    make_result = run(
        make,
        cwd=build_dir,
        capture_output=True,
        text=True,
        env=environment,
    )
    (canonical_root / "make.log").write_text(
        (make_result.stdout or "") + (make_result.stderr or "")
    )
    if make_result.returncode != 0:
        raise RuntimeError(
            f"canonical VerilogEval make failed for {agent_root.name}; "
            f"see {canonical_root / 'make.log'}"
        )

    summary_csv = build_dir / "summary.csv"
    summary_txt = build_dir / "summary.txt"
    if not summary_csv.is_file() or not summary_txt.is_file():
        raise RuntimeError(
            f"canonical VerilogEval analyzer produced no summary for "
            f"{agent_root.name}; see {canonical_root / 'make.log'}"
        )
    shutil.copyfile(summary_csv, canonical_root / "summary.csv")
    shutil.copyfile(summary_txt, canonical_root / "summary.txt")
    return parse_verilog_eval_summary(build_dir / "summary.csv", build_dir), canonical_root
