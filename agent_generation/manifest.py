"""Canonical unnumbered generation-manifest serialization."""

from __future__ import annotations

from typing import Mapping

from agent_generation.result_contract import canonical_manifest_bytes


def canonical_generation_manifest(manifest: Mapping) -> bytes:
    """Validate and serialize a manifest that hashes only bundle payloads."""

    return canonical_manifest_bytes(manifest)
