import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKES = {
    "isolation": ROOT / "tests/integration/agent-docker-isolation-smoke",
    "timeout": ROOT / "tests/integration/agent-docker-timeout-smoke",
    "budget": ROOT / "tests/integration/agent-docker-budget-smoke",
}


class AgentDockerIsolationSmokeContractTests(unittest.TestCase):
    def test_smokes_use_content_addressed_runner_and_explicit_fake_tools(self):
        for name, path in SMOKES.items():
            with self.subTest(name=name):
                self.assertTrue(path.is_file())
                self.assertTrue(os.access(path, os.X_OK))
                source = path.read_text()
                self.assertIn("AGENT_EVAL_AGENT_TOOLS", source)
                self.assertIn("package-lock.json", source)
                self.assertIn("--with-openai-api-base=", source)
                self.assertIn("--with-agent-toolset=standard", source)
                self.assertIn("--run-path-file=", source)
                self.assertIn("scripts/validate-agent-run", source)
                self.assertNotIn("--with-generator=agent", source)
                self.assertNotIn("--with-agent-tool-profile", source)
                self.assertIn('chmod -R u+w "$scratch"', source)
                self.assertIn('rm -rf "$scratch" || true', source)

    def test_isolation_smoke_searches_for_hidden_and_host_capabilities(self):
        source = SMOKES["isolation"].read_text()
        for canary in (
            "Prob001_zero_ref.sv",
            "Prob001_zero_test.sv",
            "agent-hidden-sentinel",
            "/var/run/docker.sock",
            "/opt/agent/verilog-eval",
            "AGENT_EVAL_SENTINEL_SECRET",
        ):
            self.assertIn(canary, source)
        self.assertIn('manifest["execution"]["status"] == "completed"', source)
        self.assertIn('manifest["submission"]["status"] == "published"', source)

    def test_budget_smoke_enforces_host_turns_with_reserved_outcome_code(self):
        source = SMOKES["budget"].read_text()
        self.assertIn("node_modules/@earendil-works/pi-coding-agent", source)
        self.assertIn('"type":"turn_end"', source)
        self.assertIn("--with-agent-max-turns=2", source)
        self.assertIn('manifest["execution"]["exit_code"]==86', source)
        self.assertIn('manifest["execution"]["termination_reason"]=="max_turns"', source)

    def test_timeout_smoke_keeps_execution_and_submission_orthogonal(self):
        source = SMOKES["timeout"].read_text()
        self.assertIn("sleep 120", source)
        self.assertIn("--with-agent-timeout=2", source)
        self.assertIn('manifest["execution"]["status"]=="timeout"', source)
        self.assertIn('manifest["submission"]["status"]=="published"', source)
        self.assertIn('manifest["execution"]["exit_code"]==124', source)


if __name__ == "__main__":
    unittest.main()
