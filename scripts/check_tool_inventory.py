#!/usr/bin/env python3
"""Check retained real T01.1 acceptance against this exact committed source."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from potbot import __version__
from potbot.cogs.contracts import TypedCogValue
from potbot.cogs.inventory import SIGNATURE
from potbot.domain.serialization import content_hash
from potbot.tools.inventory import OWNERS, STAGES

ROOT = Path(__file__).resolve().parents[1]


def check(value: dict) -> dict:
    assert (
        value["result"] == "passed" and value["qualification"] == "real_Git_SYN_Cog_PostgreSQL_API"
    )
    inventory = value["inventory"]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert inventory["source"]["commit_id"] == head
    assert inventory["source_version"] == __version__
    assert inventory["inventory_sha256"] == content_hash(
        {k: v for k, v in inventory.items() if k != "inventory_sha256"}
    )
    assert {c["id"] for c in inventory["components"]} == set(OWNERS)
    assert all(c["source_state"] == "present" for c in inventory["components"])
    assert all(
        c["qualification"] == "not_established_by_source_inventory" for c in inventory["components"]
    )
    assert all(
        not r["enabled"] and r["state"] == "unqualified" for r in inventory["specialist_routes"]
    )
    stages = json.loads(
        (
            ROOT / "docs/potbot-tool-mcp-implementation-bundle/potbot-tool-integration-stages.json"
        ).read_text()
    )
    ids = {s["id"] for s in stages}
    assert all(set(refs) <= ids for refs in STAGES.values())
    receipt, events = value["receipt"], value["events"]
    assert (
        receipt["cog_ref"] == str(SIGNATURE.cog_ref)
        and receipt["binding_hash"] == SIGNATURE.binding_hash
    )
    assert receipt["output_hash"] == TypedCogValue(SIGNATURE.output_type_ref, inventory).fingerprint
    assert events[0]["stage"] == "started" and events[-1]["stage"] == "completed"
    assert sum(e["stage"] == "updated" for e in events) == 17
    assert events[-1]["telemetry"]["finished_at"]
    return {
        "stage": "T01.1",
        "result": "passed",
        "source_commit": head,
        "version": __version__,
        "inventory_sha256": inventory["inventory_sha256"],
        "components": len(OWNERS),
        "source_modules": sum(len(c["source_files"]) for c in inventory["components"]),
        "work_events": len(events),
        "qualification": "inventory_only_not_transport_or_host_acceptance",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    gate = check(json.loads(args.evidence.read_text()))
    args.output.write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(gate))
