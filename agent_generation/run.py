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
from typing import Callable, Mapping, Optional, Sequence

from agent_generation.credentials import CredentialBroker, CredentialError
from agent_generation.endpoint import preflight_endpoint
from agent_generation.lifecycle import (
    abandon_recovery,
    acknowledge_recovery,
    lifecycle_lock,
    list_recovery_receipts,
    publish_recovery_receipt,
    run_lock,
    synthesize_orphan_recovery_receipts,
)
from agent_generation.report import (
    ReportError,
    ReportTransactionError,
    build_agent_report,
    validate_report_pair,
    write_agent_report,
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
from agent_generation.runtime_bindings import (
    RuntimeBindingError,
    publish_runtime_bindings,
    remove_runtime_bindings,
    validate_runtime_bindings,
)
from agent_generation.sample_result import (
    SampleInfrastructureError,
    inspect_sample_bundle_state,
)
from agent_generation.task import selected_rules
from agent_generation.tools import (
    ToolsProjectionError,
    project_agent_tools,
    validate_tools_projection,
)


_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")
_MAKE_SAFE_LOCATOR = re.compile(r"/[A-Za-z0-9._/@+=-]*")
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
_FORMAL_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "SSL_CERT_FILE",
        "DOCKER_HOST",
        "VERILOG_EVAL_ROOT",
        "VERILOG_EVAL_BUILD_ROOT",
        "VERILOG_EVAL_DATASET",
        "VERILOG_EVAL_PROBLEMS",
        "VERILOG_EVAL_CACHE_ROOT",
        "VERILOG_EVAL_JOBS",
        "AGENT_EVAL_AGENT_TOOLS",
        "AGENT_EVAL_DOCKER",
        "AGENT_EVAL_DOCKER_IMAGE_STANDARD",
        "AGENT_EVAL_DOCKER_ARCHIVE_STANDARD",
        "AGENT_EVAL_DOCKER_IMAGE_RTL",
        "AGENT_EVAL_DOCKER_ARCHIVE_RTL",
    }
)


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
        if _MAKE_SAFE_LOCATOR.fullmatch(str(resume_config)) is None:
            parser.error("resume config path is not Make-safe")
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
    credential_environment = material["api_key_environment"]
    if (
        credential_environment.startswith(("GIT_", "PYTHON", "DYLD_", "NPM_"))
        or credential_environment.endswith("_PROXY")
        or credential_environment
        in {
            "PATH",
            "HOME",
            "SHELL",
            "LANG",
            "LC_ALL",
            "DOCKER_HOST",
            "PYTHONPATH",
            "BASH_ENV",
            "ENV",
            "NODE_OPTIONS",
            "VERILOG_EVAL_JOBS",
            "VERILOG_EVAL_ROOT",
            "AGENT_EVAL_AGENT_TOOLS",
        }
    ):
        parser.error("credential environment name collides with structural runtime state")

    def locator(
        argument,
        environment_name: str,
        *,
        require_make_safe: bool = False,
    ) -> Optional[Path]:
        value = argument if argument is not None else environ.get(environment_name)
        if value is None or value == "":
            return None
        resolved = Path(value).resolve()
        if require_make_safe and _MAKE_SAFE_LOCATOR.fullmatch(str(resolved)) is None:
            parser.error(f"{environment_name} is not a Make-safe locator")
        return resolved

    source_root = locator(
        namespace.source_root,
        "VERILOG_EVAL_ROOT",
        require_make_safe=True,
    )
    build_root = locator(
        namespace.build_root,
        "VERILOG_EVAL_BUILD_ROOT",
        require_make_safe=True,
    )
    if build_root is None and source_root is not None and mode != "resume":
        build_root = source_root / "build"

    return RunnerOptions(
        mode=mode,
        resume_config=resume_config,
        jobs=resolved_jobs,
        source_root=source_root,
        dataset_dir=locator(
            namespace.dataset_dir,
            "VERILOG_EVAL_DATASET",
            require_make_safe=True,
        ),
        problems_file=locator(
            namespace.problems_file,
            "VERILOG_EVAL_PROBLEMS",
            require_make_safe=True,
        ),
        build_root=build_root,
        agent_tools=locator(namespace.agent_tools, "AGENT_EVAL_AGENT_TOOLS"),
        docker_path=locator(namespace.docker_path, "AGENT_EVAL_DOCKER"),
        docker_image=(
            namespace.docker_image
            or environ.get(
                f"AGENT_EVAL_DOCKER_IMAGE_{str(material['toolset']).upper()}"
            )
        ),
        docker_archive=locator(
            namespace.docker_archive,
            f"AGENT_EVAL_DOCKER_ARCHIVE_{str(material['toolset']).upper()}",
        ),
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
            try:
                root.relative_to(path)
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
    git_executable = git_path
    if not Path(git_path).is_absolute():
        located_git = shutil.which(git_path)
        if located_git is None:
            raise RunnerError("Git executable is unavailable")
        git_executable = located_git
    try:
        result = subprocess.run(
            (
                git_executable,
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
            env={
                "PATH": str(Path(git_executable).parent),
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
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
        "docker": docker_path,
        "git": git_path,
        "shebang-env": _resolved_executable(
            "shebang-env",
            explicit=Path("/usr/bin/env"),
            path_environment=path_environment,
        ),
        "configure-sh": _resolved_executable(
            "configure-sh",
            explicit=Path("/bin/sh"),
            path_environment=path_environment,
        ),
    }
    for executable_name in (
        "bash",
        "make",
        "iverilog",
        "timeout",
        "column",
        "sed",
        "seq",
        "expr",
        "tee",
        "mkdir",
        "rm",
        "cp",
        "chmod",
        "mv",
        "grep",
    ):
        executables[executable_name] = _resolved_executable(
            executable_name,
            explicit=None,
            path_environment=path_environment,
        )
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
    for support_name, support_path in (
        ("docker-hosts", Path("/etc/hosts")),
        ("docker-hostname", Path("/etc/hostname")),
        ("docker-resolver", Path("/etc/resolv.conf")),
    ):
        if support_path.exists():
            support_paths[support_name] = support_path
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
        "problems_file": str(Path(problems_file).resolve(strict=True)),
        "build_dir": str(build_root.resolve()),
        "docker": {
            "client": str(docker_path),
            "daemon": docker_host,
            "image": image,
            "archive": str(archive),
        },
        "tools_source": str(Path(tools_prefix).resolve(strict=True)),
        "tools_projection": str(projection.path),
        "toolchain": {name: str(path) for name, path in executables.items()},
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


def verify_selected_inputs(config: Mapping, bindings: Mapping) -> None:
    """Recheck every selected input identity before accepting grading evidence."""

    source = Path(bindings["source_root"])
    dataset = Path(bindings["dataset_dir"])
    problems_file = Path(bindings["problems_file"])
    for expected in config["benchmark"]["inputs"]:
        kind = expected["kind"]
        name = expected["name"]
        if kind == "problem_list":
            path = problems_file
        elif kind == "example":
            path = source / "scripts" / name
        elif kind == "rules":
            rules_text = selected_rules(
                config["benchmark"]["task"],
                config["benchmark"]["rules"],
            )
            content = b"" if rules_text is None else rules_text.encode("utf-8")
            actual = {
                "kind": kind,
                "name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
            if actual != expected:
                raise RunnerError("selected rules identity changed")
            continue
        else:
            path = dataset / name
        actual = _file_input_identity(path, kind=kind, name=name)
        if actual != expected:
            raise RunnerError(f"selected benchmark input changed: {name}")


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
            "models_response_sha256": evidence.endpoint_evidence[
                "response_sha256"
            ],
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


def formal_runner_environment(
    ambient: Mapping[str, str],
    *,
    api_key_environment: str,
) -> dict[str, str]:
    """Admit only formal bindings plus the explicitly selected credential."""

    admitted = _FORMAL_ENVIRONMENT_NAMES | {api_key_environment}
    return {
        name: value
        for name, value in ambient.items()
        if name in admitted and isinstance(value, str)
    }


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


def _canonical_evidence(value: Mapping) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise RunnerError(f"endpoint evidence is not canonical JSON: {error}") from error
    return (text + "\n").encode("utf-8")


def _publish_endpoint_evidence(prepared: PreparedRun) -> Path:
    path = prepared.config_path.with_name("endpoint-evidence.json")
    content = _canonical_evidence(prepared.endpoint_evidence)
    if path.exists() or path.is_symlink():
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                existing = os.read(descriptor, 1024 * 1024 + 1)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise RunnerError("existing endpoint evidence is unsafe") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or existing != content
        ):
            raise RunnerError("existing endpoint evidence does not match preflight")
        return path
    directory = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        os.fsync(directory)
    except OSError as error:
        raise RunnerError(f"cannot publish endpoint evidence: {error}") from error
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return path


def validate_runtime_material_identity(prepared: PreparedRun) -> None:
    """Bind every ephemeral locator back to the immutable material identity."""

    bindings = prepared.bindings
    expected_toolchain = {
        item["name"]: item for item in prepared.config["runtime"]["toolchain"]
    }
    actual_names = set(bindings["toolchain"])
    if actual_names != set(expected_toolchain):
        raise RunnerError("runtime toolchain locator set does not match run config")
    try:
        actual_toolchain = {
            name: executable_identity(name, Path(path))
            for name, path in bindings["toolchain"].items()
        }
        for name, expected in expected_toolchain.items():
            if actual_toolchain[name] != expected:
                raise RunnerError(f"runtime executable identity changed: {name}")

        for expected in prepared.config["runtime"]["support_files"]:
            key = expected["name"].replace("-", "_")
            locator = bindings["support_files"].get(key)
            if locator is None or support_file_identity(
                expected["name"], Path(locator)
            ) != expected:
                raise RunnerError(
                    f"runtime support-file identity changed: {expected['name']}"
                )

        projection_path = Path(bindings["tools_projection"])
        reprojection = project_agent_tools(
            Path(bindings["tools_source"]),
            projection_path.parent,
            prepared.config["agent"]["name"],
        )
        expected_tools = prepared.config["runtime"]["agent_tools"]
        if (
            reprojection.path != projection_path
            or reprojection.content_sha256 != expected_tools["content_sha256"]
            or reprojection.source_content_sha256
            != expected_tools["source_content_sha256"]
            or reprojection.lock_sha256 != expected_tools["lock_sha256"]
            or reprojection.versions != expected_tools["versions"]
        ):
            raise RunnerError("runtime Agent tools source identity changed")
        validate_tools_projection(
            projection_path,
            expected_tools["content_sha256"],
        )
    except (AgentToolsError, ToolsProjectionError, OSError) as error:
        raise RunnerError(f"runtime material identity validation failed: {error}") from error

    docker = bindings["docker"]
    docker_env = docker_environment(
        docker_host=docker["daemon"],
        path=str(Path(docker["client"]).parent),
    )
    image_id = _run_checked(
        (
            docker["client"],
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            docker["image"],
        ),
        environment=docker_env,
    ).stdout.strip()
    if image_id != prepared.config["runtime"]["docker_image_id"]:
        raise RunnerError("runtime Docker image identity changed")
    try:
        daemon = docker_daemon_identity(
            docker["client"],
            environment=docker_env,
        )
    except AgentToolsError as error:
        raise RunnerError(f"runtime Docker daemon identity failed: {error}") from error
    if daemon != prepared.config["runtime"]["docker_daemon_identity"]:
        raise RunnerError("runtime Docker daemon identity changed")

    git = bindings["toolchain"]["git"]
    assert_clean_source(
        Path(bindings["source_root"]),
        allowed_roots=(
            Path(bindings["build_dir"]),
            Path(bindings["tools_source"]),
            Path(bindings["tools_projection"]),
        ),
        git_path=git,
    )
    revision = _run_checked(
        (git, "-C", bindings["source_root"], "rev-parse", "HEAD"),
        environment=_base_environment(str(Path(git).parent)),
    ).stdout.strip()
    if revision != prepared.config["runtime"]["source_commit"]:
        raise RunnerError("runtime source revision changed")
    verify_selected_inputs(prepared.config, bindings)


def _pinned_path(bindings: Mapping) -> str:
    directories = {
        str(Path(path).parent)
        for name, path in bindings["toolchain"].items()
        if name not in {"shebang-env", "configure-sh"}
    }
    directories.add(str(Path(bindings["docker"]["client"]).parent))
    return os.pathsep.join(sorted(directories))


def _expected_sample_ids(config: Mapping) -> tuple[str, ...]:
    return tuple(
        f"{problem}_sample{sample_number:02d}"
        for problem in config["benchmark"]["problems"]
        for sample_number in range(1, config["benchmark"]["samples"] + 1)
    )


def _validate_existing_bundles(prepared: PreparedRun) -> None:
    run_dir = prepared.config_path.parent
    for sample_id in _expected_sample_ids(prepared.config):
        problem = sample_id.rsplit("_sample", 1)[0]
        output = run_dir / problem / f"{sample_id}.sv"
        try:
            inspect_sample_bundle_state(
                output,
                prepared.digest,
                remove_valid_partial=True,
            )
        except SampleInfrastructureError as error:
            raise RunnerError(
                f"existing or partial Sample Bundle is corrupt: {sample_id}"
            ) from error


def _remove_empty_runtime_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or any(path.iterdir())
    ):
        raise RunnerError(f"runtime directory is unsafe or nonempty: {path.name}")
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.rmdir(path.name, dir_fd=directory)
        os.fsync(directory)
    except OSError as error:
        raise RunnerError(f"cannot remove runtime directory: {path.name}") from error
    finally:
        os.close(directory)


def _remove_regular_marker(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise RunnerError("existing report marker is nonregular")
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)
    except OSError as error:
        raise RunnerError("cannot clear stale report marker") from error
    finally:
        os.close(directory)


def _complete_report(prepared: PreparedRun) -> Optional[Path]:
    run_dir = prepared.config_path.parent
    marker = run_dir / "agent-summary.json"
    text = run_dir / "agent-summary.txt"
    summary = run_dir / "summary.csv"
    if not marker.exists() and not marker.is_symlink():
        return None
    try:
        committed = validate_report_pair(
            summary,
            marker,
            text,
            prepared.digest,
        )
        rebuilt = build_agent_report(
            summary,
            run_config_sha256=prepared.digest,
            expected_problems=prepared.config["benchmark"]["problems"],
            expected_samples_per_problem=prepared.config["benchmark"]["samples"],
            expected_config=prepared.config,
            endpoint_evidence_sha256=prepared.endpoint_evidence["response_sha256"],
        )
    except (ReportError, ReportTransactionError):
        _remove_regular_marker(marker)
        return None
    payload = dict(committed)
    payload.pop("evidence", None)
    if payload != rebuilt:
        raise RunnerError("existing completed report does not match current bundles")
    return marker


def _invoke_process(
    command_runner: Callable,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    try:
        completed = command_runner(
            tuple(command),
            cwd=str(cwd),
            env=dict(environment),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RunnerError(f"cannot execute {Path(command[0]).name}") from error
    if completed.returncode != 0:
        raise RunnerError(
            f"{Path(command[0]).name} returned nonzero: {completed.returncode}"
        )


def execute_prepared_run(
    prepared: PreparedRun,
    options: RunnerOptions,
    *,
    command_runner: Callable = subprocess.run,
    broker_factory: Callable = CredentialBroker,
    material_validator: Callable[[PreparedRun], None] = validate_runtime_material_identity,
) -> Path:
    """Hold one run lock while configure and GNU Make execute exactly once."""

    run_dir = prepared.config_path.parent
    with run_lock(run_dir):
        existing = _complete_report(prepared)
        if existing is not None:
            return existing
        _validate_existing_bundles(prepared)
        material_validator(prepared)
        _remove_regular_marker(run_dir / "agent-summary.json")
        _publish_endpoint_evidence(prepared)
        try:
            publish_runtime_bindings(prepared.config_path, prepared.bindings)
        except RuntimeBindingError as error:
            raise RunnerError(f"cannot publish runtime bindings: {error}") from error

        bindings = prepared.bindings
        pinned_path = _pinned_path(bindings)
        configure_home = run_dir / ".configure-home"
        configure_home.mkdir(mode=0o700, exist_ok=True)
        configure_env = configure_environment(
            {
                "PATH": pinned_path,
                "SHELL": bindings["toolchain"]["bash"],
            },
            home=str(configure_home),
        )
        make_env = make_environment(
            {
                "PATH": pinned_path,
                "SHELL": bindings["toolchain"]["bash"],
            }
        )
        configure_command = [
            str(Path(bindings["source_root"]) / "configure"),
            "--with-generator=agent",
            f"--with-generator-config={prepared.config_path}",
            f"--with-task={prepared.config['benchmark']['task']}",
            f"--with-samples={prepared.config['benchmark']['samples']}",
            f"--with-examples={prepared.config['benchmark']['examples']}",
            f"--with-dataset={bindings['dataset_dir']}",
            f"--with-problems={bindings['problems_file']}",
        ]
        if prepared.config["benchmark"]["rules"]:
            configure_command.append("--with-rules")
        try:
            _invoke_process(
                command_runner,
                configure_command,
                cwd=run_dir,
                environment=configure_env,
            )
            with broker_factory(
                run_dir=run_dir,
                config_digest=prepared.digest,
                environment_name=prepared.config["endpoint"]["api_key_environment"],
                secret=prepared.credential,
                expected_sample_ids=_expected_sample_ids(prepared.config),
            ):
                _invoke_process(
                    command_runner,
                    (
                        bindings["toolchain"]["make"],
                        f"--jobs={prepared.config['jobs']}",
                        f"SHELL={bindings['toolchain']['bash']}",
                    ),
                    cwd=run_dir,
                    environment=make_env,
                )
            verify_selected_inputs(prepared.config, bindings)
        finally:
            cleanup_errors: list[str] = []
            try:
                remove_runtime_bindings(prepared.config_path)
            except RuntimeBindingError as error:
                cleanup_errors.append(f"runtime bindings: {error}")
            for runtime_directory in (
                configure_home,
                run_dir / ".agent-work",
            ):
                try:
                    _remove_empty_runtime_directory(runtime_directory)
                except (OSError, RunnerError) as error:
                    cleanup_errors.append(f"{runtime_directory.name}: {error}")
            if cleanup_errors:
                raise RunnerError(
                    "runtime cleanup failed: " + "; ".join(cleanup_errors)
                )

        summary = run_dir / "summary.csv"
        report = build_agent_report(
            summary,
            run_config_sha256=prepared.digest,
            expected_problems=prepared.config["benchmark"]["problems"],
            expected_samples_per_problem=prepared.config["benchmark"]["samples"],
            expected_config=prepared.config,
            endpoint_evidence_sha256=prepared.endpoint_evidence["response_sha256"],
        )
        marker = run_dir / "agent-summary.json"
        write_agent_report(
            report,
            summary_csv=summary,
            run_config_sha256=prepared.digest,
            json_path=marker,
            text_path=run_dir / "agent-summary.txt",
        )
        validate_report_pair(
            summary,
            marker,
            run_dir / "agent-summary.txt",
            prepared.digest,
        )
        return marker


def _write_run_path_file(path: Path, content: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_size != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RunnerError("run path file may replace only an owned empty mode-0600 file")
    directory = os.open(
        target.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    temporary = f".{target.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary,
            target.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except OSError as error:
        raise RunnerError(f"cannot publish run path file: {error}") from error
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def acknowledge_run_path(
    prepared: PreparedRun,
    options: RunnerOptions,
    *,
    stream=None,
) -> None:
    """Durably deliver the run path, then acknowledge any nonce receipt."""

    output = sys.stdout if stream is None else stream
    content = (str(prepared.config_path.parent) + "\n").encode("utf-8")
    if options.run_path_file is not None:
        _write_run_path_file(options.run_path_file, content)
    try:
        output.write(content.decode("utf-8"))
        output.flush()
    except (OSError, UnicodeError) as error:
        raise RunnerError("cannot acknowledge run path on stdout") from error
    if prepared.config["nonce"] is not None:
        build_root = prepared.config_path.parent.parent
        synthesize_orphan_recovery_receipts(build_root)
        acknowledge_recovery(build_root, prepared.digest)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> int:
    """Prepare, acknowledge, and execute one complete Agent evaluation run."""

    environ = dict(os.environ if environment is None else environment)
    try:
        options = parse_runner_options(argv, environment=environ)
        environ = formal_runner_environment(
            environ,
            api_key_environment=options.api_key_environment,
        )
        if environment is None:
            os.environ.clear()
            os.environ.update(environ)
        if options.management_action == "contamination":
            source = options.source_root
            if source is None:
                raise RunnerError("contamination check requires a source root")
            allowed = tuple(
                path
                for path in (options.build_root, options.agent_tools)
                if path is not None
            )
            assert_clean_source(source, allowed_roots=allowed)
            return 0
        if options.management_action in {"list", "abandon", "resume"}:
            result = manage_recovery(options)
            if options.management_action == "list":
                print(json.dumps(result, sort_keys=True, separators=(",", ":")))
                return 0
            if options.management_action == "abandon":
                print(result)
                return 0
            config_path = Path(result)
            config = load_run_config(config_path)
            prepared = PreparedRun(
                config_path=config_path,
                config=config,
                digest=config_path.parent.name,
                bindings={},
                endpoint_evidence={},
                credential="",
            )
            acknowledge_run_path(prepared, options)
            return 0

        evidence, credential = collect_preparation_evidence(
            options,
            environment=environ,
        )
        prepared = prepare_run(options, evidence, credential=credential)
        acknowledge_run_path(prepared, options)
        marker = execute_prepared_run(prepared, options)
        print(marker)
        return 0
    except (RunnerError, CredentialError, ReportError, ReportTransactionError) as error:
        print(f"ERROR: Agent evaluation infrastructure failed: {error}", file=sys.stderr)
        return 3
    except (ValueError, OSError) as error:
        print(f"ERROR: invalid Agent evaluation request: {error}", file=sys.stderr)
        return 2
