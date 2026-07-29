import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_eval.provenance import (
    snapshot_agent_tools,
    snapshot_git_worktree,
    write_agent_source_provenance,
)


class AgentToolsSnapshotTests(unittest.TestCase):
    def test_snapshot_is_frozen_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local-tools"
            package_bin = source / "node_modules/opencode-ai/bin"
            package_bin.mkdir(parents=True)
            executable = package_bin / "opencode"
            executable.write_text("version one\n")
            executable.chmod(0o755)
            bin_dir = source / "node_modules/.bin"
            bin_dir.mkdir()
            os.symlink("../opencode-ai/bin/opencode", bin_dir / "opencode")

            snapshot = snapshot_agent_tools(
                source=source,
                destination=root / "snapshot",
                agent="opencode",
            )
            executable.write_text("version two\n")

            self.assertTrue(snapshot.path.is_dir())
            self.assertEqual(
                (snapshot.path / "node_modules/opencode-ai/bin/opencode").read_text(),
                "version one\n",
            )
            self.assertEqual(len(snapshot.digest), 64)
            self.assertTrue((snapshot.path / "node_modules/.bin/opencode").is_file())

    def test_snapshot_rejects_symlink_outside_tools_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local-tools"
            bin_dir = source / "node_modules/.bin"
            bin_dir.mkdir(parents=True)
            os.symlink("/tmp/untrusted-opencode", bin_dir / "opencode")

            with self.assertRaisesRegex(ValueError, "escapes Agent tools root"):
                snapshot_agent_tools(
                    source=source,
                    destination=root / "snapshot",
                    agent="opencode",
                )


class HarnessSnapshotTests(unittest.TestCase):
    def test_git_worktree_snapshot_excludes_git_and_freezes_dirty_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "harness"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Test"],
                check=True,
            )
            config = source / "opencode.json"
            config.write_text('{"agent":{"chip-rtl":{"mode":"all"}}}\n')
            skill = source / "plugins/rtl/skills/rtl-design/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# RTL skill\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(source), "commit", "-qm", "initial"],
                check=True,
            )
            skill.write_text("# Updated RTL skill\n")

            snapshot = snapshot_git_worktree(source, root / "snapshot")
            skill.write_text("# Changed after snapshot\n")

            self.assertFalse((snapshot.path / ".git").exists())
            self.assertEqual(
                (snapshot.path / "plugins/rtl/skills/rtl-design/SKILL.md").read_text(),
                "# Updated RTL skill\n",
            )
            self.assertEqual(len(snapshot.digest), 64)
            self.assertTrue((snapshot.path / "opencode.json").is_file())


class SourceProvenanceTests(unittest.TestCase):
    def test_dirty_local_git_source_records_commit_patch_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent-source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Test"],
                check=True,
            )
            tracked = source / "agent.ts"
            tracked.write_text("export const version = 1;\n")
            subprocess.run(["git", "-C", str(source), "add", "agent.ts"], check=True)
            subprocess.run(
                ["git", "-C", str(source), "commit", "-qm", "initial"],
                check=True,
            )
            tracked.write_text("export const version = 2;\n")
            (source / "new-tool.ts").write_text("export const tool = true;\n")

            output = root / "run/opencode"
            metadata = write_agent_source_provenance(
                source=source,
                output_dir=output,
                tools_digest="a" * 64,
            )

            record = json.loads((output / "agent-source.json").read_text())
            self.assertEqual(record, metadata)
            self.assertTrue(record["dirty"])
            self.assertEqual(len(record["resolved_commit"]), 40)
            self.assertEqual(len(record["source_digest"]), 64)
            self.assertEqual(record["tools_digest"], "a" * 64)
            self.assertIn("agent.ts", (output / "agent-source.patch").read_text())
            self.assertEqual(record["untracked_files"], ["new-tool.ts"])
            self.assertTrue((output / "agent-source-untracked.tar").is_file())


if __name__ == "__main__":
    unittest.main()
