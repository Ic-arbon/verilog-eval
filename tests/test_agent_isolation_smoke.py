import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "tests" / "integration" / "agent-docker-isolation-smoke"


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


if __name__ == "__main__":
    unittest.main()
