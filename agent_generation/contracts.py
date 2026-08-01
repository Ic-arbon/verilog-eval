"""Immutable contracts shared by the clean-room Agent generator modules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


SUPPORTED_TASKS = frozenset({"spec-to-rtl", "code-complete-iccad2023"})
PROCESS_STATUSES = frozenset({"completed", "timeout", "error"})
TERMINATION_REASONS = frozenset({"timeout", "max_turns", "max_tool_calls"})
BUDGET_EVENT_KINDS = frozenset({"turn", "tool"})
SAMPLE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _require_nonempty(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_optional_nonnegative(name: str, value: Optional[int]) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_optional_single_line(name: str, value: Optional[str]) -> None:
    if value is not None and (not value or any(char in value for char in "\r\n\x00")):
        raise ValueError(f"{name} must be a non-empty single-line string")


@dataclass(frozen=True)
class RuntimeProvenance:
    """Non-secret identities for the source, sandbox, endpoint, and Agent tools."""

    source_revision: Optional[str] = None
    source_diff_sha256: Optional[str] = None
    docker_image: Optional[str] = None
    docker_image_id: Optional[str] = None
    agent_tools_versions: Optional[str] = None
    agent_tools_lock_sha256: Optional[str] = None
    agent_tools_content_sha256: Optional[str] = None
    api_base_url: Optional[str] = None

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            _require_optional_single_line(name, value)


@dataclass(frozen=True)
class AgentRunRequest:
    """Public task material and bounded resources for one Agent invocation."""

    sample_id: str
    agent_name: str
    model: str
    task: str
    prompt_text: str
    rules_text: Optional[str]
    workspace: Path
    timeout_seconds: int
    max_turns: int
    max_tool_calls: int
    max_input_tokens: int
    per_call_max_tokens: int

    def __post_init__(self) -> None:
        _require_nonempty("sample_id", self.sample_id)
        if SAMPLE_ID_PATTERN.fullmatch(self.sample_id) is None:
            raise ValueError("sample_id contains unsupported path characters")
        _require_nonempty("agent_name", self.agent_name)
        _require_nonempty("model", self.model)
        _require_nonempty("prompt_text", self.prompt_text)
        if self.task not in SUPPORTED_TASKS:
            raise ValueError(f"unsupported task: {self.task}")
        _require_positive("timeout_seconds", self.timeout_seconds)
        _require_positive("max_turns", self.max_turns)
        _require_positive("max_tool_calls", self.max_tool_calls)
        _require_positive("max_input_tokens", self.max_input_tokens)
        _require_positive("per_call_max_tokens", self.per_call_max_tokens)


@dataclass(frozen=True)
class AgentUsage:
    """Nullable aggregate usage for one complete Agent session."""

    input_tokens: Optional[int]
    output_tokens: Optional[int]
    turns: Optional[int]
    tool_calls: Optional[int]
    usage_source: str

    def __post_init__(self) -> None:
        _require_optional_nonnegative("input_tokens", self.input_tokens)
        _require_optional_nonnegative("output_tokens", self.output_tokens)
        _require_optional_nonnegative("turns", self.turns)
        _require_optional_nonnegative("tool_calls", self.tool_calls)
        _require_nonempty("usage_source", self.usage_source)

    @classmethod
    def unavailable(cls) -> "AgentUsage":
        return cls(
            input_tokens=None,
            output_tokens=None,
            turns=None,
            tool_calls=None,
            usage_source="unavailable",
        )


@dataclass(frozen=True)
class ProcessResult:
    """Normalized outcome from one bounded external Agent process."""

    status: str
    exit_code: Optional[int]
    duration_seconds: float
    stdout: str
    stderr: str
    usage: AgentUsage
    termination_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in PROCESS_STATUSES:
            raise ValueError(f"unsupported process status: {self.status}")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        if (
            self.termination_reason is not None
            and self.termination_reason not in TERMINATION_REASONS
        ):
            raise ValueError(
                f"unsupported termination reason: {self.termination_reason}"
            )


@dataclass(frozen=True)
class AgentEnvironment:
    """Non-secret values and explicitly inherited secret environment names."""

    variables: tuple[tuple[str, str], ...] = ()
    inherit: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        variable_names = [name for name, _value in self.variables]
        all_names = variable_names + list(self.inherit)
        if any(ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None for name in all_names):
            raise ValueError("environment contains an invalid variable name")
        if len(set(all_names)) != len(all_names):
            raise ValueError("environment variable names must be unique")
        if any("\x00" in value for _name, value in self.variables):
            raise ValueError("environment values must not contain NUL bytes")


@dataclass(frozen=True)
class AgentProcessSpec:
    """Command and public workspace passed to an isolated executor."""

    command: tuple[str, ...]
    workspace: Path
    timeout_seconds: int
    environment: AgentEnvironment = AgentEnvironment()
    max_turns: Optional[int] = None
    max_tool_calls: Optional[int] = None
    event_classifier: Optional[Callable[[str], Optional[str]]] = None
    trajectory_normalizer: Optional[Callable[[str], str]] = None

    def __post_init__(self) -> None:
        if not self.command or any(not argument for argument in self.command):
            raise ValueError("command must contain non-empty arguments")
        _require_positive("timeout_seconds", self.timeout_seconds)
        if self.max_turns is not None:
            _require_positive("max_turns", self.max_turns)
        if self.max_tool_calls is not None:
            _require_positive("max_tool_calls", self.max_tool_calls)
        has_limits = self.max_turns is not None or self.max_tool_calls is not None
        if has_limits != (self.event_classifier is not None):
            raise ValueError("budget limits and event_classifier must be supplied together")


@dataclass(frozen=True)
class SubmissionResult:
    """Result of publishing a workspace artifact to the Make output path."""

    status: str
    output_path: Path
    sha256: Optional[str]
    size_bytes: Optional[int]


@dataclass(frozen=True)
class AgentGenerationResult:
    """Orthogonal process and formal-submission outcomes for one sample."""

    process: ProcessResult
    submission: SubmissionResult
