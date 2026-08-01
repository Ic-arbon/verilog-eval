"""Thin Make-facing CLI for one immutable Agent sample."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

from agent_generation.credentials import CredentialError
from agent_generation.docker import DockerInfrastructureError
from agent_generation.run_config import RunConfigError
from agent_generation.runtime_bindings import RuntimeBindingError
from agent_generation.sample import SampleRequestError, generate_agent_sample
from agent_generation.sample_result import SampleInfrastructureError


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one VerilogEval Agent sample from immutable run config"
    )
    parser.add_argument("--run-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("prompt_filename", type=Path)
    return parser


def _format_optional(value) -> str:
    return "unavailable" if value is None else str(value)


def _print_log(manifest: Mapping) -> None:
    usage = manifest["usage"]
    if usage["input_tokens"] is None or usage["output_tokens"] is None:
        total = None
    else:
        total = usage["input_tokens"] + usage["output_tokens"]
    print(f"agent_status = {manifest['execution']['status']}")
    print(f"submission_status = {manifest['submission']['status']}")
    print(f"duration_seconds = {manifest['execution']['duration_seconds']:.6f}")
    print(f"turns = {_format_optional(usage['turns'])}")
    print(f"tool_calls = {_format_optional(usage['tool_calls'])}")
    print(f"prompt_tokens = {_format_optional(usage['input_tokens'])}")
    print(f"resp_tokens   = {_format_optional(usage['output_tokens'])}")
    print(f"total_tokens  = {_format_optional(total)}")
    print("cost          = unavailable")


def main(argv: Optional[Sequence[str]] = None, **dependencies) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        manifest = generate_agent_sample(
            run_config_path=args.run_config,
            output_path=args.output,
            prompt_path=args.prompt_filename,
            **dependencies,
        )
    except SampleRequestError as error:
        print(f"ERROR: invalid Agent sample request: {error}", file=sys.stderr)
        return 2
    except (
        CredentialError,
        DockerInfrastructureError,
        RunConfigError,
        RuntimeBindingError,
        SampleInfrastructureError,
        OSError,
        ValueError,
    ) as error:
        print(f"ERROR: Agent infrastructure failed: {error}", file=sys.stderr)
        return 3
    _print_log(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
