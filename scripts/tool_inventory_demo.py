#!/usr/bin/env python3
"""Developer-only real Git/Cog walkthrough using explicitly non-durable test journals."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.inventory import SIGNATURE
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkMonitor
from potbot.controller.tool_inventory import ToolInventoryRuntime
from potbot.domain.auth import WorkspaceContext
from potbot.tools.repository_snapshot import capture_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trusted-local-source", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refuse to overwrite a prior observation")
    from tests.unit.test_debugger_cogs_live import FixtureArtifacts, FixtureEvents

    root = Path(__file__).resolve().parents[1]
    snapshot = capture_snapshot(root, committed_ref="HEAD")
    events, artifacts = FixtureEvents(), FixtureArtifacts()
    actor = WorkspaceContext(uuid4(), uuid4())
    registry = BoundCogRegistry.tool_inventory()
    monitor = CogWorkMonitor(
        events,
        artifacts,
        actor,
        emitter_run_id=uuid4(),
        event_type_ref="synny://type/workflow-execution",
    )
    host = ToolInventoryRuntime(
        monitor,
        lambda: snapshot,
        task_revision_id=uuid4(),
        repository_state_id=uuid4(),
        expected_digest=snapshot.content_digest,
        catalog_hash=registry.snapshot.fingerprint,
        admit=lambda *_: None,
    )
    context = CogRunContext(host.catalog_hash, host.task_revision, host.repository_state, uuid4())
    source = TypedCogValue(
        SIGNATURE.input_type_ref,
        {
            "schema_version": 1,
            "profile": "python_debugger",
            "expected_content_digest": snapshot.content_digest,
        },
    )
    out = asyncio.run(registry.invoke(str(SIGNATURE.cog_ref), source, context, controller=host))
    result = {
        "qualification": "Real committed Git and SYN Cog, local test journals; not durable/host qualification",
        "inventory": out.value.value,
        "events": events.values,
        "receipt": out.receipt.to_dict(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, default=str)
    print(
        f"Verified {sum(len(c['source_files']) for c in result['inventory']['components'])} source modules; {len(events.values)} work events; no inspected code executed"
    )


if __name__ == "__main__":
    main()
