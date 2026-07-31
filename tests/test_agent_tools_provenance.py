import os
import tempfile
import unittest
from pathlib import Path

from agent_generation.provenance import AgentToolsError, digest_agent_tools


ROOT = Path(__file__).resolve().parents[1]
DIGEST_SCRIPT = ROOT / "scripts" / "agent-tools-digest"


def make_tools(root: Path) -> Path:
    tools = root / "tools"
    package = tools / "node_modules" / "fake-agent"
    binaries = tools / "node_modules" / ".bin"
    package.mkdir(parents=True)
    binaries.mkdir()
    (package / "cli.js").write_text("console.log('v1');\n")
    (binaries / "opencode").symlink_to("../fake-agent/cli.js")
    return tools


class AgentToolsProvenanceTests(unittest.TestCase):
    def test_digest_is_path_and_mtime_independent_but_content_sensitive(self):
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = make_tools(Path(first_tmp))
            second = make_tools(Path(second_tmp))

            first_digest = digest_agent_tools(first)
            os.utime(second / "node_modules" / "fake-agent" / "cli.js", (1, 1))
            second_digest = digest_agent_tools(second)

            self.assertEqual(first_digest, second_digest)
            (second / "node_modules" / "fake-agent" / "cli.js").write_text(
                "console.log('modified source');\n"
            )
            self.assertNotEqual(first_digest, digest_agent_tools(second))

    def test_symlink_that_escapes_explicit_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = make_tools(root)
            (tools / "node_modules" / ".bin" / "escape").symlink_to(
                root / "outside"
            )

            with self.assertRaises(AgentToolsError):
                digest_agent_tools(tools)

    def test_digest_cli_is_an_executable_non_secret_interface(self):
        self.assertTrue(DIGEST_SCRIPT.is_file())
        self.assertTrue(os.access(DIGEST_SCRIPT, os.X_OK))
        source = DIGEST_SCRIPT.read_text()
        self.assertIn("digest_agent_tools", source)
        self.assertNotIn("OPENAI_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
