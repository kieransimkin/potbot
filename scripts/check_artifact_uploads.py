#!/usr/bin/env python3
"""Check exact-source resumable upload acceptance, bytes, authority and work events."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path

from potbot import __version__
from potbot.cogs.artifact_uploads import UPLOAD_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.tools.artifact_ranges import MAX_RANGE_BYTES
from potbot.tools.artifact_uploads import PAGE_SLOTS, schemas
from potbot.tools.execution_contracts import MAX_INLINE_BYTES
from potbot.tools.semantic_contracts import ToolSemanticCatalog

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/potbot/tools/artifact_uploads.py",
    "src/potbot/cogs/artifact_uploads.py",
    "src/potbot/controller/artifact_uploads.py",
    "src/potbot/persistence/artifact_uploads_schema.py",
    "src/potbot/persistence/artifacts.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/controller/execution_journal.py",
    "src/potbot/cogs/registry.py",
    "src/potbot/tools/semantic_contracts.py",
)
NEGATIVES = {
    "injected_deadline_during_commit",
    "incomplete_commit",
    "conflicting_open",
    "conflicting_duplicate",
    "changed_revision",
    "invalid_chunk_hash",
    "unaligned_offset",
    "partial_slot",
    "oversized_chunk",
    "invalid_encoding",
    "foreign_workspace",
    "changed_policy",
    "abort_after_commit",
    "chunk_after_commit",
    "commit_after_abort",
    "chunk_after_abort",
    "expired_upload",
    "whole_digest_mismatch",
    "host_revoked_during_chunk",
    "host_revoked_during_commit",
    "corrupt_staged_bytes",
    "missing_staged_bytes",
}


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
    registry = BoundCogRegistry.artifact_uploads()
    catalog = ToolSemanticCatalog(registry.snapshot)
    assert evidence["catalog_hash"] == registry.snapshot.fingerprint
    assert evidence["wire_schema_hashes"] == {ref: catalog.schema_hash(ref) for ref in schemas()}
    assert (
        evidence["chunk_bytes"] == MAX_RANGE_BYTES
        and evidence["inline_limit_bytes"] == MAX_INLINE_BYTES
    )
    assert evidence["status_page_slots"] == PAGE_SLOTS
    assert set(evidence["negative_cases"]) == NEGATIVES
    assert all(
        evidence[key] == "passed"
        for key in (
            "controller_instance_resume",
            "concurrent_commit",
            "rest_readback",
            "websocket_readback",
            "empty_upload",
        )
    )
    pieces = {}
    for chunk in evidence["uploaded_chunks"]:
        data = base64.b64decode(chunk["data_base64"], validate=True)
        assert 1 <= len(data) == chunk["stored_bytes"] <= MAX_RANGE_BYTES
        assert hashlib.sha256(data).hexdigest() == chunk["chunk_sha256"]
        assert chunk["offset"] % MAX_RANGE_BYTES == 0
        if chunk["offset"] in pieces:
            assert chunk["duplicate"] and pieces[chunk["offset"]] == data
        else:
            assert not chunk["duplicate"]
        pieces[chunk["offset"]] = data
    raw = b"".join(pieces[offset] for offset in sorted(pieces))
    assert len(raw) == evidence["artifact_bytes"] > MAX_INLINE_BYTES
    assert (
        hashlib.sha256(raw).hexdigest()
        == evidence["artifact_sha256"]
        == evidence["reconstructed_sha256"]
    )
    assert sorted(pieces) == list(range(0, len(raw), MAX_RANGE_BYTES))
    assert evidence["resume_missing_offsets"] == list(
        range(2 * MAX_RANGE_BYTES, len(raw), MAX_RANGE_BYTES)
    )
    readback = bytearray()
    for chunk in evidence["readback"]:
        assert (
            chunk["offset"] == len(readback)
            and chunk["artifact_sha256"] == evidence["artifact_sha256"]
        )
        data = base64.b64decode(chunk["data_base64"], validate=True)
        assert len(data) == chunk["returned_length"] <= MAX_RANGE_BYTES
        assert hashlib.sha256(data).hexdigest() == chunk["range_sha256"]
        readback.extend(data)
    assert readback == raw
    assert (
        evidence["committed"]["state"] == "committed"
        and evidence["committed"]["artifact_sha256"] == evidence["artifact_sha256"]
    )
    events = evidence["events"]
    assert {e["telemetry"]["cog_ref"] for e in events if e["stage"] == "completed"} >= {
        str(s.cog_ref) for s in UPLOAD_COGS
    }
    for run in {e["run_id"] for e in events}:
        rows = [e for e in events if e["run_id"] == run]
        assert rows[0]["stage"] == "started" and rows[-1]["stage"] in {
            "completed",
            "failed",
            "indeterminate",
        }
        assert any(e["stage"] == "updated" for e in rows)
        assert rows[-1]["telemetry"]["metrics"]["controller_cpu_ms"]["provenance"] == "measured"
    return {
        "stage": "T01.3",
        "gap": "G10",
        "result": "passed",
        "source_commit": head,
        "version": __version__,
        "catalog_hash": registry.snapshot.fingerprint,
        "artifact_bytes": len(raw),
        "unique_chunks": len(pieces),
        "observed_uploads": len(evidence["uploaded_chunks"]),
        "durable_events": len(events),
        "negative_cases": len(NEGATIVES),
        "artifact_sha256": evidence["artifact_sha256"],
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
