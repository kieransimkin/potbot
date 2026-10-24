#!/usr/bin/env python3
"""Require exact-source real PostgreSQL/SYN/Ember/Cog/API acceptance for T01.2."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from potbot import __version__
from potbot.cogs.contracts import TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.cogs.tool_contracts import CONTRACT_COGS
from potbot.tools.semantic_contracts import ToolSemanticCatalog

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/potbot/tools/semantic_contracts.py",
    "src/potbot/tools/capability_contracts.py",
    "src/potbot/cogs/tool_contracts.py",
    "src/potbot/cogs/registry.py",
    "src/potbot/controller/tool_contracts.py",
    "src/potbot/controller/cog_work.py",
)


def check(evidence: dict) -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert (
        evidence["result"] == "passed"
        and evidence["qualification"] == "real_Git_SYN_Ember_Cog_PostgreSQL_API"
    )
    assert evidence["source_commit"] == head and evidence["version"] == __version__
    assert set(evidence["implementation_source"]) == set(SOURCES)
    for path, digest in evidence["implementation_source"].items():
        assert (
            digest
            == hashlib.sha256(
                subprocess.check_output(["git", "show", f"{head}:{path}"], cwd=ROOT)
            ).hexdigest()
        )
    registry = BoundCogRegistry.tool_contracts()
    catalog = ToolSemanticCatalog(registry.snapshot)
    assert evidence["catalog_hash"] == registry.snapshot.fingerprint
    assert len(evidence["schema_hashes"]) == 6
    for ref, digest in evidence["schema_hashes"].items():
        assert catalog.schema_hash(ref) == digest
    assert len(evidence["ember_instances"]) == 3
    assert all(e["conformance"] == "complete" for e in evidence["ember_instances"])
    assert evidence["rest_readback"] == evidence["websocket_readback"] == "passed"
    assert evidence["denials"] == 2
    signatures = {str(s.cog_ref): s for s in CONTRACT_COGS}
    assert len(evidence["outcomes"]) == 9
    for outcome in evidence["outcomes"]:
        receipt = outcome["receipt"]
        signature = signatures[receipt["cog_ref"]]
        assert receipt["binding_hash"] == signature.binding_hash
        output = TypedCogValue(signature.output_type_ref, outcome["output"])
        catalog.validate(output)
        assert receipt["output_hash"] == output.fingerprint
        events = [e for e in evidence["events"] if e["run_id"] == receipt["invocation_id"]]
        assert [e["stage"] for e in events] == [
            "started",
            "updated",
            "updated",
            "updated",
            "completed",
        ]
        for before, after in zip(events, events[1:], strict=False):
            assert (
                after["parent_event_id"] == before["id"]
                and after["trigger_ref"] == before["event_ref"]
            )
        metrics = events[-1]["telemetry"]["metrics"]
        assert metrics["elapsed_wall_ms"]["provenance"] == "measured"
        assert metrics["controller_cpu_ms"]["provenance"] == "measured"
        assert metrics["target_cpu_ms"]["value"] is None
        assert events[-1]["output_sha256"] and events[-1]["output_artifact_ref"]
    return {
        "stage": "T01.2",
        "result": "passed",
        "source_commit": head,
        "version": __version__,
        "catalog_hash": registry.snapshot.fingerprint,
        "canonical_contracts": 6,
        "installed_contract_cogs": 3,
        "completed_invocations": 9,
        "events": len(evidence["events"]),
        "ember_instances": 3,
        "qualification": "core_contracts_only_not_new_device_or_transport_acceptance",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    gate = check(json.loads(args.evidence.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(gate))
