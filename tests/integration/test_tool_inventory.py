"""Real committed Git snapshot + authenticated SYN Cog + PostgreSQL + API readback."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.api.server import create_app
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.inventory import SIGNATURE
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.tool_inventory import ToolInventoryRuntime
from potbot.domain.errors import ScopeDeniedError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.semantics.service import SynService
from potbot.tools.repository import RepositoryStateStore
from tests.integration.test_repository_states import environment

pytestmark = pytest.mark.postgres


def test_committed_inventory_cog_durable_stats_and_workspace_api(app_env, tmp_path):
    registry = BoundCogRegistry.tool_inventory()
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    states = RepositoryStateStore(app_env.db, artifacts)
    state = states.capture(
        app_env.context,
        app_env.repository_id,
        Path(__file__).resolve().parents[2],
        environment(app_env),
        committed_ref="HEAD",
    )
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "installed-tool-inventory-v1", registry.snapshot
    )
    host = ToolInventoryRuntime.from_workspace(
        database=app_env.db,
        artifacts=artifacts,
        actor=app_env.context,
        snapshot_id=UUID(sealed["scope"]["snapshot_id"]),
        task_revision_id=uuid4(),
        repository_state_id=state["id"],
        expected_digest=state["content_digest"],
        emitter_run_id=uuid4(),
        admit=lambda *_: None,
    )
    context = CogRunContext(registry.snapshot.fingerprint, host.task_revision, state["id"], uuid4())
    source = TypedCogValue(
        SIGNATURE.input_type_ref,
        {
            "schema_version": 1,
            "profile": "python_debugger",
            "expected_content_digest": state["content_digest"],
        },
    )
    outcome = asyncio.run(registry.invoke(str(SIGNATURE.cog_ref), source, context, controller=host))
    report = outcome.value.value
    assert report["source"]["commit_id"] == state["manifest"]["commit_id"]
    assert report["source"]["content_digest"] == state["content_digest"]
    module_count = sum(len(c["source_files"]) for c in report["components"])
    assert module_count > 150
    assert all(c["source_state"] == "present" for c in report["components"])
    assert all(not route["enabled"] for route in report["specialist_routes"])
    assert {t["id"] for t in report["transports"] if t["state"] == "source_present"} == {
        "mcp_stdio",
        "mcp_http",
        "cli",
        "http_sdk",
        "grpc",
        "openapi",
        "python_dap",
    }
    reader = ExecutionEventService(app_env.db)
    records, cursor = [], 0
    while True:
        page = reader.events(app_env.context, after=cursor, limit=3)
        records.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert records[0]["stage"] == "started" and records[-1]["stage"] == "completed"
    assert len([e for e in records if e["stage"] == "updated"]) == 17
    assert len({e["id"] for e in records}) == len(records)
    for before, after in zip(records, records[1:], strict=False):
        assert after["parent_event_id"] == before["id"]
        assert after["trigger_ref"] == before["event_ref"]
    terminal = records[-1]
    assert terminal["telemetry"]["metrics"]["elapsed_wall_ms"]["value"] == terminal["elapsed_ms"]
    assert terminal["telemetry"]["metrics"]["target_cpu_ms"]["value"] is None
    output = artifacts.read(
        app_env.context, UUID(terminal["output_artifact_ref"].removeprefix("artifact:"))
    )
    assert hashlib.sha256(output).hexdigest() == terminal["output_sha256"]
    assert json.loads(output)["cog_receipt"] == outcome.receipt.to_dict()
    assert json.loads(output)["value"] == report
    assert ExecutionEventService(app_env.other_db).events(app_env.other_context)["items"] == []
    with pytest.raises(ScopeDeniedError):
        RepositoryStateStore(app_env.other_db, artifacts).snapshot(
            app_env.other_context, state["id"]
        )

    def authenticate(value):
        if value != "Bearer fixture-token":
            raise ScopeDeniedError("Fixture token denied")
        return app_env.context

    app = create_app(
        task_service=object(),
        job_service=object(),
        authenticate=authenticate,
        execution_events=ExecutionEventService(app_env.db),
    )
    with TestClient(app) as api:
        result = api.get(
            "/v1/executions/events?limit=100", headers={"Authorization": "Bearer fixture-token"}
        )
        assert result.status_code == 200 and result.json()["data"]["items"] == records
        with api.websocket_connect(f"/v1/executions/{context.invocation_id}/stream") as stream:
            stream.send_json({"type": "authenticate", "token": "fixture-token", "after": 0})
            assert stream.receive_json()["type"] == "ready"
            assert [stream.receive_json()["event"] for _ in records] == records
    destination = Path(os.environ.get("POTBOT_INVENTORY_EVIDENCE_DIR", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "tool-inventory.json").write_text(
        json.dumps(
            {
                "result": "passed",
                "qualification": "real_Git_SYN_Cog_PostgreSQL_API",
                "inventory": report,
                "events": records,
                "receipt": outcome.receipt.to_dict(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
