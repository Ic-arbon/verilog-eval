from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AgentRequest:
    problem_id: str
    workspace: Path
    model: str
    timeout_seconds: int


@dataclass
class TrajectoryMetrics:
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    parse_errors: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass
class AgentResult:
    agent: str
    status: str
    exit_code: int
    final_sv: Optional[Path]
    trajectory: Path
    stderr_log: Path
    duration_seconds: float
    metrics: TrajectoryMetrics

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["final_sv"] = str(self.final_sv) if self.final_sv else None
        data["trajectory"] = str(self.trajectory)
        data["stderr_log"] = str(self.stderr_log)
        return data


@dataclass
class GradeResult:
    status: str
    passed: bool
    compile_exit_code: Optional[int] = None
    simulation_exit_code: Optional[int] = None
    log_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["log_path"] = str(self.log_path) if self.log_path else None
        return data
