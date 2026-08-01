from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


class GeneratorReachabilityTests(unittest.TestCase):
    def test_atomic_cutover_has_one_live_runner_and_one_sample_adapter(self):
        runner_entry = ROOT / "scripts/run-agent-evaluation"
        sample_entry = ROOT / "scripts/sv-agent-generate"
        self.assertIn("agent_generation.run", imports(runner_entry))
        self.assertIn("agent_generation.cli", imports(sample_entry))
        self.assertIn("agent_generation.sample", imports(ROOT / "agent_generation/cli.py"))
        self.assertFalse((ROOT / "agent_generation/submission.py").exists())
        self.assertFalse((ROOT / "agent_generation/report_transaction.py").exists())

    def test_only_make_facing_cli_can_reach_one_sample_generator(self):
        run_source = (ROOT / "agent_generation/run.py").read_text()
        self.assertIn("execute_prepared_run", run_source)
        self.assertIn("configure_command", run_source)
        self.assertIn("--jobs=", run_source)
        for forbidden in (
            "generate_agent_sample(",
            "run_agent_generation(",
            "sv-agent-generate",
            "DockerExecutor(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, run_source)

    def test_old_broad_sample_cli_and_duplicate_transactions_are_absent(self):
        cli = (ROOT / "agent_generation/cli.py").read_text()
        for forbidden in (
            "--temperature",
            "--top-p",
            "--agent-timeout",
            "--agent-thinking",
            "profile_id",
            "publish_submission",
            "write_generation_sidecars",
        ):
            self.assertNotIn(forbidden, cli)
        sample_transaction_definitions = 0
        report_transaction_definitions = 0
        for path in (ROOT / "agent_generation").glob("*.py"):
            source = path.read_text()
            sample_transaction_definitions += source.count("def commit_sample_bundle(")
            report_transaction_definitions += source.count("def commit_report_pair(")
        self.assertEqual(sample_transaction_definitions, 1)
        self.assertEqual(report_transaction_definitions, 1)

    def test_configure_and_make_know_only_generic_generator_seam(self):
        configure = (ROOT / "configure.ac").read_text()
        make = (ROOT / "Makefile.in").read_text()
        self.assertIn("--with-generator-config", configure)
        self.assertIn("selected_generator", configure)
        self.assertIn("GENERATOR_INVOKER", make)
        for leaked in (
            "opencode",
            "agent-timeout",
            "agent-thinking",
            "agent-tool-profile",
            "trajectory",
            "generation.json",
            "agent-summary",
        ):
            self.assertNotIn(leaked, configure)
            self.assertNotIn(leaked, make)


if __name__ == "__main__":
    unittest.main()
