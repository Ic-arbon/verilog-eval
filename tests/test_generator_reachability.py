from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNREACHABLE_MODULES = {
    "agent_generation.run",
    "agent_generation.runtime_bindings",
    "agent_generation.endpoint",
    "agent_generation.credentials",
    "agent_generation.tools",
}
LIVE_FILES = (
    ROOT / "agent_generation/cli.py",
    ROOT / "scripts/sv-agent-generate",
    ROOT / "configure.ac",
    ROOT / "Makefile.in",
    ROOT / "flake.nix",
)


def imported_modules(path: Path) -> set[str]:
    if path.suffix != ".py":
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class GeneratorReachabilityTests(unittest.TestCase):
    def test_preparation_modules_are_unreachable_before_atomic_cutover(self):
        direct_imports = set()
        live_text = ""
        for path in LIVE_FILES:
            live_text += path.read_text(encoding="utf-8") + "\n"
            direct_imports.update(imported_modules(path))

        self.assertTrue(UNREACHABLE_MODULES.isdisjoint(direct_imports))
        for module in UNREACHABLE_MODULES:
            self.assertNotIn(module, live_text)

    def test_preparation_does_not_invoke_configure_make_report_or_sample(self):
        source = (ROOT / "agent_generation/run.py").read_text(encoding="utf-8")
        for forbidden in (
            "sv-agent-generate",
            "sv-agent-analyze",
            "subprocess.run((\"make\"",
            "subprocess.run((\"./configure\"",
            "run_agent_generation(",
            "build_agent_report(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
