from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_generation.contracts import AgentUsage, ProcessResult
from agent_generation.result_contract import ResultContractError
from agent_generation.sample_result import (
    INVALID_CANDIDATE_BYTES,
    SampleInfrastructureError,
    commit_sample_bundle,
    sample_sidecar_paths,
    validate_sample_bundle,
)


DIGEST = "a" * 64


def completed(stdout='{"type":"done"}\n', stderr="diagnostic\n") -> ProcessResult:
    return ProcessResult(
        status="completed",
        exit_code=0,
        duration_seconds=1.5,
        stdout=stdout,
        stderr=stderr,
        usage=AgentUsage(
            input_tokens=10,
            output_tokens=20,
            turns=2,
            tool_calls=1,
            usage_source="trajectory",
        ),
    )


def runtime() -> dict:
    return {
        "source_revision": "1" * 40,
        "docker_image_id": "sha256:" + "2" * 64,
        "docker_daemon_identity": "sha256:" + "3" * 64,
        "agent_tools_content_sha256": "4" * 64,
        "endpoint_base_url": "http://127.0.0.1:58000/v1",
        "endpoint_evidence_sha256": "5" * 64,
    }


def limits() -> dict:
    return {
        "timeout_seconds": 300,
        "max_turns": 20,
        "max_tool_calls": 50,
        "max_input_tokens": 16384,
        "max_output_tokens": 16384,
    }


class SampleResultTransactionTests(unittest.TestCase):
    def test_frozen_invalid_candidate_fixture_is_exact(self):
        fixture = Path(__file__).parent / "fixtures/invalid-agent-submission.sv"
        self.assertEqual(fixture.read_bytes(), INVALID_CANDIDATE_BYTES)

    def commit(self, root: Path, *, process=None, redaction_values=(), fault=None):
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
        return commit_sample_bundle(
            workspace=workspace,
            output_path=output,
            sample_id="Prob001_zero_sample01",
            agent="pi",
            model="qwen3.6-coder",
            run_config_sha256=DIGEST,
            process=process or completed(),
            limits=limits(),
            runtime=runtime(),
            diagnostic_redaction_values=redaction_values,
            fault=fault,
        )

    def test_regular_workspace_candidate_commits_hash_valid_bundle_candidate_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            candidate = b"module TopModule(output zero); assign zero=1'b0; endmodule\n"
            (workspace / "TopModule.sv").write_bytes(candidate)
            events = []

            manifest = self.commit(root, fault=events.append)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            paths = sample_sidecar_paths(output)

            self.assertEqual(output.read_bytes(), candidate)
            self.assertEqual(manifest["submission"]["status"], "published")
            self.assertEqual(
                manifest["submission"]["source_sha256"],
                hashlib.sha256(candidate).hexdigest(),
            )
            self.assertEqual(
                manifest["artifacts"]["candidate"]["sha256"],
                hashlib.sha256(candidate).hexdigest(),
            )
            self.assertEqual(json.loads(paths["manifest"].read_text()), manifest)
            self.assertNotIn("manifest", manifest["artifacts"])
            self.assertLess(events.index("sidecars_synced"), events.index("candidate_renamed"))
            self.assertEqual(validate_sample_bundle(output, DIGEST), manifest)

    def test_chat_only_candidate_uses_frozen_invalid_verilog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            process = completed(stdout="module TopModule; endmodule\n")

            manifest = self.commit(root, process=process)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"

            self.assertEqual(output.read_bytes(), INVALID_CANDIDATE_BYTES)
            self.assertEqual(manifest["submission"]["status"], "missing")
            self.assertIsNone(manifest["submission"]["source_sha256"])

    def test_candidate_content_is_opaque_to_diagnostic_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            redaction_value = "local"
            candidate = (
                b"module TopModule(output logic zero); "
                b"localparam ZERO = 1'b0; assign zero = ZERO; endmodule\n"
            )
            (workspace / "TopModule.sv").write_bytes(candidate)
            process = completed(stdout=redaction_value, stderr=redaction_value)

            manifest = self.commit(
                root,
                process=process,
                redaction_values=(redaction_value,),
            )
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            paths = sample_sidecar_paths(output)

            self.assertEqual(output.read_bytes(), candidate)
            self.assertEqual(manifest["submission"]["status"], "published")
            self.assertNotIn(redaction_value, paths["trajectory"].read_text())
            self.assertNotIn(redaction_value, paths["stderr"].read_text())

    def test_secret_is_redacted_from_trajectory_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "transport-secret-canary"
            process = completed(stdout=f'{{"text":"{secret}"}}\n', stderr=secret)
            manifest = self.commit(
                root,
                process=process,
                redaction_values=(secret,),
            )
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            paths = sample_sidecar_paths(output)

            self.assertNotIn(secret, paths["trajectory"].read_text())
            self.assertNotIn(secret, paths["stderr"].read_text())
            self.assertEqual(manifest["usage"]["input_tokens"], 10)

    def test_regular_partial_sidecars_without_candidate_are_removed_then_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            paths = sample_sidecar_paths(output)
            output.parent.mkdir(parents=True)
            paths["trajectory"].write_text("partial")
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "TopModule.sv").write_text("module TopModule; endmodule\n")

            manifest = self.commit(root)

            self.assertEqual(manifest["submission"]["status"], "published")
            self.assertEqual(paths["trajectory"].read_text(), completed().stdout)

    def test_suspicious_partial_or_candidate_mismatch_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            paths = sample_sidecar_paths(output)
            output.parent.mkdir(parents=True)
            outside = root / "outside"
            outside.write_text("secret")
            paths["stderr"].symlink_to(outside)
            (root / "workspace").mkdir()
            with self.assertRaises(SampleInfrastructureError):
                self.commit(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.commit(root)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            output.write_text("tampered\n")
            with self.assertRaises(SampleInfrastructureError):
                self.commit(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.commit(root)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            paths = sample_sidecar_paths(output)
            output.unlink()
            paths["trajectory"].write_text("mismatched partial trajectory\n")
            with self.assertRaises(SampleInfrastructureError):
                self.commit(root)

    def test_catchable_failure_after_candidate_rename_removes_completion_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fail(event):
                if event == "candidate_renamed":
                    raise OSError("injected sync failure")

            with self.assertRaises(SampleInfrastructureError):
                self.commit(root, fault=fail)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            self.assertFalse(output.exists())

    def test_invalid_execution_contract_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = ProcessResult(
                status="completed",
                exit_code=9,
                duration_seconds=1,
                stdout="",
                stderr="",
                usage=AgentUsage.unavailable(),
            )
            with self.assertRaises((ResultContractError, SampleInfrastructureError)):
                self.commit(root, process=invalid)
            output = root / "build/Prob001_zero/Prob001_zero_sample01.sv"
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
