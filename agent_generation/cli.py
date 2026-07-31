"""Single-sample orchestration for the Agent Generator Program Protocol."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from agent_generation.contracts import (
    AgentGenerationResult,
    AgentProcessSpec,
    AgentRunRequest,
)
from agent_generation.docker import DockerExecutor, DockerInfrastructureError
from agent_generation.drivers.base import AgentDriver, AgentExecutor
from agent_generation.drivers.opencode import OpenCodeDriver
from agent_generation.drivers.pi import PiDriver
from agent_generation.manifest import write_generation_sidecars
from agent_generation.submission import publish_submission
from agent_generation.task import selected_rules
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
        environment = driver.environment(request)
        process = executor.run(
            AgentProcessSpec(
                command=command,
                workspace=prepared.root,
                timeout_seconds=request.timeout_seconds,
                environment=environment,
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


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one VerilogEval sample with an external Agent"
    )
    parser.add_argument("--agent", choices=("pi", "opencode"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--task",
        choices=("spec-to-rtl", "code-complete-iccad2023"),
        required=True,
    )
    parser.add_argument("--examples", type=int, default=0)
    parser.add_argument("--rules", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument(
        "--agent-thinking", choices=("on", "off"), default="on"
    )
    parser.add_argument("--agent-timeout", type=int, default=300)
    parser.add_argument("--agent-max-turns", type=int, default=20)
    parser.add_argument("--agent-max-tool-calls", type=int, default=50)
    parser.add_argument(
        "--agent-tool-profile", choices=("base", "rtl"), default="base"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-environment", default="OPENAI_API_KEY")
    parser.add_argument("--docker-path")
    parser.add_argument("--docker-image")
    parser.add_argument("--agent-tools", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("prompt_filename", type=Path)
    return parser


def _driver_from_args(args: argparse.Namespace, base_url: str) -> AgentDriver:
    common = {
        "base_url": base_url,
        "api_key_environment": args.api_key_environment,
        "thinking_enabled": args.agent_thinking == "on",
    }
    if args.agent == "pi":
        return PiDriver(**common)
    return OpenCodeDriver(
        **common,
        temperature=args.temperature,
        top_p=args.top_p,
    )


def _required_path(
    explicit: Optional[Path],
    environment: Mapping[str, str],
    name: str,
) -> Path:
    value = explicit or (Path(environment[name]) if environment.get(name) else None)
    if value is None:
        raise DockerInfrastructureError(f"{name} is required")
    return value


def _executor_from_args(
    args: argparse.Namespace,
    environment: Mapping[str, str],
) -> DockerExecutor:
    profile_suffix = "BASE" if args.agent_tool_profile == "base" else "RTL"
    image = args.docker_image or environment.get(
        f"AGENT_EVAL_DOCKER_IMAGE_{profile_suffix}"
    )
    if not image:
        raise DockerInfrastructureError(
            f"AGENT_EVAL_DOCKER_IMAGE_{profile_suffix} is required"
        )
    docker_path = args.docker_path or environment.get("AGENT_EVAL_DOCKER", "docker")
    agent_tools = _required_path(
        args.agent_tools,
        environment,
        "AGENT_EVAL_AGENT_TOOLS",
    )
    return DockerExecutor(
        docker_path=docker_path,
        image=image,
        agent_tools=agent_tools,
        uid=os.getuid(),
        gid=os.getgid(),
        host_environment=environment,
    )


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    driver: Optional[AgentDriver] = None,
    executor: Optional[AgentExecutor] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    runtime_environment = dict(os.environ if environment is None else environment)

    if args.examples < 0:
        parser.error("--examples must not be negative")
    if args.examples != 0:
        parser.error("Agent examples are not implemented yet; use --examples=0")

    try:
        base_url = args.base_url or runtime_environment.get(
            "OPENAI_API_BASE", "http://127.0.0.1:58000/v1"
        )
        selected_driver = driver or _driver_from_args(args, base_url)
        selected_executor = executor or _executor_from_args(
            args, runtime_environment
        )
        output_path = args.output.resolve()
        work_root = (
            args.work_root.resolve()
            if args.work_root is not None
            else output_path.parent / ".agent-work"
        )
        config = AgentGeneratorConfig(
            sample_id=output_path.stem,
            agent_name=args.agent,
            model=args.model,
            task=args.task,
            prompt_path=args.prompt_filename.resolve(),
            output_path=output_path,
            work_root=work_root,
            timeout_seconds=args.agent_timeout,
            max_turns=args.agent_max_turns,
            max_tool_calls=args.agent_max_tool_calls,
            per_call_max_tokens=args.max_tokens,
            rules_text=selected_rules(args.task, args.rules),
        )
        run_agent_generation(config, selected_driver, selected_executor)
    except DockerInfrastructureError as error:
        print(f"ERROR: Agent sandbox infrastructure failed: {error}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError) as error:
        print(f"ERROR: Invalid Agent generation request: {error}", file=sys.stderr)
        return 2
    return 0
