"""Stage one public benchmark task in an ephemeral Agent workspace."""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from agent_generation.contracts import SUPPORTED_TASKS


@dataclass(frozen=True)
class PreparedWorkspace:
    """Workspace path and optional digest of a public starter artifact."""

    root: Path
    starter_sha256: Optional[str]


def _validate_workspace_inputs(
    task: str,
    prompt_text: str,
    starter_text: Optional[str],
) -> None:
    if task not in SUPPORTED_TASKS:
        raise ValueError(f"unsupported task: {task}")
    if not prompt_text:
        raise ValueError("prompt_text must not be empty")
    if task == "code-complete-iccad2023" and starter_text is None:
        raise ValueError("code-complete-iccad2023 requires a public starter")
    if task == "spec-to-rtl" and starter_text is not None:
        raise ValueError("spec-to-rtl must not receive a starter")


@contextmanager
def staged_workspace(
    *,
    work_root: Path,
    task: str,
    prompt_text: str,
    rules_text: Optional[str] = None,
    starter_text: Optional[str] = None,
) -> Iterator[PreparedWorkspace]:
    """Yield a fresh workspace and remove all runtime state on exit."""

    _validate_workspace_inputs(task, prompt_text, starter_text)
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sample-", dir=work_root) as tmp:
        workspace = Path(tmp)
        (workspace / "TASK.md").write_text(prompt_text, encoding="utf-8")
        if rules_text is not None:
            (workspace / "RULES.md").write_text(rules_text, encoding="utf-8")

        starter_sha256 = None
        if starter_text is not None:
            starter_bytes = starter_text.encode("utf-8")
            (workspace / "TopModule.sv").write_bytes(starter_bytes)
            starter_sha256 = hashlib.sha256(starter_bytes).hexdigest()

        yield PreparedWorkspace(
            root=workspace,
            starter_sha256=starter_sha256,
        )
