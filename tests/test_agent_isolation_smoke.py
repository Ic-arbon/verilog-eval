import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "tests" / "integration" / "agent-docker-isolation-smoke"
TIMEOUT_SMOKE_SCRIPT = (
    ROOT / "tests" / "integration" / "agent-docker-timeout-smoke"
)


class AgentDockerIsolationSmokeContractTests(unittest.TestCase):
    def test_smoke_uses_a_fake_agent_and_fresh_make_build(self):
        self.assertTrue(SMOKE_SCRIPT.is_file())
        self.assertTrue(os.access(SMOKE_SCRIPT, os.X_OK))

        source = SMOKE_SCRIPT.read_text()
        self.assertIn("AGENT_EVAL_AGENT_TOOLS", source)
        self.assertIn("node_modules/.bin/opencode", source)
        self.assertIn("VERILOG_EVAL_BUILD_ROOT", source)
        self.assertIn("nix run", source)
        self.assertIn("--with-generator=agent", source)
        self.assertIn("--with-problems=", source)
        self.assertIn('chmod -R u+w "$scratch"', source)
        self.assertIn('rm -rf "$scratch" || true', source)

    def test_smoke_searches_for_hidden_grader_and_host_capabilities(self):
        source = SMOKE_SCRIPT.read_text()

        self.assertIn("Prob001_zero_ref.sv", source)
        self.assertIn("Prob001_zero_test.sv", source)
        self.assertIn("agent-hidden-sentinel", source)
        self.assertIn("/var/run/docker.sock", source)
        self.assertIn("/opt/agent/verilog-eval", source)
        self.assertIn('execution_status == "completed"', source)
        self.assertIn('submission_status == "published"', source)
        self.assertIn('report_path = manifest_path.parents[1] / "agent-summary.json"', source)
        self.assertIn('{"completed": 1}', source)

    def test_timeout_smoke_keeps_execution_and_submission_orthogonal(self):
        self.assertTrue(TIMEOUT_SMOKE_SCRIPT.is_file())
        self.assertTrue(os.access(TIMEOUT_SMOKE_SCRIPT, os.X_OK))

        source = TIMEOUT_SMOKE_SCRIPT.read_text()
        self.assertIn("node_modules/.bin/opencode", source)
        self.assertIn("sleep 120", source)
        self.assertIn("--with-agent-timeout=2", source)
        self.assertIn('execution_status == "timeout"', source)
        self.assertIn('submission_status == "published"', source)
        self.assertIn("exit_code == 124", source)
        self.assertIn("no new Agent containers remain", source)
        self.assertIn('chmod -R u+w "$scratch"', source)
        self.assertIn('report_path = manifest_path.parents[1] / "agent-summary.json"', source)
        self.assertIn('{"timeout": 1}', source)


if __name__ == "__main__":
    unittest.main()
