from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_generation.run import (
    PreparationEvidence,
    RunnerError,
    acknowledge_run_path,
    assert_clean_source,
    collect_input_manifest,
    collect_preparation_evidence,
    configure_environment,
    docker_environment,
    execute_prepared_run,
    formal_runner_environment,
    make_environment,
    manage_recovery,
    parse_runner_options,
    prepare_run,
    runner_environment,
    verify_selected_inputs,
)
from agent_generation.provenance import (
    docker_daemon_identity,
    executable_identity,
    support_file_identity,
)
from agent_generation.run_config import publish_run_config
from tests.test_agent_endpoint import ThreadedServer, json_handler
from tests.test_agent_run_config import valid_config
from tests.test_agent_tools_projection import add_pi_tools, make_tools
from tests.test_dcd_pi_bundle import make_bundle


class RunnerCliStateTests(unittest.TestCase):
    def test_ordinary_defaults_and_jobs_are_resolved_material_inputs(self):
        options = parse_runner_options(
            ["--with-agent=pi"],
            environment={"VERILOG_EVAL_JOBS": "16"},
        )

        self.assertEqual(options.mode, "ordinary")
        self.assertEqual(options.agent, "pi")
        self.assertEqual(options.model, "qwen3.6-coder")
        self.assertEqual(options.max_input_tokens, 16384)
        self.assertEqual(options.max_output_tokens, 16384)
        self.assertTrue(options.thinking)
        self.assertEqual(options.jobs, 16)
        self.assertEqual(options.api_key_environment, "OPENAI_API_KEY")

    def test_dcd_front_end_pi_entry_is_an_explicit_material_agent(self):
        options = parse_runner_options(
            [
                "--with-agent=pi-dcd-front-end",
                "--dcd-pi-bundle=/opt/agent/dcd-pi.tar",
            ],
            environment={"VERILOG_EVAL_JOBS": "8"},
        )

        self.assertEqual(options.agent, "pi-dcd-front-end")
        self.assertEqual(options.dcd_pi_bundle, Path("/opt/agent/dcd-pi.tar"))
        self.assertEqual(options.jobs, 8)

        with self.assertRaises(SystemExit):
            parse_runner_options(
                ["--with-agent=pi-dcd-rtl-module"],
                environment={"VERILOG_EVAL_JOBS": "8"},
            )

    def test_sampling_options_are_not_part_of_agent_interface(self):
        for option in ("--with-temperature=0.6", "--with-top-p=0.95"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parse_runner_options([option], environment={})

    def test_make_visible_locators_fail_closed_before_preparation(self):
        for locator in (
            "/tmp/contains space",
            "/tmp/contains#comment",
            "/tmp/contains$make",
            "/tmp/contains%pattern",
            "/tmp/contains\nnewline",
        ):
            with self.subTest(locator=locator), self.assertRaises(SystemExit):
                parse_runner_options(
                    [f"--source-root={locator}"],
                    environment={},
                )

    def test_new_run_and_resume_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            parse_runner_options(
                ["--new-run", "--resume=/tmp/run-config.json"],
                environment={},
            )

    def test_resume_loads_material_config_and_rejects_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = publish_run_config(Path(tmp), valid_config())

            options = parse_runner_options(
                [f"--resume={config_path}", "--source-root=/rebound/source"],
                environment={"VERILOG_EVAL_JOBS": "4"},
            )
            self.assertEqual(options.mode, "resume")
            self.assertEqual(options.agent, "pi")
            self.assertEqual(options.jobs, 4)
            self.assertEqual(options.source_root, Path("/rebound/source"))

            with self.assertRaises(SystemExit):
                parse_runner_options(
                    [f"--resume={config_path}", "--with-model=other"],
                    environment={},
                )
            with self.assertRaises(SystemExit):
                parse_runner_options(
                    [f"--resume={config_path}"],
                    environment={"VERILOG_EVAL_JOBS": "8"},
                )

    def test_recovery_management_is_explicit_and_build_root_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = valid_config()
            config["nonce"] = "5" * 32
            config_path = publish_run_config(root, config)
            from agent_generation.lifecycle import publish_recovery_receipt

            publish_recovery_receipt(root, config_path.parent.name)
            listed = parse_runner_options(
                [f"--build-root={root}", "--list-recoveries"], environment={}
            )
            self.assertEqual(len(manage_recovery(listed)), 1)

            resume = parse_runner_options(
                [
                    f"--build-root={root}",
                    f"--resume-recovery={config_path.parent.name}",
                ],
                environment={},
            )
            self.assertEqual(manage_recovery(resume), config_path.resolve())

    def test_invalid_jobs_fail_before_side_effects(self):
        for value in ("0", "-1", "many", "1.5"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_runner_options([], environment={"VERILOG_EVAL_JOBS": value})


class SourceAndInputIdentityTests(unittest.TestCase):
    def init_repository(self, root: Path) -> None:
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        subprocess.run(("git", "-C", str(root), "config", "user.email", "test@example.test"), check=True)
        subprocess.run(("git", "-C", str(root), "config", "user.name", "Test"), check=True)
        (root / "tracked.txt").write_text("clean\n")
        (root / ".gitignore").write_text("ignored.py\nbuild/\n")
        subprocess.run(("git", "-C", str(root), "add", "."), check=True)
        subprocess.run(("git", "-C", str(root), "commit", "-qm", "base"), check=True)

    def test_clean_source_rejects_tracked_and_runtime_affecting_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.init_repository(root)
            assert_clean_source(root, allowed_roots=())

            (root / "tracked.txt").write_text("dirty\n")
            with self.assertRaises(RunnerError):
                assert_clean_source(root, allowed_roots=())
            subprocess.run(("git", "-C", str(root), "checkout", "--", "tracked.txt"), check=True)

            (root / "shadow.py").write_text("raise RuntimeError\n")
            with self.assertRaises(RunnerError):
                assert_clean_source(root, allowed_roots=())
            (root / "shadow.py").unlink()

            (root / "ignored.py").write_text("raise RuntimeError\n")
            with self.assertRaises(RunnerError):
                assert_clean_source(root, allowed_roots=())

    def test_allowlisted_build_root_does_not_influence_source_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.init_repository(root)
            build = root / "build"
            build.mkdir()
            (build / "generated.py").write_text("generated = True\n")
            assert_clean_source(root, allowed_roots=(build,))

    def test_input_manifest_hashes_every_selected_public_and_hidden_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "dataset_spec-to-rtl"
            dataset.mkdir()
            problems = dataset / "problems.txt"
            problems.write_text("Prob001_zero\n")
            (dataset / "Prob001_zero_prompt.txt").write_text("prompt\n")
            (dataset / "Prob001_zero_test.sv").write_text("test\n")
            (dataset / "Prob001_zero_ref.sv").write_text("ref\n")

            problem_ids, manifest = collect_input_manifest(
                source_root=root,
                dataset_dir=dataset,
                problems_file=problems,
                task="spec-to-rtl",
                rules=True,
                examples=0,
            )

            self.assertEqual(problem_ids, ("Prob001_zero",))
            self.assertEqual(
                [item["kind"] for item in manifest],
                ["problem_list", "prompt", "hidden_test", "hidden_reference", "rules"],
            )
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest))
            self.assertNotIn(str(dataset), repr(manifest))

    def test_real_preparation_collects_all_identities_without_running_npm_or_make(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            self.init_repository(source)
            dataset = source / "dataset_spec-to-rtl"
            dataset.mkdir()
            (dataset / "problems.txt").write_text("Prob001_zero\n")
            (dataset / "Prob001_zero_prompt.txt").write_text("prompt\n")
            (dataset / "Prob001_zero_test.sv").write_text("test\n")
            (dataset / "Prob001_zero_ref.sv").write_text("ref\n")
            subprocess.run(("git", "-C", str(source), "add", "."), check=True)
            subprocess.run(("git", "-C", str(source), "commit", "-qm", "dataset"), check=True)

            tools = make_tools(root)
            add_pi_tools(tools)
            dcd_bundle = root / "dcd-pi.tar"
            make_bundle(dcd_bundle)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls.log"
            docker = bin_dir / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {calls}\n"
                "case \"$1\" in\n"
                "  load) exit 0 ;;\n"
                "  image) printf 'sha256:%064d\\n' 2; exit 0 ;;\n"
                "  version) printf '{\"ID\":\"daemon\",\"Os\":\"linux\",\"Arch\":\"amd64\"}\\n'; exit 0 ;;\n"
                "esac\n"
                "exit 1\n"
            )
            docker.chmod(0o755)
            for name in ("bash", "make", "iverilog", "timeout"):
                path = bin_dir / name
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)
            image_archive = root / "image.tar"
            image_archive.write_bytes(b"image")
            ca_bundle = root / "ca.pem"
            ca_bundle.write_text("ca\n")

            handler = json_handler({"data": [{"id": "qwen3.6-coder"}]})
            with ThreadedServer(handler) as base:
                options = parse_runner_options(
                    [
                        "--with-agent=pi-dcd-front-end",
                        "--with-agent-toolset=rtl",
                        f"--dcd-pi-bundle={dcd_bundle}",
                        f"--with-openai-api-base={base}",
                        f"--source-root={source}",
                        f"--with-dataset={dataset}",
                        f"--with-problems={dataset / 'problems.txt'}",
                        f"--build-root={root / 'runs'}",
                        f"--agent-tools={tools}",
                        f"--docker-path={docker}",
                        "--docker-image=verilog-eval-agent-sandbox:rtl",
                        f"--docker-archive={image_archive}",
                    ],
                    environment={"VERILOG_EVAL_JOBS": "4"},
                )
                evidence, credential = collect_preparation_evidence(
                    options,
                    environment={
                        "OPENAI_API_KEY": "secret",
                        "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        "SSL_CERT_FILE": str(ca_bundle),
                        "VERILOG_EVAL_CACHE_ROOT": str(root / "cache"),
                        "DOCKER_HOST": "unix:///var/run/docker.sock",
                    },
                )

            self.assertEqual(credential, "secret")
            self.assertEqual(evidence.problems, ("Prob001_zero",))
            self.assertRegex(evidence.docker_daemon_identity, r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(evidence.tools_content_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual(evidence.endpoint_evidence["model"], "qwen3.6-coder")
            dcd_support = [
                item
                for item in evidence.support_identities
                if item["name"] == "dcd-pi-bundle"
            ]
            self.assertEqual(len(dcd_support), 1)
            self.assertEqual(
                evidence.runtime_bindings["support_files"]["dcd_pi_bundle"],
                str(dcd_bundle.resolve()),
            )
            self.assertNotIn(str(dcd_bundle), repr(dcd_support[0]))
            command_log = calls.read_text()
            self.assertIn("load --input", command_log)
            self.assertIn("image inspect", command_log)
            self.assertNotIn("npm", command_log)
            self.assertNotIn("make ", command_log)


    def test_selected_inputs_are_rechecked_before_report_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = RunPreparationTests()
            prepared = prepare_run(
                helper.options(root),
                helper.evidence(root),
                credential="secret",
            )
            dataset = Path(prepared.bindings["dataset_dir"])
            dataset.mkdir(parents=True)
            Path(prepared.bindings["problems_file"]).write_text("Prob001_zero\n")
            hidden = dataset / "Prob001_zero_test.sv"
            hidden.write_text("test\n")

            verify_selected_inputs(prepared.config, prepared.bindings)
            hidden.write_text("changed after grading\n")
            with self.assertRaises(RunnerError):
                verify_selected_inputs(prepared.config, prepared.bindings)


class HostIdentityTests(unittest.TestCase):
    def test_executable_and_support_identities_are_content_not_path_based(self):
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = Path(first_tmp) / "tool"
            second = Path(second_tmp) / "tool"
            first.write_text("same bytes\n")
            second.write_text("same bytes\n")
            first.chmod(0o755)
            second.chmod(0o755)

            self.assertEqual(
                executable_identity("tool", first),
                executable_identity("tool", second),
            )
            support = support_file_identity("support", first)
            self.assertEqual(support["name"], "support")
            self.assertEqual(support["size_bytes"], len("same bytes\n"))
            self.assertNotIn(str(first_tmp), repr(support))

    def test_docker_daemon_identity_hashes_bounded_server_record(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"ID":"daemon","Os":"linux","Arch":"amd64"}\n',
                stderr="",
            )

        identity = docker_daemon_identity(
            "/pinned/docker",
            environment={"PATH": "/pinned", "DOCKER_HOST": "unix:///socket"},
            runner=runner,
        )
        self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(calls[0][0][1:3], ("version", "--format"))
        self.assertEqual(calls[0][1]["env"], {"PATH": "/pinned", "DOCKER_HOST": "unix:///socket"})


class RunPreparationTests(unittest.TestCase):
    def evidence(self, root: Path) -> PreparationEvidence:
        return PreparationEvidence(
            source_commit="1" * 40,
            problems=("Prob001_zero",),
            inputs=(
                {
                    "kind": "problem_list",
                    "name": "problems.txt",
                    "sha256": "488cf274ebb5572dba9d1da38ef92dc545a58c4cd03ad394c06d0e12526ef812",
                    "size_bytes": 13,
                },
                {
                    "kind": "hidden_test",
                    "name": "Prob001_zero_test.sv",
                    "sha256": "f2ca1bb6c7e907d06dafe4687e579fce76b37e4e93b7605022da52e6ccc26fd2",
                    "size_bytes": 5,
                },
            ),
            docker_image_id="sha256:" + "2" * 64,
            docker_daemon_identity="linux/amd64@daemon",
            tools_content_sha256="3" * 64,
            tools_source_content_sha256="9" * 64,
            tools_lock_sha256="4" * 64,
            tools_versions={"opencode-ai": "1.18.7"},
            toolchain_identities=(
                {"name": "python", "identity": "sha256:" + "5" * 64},
                {"name": "make", "identity": "sha256:" + "6" * 64},
            ),
            support_identities=(
                {"name": "ca-bundle", "sha256": "7" * 64, "size_bytes": 10},
            ),
            endpoint_evidence={"response_sha256": "8" * 64},
            runtime_bindings={
                "source_root": str(root / "source"),
                "dataset_dir": str(root / "source/dataset_spec-to-rtl"),
                "problems_file": str(root / "source/dataset_spec-to-rtl/problems.txt"),
                "build_dir": str(root / "build"),
                "docker": {
                    "client": str(root / "bin/docker"),
                    "daemon": "unix:///var/run/docker.sock",
                    "image": "verilog-eval-agent-sandbox:standard",
                    "archive": str(root / "image.tar"),
                },
                "tools_source": str(root / "tools-source"),
                "tools_projection": str(root / "projection"),
                "toolchain": {
                    "bash": str(root / "bin/bash"),
                    "make": str(root / "bin/make"),
                    "iverilog": str(root / "bin/iverilog"),
                    "timeout": str(root / "bin/timeout"),
                },
                "support_files": {"ca_bundle": str(root / "ca-bundle.crt")},
                "credential_broker": ".credential.sock",
            },
        )

    def options(self, root: Path, *extra: str):
        return parse_runner_options(
            [f"--build-root={root / 'runs'}", *extra],
            environment={"VERILOG_EVAL_JOBS": "4"},
        )

    def test_ordinary_run_identity_is_deterministic_and_bindings_remain_ephemeral(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = prepare_run(
                self.options(root), self.evidence(root), credential="secret"
            )
            second = prepare_run(
                self.options(root), self.evidence(root), credential="secret"
            )

            self.assertEqual(first.config_path, second.config_path)
            self.assertEqual(first.config["jobs"], 4)
            self.assertIsNone(first.config["nonce"])
            self.assertEqual(first.bindings["run_config_sha256"], first.digest)
            self.assertFalse(first.config_path.with_name("runtime-bindings.json").exists())

    def test_new_run_path_file_and_stdout_acknowledge_recovery_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            import io
            import json

            root = Path(tmp)
            path_file = root / "run-path.txt"
            options = self.options(root, "--new-run", f"--run-path-file={path_file}")
            prepared = prepare_run(options, self.evidence(root), credential="secret")
            stream = io.StringIO()

            acknowledge_run_path(prepared, options, stream=stream)

            self.assertEqual(path_file.read_text(), str(prepared.config_path.parent) + "\n")
            self.assertEqual(stream.getvalue(), str(prepared.config_path.parent) + "\n")
            receipt = json.loads(
                (root / "runs/.recoveries" / f"{prepared.digest}.json").read_text()
            )
            self.assertTrue(receipt["acknowledged"])

    def test_new_run_has_nonce_and_durable_unacknowledged_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = prepare_run(
                self.options(root, "--new-run"),
                self.evidence(root),
                credential="secret",
            )

            self.assertRegex(prepared.config["nonce"], r"^[0-9a-f]{32}$")
            receipt = root / "runs/.recoveries" / f"{prepared.digest}.json"
            self.assertTrue(receipt.is_file())
            self.assertFalse(__import__("json").loads(receipt.read_text())["acknowledged"])

    def test_resume_rebinds_locators_only_when_material_identity_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = prepare_run(
                self.options(root), self.evidence(root), credential="secret"
            )
            resumed_options = parse_runner_options(
                [f"--resume={initial.config_path}", "--source-root=/new/source"],
                environment={},
            )
            rebound = self.evidence(root)
            rebound.runtime_bindings["source_root"] = "/new/source"
            resumed = prepare_run(resumed_options, rebound, credential="secret")
            self.assertEqual(resumed.config_path, initial.config_path)
            self.assertEqual(resumed.bindings["source_root"], "/new/source")

            changed = self.evidence(root)
            changed.tools_content_sha256 = "f" * 64
            with self.assertRaises(RunnerError):
                prepare_run(resumed_options, changed, credential="secret")


class RunExecutionTests(unittest.TestCase):
    def prepared_fixture(self, root: Path):
        helper = RunPreparationTests()
        options = helper.options(root)
        prepared = prepare_run(options, helper.evidence(root), credential="secret")
        dataset = Path(prepared.bindings["dataset_dir"])
        dataset.mkdir(parents=True)
        Path(prepared.bindings["problems_file"]).write_text("Prob001_zero\n")
        (dataset / "Prob001_zero_test.sv").write_text("test\n")
        return options, prepared

    def test_runner_invokes_configure_and_make_once_then_reports_after_broker_stop(self):
        from agent_generation.contracts import AgentUsage, ProcessResult
        from agent_generation.sample_result import commit_sample_bundle
        from tests.test_agent_sample_result import limits, runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options, prepared = self.prepared_fixture(root)
            source = Path(prepared.bindings["source_root"])
            source.mkdir(parents=True, exist_ok=True)
            configure = source / "configure"
            configure.write_text("#!/bin/sh\nexit 0\n")
            configure.chmod(0o755)
            events = []

            class Broker:
                def __init__(self, **_kwargs):
                    pass

                def __enter__(self):
                    events.append("broker_started")
                    return self

                def __exit__(self, *_args):
                    self_path = prepared.config_path.parent / "agent-summary.json"
                    if self_path.exists():
                        raise AssertionError("report marker preceded broker shutdown")
                    events.append("broker_stopped")

            calls = []

            def command_runner(command, **kwargs):
                calls.append((tuple(command), kwargs))
                self.assertTrue(
                    prepared.config_path.with_name("runtime-bindings.json").is_file()
                )
                if Path(command[0]).name == "make":
                    events.append("make")
                    run = Path(kwargs["cwd"])
                    sample_id = "Prob001_zero_sample01"
                    workspace = run / "fake-workspace"
                    workspace.mkdir()
                    (workspace / "TopModule.sv").write_text(
                        "module TopModule; endmodule\n"
                    )
                    commit_sample_bundle(
                        workspace=workspace,
                        output_path=run / "Prob001_zero" / f"{sample_id}.sv",
                        sample_id=sample_id,
                        agent="opencode",
                        model="qwen3.6-coder",
                        run_config_sha256=prepared.digest,
                        process=ProcessResult(
                            status="completed",
                            exit_code=0,
                            duration_seconds=1,
                            stdout="",
                            stderr="",
                            usage=AgentUsage(
                                input_tokens=None,
                                output_tokens=None,
                                turns=0,
                                tool_calls=0,
                                usage_source="trajectory",
                            ),
                        ),
                        limits=limits(),
                        runtime={
                            "source_revision": prepared.config["runtime"]["source_commit"],
                            "docker_image_id": prepared.config["runtime"]["docker_image_id"],
                            "docker_daemon_identity": prepared.config["runtime"]["docker_daemon_identity"],
                            "agent_tools_content_sha256": prepared.config["runtime"]["agent_tools"]["content_sha256"],
                            "endpoint_base_url": prepared.config["endpoint"]["base_url"],
                            "endpoint_evidence_sha256": prepared.endpoint_evidence["response_sha256"],
                        },
                    )
                    (run / "summary.csv").write_text(
                        "Prob001_zero,1,1,1.0,.\n"
                    )
                return subprocess.CompletedProcess(command, 0)

            report_path = execute_prepared_run(
                prepared,
                options,
                command_runner=command_runner,
                broker_factory=Broker,
                material_validator=lambda _prepared: None,
            )

            self.assertEqual([Path(call[0][0]).name for call in calls], ["configure", "make"])
            recorded_argv = " ".join(
                argument for call in calls for argument in call[0]
            )
            self.assertNotIn("sv-agent-generate", recorded_argv)
            self.assertEqual(events, ["broker_started", "make", "broker_stopped"])
            self.assertEqual(report_path, prepared.config_path.parent / "agent-summary.json")
            self.assertTrue(report_path.is_file())
            self.assertFalse(prepared.config_path.with_name("runtime-bindings.json").exists())
            self.assertNotIn("OPENAI_API_KEY", calls[0][1]["env"])
            self.assertNotIn("OPENAI_API_KEY", calls[1][1]["env"])
            self.assertTrue(
                prepared.config_path.with_name("agent-summary.txt").is_file()
            )
            self.assertTrue(prepared.config_path.with_name("summary.csv").is_file())

    def test_make_failure_leaves_no_report_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options, prepared = self.prepared_fixture(root)
            source = Path(prepared.bindings["source_root"])
            source.mkdir(parents=True, exist_ok=True)
            configure = source / "configure"
            configure.write_text("#!/bin/sh\nexit 0\n")
            configure.chmod(0o755)

            class Broker:
                def __init__(self, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass

            def runner(command, **kwargs):
                if Path(command[0]).name == "make":
                    work = Path(kwargs["cwd"]) / ".agent-work"
                    work.mkdir(exist_ok=True)
                    (work / "crash-remnant").write_text("partial\n")
                    return subprocess.CompletedProcess(command, 7)
                return subprocess.CompletedProcess(command, 0)

            with self.assertRaises(RunnerError):
                execute_prepared_run(
                    prepared,
                    options,
                    command_runner=runner,
                    broker_factory=Broker,
                    material_validator=lambda _prepared: None,
                )
            self.assertFalse((prepared.config_path.parent / "agent-summary.json").exists())
            self.assertFalse(prepared.config_path.with_name("runtime-bindings.json").exists())


class ClosedEnvironmentTests(unittest.TestCase):
    def test_formal_runner_environment_is_an_allowlist(self):
        result = formal_runner_environment(
            {
                "PATH": "/pinned/bin",
                "VERILOG_EVAL_ROOT": "/source",
                "AGENT_EVAL_DCD_PI_BUNDLE": "/opt/agent/dcd-pi.tar",
                "CUSTOM_API_KEY": "secret",
                "OPENAI_API_KEY": "unselected-secret",
                "OTHER_SECRET": "leak",
                "HTTP_PROXY": "http://proxy",
                "BASH_ENV": "/leak",
            },
            api_key_environment="CUSTOM_API_KEY",
        )
        self.assertEqual(
            result,
            {
                "PATH": "/pinned/bin",
                "VERILOG_EVAL_ROOT": "/source",
                "AGENT_EVAL_DCD_PI_BUNDLE": "/opt/agent/dcd-pi.tar",
                "CUSTOM_API_KEY": "secret",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

    def test_runner_environment_keeps_only_explicit_inputs_and_selected_secret(self):
        ambient = {
            "PATH": "/ambient/bin",
            "HOME": "/ambient/home",
            "OPENAI_API_KEY": "secret",
            "OTHER_SECRET": "leak",
            "PYTHONPATH": "/leak",
            "HTTP_PROXY": "http://proxy",
            "SSL_CERT_FILE": "/ca.pem",
        }
        result = runner_environment(
            ambient,
            path="/pinned/bin",
            home="/private/home",
            api_key_environment="OPENAI_API_KEY",
        )
        self.assertEqual(
            result,
            {
                "PATH": "/pinned/bin",
                "HOME": "/private/home",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "SSL_CERT_FILE": "/ca.pem",
                "OPENAI_API_KEY": "secret",
            },
        )

    def test_configure_make_and_docker_environments_are_separate(self):
        toolchain = {"PATH": "/pinned/bin", "SHELL": "/pinned/bin/bash"}
        configure = configure_environment(toolchain, home="/tmp/config-home")
        make = make_environment(toolchain)
        docker = docker_environment(
            docker_host="unix:///var/run/docker.sock", path="/pinned/bin"
        )

        self.assertEqual(configure["HOME"], "/tmp/config-home")
        self.assertNotIn("OPENAI_API_KEY", configure)
        self.assertNotIn("MAKEFLAGS", make)
        self.assertEqual(docker["DOCKER_HOST"], "unix:///var/run/docker.sock")
        for environment in (configure, make, docker):
            for forbidden in (
                "PYTHONPATH",
                "BASH_ENV",
                "ENV",
                "NODE_OPTIONS",
                "HTTP_PROXY",
                "DOCKER_CONFIG",
            ):
                self.assertNotIn(forbidden, environment)


if __name__ == "__main__":
    unittest.main()
