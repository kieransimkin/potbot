#!/usr/bin/env python3
"""Require exact-source registered Cog, bounded-byte and monitoring acceptance."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path

from potbot import __version__
from potbot.cogs.artifact_ranges import RANGE_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.tools.artifact_ranges import MAX_RANGE_BYTES, READ_BLOCK_BYTES, schemas
from potbot.tools.semantic_contracts import ToolSemanticCatalog

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/potbot/tools/artifact_ranges.py",
    "src/potbot/cogs/artifact_ranges.py",
    "src/potbot/controller/artifact_ranges.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/persistence/artifacts.py",
    "src/potbot/persistence/artifact_ranges_schema.py",
)


def check(evidence: dict) -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert evidence["result"] == "passed" and evidence["source_commit"] == head
    assert evidence["version"] == __version__ and set(evidence["implementation_source"]) == set(
        SOURCES
    )
    for name, digest in evidence["implementation_source"].items():
        assert (
            digest
            == hashlib.sha256(
                subprocess.check_output(["git", "show", f"{head}:{name}"], cwd=ROOT)
            ).hexdigest()
        )
    registry = BoundCogRegistry.artifact_ranges()
    assert evidence["catalog_hash"] == registry.snapshot.fingerprint
    catalog = ToolSemanticCatalog(registry.snapshot)
    assert evidence["wire_schema_hashes"] == {ref: catalog.schema_hash(ref) for ref in schemas()}
    assert evidence["maximum_range_bytes"] == MAX_RANGE_BYTES
    assert evidence["maximum_read_request_bytes"] == READ_BLOCK_BYTES
    assert evidence["artifact_bytes"] > 131072
    assert (
        evidence["restart_resume"]
        == evidence["rest_readback"]
        == evidence["websocket_readback"]
        == "passed"
    )
    assert set(evidence["negative_cases"]) == {
        "changed_revision",
        "oversized_range",
        "foreign_workspace",
        "changed_policy",
        "tampering_outside_range",
        "closed_lease",
        "expired_lease",
        "revoked_during_io",
        "missing_bytes",
        "revoked_admission",
    }
    pieces = {}
    for chunk in evidence["chunks"]:
        data = base64.b64decode(chunk["data_base64"], validate=True)
        assert len(data) == chunk["returned_length"] <= MAX_RANGE_BYTES
        assert chunk["maximum_read_request_bytes"] == READ_BLOCK_BYTES
        assert chunk["verified_byte_length"] == evidence["artifact_bytes"]
        assert hashlib.sha256(data).hexdigest() == chunk["range_sha256"]
        if chunk["offset"] in pieces:
            assert pieces[chunk["offset"]] == data
        pieces[chunk["offset"]] = data
    raw = b"".join(pieces[offset] for offset in sorted(pieces))
    assert len(raw) == evidence["artifact_bytes"]
    assert (
        hashlib.sha256(raw).hexdigest()
        == evidence["artifact_sha256"]
        == evidence["reconstructed_sha256"]
    )
    events = evidence["events"]
    assert {e["telemetry"]["cog_ref"] for e in events if e["stage"] == "completed"} >= {
        str(c.cog_ref) for c in RANGE_COGS
    }
    for run in {e["run_id"] for e in events}:
        rows = [e for e in events if e["run_id"] == run]
        if len(rows) == 1:
            assert rows[0]["stage"] == "failed"
            continue
        assert rows[0]["stage"] == "started" and rows[-1]["stage"] in {"completed", "failed"}
        assert any(e["stage"] == "updated" for e in rows)
        assert rows[-1]["telemetry"]["metrics"]["controller_cpu_ms"]["provenance"] == "measured"
    return {
        "stage": "T01.3",
        "gap": "G10",
        "result": "passed",
        "version": __version__,
        "source_commit": head,
        "catalog_hash": registry.snapshot.fingerprint,
        "artifact_bytes": len(raw),
        "unique_ranges": len(pieces),
        "observed_chunks": len(evidence["chunks"]),
        "durable_events": len(events),
        "reconstructed_sha256": evidence["artifact_sha256"],
        "qualification": evidence["qualification"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = check(json.loads(args.evidence.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
