from pathlib import Path


AGENT_INSTRUCTIONS = """\
Read TASK.md and implement the requested Verilog module.

Requirements:
- Write the final implementation to TopModule.sv.
- You may create your own testbench files.
- Use iverilog to compile and test your work.
- Fix errors before finishing.
- Work only inside /workspace.
- Do not ask the user questions.
- Finish only when TopModule.sv exists and compiles.
"""


def prepare_workspace(
    repo_root: Path,
    run_root: Path,
    task: str,
    problem: str,
) -> Path:
    dataset = repo_root / f"dataset_{task}"
    prompt_path = dataset / f"{problem}_prompt.txt"
    if not prompt_path.is_file():
        raise FileNotFoundError(f"prompt not found: {prompt_path}")

    workspace = run_root / problem / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "TASK.md").write_text(prompt_path.read_text())
    (workspace / "AGENT_INSTRUCTIONS.md").write_text(AGENT_INSTRUCTIONS)
    return workspace
