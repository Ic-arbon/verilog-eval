from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AgentFlakeIntegrationTests(unittest.TestCase):
    def agent_app(self) -> str:
        flake = (ROOT / "flake.nix").read_text()
        return flake.split("runAgentEvaluation =", 1)[1].split(
            "runVllmEvaluation =", 1
        )[0]

    def test_agent_app_only_binds_resources_and_execs_python_runner(self):
        app = self.agent_app()
        self.assertIn("scripts/run-agent-evaluation", app)
        self.assertIn("python3 -I", app)
        self.assertIn("os.execve(", app)
        self.assertIn('python = "${pkgs.python311}/bin/python3"', app)
        self.assertIn('"BASH_ENV"', app)
        self.assertIn('"LD_PRELOAD"', app)
        self.assertIn("AGENT_EVAL_DOCKER_IMAGE_STANDARD", app)
        self.assertIn("AGENT_EVAL_DOCKER_ARCHIVE_STANDARD", app)
        self.assertIn("AGENT_EVAL_DOCKER_IMAGE_RTL", app)
        self.assertIn("AGENT_EVAL_DOCKER_ARCHIVE_RTL", app)
        for forbidden in (
            '"$root/configure"',
            "exec make",
            "curl",
            "health",
            "npm",
            "verilog-agent-tools-setup",
            "VERILOG_EVAL_VLLM_KEY_FILE",
            "source_revision",
            "config_key",
            "agent-nix-eval",
        ):
            self.assertNotIn(forbidden, app)

    def test_agent_image_tags_are_semantic_and_source_is_not_build_context(self):
        flake = (ROOT / "flake.nix").read_text()
        self.assertIn('agentSandboxImageTag = "standard"', flake)
        self.assertIn('minimalRtlSandboxImageTag = "rtl"', flake)
        image_builder = flake.split("mkAgentSandboxImage =", 1)[1].split(
            "pythonRequirements =", 1
        )[0]
        self.assertIn("dockerTools.buildLayeredImage", image_builder)
        self.assertNotIn("copyToRoot = ./.;", image_builder)
        self.assertNotIn('tag = "v1"', image_builder)

    def test_setup_remains_separate_from_formal_agent_app(self):
        flake = (ROOT / "flake.nix").read_text()
        setup = flake.split("setupAgentTools =", 1)[1].split("setupPython =", 1)[0]
        self.assertIn("npm install", setup)
        self.assertNotIn("npm install", self.agent_app())


if __name__ == "__main__":
    unittest.main()
