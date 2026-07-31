import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class MaxTokensConfigurationTests(unittest.TestCase):
    def test_configure_propagates_generation_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_dir = root / "build"
            bin_dir = root / "bin"
            build_dir.mkdir()
            bin_dir.mkdir()

            problems = root / "problems.txt"
            problems.write_text("Prob001_zero\n")

            for command, body in {
                "iverilog": "#!/bin/sh\nexit 0\n",
                "column": "#!/bin/sh\ncat\n",
            }.items():
                executable = bin_dir / command
                executable.write_text(body)
                executable.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                [
                    str(REPO_ROOT / "configure"),
                    "--with-model=qwen3.6-coder",
                    "--with-task=spec-to-rtl",
                    "--with-samples=1",
                    "--with-temperature=0.6",
                    "--with-top-p=0.95",
                    "--with-max-tokens=8192",
                    "--with-qwen-thinking=off",
                    f"--with-problems={problems}",
                ],
                cwd=build_dir,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            configure_output = result.stdout + result.stderr
            self.assertNotIn("unrecognized options: --with-max-tokens", configure_output)
            self.assertNotIn("unrecognized options: --with-qwen-thinking", configure_output)
            makefile = (build_dir / "Makefile").read_text()
            self.assertIn('GENERATE_FLAGS += "--max-tokens=8192"', makefile)
            self.assertIn('GENERATE_FLAGS += "--qwen-thinking=off"', makefile)

    def test_flake_defaults_to_qwen_generation_settings(self):
        flake = (REPO_ROOT / "flake.nix").read_text()
        self.assertIn("--with-max-tokens=8192", flake)
        self.assertIn("--with-qwen-thinking=on", flake)


if __name__ == "__main__":
    unittest.main()
