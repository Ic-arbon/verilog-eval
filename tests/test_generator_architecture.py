from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVOKER = ROOT / "scripts/sv-invoke-generator"
CONFIGURE = ROOT / "configure"


def nul_records(path: Path) -> list[str]:
    content = path.read_bytes()
    if not content.endswith(b"\0"):
        raise AssertionError("argument file has no final NUL")
    return [item.decode("utf-8") for item in content[:-1].split(b"\0")]


class GeneratorInvokerTests(unittest.TestCase):
    def fixture(self, root: Path):
        scripts = root / "scripts"
        scripts.mkdir()
        recorder = scripts / "sv-generate"
        recorder.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "open(os.environ['ARGV_RECORD'], 'w').write(json.dumps(sys.argv))\n"
        )
        recorder.chmod(0o755)
        args_file = root / ".generator-args"
        args_file.write_bytes(
            b"--model=danger $(touch nope)\0"
            b"--examples=0\0"
            b"--task=spec-to-rtl\0"
            b"--max-tokens=1024\0"
            b"--temperature=0.85\0"
            b"--top-p=0.95\0"
        )
        args_file.chmod(0o600)
        return recorder, args_file

    def test_execv_preserves_static_and_dynamic_argv_without_shell_evaluation(self):
        self.assertTrue(INVOKER.is_file())
        self.assertTrue(os.access(INVOKER, os.X_OK))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorder, args_file = self.fixture(root)
            record = root / "argv.json"
            output = root / "out;touch nope.sv"
            prompt = root / "prompt $(touch nope).txt"
            result = subprocess.run(
                (
                    str(INVOKER),
                    "--program",
                    str(recorder),
                    "--args-file",
                    str(args_file),
                    "--",
                    "--verbose",
                    "--output",
                    str(output),
                    str(prompt),
                ),
                env={**os.environ, "ARGV_RECORD": str(record)},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(record.read_text())
            self.assertEqual(argv[1:7], nul_records(args_file))
            self.assertEqual(argv[7:], ["--verbose", "--output", str(output), str(prompt)])
            self.assertFalse((root / "nope").exists())

    def test_argument_file_type_owner_mode_termination_and_shape_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorder, args_file = self.fixture(root)
            cases = []
            args_file.chmod(0o644)
            cases.append(("mode", args_file))

            symlink = root / "symlink-dir/.generator-args"
            symlink.parent.mkdir()
            symlink.symlink_to(args_file)
            cases.append(("symlink", symlink))

            malformed = root / "malformed/.generator-args"
            malformed.parent.mkdir()
            malformed.write_bytes(b"--model=x")
            malformed.chmod(0o600)
            cases.append(("termination", malformed))

            empty = root / "empty/.generator-args"
            empty.parent.mkdir()
            empty.write_bytes(b"--run-config=/x\0\0")
            empty.chmod(0o600)
            agent_program = empty.parent / "scripts/sv-agent-generate"
            agent_program.parent.mkdir()
            agent_program.write_text("#!/bin/sh\nexit 0\n")
            agent_program.chmod(0o755)
            cases.append(("empty", empty))

            for name, path in cases:
                with self.subTest(name=name):
                    program = agent_program if name == "empty" else recorder
                    result = subprocess.run(
                        (
                            str(INVOKER),
                            "--program",
                            str(program),
                            "--args-file",
                            str(path),
                            "--",
                            "--verbose",
                            "--output",
                            str(root / "out.sv"),
                            str(root / "prompt.txt"),
                        ),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)


class GraderPreservationTests(unittest.TestCase):
    def test_original_model_and_icarus_authorities_are_byte_preserved(self):
        base_model = subprocess.run(
            ("git", "show", "245c19918f18abb7e6aa328282f3624afc0e2884:scripts/sv-generate"),
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual((ROOT / "scripts/sv-generate").read_bytes(), base_model)

        base_make = subprocess.run(
            ("git", "show", "245c19918f18abb7e6aa328282f3624afc0e2884:Makefile.in"),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        current_make = (ROOT / "Makefile.in").read_text()
        for start, end in (
            ("# Test verilog samples", "# Problem-level clean"),
        ):
            base_section = base_make.split(start, 1)[1].split(end, 1)[0]
            current_section = current_make.split(start, 1)[1].split(end, 1)[0]
            self.assertEqual(current_section, base_section)
        self.assertIn(
            "$(scripts_dir)/sv-iv-analyze --csv=summary.csv $(problems)",
            current_make,
        )
        self.assertIn(
            "IVERILOG_COMPILE=@IVERILOG@ -Wall -Winfloop -Wno-timescale -g2012 -s tb",
            current_make,
        )


class ConfiguredGeneratorSeamTests(unittest.TestCase):
    def configure(self, root: Path, *arguments: str):
        fake_bin = root / "bin"
        build = root / "build"
        fake_bin.mkdir()
        build.mkdir()
        iverilog = fake_bin / "iverilog"
        iverilog.write_text("#!/bin/sh\nexit 0\n")
        iverilog.chmod(0o755)
        problems = root / "problems.txt"
        problems.write_text("Prob001_zero\n")
        environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
        result = subprocess.run(
            (
                str(CONFIGURE),
                "--with-task=spec-to-rtl",
                "--with-samples=1",
                f"--with-problems={problems}",
                *arguments,
            ),
            cwd=build,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, build

    def test_model_static_argv_is_exact_legacy_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dangerous_model = "model;$(touch should-not-exist)\nsecond-line"
            result, build = self.configure(
                root,
                f"--with-model={dangerous_model}",
                "--with-examples=2",
                "--with-rules",
                "--with-max-tokens=2048",
                "--with-temperature=0.7",
                "--with-top-p=0.8",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                nul_records(build / ".generator-args"),
                [
                    f"--model={dangerous_model}",
                    "--examples=2",
                    "--rules",
                    "--task=spec-to-rtl",
                    "--max-tokens=2048",
                    "--temperature=0.7",
                    "--top-p=0.8",
                ],
            )
            self.assertEqual((build / ".generator-args").stat().st_mode & 0o777, 0o600)
            makefile = (build / "Makefile").read_text()
            self.assertIn("GENERATOR_INVOKER", makefile)
            self.assertIn("GENERATOR_PROGRAM", makefile)
            self.assertNotIn("GENERATE_FLAGS", makefile)
            self.assertNotIn("agent-thinking", makefile)
            self.assertNotIn("generation_trajectories", makefile)

    def test_generated_make_invokes_model_through_adapter_with_exact_argv(self):
        make_version = subprocess.run(
            ("make", "--version"), text=True, capture_output=True, check=True
        ).stdout.splitlines()[0]
        if "GNU Make 3." in make_version:
            self.skipTest("the upstream Makefile requires GNU Make 4 shell assignment")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            build = root / "build"
            (source / "scripts").mkdir(parents=True)
            build.mkdir()
            for name in ("configure", "Makefile.in"):
                shutil.copy2(ROOT / name, source / name)
            for name in ("sv-invoke-generator", "echo-progress", "sv-iv-analyze"):
                shutil.copy2(ROOT / "scripts" / name, source / "scripts" / name)
            recorder = source / "scripts/sv-generate"
            recorder.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['ARGV_RECORD']).write_text(json.dumps(sys.argv))\n"
                "out=pathlib.Path(sys.argv[sys.argv.index('--output')+1])\n"
                "out.write_text('module TopModule; endmodule\\n')\n"
            )
            recorder.chmod(0o755)
            dataset = source / "dataset_spec-to-rtl"
            dataset.mkdir()
            (dataset / "problems.txt").write_text("Prob001_zero\n")
            (dataset / "Prob001_zero_prompt.txt").write_text("prompt\n")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            iverilog = fake_bin / "iverilog"
            iverilog.write_text("#!/bin/sh\nexit 0\n")
            iverilog.chmod(0o755)
            seq = fake_bin / "seq"
            seq.write_text("#!/bin/sh\nprintf '01\\n'\n")
            seq.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "ARGV_RECORD": str(root / "argv.json"),
            }
            configured = subprocess.run(
                (
                    str(source / "configure"),
                    "--with-model=qwen3.6-coder",
                    "--with-task=spec-to-rtl",
                    "--with-samples=1",
                    f"--with-dataset={dataset}",
                    f"--with-problems={dataset / 'problems.txt'}",
                    "--with-max-tokens=2048",
                    "--with-temperature=0.7",
                    "--with-top-p=0.8",
                ),
                cwd=build,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            generated = subprocess.run(
                ("make", "SHELL=/bin/bash", "ECHO=echo", "Prob001_zero-sv-generate"),
                cwd=build,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            argv = json.loads((root / "argv.json").read_text())
            self.assertEqual(
                argv[1:],
                [
                    "--model=qwen3.6-coder",
                    "--examples=0",
                    "--task=spec-to-rtl",
                    "--max-tokens=2048",
                    "--temperature=0.7",
                    "--top-p=0.8",
                    "--verbose",
                    "--output",
                    "Prob001_zero/Prob001_zero_sample01.sv",
                    str(dataset / "Prob001_zero_prompt.txt"),
                ],
            )

    def test_agent_config_is_one_opaque_static_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config dir;literal$(touch nope)/run-config.json"
            config.parent.mkdir()
            config.write_text("{}\n")
            result, build = self.configure(
                root,
                "--with-generator=agent",
                f"--with-generator-config={config}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                nul_records(build / ".generator-args"),
                [f"--run-config={config}"],
            )
            self.assertFalse((root / "nope").exists())


if __name__ == "__main__":
    unittest.main()
