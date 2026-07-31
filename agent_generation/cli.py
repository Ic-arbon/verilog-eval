"""Single-sample orchestration for the Agent Generator Program Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_generation.contracts import (
    AgentGenerationResult,
    AgentProcessSpec,
    AgentRunRequest,
)
from agent_generation.drivers.base import AgentDriver, AgentExecutor
from agent_generation.manifest import write_generation_sidecars
from agent_generation.submission import publish_submission
from agent_generation.workspace import staged_workspace


@dataclass(frozen=True)
class AgentGeneratorConfig:
    """Host-side inputs for one Make generation target."""

    sample_id: str
    agent_name: str
    model: str
    task: str
    prompt_path: Path
    output_path: Path
    work_root: Path
    timeout_seconds: int
    max_turns: int
    max_tool_calls: int
    per_call_max_tokens: int
    rules_text: Optional[str]

    def __post_init__(self) -> None:
        if self.output_path.stem != self.sample_id:
            raise ValueError("sample_id must match the output filename stem")


def _read_public_starter(config: AgentGeneratorConfig) -> Optional[str]:
    if config.task != "code-complete-iccad2023":
        return None
    prompt_suffix = "_prompt.txt"
    if not config.prompt_path.name.endswith(prompt_suffix):
        raise ValueError("code-complete prompt filename must end in _prompt.txt")
    problem = config.prompt_path.name[: -len(prompt_suffix)]
    interface_path = config.prompt_path.with_name(f"{problem}_ifc.txt")
    if not interface_path.is_file():
        raise FileNotFoundError(f"public interface not found: {interface_path}")
    return interface_path.read_text(encoding="utf-8")


def _format_optional_integer(value: Optional[int]) -> str:
    return "unavailable" if value is None else str(value)


def _print_generation_log(result: AgentGenerationResult) -> None:
    process = result.process
    usage = process.usage
    if usage.input_tokens is None or usage.output_tokens is None:
        total_tokens = None
    else:
        total_tokens = usage.input_tokens + usage.output_tokens

    print(f"agent_status = {process.status}")
    print(f"submission_status = {result.submission.status}")
    print(f"duration_seconds = {process.duration_seconds:.6f}")
    print(f"turns = {_format_optional_integer(usage.turns)}")
    print(f"tool_calls = {_format_optional_integer(usage.tool_calls)}")
    print(f"prompt_tokens = {_format_optional_integer(usage.input_tokens)}")
    print(f"resp_tokens   = {_format_optional_integer(usage.output_tokens)}")
    print(f"total_tokens  = {_format_optional_integer(total_tokens)}")
    print("cost          = unavailable")


def run_agent_generation(
    config: AgentGeneratorConfig,
    driver: AgentDriver,
    executor: AgentExecutor,
) -> AgentGenerationResult:
    """Run one Agent in one ephemeral workspace and publish one candidate."""

    if not config.prompt_path.is_file():
        raise FileNotFoundError(f"public prompt not found: {config.prompt_path}")
    prompt_text = config.prompt_path.read_text(encoding="utf-8")
    starter_text = _read_public_starter(config)

    with staged_workspace(
        work_root=config.work_root,
        task=config.task,
        prompt_text=prompt_text,
        rules_text=config.rules_text,
        starter_text=starter_text,
    ) as prepared:
        request = AgentRunRequest(
            sample_id=config.sample_id,
            agent_name=config.agent_name,
            model=config.model,
            task=config.task,
            prompt_text=prompt_text,
            rules_text=config.rules_text,
            workspace=prepared.root,
            timeout_seconds=config.timeout_seconds,
            max_turns=config.max_turns,
            max_tool_calls=config.max_tool_calls,
            per_call_max_tokens=config.per_call_max_tokens,
        )
        driver.write_config(request)
        command = tuple(driver.build_command(request))
        process = executor.run(
            AgentProcessSpec(
                command=command,
                workspace=prepared.root,
                timeout_seconds=request.timeout_seconds,
            )
        )
        submission = publish_submission(
            prepared.root,
            config.output_path,
            starter_sha256=prepared.starter_sha256,
        )
        result = AgentGenerationResult(
            process=process,
            submission=submission,
        )
        write_generation_sidecars(
            output_path=config.output_path,
            request=request,
            profile_id=driver.profile_id,
            result=result,
        )

    _print_generation_log(result)
    return result
