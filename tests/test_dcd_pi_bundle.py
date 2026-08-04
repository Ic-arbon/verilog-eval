from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_generation.dcd import DcdPiBundleError, stage_dcd_pi_bundle


EXTENSION_FILES = (
    "agents.ts",
    "eda-tools.ts",
    "front-end-flow.ts",
    "index.ts",
    "process-supervisor.mjs",
    "run-agent.ts",
)


def add_file(archive: tarfile.TarFile, name: str, content: str) -> None:
    payload = content.encode("utf-8")
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(payload))


def make_bundle(path: Path, *, malicious: str | None = None) -> None:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for index in range(16):
            name = (
                "front-end-design-orchestrator"
                if index == 0
                else f"domain-{index:02d}-orchestrator"
            )
            add_file(
                archive,
                f"agents/{name}.md",
                f"---\nname: {name}\ndescription: fixture\n---\nfixture\n",
            )
        for index in range(17):
            name = "chip-front-end-design" if index == 0 else f"chip-domain-{index:02d}"
            add_file(
                archive,
                f"skills/{name}/SKILL.md",
                f"---\nname: {name}\ndescription: fixture\n---\nfixture\n",
            )
        for name in EXTENSION_FILES:
            add_file(
                archive,
                f"extensions/digital-chip-design-agents/{name}",
                f"// {name}\n",
            )

        if malicious == "escape":
            add_file(archive, "../escape", "outside\n")
        elif malicious == "extra":
            add_file(archive, "extensions/unexpected.ts", "unexpected\n")
        elif malicious == "symlink":
            member = tarfile.TarInfo("agents/escape.md")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)


class DcdPiBundleTests(unittest.TestCase):
    def test_exact_bundle_is_staged_as_writable_pi_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "dcd-pi.tar"
            destination = root / "workspace" / ".agent-config" / "pi"
            make_bundle(bundle)

            staged = stage_dcd_pi_bundle(
                bundle,
                destination,
                expected_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
                expected_size_bytes=bundle.stat().st_size,
            )

            self.assertEqual(staged, destination)
            self.assertTrue(
                (destination / "agents" / "front-end-design-orchestrator.md").is_file()
            )
            self.assertTrue(
                (destination / "skills" / "chip-front-end-design" / "SKILL.md").is_file()
            )
            self.assertEqual(
                sorted(
                    path.name
                    for path in (
                        destination / "extensions" / "digital-chip-design-agents"
                    ).iterdir()
                ),
                sorted(EXTENSION_FILES),
            )
            self.assertEqual(
                len(list((destination / "agents").glob("*.md"))),
                16,
            )
            self.assertEqual(
                len(list((destination / "skills").glob("*/SKILL.md"))),
                17,
            )
            self.assertNotEqual(destination.stat().st_mode & 0o200, 0)

    def test_extraction_uses_the_same_byte_snapshot_as_identity_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "dcd-pi.tar"
            destination = root / "pi"
            make_bundle(bundle)
            original = bundle.read_bytes()
            altered = original.replace(b"fixture\n", b"changed\n", 1)
            self.assertEqual(len(altered), len(original))
            real_tar_open = tarfile.open

            def mutate_source_then_open(*args, **kwargs):
                bundle.write_bytes(altered)
                return real_tar_open(*args, **kwargs)

            with patch("agent_generation.dcd.tarfile.open", mutate_source_then_open):
                stage_dcd_pi_bundle(
                    bundle,
                    destination,
                    expected_sha256=hashlib.sha256(original).hexdigest(),
                    expected_size_bytes=len(original),
                )

            staged_agent = destination / "agents" / "front-end-design-orchestrator.md"
            self.assertIn("fixture", staged_agent.read_text())
            self.assertNotIn("changed", staged_agent.read_text())

    def test_material_identity_mismatch_is_rejected_without_partial_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "dcd-pi.tar"
            destination = root / "pi"
            make_bundle(bundle)

            with self.assertRaisesRegex(DcdPiBundleError, "identity"):
                stage_dcd_pi_bundle(
                    bundle,
                    destination,
                    expected_sha256="0" * 64,
                    expected_size_bytes=bundle.stat().st_size,
                )

            self.assertFalse(destination.exists())

    def test_unsafe_or_nonexact_bundles_are_rejected_without_partial_publish(self):
        for mutation in ("escape", "extra", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = root / "dcd-pi.tar"
                destination = root / "pi"
                make_bundle(bundle, malicious=mutation)

                with self.assertRaises(DcdPiBundleError):
                    stage_dcd_pi_bundle(bundle, destination)

                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
