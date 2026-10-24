"""Real PostgreSQL/SYN/Ember/artifact/Cog/API gate; no in-memory ledger."""

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from potbot import __version__
from potbot.api.server import create_app
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.cogs.tool_contracts import CONVERT, SEARCH, VALIDATE, contract_ember_bundle
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.tool_contracts import ToolContractRuntime
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.semantics.ember_service import EmberContractService
from potbot.semantics.embers import EmberContractResolver, EmberInstance
from potbot.semantics.service import SynService
from potbot.tools.semantic_contracts import (
    CAPABILITY,
    EVIDENCE,
    IMPLEMENTATION,
    INSTALLATION,
    INVOCATION,
    TOOL,
)
from tests.integration.test_repository_states import setup
from tests.unit.test_debugger_cogs_live import assert_lifecycles
from tests.unit.test_semantic_tool_contracts import records, table

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "src/potbot/tools/semantic_contracts.py",
    "src/potbot/tools/capability_contracts.py",
    "src/potbot/cogs/tool_contracts.py",
    "src/potbot/cogs/registry.py",
    "src/potbot/controller/tool_contracts.py",
    "src/potbot/controller/cog_work.py",
)


def test_canonical_contracts_real_syn_ember_cogs_durable_stats_and_api(app_env, tmp_path):
    fixture, artifacts, states, env, state, manager = setup(app_env, tmp_path)
    state = states.capture(
        app_env.context, app_env.repository_id, fixture.path, env, committed_ref="HEAD"
    )
    registry = BoundCogRegistry.tool_contracts()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "canonical-tool-contracts-v1", registry.snapshot
    )
    snapshot_id = UUID(sealed["scope"]["snapshot_id"])
    revoked = []

    def admission(*_):
        if revoked:
            raise ScopeDeniedError("Fixture grant revoked")

    host = ToolContractRuntime.from_workspace(
        database=app_env.db,
        artifacts=artifacts,
        actor=app_env.context,
        snapshot_id=snapshot_id,
        task_revision_id=uuid4(),
        repository_state_id=state["id"],
        emitter_run_id=uuid4(),
        corpus=({"id": "python", "text": "Python stack inspection", "source_sha256": "a" * 64},),
        admit=admission,
    )
    bundle = contract_ember_bundle(
        workspace_id=app_env.context.workspace_id,
        snapshot_id=snapshot_id,
        namespace=sealed["scope"]["namespace"],
    )
    service = EmberContractService(app_env.db)
    imported = service.import_bundle(app_env.context, snapshot_id, bundle)
    read, types = EmberContractService(app_env.db).read_bundle(
        app_env.context, UUID(imported["bundle_id"]), projection="owner"
    )
    assert read.fingerprint == bundle.fingerprint
    effective = EmberContractResolver(types, read, expected_scope=bundle.scope).validate_all()
    assert len(effective) == 3
    invalid_bundle = replace(
        bundle, instances=(EmberInstance.create(str(SEARCH.cog_ref), SEARCH.cog_type_ref),)
    )
    with pytest.raises(InvalidInputError):
        service.import_bundle(app_env.context, snapshot_id, invalid_bundle)

    outcomes = []

    def call(signature, value, upstream=None):
        context = CogRunContext(
            registry.snapshot.fingerprint, host.task_revision, state["id"], uuid4(), upstream
        )
        out = asyncio.run(
            registry.invoke(
                str(signature.cog_ref),
                TypedCogValue(signature.input_type_ref, value),
                context,
                controller=host,
            )
        )
        outcomes.append(out)
        return out

    result = call(SEARCH, {"schema_version": 1, "text": "Python", "limit": 4})
    assert result.value.value["items"][0]["id"] == "python"
    values = records(app_env.context)
    typed = [
        TypedCogValue(ref, value.to_dict())
        for ref, value in zip(
            (CAPABILITY, IMPLEMENTATION, INSTALLATION, INVOCATION, EVIDENCE), values, strict=True
        )
    ]
    typed.extend(
        [
            result.value,
            TypedCogValue(
                TOOL,
                {
                    "schema_version": 1,
                    "tool_ref": "syn://thing/search-tool",
                    "capability_hash": values[0].fingerprint,
                    "classification": "discovery_descriptor",
                },
            ),
        ]
    )
    for value in typed:
        checked = call(
            VALIDATE,
            {
                "schema_version": 1,
                "type_ref": value.type_ref,
                "schema_hash": host.catalog.schema_hash(value.type_ref),
                "value": value.value,
            },
            result.receipt.fingerprint,
        )
        assert host.catalog.decode(checked.value.value["canonical_json"]) == value
    converted = call(CONVERT, table())
    assert converted.value.value["rows"][0]["value"] == "1.250"
    invalid = table()
    invalid["precision"] = 0
    with pytest.raises(InvalidInputError):
        call(CONVERT, invalid)
    revoked.append(True)
    with pytest.raises(ScopeDeniedError):
        call(SEARCH, {"schema_version": 1, "text": "Python", "limit": 4})

    # New service instance, pagination and artifact bytes establish durable readback.
    reader = ExecutionEventService(app_env.db)
    events, cursor = [], 0
    while True:
        page = reader.events(app_env.context, after=cursor, limit=7)
        events.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert_lifecycles(events, outcomes)
    failed = [e for e in events if e["stage"] == "failed"]
    assert len(failed) == 2
    assert failed[-1]["telemetry"]["phase"] == "admission_denied"
    for outcome in outcomes:
        terminal = next(
            e
            for e in events
            if e["run_id"] == str(outcome.receipt.invocation_id) and e["stage"] == "completed"
        )
        data = artifacts.read(
            app_env.context, UUID(terminal["output_artifact_ref"].removeprefix("artifact:"))
        )
        assert hashlib.sha256(data).hexdigest() == terminal["output_sha256"]
        assert json.loads(data)["cog_receipt"] == outcome.receipt.to_dict()
        assert json.loads(data)["value"] == outcome.value.value
    assert ExecutionEventService(app_env.other_db).events(app_env.other_context)["items"] == []
    with pytest.raises(ScopeDeniedError):
        EmberContractService(app_env.other_db).read_bundle(
            app_env.other_context, UUID(imported["bundle_id"]), projection="owner"
        )

    def authenticate(value):
        if value != "Bearer fixture-token":
            raise ScopeDeniedError("Denied")
        return app_env.context

    app = create_app(
        task_service=object(),
        job_service=object(),
        authenticate=authenticate,
        execution_events=ExecutionEventService(app_env.db),
    )
    with TestClient(app) as api:
        response = api.get(
            "/v1/executions/events?limit=100", headers={"Authorization": "Bearer fixture-token"}
        )
        assert response.status_code == 200 and response.json()["data"]["items"] == events
        first_events = [e for e in events if e["run_id"] == str(result.receipt.invocation_id)]
        with api.websocket_connect(
            f"/v1/executions/{result.receipt.invocation_id}/stream"
        ) as stream:
            stream.send_json({"type": "authenticate", "token": "fixture-token", "after": 0})
            assert stream.receive_json()["type"] == "ready"
            assert [stream.receive_json()["event"] for _ in first_events] == first_events
    destination = Path(os.environ.get("POTBOT_CONTRACT_EVIDENCE_DIR", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "semantic-contracts.json").write_text(
        json.dumps(
            {
                "result": "passed",
                "qualification": "real_Git_SYN_Ember_Cog_PostgreSQL_API",
                "version": __version__,
                "source_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "implementation_source": {
                    p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCE_FILES
                },
                "catalog_hash": registry.snapshot.fingerprint,
                "fixture_source_digest": state["content_digest"],
                "schema_hashes": {
                    r: host.catalog.schema_hash(r)
                    for r in (TOOL, CAPABILITY, IMPLEMENTATION, INSTALLATION, INVOCATION, EVIDENCE)
                },
                "ember_bundle_hash": read.fingerprint,
                "ember_instances": [r.to_dict() for r in effective],
                "outcomes": [
                    {"receipt": o.receipt.to_dict(), "output": o.value.value} for o in outcomes
                ],
                "events": events,
                "denials": len(failed),
                "rest_readback": "passed",
                "websocket_readback": "passed",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
