from __future__ import annotations

import collections
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = "245c19918f18abb7e6aa328282f3624afc0e2884"
SCANNED_ROOTS = (
    "agent_generation",
    "scripts",
    "docs",
)
SCANNED_FILES = ("configure.ac", "configure", "Makefile.in", "flake.nix", "README.md", "CONTEXT.md")
PATTERNS = (
    re.compile(r"\bagent-(?:generation|evaluation)/v[0-9]+\b"),
    re.compile(r"\b(?:schema_version|profile_id)\b"),
    re.compile(r"\b(?:pi|opencode|agent|sandbox)[A-Za-z0-9_-]*-v[0-9]+\b", re.IGNORECASE),
    re.compile(r"\bverilog-eval-agent-[A-Za-z0-9_.-]*:v[0-9]+\b"),
)
NUMBERED_FILENAME = re.compile(
    r"(?:agent|generator|schema|profile|protocol|sandbox)[^/]*[-_]v?[0-9]+(?:\.|$)",
    re.IGNORECASE,
)
EXCLUDED = {
    Path("plans/deepen-generator-seam.md"),  # accepted migration evidence
}


def current_paths() -> list[Path]:
    paths = [ROOT / name for name in SCANNED_FILES if (ROOT / name).is_file()]
    for directory in SCANNED_ROOTS:
        root = ROOT / directory
        if not root.exists():
            continue
        paths.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.relative_to(ROOT) not in EXCLUDED
        )
    return sorted(set(paths))


def terms(path: Path, content: str) -> collections.Counter[str]:
    found: collections.Counter[str] = collections.Counter()
    relative = path.relative_to(ROOT).as_posix()
    if NUMBERED_FILENAME.search(relative):
        found[f"filename:{relative}"] += 1
    semantic_content = "\n".join(
        line
        for line in content.splitlines()
        if "owned-version-negative-guard" not in line
    )
    for pattern in PATTERNS:
        found.update(match.group(0) for match in pattern.finditer(semantic_content))
    return found


def baseline_content(relative: Path) -> str:
    result = subprocess.run(
        ("git", "show", f"{BASE}:{relative.as_posix()}"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


class OwnedVersionLabelGuardTests(unittest.TestCase):
    def test_no_agent_owned_numbered_identifier_is_added_since_fixed_revision(self):
        current: collections.Counter[str] = collections.Counter()
        baseline: collections.Counter[str] = collections.Counter()
        for path in current_paths():
            relative = path.relative_to(ROOT)
            current.update(terms(path, path.read_text(encoding="utf-8", errors="replace")))
            baseline.update(terms(path, baseline_content(relative)))

        additions = {
            token: count - baseline[token]
            for token, count in current.items()
            if count > baseline[token]
        }
        self.assertEqual(additions, {})


if __name__ == "__main__":
    unittest.main()
