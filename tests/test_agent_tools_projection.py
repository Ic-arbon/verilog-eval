from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_generation.tools import ToolsProjectionError, project_agent_tools


def make_tools(root: Path) -> Path:
    tools = root / "tools"
    agent = tools / "node_modules" / "opencode-ai"
    shared = tools / "node_modules" / "shared"
    unrelated = tools / "node_modules" / "unrelated"
    bins = tools / "node_modules" / ".bin"
    stale_nested = agent / "node_modules" / "stale-package"
    for path in (agent, shared, unrelated, bins, stale_nested):
        path.mkdir(parents=True, exist_ok=True)
    (agent / "cli.js").write_text("require('shared')\n", encoding="utf-8")
    (agent / "package.json").write_text(
        json.dumps({"name": "opencode-ai", "version": "1.18.7"}), encoding="utf-8"
    )
    (shared / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    (shared / "package.json").write_text(
        json.dumps({"name": "shared", "version": "2.0.0"}), encoding="utf-8"
    )
    (unrelated / "secret-tool").write_text("not selected\n", encoding="utf-8")
    (unrelated / "package.json").write_text(
        json.dumps({"name": "unrelated", "version": "9.0.0"}), encoding="utf-8"
    )
    (stale_nested / "index.js").write_text("stale and unlocked\n", encoding="utf-8")
    (bins / "opencode").symlink_to("../opencode-ai/cli.js")
    (bins / "unrelated").symlink_to("../unrelated/secret-tool")
    lock = {
        "name": "agent-tools",
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"opencode-ai": "1.18.7"}},
            "node_modules/opencode-ai": {
                "version": "1.18.7",
                "dependencies": {"shared": "2.0.0"},
                "peerDependencies": {"optional-peer": "1.0.0"},
                "peerDependenciesMeta": {"optional-peer": {"optional": True}},
                "bin": {"opencode": "cli.js"},
            },
            "node_modules/shared": {"version": "2.0.0"},
            "node_modules/unrelated": {"version": "9.0.0", "dev": True},
        },
    }
    (tools / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return tools


class ToolsProjectionTests(unittest.TestCase):
    def test_selected_agent_and_transitive_packages_are_copied_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_tools(root)
            projection = project_agent_tools(
                source_prefix=source,
                projection_root=root / "projections",
                agent="opencode",
            )

            self.assertTrue((projection.path / "node_modules/opencode-ai/cli.js").is_file())
            self.assertTrue((projection.path / "node_modules/shared/index.js").is_file())
            self.assertFalse((projection.path / "node_modules/unrelated").exists())
            self.assertFalse(
                (projection.path / "node_modules/opencode-ai/node_modules/stale-package").exists()
            )
            self.assertTrue((projection.path / "node_modules/.bin/opencode").is_symlink())
            self.assertFalse((projection.path / "node_modules/.bin/unrelated").exists())
            self.assertRegex(projection.content_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(projection.lock_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual(
                projection.versions,
                {"opencode-ai": "1.18.7", "shared": "2.0.0"},
            )
            source_inode = (source / "node_modules/opencode-ai/cli.js").stat().st_ino
            projected_inode = (
                projection.path / "node_modules/opencode-ai/cli.js"
            ).stat().st_ino
            self.assertNotEqual(source_inode, projected_inode)
            self.assertEqual(projection.path.stat().st_mode & 0o222, 0)

    def test_projection_reuse_requires_exact_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_tools(root)
            first = project_agent_tools(source, root / "projections", "opencode")
            second = project_agent_tools(source, root / "projections", "opencode")
            self.assertEqual(first, second)

            (source / "node_modules/shared/index.js").write_text("changed\n")
            changed = project_agent_tools(source, root / "projections", "opencode")
            self.assertNotEqual(first.path, changed.path)

    def test_npmrc_cache_external_symlink_and_hardlink_are_rejected(self):
        mutations = ("npmrc", "cache", "escape", "hardlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = make_tools(root)
                if mutation == "npmrc":
                    (source / ".npmrc").write_text("//registry/:_authToken=secret\n")
                elif mutation == "cache":
                    cache = source / ".npm"
                    cache.mkdir()
                    (cache / "entry").write_text("cache")
                elif mutation == "escape":
                    (source / "node_modules/opencode-ai/escape").symlink_to(root / "outside")
                else:
                    target = source / "node_modules/opencode-ai/cli.js"
                    os.link(target, source / "node_modules/opencode-ai/hardlink.js")

                with self.assertRaises(ToolsProjectionError):
                    project_agent_tools(source, root / "projections", "opencode")


if __name__ == "__main__":
    unittest.main()
