from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = REPO_ROOT / "configure"


class AgentConfigureTests(unittest.TestCase):
    def configure(self, root: Path, *extra_args: str):
        fake_bin = root / "bin"
        build_dir = root / "build"
        fake_bin.mkdir()
        build_dir.mkdir()
        iverilog = fake_bin / "iverilog"
        iverilog.write_text("#!/bin/sh\nexit 0\n")
        iverilog.chmod(0o755)
        problems = root / "problems.txt"
        problems.write_text("Prob001_zero\n")
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        result = subprocess.run(
            [
                str(CONFIGURE),
                "--with-model=qwen3.6-coder",
                "--with-task=spec-to-rtl",
                "--with-samples=1",
                f"--with-problems={problems}",
                *extra_args,
            ],
            cwd=build_dir,
            env=environment,
            capture_output=True,
            text=True,
        )
        return result, build_dir

    def evaluated_make_variables(self, build_dir: Path) -> str:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "ECHO=echo",
                "debug-generator",
                "debug-GENERATE_VERILOG",
                "debug-GENERATE_FLAGS",
            ],
            cwd=build_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def test_default_model_generator_keeps_existing_program_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, build_dir = self.configure(Path(tmp))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            variables = self.evaluated_make_variables(build_dir)
            self.assertIn("generator = model", variables)
            self.assertIn("scripts/sv-generate", variables)
            self.assertNotIn("scripts/sv-agent-generate", variables)
            self.assertNotIn("--agent=", variables)

    def test_agent_generator_receives_agent_specific_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, build_dir = self.configure(
                Path(tmp),
                "--with-generator=agent",
                "--with-agent=opencode",
                "--with-agent-timeout=240",
                "--with-agent-max-turns=18",
                "--with-agent-max-tool-calls=44",
                "--with-agent-thinking=off",
                "--with-agent-tool-profile=rtl",
            )

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn("unrecognized options", output)
            variables = self.evaluated_make_variables(build_dir)
            self.assertIn("generator = agent", variables)
            self.assertIn("scripts/sv-agent-generate", variables)
            self.assertIn("--agent=opencode", variables)
            self.assertIn("--agent-timeout=240", variables)
            self.assertIn("--agent-max-turns=18", variables)
            self.assertIn("--agent-max-tool-calls=44", variables)
            self.assertIn("--agent-thinking=off", variables)
            self.assertIn("--agent-tool-profile=rtl", variables)
            self.assertIn("--max-tokens=1024", variables)

            makefile = (build_dir / "Makefile").read_text()
            self.assertIn("_sample%-generation.json", makefile)
            self.assertIn("_sample%-trajectory.jsonl", makefile)
            self.assertIn("_sample%-stderr.log", makefile)

    def test_invalid_generator_is_rejected_during_configure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _build_dir = self.configure(
                Path(tmp),
                "--with-generator=transcript-extractor",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "--with-generator must be model or agent",
                result.stdout + result.stderr,
            )

    def test_invalid_agent_budgets_are_rejected_before_make(self):
        invalid_options = (
            "--with-agent-timeout=0",
            "--with-agent-max-turns=not-a-number",
            "--with-agent-max-tool-calls=-1",
        )

        for option in invalid_options:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                result, _build_dir = self.configure(Path(tmp), option)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "Agent budgets must be positive integers",
                    result.stdout + result.stderr,
                )


if __name__ == "__main__":
    unittest.main()
