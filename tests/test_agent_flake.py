from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class AgentFlakeIntegrationTests(unittest.TestCase):
    def test_agent_app_uses_make_as_the_only_benchmark_orchestrator(self):
        flake = (REPO_ROOT / "flake.nix").read_text()
        agent_app = flake.split("runAgentEvaluation =", 1)[1].split(
            "runVllmEvaluation =", 1
        )[0]

        self.assertIn("--with-generator=agent", agent_app)
        self.assertIn("--with-model=qwen3.6-coder", agent_app)
        self.assertIn("--with-task=spec-to-rtl", agent_app)
        self.assertIn('"$root/configure"', agent_app)
        self.assertIn("exec make", agent_app)
        self.assertNotIn("agent_eval/runner.py", agent_app)
        self.assertNotIn("PYTHONPATH", agent_app)
        self.assertNotIn("bubblewrap", agent_app)

    def test_agent_app_loads_selected_pinned_image_and_scopes_credentials(self):
        flake = (REPO_ROOT / "flake.nix").read_text()
        agent_app = flake.split("runAgentEvaluation =", 1)[1].split(
            "runVllmEvaluation =", 1
        )[0]

        self.assertIn("docker load --input", agent_app)
        self.assertIn("docker image inspect", agent_app)
        self.assertIn("AGENT_EVAL_DOCKER_IMAGE_BASE", agent_app)
        self.assertIn("AGENT_EVAL_DOCKER_IMAGE_RTL", agent_app)
        self.assertIn("AGENT_EVAL_AGENT_TOOLS", agent_app)
        self.assertIn("OPENAI_API_BASE", agent_app)
        self.assertIn("OPENAI_API_KEY", agent_app)
        self.assertIn("VERILOG_EVAL_SOURCE_REVISION", agent_app)
        self.assertIn("VERILOG_EVAL_SOURCE_DIFF_SHA256", agent_app)
        self.assertIn("AGENT_EVAL_DOCKER_IMAGE_ID", agent_app)
        self.assertIn("AGENT_EVAL_AGENT_TOOLS_VERSIONS", agent_app)
        self.assertNotIn("AGENT_EVAL_STORE_ROOTS", agent_app)

    def test_build_hashes_include_source_and_endpoint_provenance(self):
        flake = (REPO_ROOT / "flake.nix").read_text()
        model_app = flake.split("runEvaluation =", 1)[1].split(
            "runAgentEvaluation =", 1
        )[0]
        agent_app = flake.split("runAgentEvaluation =", 1)[1].split(
            "runVllmEvaluation =", 1
        )[0]

        for app in (model_app, agent_app):
            with self.subTest(app=app[:40]):
                self.assertIn("source_revision", app)
                self.assertIn("source_diff_sha256", app)
                self.assertIn("OPENAI_API_BASE", app)
                self.assertIn('printf \'%s\\0\'', app)


if __name__ == "__main__":
    unittest.main()
