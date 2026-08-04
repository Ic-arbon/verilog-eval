from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_generation.cli import _argument_parser, main
from agent_generation.contracts import AgentEnvironment, AgentUsage, ProcessResult
from agent_generation.run_config import publish_run_config
from agent_generation.runtime_bindings import publish_runtime_bindings
from agent_generation.tools import project_agent_tools
from tests.test_agent_run_config import valid_config
from tests.test_agent_tools_projection import add_pi_tools, make_tools
from tests.test_dcd_pi_bundle import make_bundle


class FakeDriver:
    def __init__(self):
        self.requests = []

    def write_config(self, request):
        self.requests.append(request)
        path = request.workspace / ".agent-config/fake.json"
        path.parent.mkdir()
        path.write_text('{"fake":true}\n')
        return (path,)

    def build_command(self, request):
        return ("fake-agent", request.sample_id)

    def environment(self, request):
        return AgentEnvironment(
            variables=(("HOME", "/workspace/.home"),),
            inherit=("OPENAI_API_KEY",),
        )

    def classify_budget_event(self, line):
        return None

    def normalize_trajectory_line(self, line):
        return line

    def parse_event(self, line):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None


class FakeExecutor:
    def __init__(self, candidate=None):
        self.candidate = candidate
        self.specs = []
        self.snapshots = []

    def run(self, spec):
        self.specs.append(spec)
        self.snapshots.append(sorted(path.name for path in spec.workspace.iterdir()))
        if self.candidate is not None:
            (spec.workspace / "TopModule.sv").write_text(self.candidate)
        return ProcessResult(
            status="completed",
            exit_code=0,
            duration_seconds=1.25,
            stdout='{"type":"turn_end","message":{"usage":{"input":10,"output":20}}}\n',
            stderr="diagnostic\n",
            usage=AgentUsage.unavailable(),
        )


class AgentSampleFixture:
    def __init__(self, root: Path, *, agent: str = "opencode", dcd_bundle: Path | None = None):
        self.root = root
        self.dataset = root / "dataset_spec-to-rtl"
        self.dataset.mkdir()
        self.prompt = self.dataset / "Prob001_zero_prompt.txt"
        self.prompt.write_text("Produce a constant-zero TopModule.\n")
        (self.dataset / "Prob001_zero_test.sv").write_text("test\n")
        (self.dataset / "Prob001_zero_ref.sv").write_text("ref\n")
        tools = make_tools(root)
        if agent == "pi-dcd-front-end":
            add_pi_tools(tools)
        projection = project_agent_tools(tools, root / "projections", agent)

        config = valid_config()
        config["agent"].update(
            name=agent,
            toolset="rtl" if agent == "pi-dcd-front-end" else "standard",
        )
        config["endpoint"]["models_response_sha256"] = "5" * 64
        config["benchmark"]["inputs"] = [
            {
                "kind": "problem_list",
                "name": "problems.txt",
                "sha256": "a" * 64,
                "size_bytes": 13,
            },
            {
                "kind": "prompt",
                "name": self.prompt.name,
                "sha256": hashlib.sha256(self.prompt.read_bytes()).hexdigest(),
                "size_bytes": self.prompt.stat().st_size,
            },
            {
                "kind": "hidden_test",
                "name": "Prob001_zero_test.sv",
                "sha256": hashlib.sha256((self.dataset / "Prob001_zero_test.sv").read_bytes()).hexdigest(),
                "size_bytes": 5,
            },
            {
                "kind": "hidden_reference",
                "name": "Prob001_zero_ref.sv",
                "sha256": hashlib.sha256((self.dataset / "Prob001_zero_ref.sv").read_bytes()).hexdigest(),
                "size_bytes": 4,
            },
        ]
        config["runtime"]["agent_tools"] = {
            "content_sha256": projection.content_sha256,
            "source_content_sha256": projection.source_content_sha256,
            "lock_sha256": projection.lock_sha256,
            "versions": projection.versions,
        }
        if dcd_bundle is not None:
            content = dcd_bundle.read_bytes()
            config["runtime"]["support_files"].append(
                {
                    "name": "dcd-pi-bundle",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
        self.config_path = publish_run_config(root / "runs", config)
        self.run = self.config_path.parent
        evidence = {
            "base_url": config["endpoint"]["base_url"],
            "model": config["agent"]["model"],
            "model_count": 1,
            "models_url": config["endpoint"]["base_url"] + "/models",
            "response_sha256": "5" * 64,
        }
        (self.run / "endpoint-evidence.json").write_text(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n"
        )
        bindings = {
            "run_config_sha256": self.run.name,
            "source_root": str(root / "source"),
            "dataset_dir": str(self.dataset),
            "problems_file": str(self.dataset / "problems.txt"),
            "build_dir": str(self.run),
            "docker": {
                "client": str(root / "bin/docker"),
                "daemon": "unix:///var/run/docker.sock",
                "image": "verilog-eval-agent-sandbox:standard",
                "archive": str(root / "image.tar"),
            },
            "tools_source": str(root / "tools-source"),
            "tools_projection": str(projection.path),
            "toolchain": {
                "bash": str(root / "bin/bash"),
                "make": str(root / "bin/make"),
                "iverilog": str(root / "bin/iverilog"),
                "timeout": str(root / "bin/timeout"),
            },
            "support_files": (
                {"dcd_pi_bundle": str(dcd_bundle)}
                if dcd_bundle is not None
                else {}
            ),
            "credential_broker": ".credential.sock",
        }
        publish_runtime_bindings(self.config_path, bindings)
        self.output = self.run / "Prob001_zero/Prob001_zero_sample01.sv"


class DcdInspectingDriver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.saw_front_end_resources = False

    def write_config(self, request):
        self.requests.append(request)
        self.saw_front_end_resources = (
            request.workspace
            / ".agent-config"
            / "pi"
            / "agents"
            / "front-end-design-orchestrator.md"
        ).is_file()
        path = request.workspace / ".agent-config" / "pi" / "models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"fake":true}\n')
        return (path,)


class AgentGeneratorCliTests(unittest.TestCase):
    def test_script_is_executable_narrow_generator_program(self):
        script = Path(__file__).resolve().parents[1] / "scripts/sv-agent-generate"
        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK))
        source = script.read_text()
        self.assertNotIn("temperature", source)
        self.assertNotIn("top_p", source)

    def test_parser_exposes_only_run_config_output_verbose_and_prompt(self):
        parser = _argument_parser()
        args = parser.parse_args(
            ["--run-config=/run/config", "--output=out.sv", "--verbose", "prompt.txt"]
        )
        self.assertEqual(args.run_config, Path("/run/config"))
        for forbidden in ("--temperature=0.6", "--top-p=0.95", "--agent=pi"):
            with self.subTest(forbidden=forbidden), self.assertRaises(SystemExit):
                parser.parse_args(
                    ["--run-config=/run/config", "--output=out.sv", forbidden, "prompt.txt"]
                )

    def test_narrow_cli_commits_candidate_and_unnumbered_sidecars(self):
        candidate = "module TopModule(output zero); assign zero=1'b0; endmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            fixture = AgentSampleFixture(Path(tmp))
            driver = FakeDriver()
            executor = FakeExecutor(candidate)
            credential_calls = []

            def credential_client(**request):
                credential_calls.append(request)
                return "secret-canary"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = main(
                    [
                        f"--run-config={fixture.config_path}",
                        f"--output={fixture.output}",
                        "--verbose",
                        str(fixture.prompt),
                    ],
                    driver=driver,
                    executor=executor,
                    credential_client=credential_client,
                )

            self.assertEqual(status, 0)
            self.assertEqual(fixture.output.read_text(), candidate)
            manifest = json.loads(
                fixture.output.with_name("Prob001_zero_sample01-generation.json").read_text()
            )
            self.assertNotIn("schema_version", manifest)
            self.assertNotIn("profile", json.dumps(manifest))
            self.assertEqual(manifest["producer"]["run_config_sha256"], fixture.run.name)
            self.assertEqual(manifest["submission"]["status"], "published")
            self.assertEqual(manifest["usage"]["usage_source"], "trajectory")
            self.assertEqual(credential_calls[0]["sample_id"], "Prob001_zero_sample01")
            self.assertEqual(executor.snapshots[0], [".agent-config", "TASK.md"])
            self.assertEqual(list((fixture.run / ".agent-work").glob("sample-*")), [])
            self.assertIn("agent_status = completed", stdout.getvalue())

    def test_dcd_resources_are_staged_before_the_front_end_driver_writes_config(self):
        candidate = "module TopModule(output zero); assign zero=1'b0; endmodule\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "dcd-pi.tar"
            make_bundle(bundle)
            fixture = AgentSampleFixture(
                root,
                agent="pi-dcd-front-end",
                dcd_bundle=bundle,
            )
            driver = DcdInspectingDriver()
            executor = FakeExecutor(candidate)

            status = main(
                [
                    f"--run-config={fixture.config_path}",
                    f"--output={fixture.output}",
                    str(fixture.prompt),
                ],
                driver=driver,
                executor=executor,
                credential_client=lambda **_request: "secret",
            )

            self.assertEqual(status, 0)
            self.assertTrue(driver.saw_front_end_resources)
            self.assertEqual(fixture.output.read_text(), candidate)

    def test_prompt_identity_mismatch_fails_before_agent_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = AgentSampleFixture(Path(tmp))
            fixture.prompt.write_text("tampered\n")
            executor = FakeExecutor("module TopModule; endmodule\n")
            status = main(
                [
                    f"--run-config={fixture.config_path}",
                    f"--output={fixture.output}",
                    str(fixture.prompt),
                ],
                driver=FakeDriver(),
                executor=executor,
                credential_client=lambda **_request: "secret",
            )
            self.assertEqual(status, 2)
            self.assertEqual(executor.specs, [])
            self.assertFalse(fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
