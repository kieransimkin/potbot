#!/usr/bin/env python3
"""Source-checkout walkthrough; fixture journals are explicitly not durable qualification."""

import argparse
import json
import sys

from potbot.cogs.tool_contracts import CONVERT, SEARCH, VALIDATE
from potbot.tools.semantic_contracts import RESULTS, schemas


def main():
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.unit.test_semantic_tool_contracts import invoke, runtime, table

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trusted-local-fixture", required=True, action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry, host, events, _ = runtime()
    search = invoke(registry, host, SEARCH, {"schema_version": 1, "text": "Python", "limit": 4})
    checked = invoke(
        registry,
        host,
        VALIDATE,
        {
            "schema_version": 1,
            "type_ref": RESULTS,
            "schema_hash": host.catalog.schema_hash(RESULTS),
            "value": search.value.value,
        },
        search.receipt.fingerprint,
    )
    converted = invoke(registry, host, CONVERT, table())
    data = {
        "qualification": "Actual installed SYN Cog fixture; memory journals, not durable or host qualification",
        "schema_count": len(schemas()),
        "catalog_hash": registry.snapshot.fingerprint,
        "search": search.value.value,
        "checked": checked.value.value,
        "converted": converted.value.value,
        "events": [
            {
                "cog": e["telemetry"]["cog_ref"],
                "stage": e["stage"],
                "phase": e["telemetry"]["phase"],
                "wall_ms": e["elapsed_ms"],
                "cpu_ms": e["telemetry"]["metrics"]["controller_cpu_ms"]["value"],
            }
            for e in events.values
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    print(
        f"Three Cogs, {len(events.values)} actual work events; typed search/result/receipt and unit conversion retained"
    )


if __name__ == "__main__":
    main()
