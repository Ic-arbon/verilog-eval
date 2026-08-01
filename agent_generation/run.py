"""Agent-owned run preparation and, after cutover, orchestration composition root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from agent_generation.endpoint import preflight_endpoint
from agent_generation.lifecycle import (
    abandon_recovery,
    lifecycle_lock,
    list_recovery_receipts,
    publish_recovery_receipt,
    synthesize_orphan_recovery_receipts,
)
from agent_generation.provenance import (
    AgentToolsError,
    docker_daemon_identity,
    executable_identity,
    support_file_identity,
)
from agent_generation.run_config import (
    canonical_run_config,
    load_run_config,
    publish_run_config,
)
from agent_generation.runtime_bindings import validate_runtime_bindings
from agent_generation.task import selected_rules
from agent_generation.tools import ToolsProjectionError, project_agent_tools


_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")
_DEFAULTS = {
    "agent": "opencode",
    "model": "qwen3.6-coder",
    "task": "spec-to-rtl",
    "samples": 1,
    "examples": 0,
    "rules": False,
    "timeout_seconds": 300,
    "max_turns": 20,
    "max_tool_calls": 50,
    "max_input_tokens": 16384,
    "max_output_tokens": 16384,
    "thinking": True,
    "toolset": "standard",
    "base_url": "http://127.0.0.1:58000/v1",
    "api_key_environment": "OPENAI_API_KEY",
}
_MATERIAL_DESTINATIONS = frozenset(_DEFAULTS)


class RunnerError(RuntimeError):
    """Run preparation or orchestration failed before a valid benchmark result."""


@dataclass(frozen=True)
class RunnerOptions:
    mode: str
    resume_config: Optional[Path]
    agent: str
    model: str
    task: str
    samples: int
    examples: int
    rules: bool
    timeout_seconds: int
    max_turns: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    thinking: bool
    toolset: str
    base_url: str
    api_key_environment: str
    jobs: int
    source_root: Optional[Path]
    dataset_dir: Optional[Path]
    problems_file: Optional[Path]
    build_root: Optional[Path]
    agent_tools: Optional[Path]
    docker_path: Optional[Path]
    docker_image: Optional[str]
    docker_archive: Optional[Path]
    run_path_file: Optional[Path]
    management_action: Optional[str]
    recovery_digest: Optional[str]
    check_host_contamination: bool


@dataclass
class PreparationEvidence:
    """Resolved material identities and separately carried machine locators."""

    source_commit: str
    problems: tuple[str, ...]
    inputs: tuple[dict, ...]
    docker_image_id: str
    docker_daemon_identity: str
    tools_content_sha256: str
    tools_source_content_sha256: str
    tools_lock_sha256: str
    tools_versions: dict[str, str]
    toolchain_identities: tuple[dict, ...]
    support_identities: tuple[dict, ...]
    endpoint_evidence: dict
    runtime_bindings: dict


@dataclass(frozen=True)
class PreparedRun:
    config_path: Path
    config: dict
    digest: str
    bindings: dict
    endpoint_evidence: dict
    credential: str = field(repr=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and run one content-addressed Agent evaluation"
    )
    parser.add_argument(
        "--with-agent", choices=("pi", "opencode"), dest="agent"
    )
    parser.add_argument("--with-model", dest="model")
    parser.add_argument(
        "--with-task",
        choices=("spec-to-rtl", "code-complete-iccad2023"),
        dest="task",
    )
    parser.add_argument("--with-samples", type=int, dest="samples")
    parser.add_argument("--with-examples", type=int, dest="examples")
    rules = parser.add_mutually_exclusive_group()
    rules.add_argument("--with-rules", dest="rules", action="store_true", default=None)
    rules.add_argument("--without-rules", dest="rules", action="store_false")
    parser.add_argument("--with-agent-timeout", dest="timeout_seconds", type=int)
    parser.add_argument("--with-agent-max-turns", dest="max_turns", type=int)
    parser.add_argument(
        "--with-agent-max-tool-calls", dest="max_tool_calls", type=int
    )
    parser.add_argument(
        "--with-agent-max-input-tokens", dest="max_input_tokens", type=int
    )
    parser.add_argument("--with-max-tokens", dest="max_output_tokens", type=int)
    parser.add_argument(
        "--with-agent-thinking",
        choices=("on", "off"),
        dest="thinking_text",
    )
    parser.add_argument(
        "--with-agent-toolset", choices=("standard", "rtl"), dest="toolset"
    )
    parser.add_argument("--with-openai-api-base", dest="base_url")
    parser.add_argument(
        "--with-api-key-environment", dest="api_key_environment"
    )

    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--with-dataset", dest="dataset_dir", type=Path)
    parser.add_argument("--with-problems", dest="problems_file", type=Path)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--agent-tools", type=Path)
    parser.add_argument("--docker-path", type=Path)
    parser.add_argument("--docker-image")
    parser.add_argument("--docker-archive", type=Path)
    parser.add_argument("--run-path-file", type=Path)

    parser.add_argument("--new-run", action="store_true")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--list-recoveries", action="store_true")
    parser.add_argument("--resume-recovery")
    parser.add_argument("--abandon-recovery")
    parser.add_argument("--check-host-contamination", action="store_true")
    return parser


def _jobs(parser: argparse.ArgumentParser, environment: Mapping[str, str]) -> int:
    text = environment.get("VERILOG_EVAL_JOBS", "4")
    if _POSITIVE_INTEGER.fullmatch(text) is None:
        parser.error("VERILOG_EVAL_JOBS must be a positive integer")
    return int(text)


def _material_overrides(namespace: argparse.Namespace) -> list[str]:
    overrides = []
    for name in _MATERIAL_DESTINATIONS:
        parser_name = "thinking_text" if name == "thinking" else name
        if getattr(namespace, parser_name, None) is not None:
            overrides.append(name)
    return sorted(overrides)


def _loaded_material(config: dict) -> dict:
    return {
        "agent": config["agent"]["name"],
        "model": config["agent"]["model"],
        "task": config["benchmark"]["task"],
        "samples": config["benchmark"]["samples"],
        "examples": config["benchmark"]["examples"],
        "rules": config["benchmark"]["rules"],
        "timeout_seconds": config["limits"]["timeout_seconds"],
        "max_turns": config["limits"]["max_turns"],
        "max_tool_calls": config["limits"]["max_tool_calls"],
        "max_input_tokens": config["limits"]["max_input_tokens"],
        "max_output_tokens": config["limits"]["max_output_tokens"],
        "thinking": config["agent"]["thinking"],
        "toolset": config["agent"]["toolset"],
        "base_url": config["endpoint"]["base_url"],
        "api_key_environment": config["endpoint"]["api_key_environment"],
    }


def parse_runner_options(
    argv: Optional[Sequence[str]] = None,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> RunnerOptions:
    """Resolve the closed ordinary/new-run/resume CLI state machine."""

    parser = _parser()
    namespace = parser.parse_args(argv)
    environ = dict(os.environ if environment is None else environment)
    selected_actions = [
        ("list", None) if namespace.list_recoveries else None,
        ("resume", namespace.resume_recovery) if namespace.resume_recovery else None,
        ("abandon", namespace.abandon_recovery) if namespace.abandon_recovery else None,
        ("contamination", None) if namespace.check_host_contamination else None,
    ]
    selected_actions = [action for action in selected_actions if action is not None]
    if len(selected_actions) > 1:
        parser.error("select at most one recovery/contamination action")
    management_action, recovery_digest = (
        selected_actions[0] if selected_actions else (None, None)
    )
    if namespace.new_run and namespace.resume is not None:
        parser.error("--new-run and --resume are mutually exclusive")

    resolved_jobs = _jobs(parser, environ)
    resume_config: Optional[Path] = None
    if namespace.resume is not None:
        overrides = _material_overrides(namespace)
        if overrides:
            parser.error(
                "material options are forbidden with --resume: " + ", ".join(overrides)
            )
        resume_config = namespace.resume.resolve()
        try:
            config = load_run_config(resume_config)
        except ValueError as error:
            parser.error(str(error))
        material = _loaded_material(config)
        if "VERILOG_EVAL_JOBS" in environ and resolved_jobs != config["jobs"]:
            parser.error("VERILOG_EVAL_JOBS must equal the resumed run configuration")
        resolved_jobs = config["jobs"]
        mode = "resume"
    else:
        material = dict(_DEFAULTS)
        for name in _MATERIAL_DESTINATIONS:
            parser_name = "thinking_text" if name == "thinking" else name
            value = getattr(namespace, parser_name, None)
            if value is not None:
                material[name] = value == "on" if name == "thinking" else value
        mode = "new-run" if namespace.new_run else "ordinary"

    for name in (
        "samples",
        "timeout_seconds",
        "max_turns",
        "max_tool_calls",
        "max_input_tokens",
        "max_output_tokens",
    ):
        if isinstance(material[name], bool) or material[name] <= 0:
            parser.error(f"{name} must be a positive integer")
    if isinstance(material["examples"], bool) or material["examples"] < 0:
        parser.error("examples must be a non-negative integer")
    if material["examples"] != 0:
        parser.error("Agent examples are not implemented; use --with-examples=0")

    def locator(argument, environment_name: str) -> Optional[Path]:
        if argument is not None:
            return Path(argument).resolve()
        value = environ.get(environment_name)
        return Path(value).resolve() if value else None

    return RunnerOptions(
        mode=mode,
        resume_config=resume_config,
        jobs=resolved_jobs,
        source_root=locator(namespace.source_root, "VERILOG_EVAL_ROOT"),
        dataset_dir=locator(namespace.dataset_dir, "VERILOG_EVAL_DATASET"),
        problems_file=locator(namespace.problems_file, "VERILOG_EVAL_PROBLEMS"),
        build_root=locator(namespace.build_root, "VERILOG_EVAL_BUILD_ROOT"),
        agent_tools=locator(namespace.agent_tools, "AGENT_EVAL_AGENT_TOOLS"),
        docker_path=locator(namespace.docker_path, "AGENT_EVAL_DOCKER"),
        docker_image=namespace.docker_image,
        docker_archive=locator(namespace.docker_archive, "AGENT_EVAL_DOCKER_ARCHIVE"),
        run_path_file=(namespace.run_path_file.resolve() if namespace.run_path_file else None),
        management_action=management_action,
        recovery_digest=recovery_digest,
        check_host_contamination=namespace.check_host_contamination,
        **material,
    )


def manage_recovery(options: RunnerOptions):
    """Perform one explicit receipt operation without starting evaluation."""

    action = options.management_action
    if action not in {"list", "resume", "abandon"}:
        raise RunnerError("no recovery management action was selected")
    if options.build_root is None:
        raise RunnerError("recovery management requires --build-root")
    synthesize_orphan_recovery_receipts(options.build_root)
    if action == "list":
        return list_recovery_receipts(options.build_root)
    digest = options.recovery_digest
    if digest is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RunnerError("recovery digest must be 64 lowercase hex characters")
    receipts = {
        receipt["config_sha256"]: receipt
        for receipt in list_recovery_receipts(options.build_root)
    }
    if digest not in receipts:
        raise RunnerError("recovery receipt does not exist")
    if action == "abandon":
        return abandon_recovery(options.build_root, digest)
    config_path = options.build_root / digest / "run-config.json"
    load_run_config(config_path)
    return config_path


_RUNTIME_SUFFIXES = frozenset(
    {
        ".py",
        ".pyc",
        ".pth",
        ".sh",
        ".bash",
        ".ac",
        ".m4",
        ".mk",
        ".in",
        ".json",
        ".toml",
        ".sv",
        ".v",
        ".txt",
    }
)


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def assert_clean_source(
    source_root: Path,
    *,
    allowed_roots: Sequence[Path],
    git_path: str = "git",
) -> None:
    """Reject tracked dirtiness and ambient runtime-affecting source files."""

    root = Path(source_root).resolve(strict=True)
    allowed = tuple(Path(path).resolve(strict=False) for path in allowed_roots)
    try:
        result = subprocess.run(
            (
                git_path,
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ),
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise RunnerError(f"cannot inspect source tree: {error}") from error
    if result.returncode != 0:
        raise RunnerError("source root is not a readable Git worktree")
    records = [record for record in result.stdout.split(b"\0") if record]
    for record in records:
        if len(record) < 4 or record[2:3] != b" ":
            raise RunnerError("Git returned malformed source status")
        status_text = record[:2]
        relative = record[3:].decode("utf-8", errors="surrogateescape")
        if status_text not in {b"??", b"!!"}:
            raise RunnerError(f"tracked source is dirty: {relative}")
        candidate = (root / relative.rstrip("/")).resolve(strict=False)
        if _under(candidate, allowed):
            continue
        basename = candidate.name
        runtime_affecting = (
            candidate.suffix.lower() in _RUNTIME_SUFFIXES
            or basename in {"configure", "Makefile", "Makefile.in", "sitecustomize.py", "usercustomize.py"}
        )
        try:
            runtime_affecting = runtime_affecting or bool(
                candidate.lstat().st_mode & 0o111
            )
        except OSError:
            pass
        if runtime_affecting:
            raise RunnerError(
                f"untracked or ignored runtime-affecting source entry: {relative}"
            )


def _file_input_identity(path: Path, *, kind: str, name: Optional[str] = None) -> dict:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RunnerError(f"selected benchmark input is missing: {path.name}") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RunnerError(f"selected benchmark input is not regular: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as input_file:
            for block in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise RunnerError(f"cannot hash selected input {path.name}: {error}") from error
    return {
        "kind": kind,
        "name": name or path.name,
        "sha256": digest.hexdigest(),
        "size_bytes": metadata.st_size,
    }


def collect_input_manifest(
    *,
    source_root: Path,
    dataset_dir: Path,
    problems_file: Path,
    task: str,
    rules: bool,
    examples: int,
) -> tuple[tuple[str, ...], tuple[dict, ...]]:
    """Hash every selected public and hidden input without scheduling any work."""

    source = Path(source_root).resolve(strict=True)
    dataset = Path(dataset_dir).resolve(strict=True)
    problems_path = Path(problems_file).resolve(strict=True)
    try:
        lines = problems_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RunnerError(f"cannot read problems file: {error}") from error
    problems = tuple(line.strip() for line in lines if line.strip())
    if not problems or len(set(problems)) != len(problems):
        raise RunnerError("problems file must contain unique sample-safe IDs")
    if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", item) is None for item in problems):
        raise RunnerError("problems file contains an unsafe ID")

    manifest: list[dict] = [
        _file_input_identity(problems_path, kind="problem_list")
    ]
    for problem in problems:
        manifest.append(
            _file_input_identity(
                dataset / f"{problem}_prompt.txt",
                kind="prompt",
            )
        )
        if task == "code-complete-iccad2023":
            manifest.append(
                _file_input_identity(
                    dataset / f"{problem}_ifc.txt",
                    kind="public_starter",
                )
            )
        manifest.append(
            _file_input_identity(
                dataset / f"{problem}_test.sv",
                kind="hidden_test",
            )
        )
        manifest.append(
            _file_input_identity(
                dataset / f"{problem}_ref.sv",
                kind="hidden_reference",
            )
        )
    if examples:
        manifest.append(
            _file_input_identity(
                source
                / "scripts"
                / f"verilog-example-prefix_{task}_{examples}-shot.txt",
                kind="example",
            )
        )
    rules_text = selected_rules(task, rules)
    if rules_text is not None:
        content = rules_text.encode("utf-8")
        manifest.append(
            {
                "kind": "rules",
                "name": "selected-rules.txt",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return problems, tuple(manifest)


def _resolved_executable(
    name: str,
    *,
    explicit: Optional[Path],
    path_environment: str,
) -> Path:
    if explicit is not None:
        candidate = Path(explicit).resolve(strict=True)
    else:
        located = shutil.which(name, path=path_environment)
        if located is None:
            raise RunnerError(f"required executable is unavailable: {name}")
        candidate = Path(located).resolve(strict=True)
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RunnerError(f"required executable is not executable: {name}")
    return candidate


def _run_checked(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: float = 30,
) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            tuple(command),
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RunnerError(f"external preparation command failed: {command[0]}") from error
    if completed.returncode != 0:
        raise RunnerError(f"external preparation command returned nonzero: {command[0]}")
    return completed


def collect_preparation_evidence(
    options: RunnerOptions,
    *,
    environment: Mapping[str, str],
) -> tuple[PreparationEvidence, str]:
    """Resolve external resources and identities without invoking benchmark work."""

    environ = dict(environment)
    source = options.source_root
    if source is None:
        raise RunnerError("source root must be supplied explicitly")
    source = source.resolve(strict=True)
    build_root = options.build_root or (source / "build")
    dataset = options.dataset_dir or (source / f"dataset_{options.task}")
    problems_file = options.problems_file or (dataset / "problems.txt")
    tools_prefix = options.agent_tools
    if tools_prefix is None:
        raise RunnerError("explicit Agent tools prefix is required")
    path_environment = environ.get("PATH", "")
    git_path = _resolved_executable(
        "git", explicit=None, path_environment=path_environment
    )
    docker_path = _resolved_executable(
        "docker",
        explicit=options.docker_path,
        path_environment=environ.get("PATH", ""),
    )
    image = options.docker_image
    archive = options.docker_archive
    if not image or archive is None:
        raise RunnerError("Docker image reference and pinned archive are required")
    archive = archive.resolve(strict=True)

    cache_root = Path(
        environ.get(
            "VERILOG_EVAL_CACHE_ROOT",
            str(build_root.parent / ".verilog-eval-cache"),
        )
    ).resolve()
    assert_clean_source(
        source,
        allowed_roots=(build_root, cache_root, tools_prefix),
        git_path=str(git_path),
    )
    problems, inputs = collect_input_manifest(
        source_root=source,
        dataset_dir=dataset,
        problems_file=problems_file,
        task=options.task,
        rules=options.rules,
        examples=options.examples,
    )

    credential = environ.get(options.api_key_environment)
    if not credential:
        raise RunnerError(
            f"selected credential environment is missing: {options.api_key_environment}"
        )
    endpoint_evidence = preflight_endpoint(
        base_url=options.base_url,
        model=options.model,
        api_key=credential,
        timeout_seconds=min(15.0, float(options.timeout_seconds)),
        ca_bundle=(Path(environ["SSL_CERT_FILE"]) if environ.get("SSL_CERT_FILE") else None),
    )

    try:
        projection = project_agent_tools(
            tools_prefix,
            cache_root / "agent-tools-projections",
            options.agent,
        )
    except (OSError, ToolsProjectionError) as error:
        raise RunnerError(f"cannot prepare Agent tools projection: {error}") from error

    executables = {
        "python": Path(sys.executable).resolve(strict=True),
        "bash": _resolved_executable("bash", explicit=None, path_environment=path_environment),
        "make": _resolved_executable("make", explicit=None, path_environment=path_environment),
        "iverilog": _resolved_executable("iverilog", explicit=None, path_environment=path_environment),
        "timeout": _resolved_executable("timeout", explicit=None, path_environment=path_environment),
        "docker": docker_path,
        "git": git_path,
    }
    try:
        toolchain_identities = tuple(
            executable_identity(name, path)
            for name, path in sorted(executables.items())
        )
    except AgentToolsError as error:
        raise RunnerError(f"cannot identify host toolchain: {error}") from error

    support_paths: dict[str, Path] = {}
    if environ.get("SSL_CERT_FILE"):
        support_paths["ca-bundle"] = Path(environ["SSL_CERT_FILE"]).resolve(strict=True)
    resolver = Path("/etc/resolv.conf")
    if resolver.exists():
        support_paths["resolver"] = resolver
    try:
        support_identities = tuple(
            support_file_identity(name, path)
            for name, path in sorted(support_paths.items())
        )
    except AgentToolsError as error:
        raise RunnerError(f"cannot identify support files: {error}") from error

    docker_host = environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    docker_env = docker_environment(
        docker_host=docker_host,
        path=str(docker_path.parent),
    )
    _run_checked(
        (str(docker_path), "load", "--input", str(archive)),
        environment=docker_env,
        timeout=120,
    )
    inspected = _run_checked(
        (
            str(docker_path),
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
        ),
        environment=docker_env,
    ).stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", inspected) is None:
        raise RunnerError("Docker image content ID is invalid")
    try:
        daemon_identity = docker_daemon_identity(
            str(docker_path),
            environment=docker_env,
        )
    except AgentToolsError as error:
        raise RunnerError(f"cannot identify Docker daemon: {error}") from error

    git_environment = _base_environment(path_environment)
    source_commit = _run_checked(
        (str(git_path), "-C", str(source), "rev-parse", "HEAD"),
        environment=git_environment,
    ).stdout.strip()
    support_bindings = {
        name.replace("-", "_"): str(path) for name, path in support_paths.items()
    }
    runtime_bindings = {
        "source_root": str(source),
        "dataset_dir": str(dataset.resolve(strict=True)),
        "build_dir": str(build_root.resolve()),
        "docker": {
            "client": str(docker_path),
            "daemon": docker_host,
            "image": image,
            "archive": str(archive),
        },
        "tools_projection": str(projection.path),
        "toolchain": {name: str(path) for name, path in executables.items() if name != "python"},
        "support_files": support_bindings,
        "credential_broker": ".credential.sock",
    }
    return (
        PreparationEvidence(
            source_commit=source_commit,
            problems=problems,
            inputs=inputs,
            docker_image_id=inspected,
            docker_daemon_identity=daemon_identity,
            tools_content_sha256=projection.content_sha256,
            tools_source_content_sha256=projection.source_content_sha256,
            tools_lock_sha256=projection.lock_sha256,
            tools_versions=projection.versions,
            toolchain_identities=toolchain_identities,
            support_identities=support_identities,
            endpoint_evidence=endpoint_evidence,
            runtime_bindings=runtime_bindings,
        ),
        credential,
    )


def _material_config(
    options: RunnerOptions,
    evidence: PreparationEvidence,
    *,
    nonce: Optional[str],
) -> dict:
    return {
        "agent": {
            "name": options.agent,
            "model": options.model,
            "thinking": options.thinking,
            "toolset": options.toolset,
        },
        "benchmark": {
            "task": options.task,
            "samples": options.samples,
            "examples": options.examples,
            "rules": options.rules,
            "problems": list(evidence.problems),
            "inputs": list(evidence.inputs),
        },
        "endpoint": {
            "base_url": options.base_url,
            "api_key_environment": options.api_key_environment,
        },
        "limits": {
            "timeout_seconds": options.timeout_seconds,
            "max_turns": options.max_turns,
            "max_tool_calls": options.max_tool_calls,
            "max_input_tokens": options.max_input_tokens,
            "max_output_tokens": options.max_output_tokens,
        },
        "runtime": {
            "source_commit": evidence.source_commit,
            "docker_image_id": evidence.docker_image_id,
            "docker_daemon_identity": evidence.docker_daemon_identity,
            "agent_tools": {
                "content_sha256": evidence.tools_content_sha256,
                "source_content_sha256": evidence.tools_source_content_sha256,
                "lock_sha256": evidence.tools_lock_sha256,
                "versions": dict(evidence.tools_versions),
            },
            "toolchain": list(evidence.toolchain_identities),
            "support_files": list(evidence.support_identities),
        },
        "jobs": options.jobs,
        "nonce": nonce,
    }


def prepare_run(
    options: RunnerOptions,
    evidence: PreparationEvidence,
    *,
    credential: str,
) -> PreparedRun:
    """Resolve one immutable run without invoking configure, Make, or a sample."""

    if not credential:
        raise RunnerError("selected credential value is missing")
    if not evidence.problems or not evidence.inputs:
        raise RunnerError("selected benchmark input manifest is empty")
    endpoint_digest = evidence.endpoint_evidence.get("response_sha256")
    if not isinstance(endpoint_digest, str) or re.fullmatch(
        r"[0-9a-f]{64}", endpoint_digest
    ) is None:
        raise RunnerError("endpoint evidence is invalid")

    if options.mode == "resume":
        if options.resume_config is None:
            raise RunnerError("resume mode has no config path")
        loaded = load_run_config(options.resume_config)
        candidate = _material_config(options, evidence, nonce=loaded["nonce"])
        if canonical_run_config(candidate, forbidden_values=(credential,)) != canonical_run_config(loaded):
            raise RunnerError("runtime locators do not resolve to the resumed material identity")
        config_path = options.resume_config
        config = loaded
        build_root = config_path.parent.parent
    else:
        build_root = options.build_root
        if build_root is None:
            raise RunnerError("build root is required for a new run")
        if options.mode == "new-run":
            synthesize_orphan_recovery_receipts(build_root)
            unacknowledged = [
                receipt
                for receipt in list_recovery_receipts(build_root)
                if not receipt["acknowledged"]
            ]
            if unacknowledged:
                raise RunnerError(
                    "unacknowledged new-run recovery must be resumed or abandoned"
                )
            nonce = secrets.token_hex(16)
        else:
            nonce = None
        config = _material_config(options, evidence, nonce=nonce)
        try:
            with lifecycle_lock(build_root, exclusive=False):
                config_path = publish_run_config(build_root, config)
        except ValueError as error:
            raise RunnerError(f"cannot publish run configuration: {error}") from error
        if options.mode == "new-run":
            publish_recovery_receipt(build_root, config_path.parent.name)

    digest = config_path.parent.name
    bindings = json.loads(json.dumps(evidence.runtime_bindings))
    bindings["run_config_sha256"] = digest
    bindings["build_dir"] = str(config_path.parent)
    validate_runtime_bindings(bindings, expected_config_digest=digest)
    return PreparedRun(
        config_path=config_path,
        config=config,
        digest=digest,
        bindings=bindings,
        endpoint_evidence=json.loads(json.dumps(evidence.endpoint_evidence)),
        credential=credential,
    )


def _base_environment(path: str) -> dict[str, str]:
    return {"PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def runner_environment(
    ambient: Mapping[str, str],
    *,
    path: str,
    home: str,
    api_key_environment: str,
) -> dict[str, str]:
    """Construct the runner environment without ambient startup/proxy state."""

    if api_key_environment not in ambient or not ambient[api_key_environment]:
        raise RunnerError(f"selected credential environment is missing: {api_key_environment}")
    result = _base_environment(path)
    result["HOME"] = home
    certificate = ambient.get("SSL_CERT_FILE")
    if certificate:
        result["SSL_CERT_FILE"] = certificate
    result[api_key_environment] = ambient[api_key_environment]
    return result


def configure_environment(
    toolchain: Mapping[str, str],
    *,
    home: str,
) -> dict[str, str]:
    result = _base_environment(toolchain["PATH"])
    result["SHELL"] = toolchain["SHELL"]
    result["HOME"] = home
    return result


def make_environment(toolchain: Mapping[str, str]) -> dict[str, str]:
    result = _base_environment(toolchain["PATH"])
    result["SHELL"] = toolchain["SHELL"]
    return result


def docker_environment(*, docker_host: str, path: str) -> dict[str, str]:
    result = _base_environment(path)
    result["DOCKER_HOST"] = docker_host
    return result
