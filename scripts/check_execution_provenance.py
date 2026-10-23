#!/usr/bin/env python3
"""Require exact-source durable acceptance of the T01.3 core provenance profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from potbot import __version__
from potbot.cogs.registry import BoundCogRegistry
from potbot.tools.execution_contracts import WORK_KINDS

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/potbot/cogs/execution.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/controller/execution_journal.py",
    "src/potbot/controller/execution_receipts.py",
    "src/potbot/controller/execution_events.py",
    "src/potbot/controller/cog_work.py",
    "src/potbot/tools/execution_contracts.py",
    "src/potbot/persistence/execution_provenance_schema.py",
)


def check(evidence: dict) -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert evidence["result"] == "passed" and evidence["version"] == __version__
    assert evidence["source_commit"] == head
    assert set(evidence["implementation_source"]) == set(SOURCES)
    for path, digest in evidence["implementation_source"].items():
        assert (
            digest
            == hashlib.sha256(
                subprocess.check_output(["git", "show", f"{head}:{path}"], cwd=ROOT)
            ).hexdigest()
        )
    assert evidence["catalog_hash"] == BoundCogRegistry.provenance().snapshot.fingerprint
    assert evidence["original_effects"] == evidence["effects_after_replay"] > 0
    assert evidence["rest_readback"] == evidence["websocket_readback"] == "passed"
    assert evidence["actual_cli_process_ids"] and evidence["profiling"][0]["output_hash"]
    assert evidence["signature"] == "synthetic_test_keys_only"
    receipt = evidence["receipt"]
    assert receipt["run_count"] == 18 and receipt["trace_complete"]
    assert receipt["signature_status"] == "valid_at_seal" and not receipt["reexecution_authorized"]
    assert receipt["outcome_verification"] == "not_assessed"
    events = evidence["events"]
    assert {e["telemetry"]["work_kind"] for e in events if e["stage"] == "completed"} == {
        *WORK_KINDS,
        "seal",
        "replay",
    }
    for run in evidence["trace"]["runs"]:
        records = [e for e in events if e["run_id"] == run["run_id"]]
        assert records[0]["stage"] == "started" and records[-1]["stage"] == "completed"
        assert any(e["stage"] == "updated" for e in records)
        assert all(e["telemetry"]["execution_scope_hash"] == run["scope_hash"] for e in records)
        assert records[-1]["telemetry"]["metrics"]["target_cpu_ms"]["value"] is None
    return {
        "stage": "T01.3",
        "qualification": "core_profile_fixture_not_universal_adapter_or_hardware_qualification",
        "result": "passed",
        "source_commit": head,
        "version": __version__,
        "catalog_hash": evidence["catalog_hash"],
        "signed_trace_runs": 18,
        "signed_trace_events": receipt["event_count"],
        "durable_events": len(events),
        "replay_pages": evidence["replay_pages"],
        "original_effects": evidence["original_effects"],
        "effects_after_replay": evidence["effects_after_replay"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = check(json.loads(args.evidence.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
