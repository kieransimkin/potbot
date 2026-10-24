#!/usr/bin/env python3
"""Require exact-source monitored transport acceptance, without live-host claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from potbot import __version__

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "src/potbot/controller/transport_cogs.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/tools/cli_adapter.py",
    "tests/integration/test_transport_cogs.py",
)


def check(evidence: dict) -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert evidence["result"] == "passed" and evidence["source_commit"] == head
    assert evidence["version"] == __version__
    assert set(evidence["implementation_source"]) == set(SOURCES)
    for path, digest in evidence["implementation_source"].items():
        assert (
            digest
            == hashlib.sha256(
                subprocess.check_output(["git", "show", f"{head}:{path}"], cwd=ROOT)
            ).hexdigest()
        )
    assert evidence["transports"] == [
        "rest",
        "graphql",
        "grpc",
        "sandboxed_mcp_stdio",
        "sandboxed_cli",
    ]
    assert evidence["completed_runs"] == 6 and evidence["uncertain_write_attempts"] == 1
    assert evidence["cancelled_process_tree_stopped"] is True
    events = evidence["events"]
    runs = {e["run_id"] for e in events}
    terminals = []
    for run in runs:
        records = sorted((e for e in events if e["run_id"] == run), key=lambda e: e["sequence"])
        assert records[0]["stage"] == "started"
        assert records[-1]["stage"] in {"completed", "cancelled", "indeterminate"}
        assert (
            sum(
                e["stage"] in {"completed", "cancelled", "failed", "indeterminate"} for e in records
            )
            == 1
        )
        assert any(e["stage"] == "updated" for e in records)
        for previous, current in zip(records, records[1:], strict=False):
            assert str(current["parent_event_id"]) == str(previous["id"])
        telemetry = records[-1]["telemetry"]
        assert telemetry["metrics"]["elapsed_wall_ms"]["provenance"] == "measured"
        assert telemetry["metrics"]["controller_cpu_ms"]["provenance"] == "measured"
        assert telemetry["metrics"]["target_cpu_ms"]["value"] is None
        assert telemetry["implementation"]["implementation_hash"]
        terminals.append(records[-1]["stage"])
    assert terminals.count("completed") == 6 and terminals.count("indeterminate") == 1
    assert terminals.count("cancelled") == 1
    return {
        "result": "passed",
        "stage": "T02.4",
        "version": __version__,
        "source_commit": head,
        "qualification": evidence["qualification"],
        "transports": evidence["transports"],
        "completed_runs": 6,
        "durable_events": len(events),
        "uncertain_write_attempts": 1,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check(json.loads(args.evidence.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
