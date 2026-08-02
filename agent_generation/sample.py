"""One Make-invoked Agent sample behind an immutable run configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from agent_generation.contracts import AgentProcessSpec, AgentRunRequest
from agent_generation.credentials import request_credential
from agent_generation.docker import DockerExecutor, DockerInfrastructureError
from agent_generation.drivers.base import AgentDriver, AgentExecutor
from agent_generation.drivers.opencode import OpenCodeDriver
from agent_generation.drivers.pi import PiDriver
from agent_generation.metrics import aggregate_trajectory_usage
from agent_generation.run_config import load_run_config
from agent_generation.runtime_bindings import load_runtime_bindings
from agent_generation.sample_result import commit_sample_bundle
from agent_generation.task import selected_rules
from agent_generation.tools import ToolsProjectionError, validate_tools_projection
from agent_generation.workspace import staged_workspace


class SampleRequestError(ValueError):
    """A Make sample target does not belong to the selected immutable run."""


def _read_regular(path: Path, maximum: int = 16 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SampleRequestError(f"cannot open selected public input: {path.name}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise SampleRequestError("selected public input is not bounded regular")
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > maximum:
            raise SampleRequestError("selected public input exceeds bound")
        return bytes(content)
    finally:
        os.close(descriptor)


def _selected_input(config: dict, *, kind: str, name: str) -> dict:
    matches = [
        item
        for item in config["benchmark"]["inputs"]
        if item["kind"] == kind and item["name"] == name
    ]
    if len(matches) != 1:
        raise SampleRequestError(f"selected input identity is missing: {name}")
    return matches[0]


def _verify_input(config: dict, path: Path, *, kind: str) -> str:
    content = _read_regular(path)
    expected = _selected_input(config, kind=kind, name=path.name)
    actual = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    if actual != {
        "sha256": expected["sha256"],
        "size_bytes": expected["size_bytes"],
    }:
        raise SampleRequestError(f"selected input identity changed: {path.name}")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SampleRequestError(f"selected public input is not UTF-8: {path.name}") from error


def _sample_identity(config: dict, output_path: Path, prompt_path: Path) -> tuple[str, str]:
    sample_id = output_path.stem
    match = re.fullmatch(r"(.+)_sample([0-9]+)", sample_id)
    if match is None:
        raise SampleRequestError("output filename has no canonical sample identity")
    problem, sample_number_text = match.groups()
    if problem not in config["benchmark"]["problems"]:
        raise SampleRequestError("output problem is not selected by this run")
    sample_number = int(sample_number_text)
    if not 1 <= sample_number <= config["benchmark"]["samples"]:
        raise SampleRequestError("output sample number is outside the run")
    if output_path.parent.name != problem:
        raise SampleRequestError("output directory does not match its problem")
    if prompt_path.name != f"{problem}_prompt.txt":
        raise SampleRequestError("prompt does not match output problem")
    return sample_id, problem


def _driver(config: dict) -> AgentDriver:
    common = {
        "base_url": config["endpoint"]["base_url"],
        "api_key_environment": config["endpoint"]["api_key_environment"],
        "thinking_enabled": config["agent"]["thinking"],
    }
    agent = config["agent"]["name"]
    if agent == "pi":
        return PiDriver(**common)
    if agent == "pi-dcd-rtl-module":
        return PiDriver(entry="rtl-module", **common)
    return OpenCodeDriver(**common)


def _endpoint_evidence(run_dir: Path) -> dict:
    path = run_dir / "endpoint-evidence.json"
    content = _read_regular(path, maximum=1024 * 1024)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SampleRequestError("endpoint evidence is invalid JSON") from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("response_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["response_sha256"]) is None
    ):
        raise SampleRequestError("endpoint evidence identity is invalid")
    canonical = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if content != canonical:
        raise SampleRequestError("endpoint evidence bytes are not canonical")
    return value


def generate_agent_sample(
    *,
    run_config_path: Path,
    output_path: Path,
    prompt_path: Path,
    driver: Optional[AgentDriver] = None,
    executor: Optional[AgentExecutor] = None,
    credential_client: Callable = request_credential,
) -> dict:
    """Run exactly one Make-selected Agent target and commit its Sample Bundle."""

    config_path = Path(run_config_path).resolve(strict=True)
    config = load_run_config(config_path)
    run_dir = config_path.parent
    digest = run_dir.name
    bindings = load_runtime_bindings(config_path)
    output = Path(output_path).resolve(strict=False)
    prompt = Path(prompt_path).resolve(strict=True)
    try:
        output.relative_to(run_dir.resolve())
    except ValueError as error:
        raise SampleRequestError("output target is outside the immutable run") from error
    sample_id, problem = _sample_identity(config, output, prompt)
    prompt_text = _verify_input(config, prompt, kind="prompt")

    starter_text = None
    starter_sha256 = None
    if config["benchmark"]["task"] == "code-complete-iccad2023":
        starter_path = Path(bindings["dataset_dir"]) / f"{problem}_ifc.txt"
        starter_text = _verify_input(config, starter_path, kind="public_starter")
        starter_sha256 = hashlib.sha256(starter_text.encode("utf-8")).hexdigest()

    try:
        validate_tools_projection(
            Path(bindings["tools_projection"]),
            config["runtime"]["agent_tools"]["content_sha256"],
        )
    except ToolsProjectionError as error:
        raise DockerInfrastructureError(f"Agent tools projection is invalid: {error}") from error
    evidence = _endpoint_evidence(run_dir)
    if (
        evidence["response_sha256"]
        != config["endpoint"]["models_response_sha256"]
    ):
        raise SampleRequestError("endpoint evidence does not match run identity")
    key_name = config["endpoint"]["api_key_environment"]
    credential = credential_client(
        run_dir=run_dir,
        config_digest=digest,
        sample_id=sample_id,
        environment_name=key_name,
    )
    selected_driver = driver or _driver(config)
    selected_executor = executor or DockerExecutor(
        docker_path=bindings["docker"]["client"],
        image=bindings["docker"]["image"],
        agent_tools=Path(bindings["tools_projection"]),
        uid=os.getuid(),
        gid=os.getgid(),
        host_environment={
            key_name: credential,
            "DOCKER_HOST": bindings["docker"]["daemon"],
            "PATH": str(Path(bindings["docker"]["client"]).parent),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )

    work_root = run_dir / ".agent-work"
    rules_text = selected_rules(
        config["benchmark"]["task"],
        config["benchmark"]["rules"],
    )
    limits = config["limits"]
    with staged_workspace(
        work_root=work_root,
        task=config["benchmark"]["task"],
        prompt_text=prompt_text,
        rules_text=rules_text,
        starter_text=starter_text,
    ) as prepared:
        request = AgentRunRequest(
            sample_id=sample_id,
            agent_name=config["agent"]["name"],
            model=config["agent"]["model"],
            task=config["benchmark"]["task"],
            prompt_text=prompt_text,
            rules_text=rules_text,
            workspace=prepared.root,
            timeout_seconds=limits["timeout_seconds"],
            max_turns=limits["max_turns"],
            max_tool_calls=limits["max_tool_calls"],
            max_input_tokens=limits["max_input_tokens"],
            per_call_max_tokens=limits["max_output_tokens"],
        )
        selected_driver.write_config(request)
        process = selected_executor.run(
            AgentProcessSpec(
                command=tuple(selected_driver.build_command(request)),
                workspace=prepared.root,
                timeout_seconds=request.timeout_seconds,
                environment=selected_driver.environment(request),
                max_turns=request.max_turns,
                max_tool_calls=request.max_tool_calls,
                event_classifier=selected_driver.classify_budget_event,
                trajectory_normalizer=selected_driver.normalize_trajectory_line,
            )
        )
        if process.usage.usage_source == "unavailable":
            process = replace(
                process,
                usage=aggregate_trajectory_usage(selected_driver, process.stdout),
            )
        manifest = commit_sample_bundle(
            workspace=prepared.root,
            output_path=output,
            sample_id=sample_id,
            agent=config["agent"]["name"],
            model=config["agent"]["model"],
            run_config_sha256=digest,
            process=process,
            limits=limits,
            runtime={
                "source_revision": config["runtime"]["source_commit"],
                "docker_image_id": config["runtime"]["docker_image_id"],
                "docker_daemon_identity": config["runtime"]["docker_daemon_identity"],
                "agent_tools_content_sha256": config["runtime"]["agent_tools"]["content_sha256"],
                "endpoint_base_url": config["endpoint"]["base_url"],
                "endpoint_evidence_sha256": evidence["response_sha256"],
            },
            secret_values=(credential,),
            starter_sha256=starter_sha256,
        )
    return manifest
