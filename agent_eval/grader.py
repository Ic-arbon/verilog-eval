import re
import subprocess
from pathlib import Path
from typing import Callable

from agent_eval.models import GradeResult


CommandRunner = Callable[..., subprocess.CompletedProcess]


def grade_submission(
    candidate: Path,
    reference: Path,
    testbench: Path,
    output_dir: Path,
    run: CommandRunner = subprocess.run,
    timeout_seconds: int = 30,
) -> GradeResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "grade.log"
    if not candidate.is_file():
        log_path.write_text("TopModule.sv was not submitted.\n")
        return GradeResult(
            status="missing_submission", passed=False, log_path=log_path
        )

    simulation = output_dir / "simulation"
    compile_result = run(
        [
            "iverilog",
            "-Wall",
            "-Winfloop",
            "-Wno-timescale",
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(simulation),
            str(candidate),
            str(testbench),
            str(reference),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    compile_output = (compile_result.stdout or "") + (compile_result.stderr or "")
    if compile_result.returncode != 0:
        log_path.write_text(compile_output)
        return GradeResult(
            status="compile_error",
            passed=False,
            compile_exit_code=compile_result.returncode,
            log_path=log_path,
        )

    try:
        simulation_result = run(
            ["vvp", str(simulation)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        simulation_output = (simulation_result.stdout or "") + (
            simulation_result.stderr or ""
        )
        log_path.write_text(compile_output + simulation_output)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        log_path.write_text(compile_output + output + "\nTIMEOUT\n")
        return GradeResult(
            status="timeout",
            passed=False,
            compile_exit_code=compile_result.returncode,
            log_path=log_path,
        )

    passed = bool(re.search(r"^Mismatches: 0 in \d+ samples$", simulation_output, re.M))
    return GradeResult(
        status="passed" if passed else "failed",
        passed=passed,
        compile_exit_code=compile_result.returncode,
        simulation_exit_code=simulation_result.returncode,
        log_path=log_path,
    )
