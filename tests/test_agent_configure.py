from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_generator_architecture import ConfiguredGeneratorSeamTests, nul_records


class AgentConfigureTests(unittest.TestCase):
    def configure(self, root: Path, *arguments: str):
        return ConfiguredGeneratorSeamTests().configure(root, *arguments)

    def test_default_model_generator_keeps_legacy_static_option_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, build = self.configure(Path(tmp), "--with-model=qwen3.6-coder")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                nul_records(build / ".generator-args"),
                [
                    "--model=qwen3.6-coder",
                    "--examples=0",
                    "--task=spec-to-rtl",
                    "--max-tokens=1024",
                    "--temperature=0.85",
                    "--top-p=0.95",
                ],
            )

    def test_agent_generator_accepts_only_opaque_config_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run-config.json"
            config.write_text("{}\n")
            result, build = self.configure(
                root,
                "--with-generator=agent",
                f"--with-generator-config={config}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                nul_records(build / ".generator-args"),
                [f"--run-config={config}"],
            )
            makefile = (build / "Makefile").read_text()
            for leaked in (
                "opencode",
                "pi",
                "agent-timeout",
                "agent-thinking",
                "trajectory",
                "generation.json",
                "agent-summary",
            ):
                self.assertNotIn(leaked, makefile)

    def test_agent_generator_requires_config_and_rejects_old_agent_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _build = self.configure(Path(tmp), "--with-generator=agent")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires --with-generator-config", result.stdout + result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run-config.json"
            config.write_text("{}\n")
            result, _build = self.configure(
                root,
                "--with-generator=agent",
                f"--with-generator-config={config}",
                "--with-agent-timeout=1",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("unrecognized options", result.stdout + result.stderr)

    def test_invalid_generator_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _build = self.configure(
                Path(tmp), "--with-generator=transcript-extractor"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be model or agent", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
