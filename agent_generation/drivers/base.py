"""Narrow interfaces for external Agent CLI differences and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

from agent_generation.contracts import (
    AgentEnvironment,
    AgentProcessSpec,
    AgentRunRequest,
    ProcessResult,
)


ARTIFACT_INSTRUCTION = """\
Read /workspace/TASK.md and any other public task files in /workspace.
Complete the benchmark task using the available tools.
The only formal submission is /workspace/TopModule.sv.
Before finishing, verify that /workspace/TopModule.sv exists.
"""


class AgentDriver(Protocol):
    """Translate one Agent CLI without owning lifecycle or grading."""

    @property
    def profile_id(self) -> str:
        ...

    def write_config(self, request: AgentRunRequest) -> tuple[Path, ...]:
        ...

    def build_command(self, request: AgentRunRequest) -> tuple[str, ...]:
        ...

    def environment(self, request: AgentRunRequest) -> AgentEnvironment:
        ...

    def parse_event(self, line: str) -> Optional[object]:
        ...

    def classify_budget_event(self, line: str) -> Optional[str]:
        ...


class AgentExecutor(Protocol):
    """Execute one already-built Agent process specification."""

    def run(self, spec: AgentProcessSpec) -> ProcessResult:
        ...
