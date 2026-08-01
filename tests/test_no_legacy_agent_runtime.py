import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NoLegacyAgentRuntimeTests(unittest.TestCase):
    def test_legacy_runner_package_and_entrypoints_are_removed(self):
        legacy_paths = (
            ROOT / "agent_eval",
            ROOT / "scripts" / "agent-eval",
            ROOT / "scripts" / "agent-eval-stats",
            ROOT / "scripts" / "sv-agent-analyze",
        )
        for path in legacy_paths:
            with self.subTest(path=path):
                self.assertFalse(path.exists())

    def test_public_docs_name_only_the_make_driven_agent_entrypoint(self):
        documentation = "\n".join(
            (
                (ROOT / "README.md").read_text(),
                (ROOT / "docs" / "agent-evaluation.md").read_text(),
            )
        )
        self.assertNotIn("agent_eval/", documentation)
        self.assertNotIn("./scripts/agent-eval ", documentation)
        self.assertNotIn("./scripts/agent-eval-stats", documentation)
        self.assertNotIn("Bubblewrap", documentation)
        self.assertIn("nix run .#agent-eval", documentation)
        self.assertIn("agent-summary.json", documentation)


if __name__ == "__main__":
    unittest.main()
