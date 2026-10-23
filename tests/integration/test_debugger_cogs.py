"""Actual Python SYN Cogs + sealed catalog + PostgreSQL + production Linux sandbox."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.api.server import create_app
from potbot.cogs.debugger import signature
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.execution_events import ExecutionEventService
from potbot.debugging import DebugIdentity
from potbot.debugging.cog_acceptance import python_cog_walkthrough
from potbot.debugging.cog_client import DebuggerCogClient
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.debugging.python import DEBUGPY_VERSION, launch_configuration
from potbot.debugging.sandbox import SandboxedDebugger
from potbot.domain.errors import ScopeDeniedError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.semantics.service import SynService
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor
from potbot.tools.supervisor import ProcessRequest
from potbot.workspace_sandbox import load_workspace_sandbox

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]


@pytest.mark.parametrize("fail", [False, True])
def test_real_python_cogs_durable_monitoring_and_api(app_env, tmp_path, fail):
    registry = BoundCogRegistry.debugger()
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    imported = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "installed-debugger-cogs-v1", registry.snapshot
    )
    assert imported["snapshot"]["source_hash"] == registry.snapshot.fingerprint
    snapshot_id = UUID(imported["scope"]["snapshot_id"])
    sandbox = load_workspace_sandbox(app_env.db, app_env.context)
    candidate = sandbox.path("candidates") / "python-cog-fixture"
    candidate.mkdir()
    program = candidate / "nested.py"
    shutil.copyfile(Path(__file__).resolve().parents[2] / "examples/debugging/nested.py", program)
    identity = DebugIdentity(
        app_env.context.workspace_id,
        uuid4(),
        candidate,
        (("nested.py", hashlib.sha256(program.read_bytes()).hexdigest()),),
        "debugpy",
        DEBUGPY_VERSION,
        f"Python {sys.version.split()[0]}",
        "synny://type/workflow-execution",
    )
    request = ProcessRequest(
        argv=(sys.executable, "-I", "-B", "-m", "debugpy.adapter"),
        cwd=candidate,
        workspace_root=candidate,
        sandbox_root=sandbox.root,
        timeout_ms=120_000,
        environment=sandbox.runtime_environment(),
        writable_roots=tuple(sandbox.path(x) for x in ("tmp", "cache")),
    )
    host = SandboxedDebugger(
        supervisor=InteractiveProcessSupervisor(),
        request=request,
        identity=identity,
        executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        admit=lambda *_: None,
        record=lambda _: None,
        initialize_on_enter=False,
    )
    configuration = launch_configuration(
        identity,
        program=program,
        python=Path(sys.executable),
        arguments=("--fail",) if fail else (),
    )

    def admit(selected, source, context):
        if selected.cog_ref in {signature("attach").cog_ref, signature("evaluate").cog_ref}:
            raise ScopeDeniedError("Fixture grants neither broad attach nor expression execution")
        if (
            selected.cog_ref == signature("launch").cog_ref
            and source.value["configuration"] != configuration
        ):
            raise ScopeDeniedError("Only this exact authored fixture launch is admitted")

    async def scenario():
        async with host as session:
            runtime = DebuggerCogRuntime.from_workspace(
                session,
                database=app_env.db,
                artifacts=artifacts,
                actor=app_env.context,
                snapshot_id=snapshot_id,
                task_revision_id=uuid4(),
                repository_state_id=uuid4(),
                admit=admit,
            )
            client = DebuggerCogClient(registry, runtime)
            observations = await python_cog_walkthrough(
                client, program=program, configuration=configuration, fail=fail
            )
            return observations, client.history

    observations, outcomes = asyncio.run(scenario())
    assert host.exit.processes_stopped and len(host.exit.isolation_identity) == 64
    # New service instance reconstructs the whole history from real durable state.
    reader = ExecutionEventService(app_env.db)
    cursor, records = 0, []
    while True:
        page = reader.events(app_env.context, after=cursor, limit=7)
        records.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    for outcome in outcomes:
        events = [e for e in records if e["run_id"] == str(outcome.receipt.invocation_id)]
        assert events[0]["stage"] == "started" and events[-1]["stage"] == "completed"
        assert any(e["stage"] == "updated" for e in events)
        assert sum(e["stage"] == "completed" for e in events) == 1
        for previous, current in zip(events, events[1:], strict=False):
            assert current["parent_event_id"] == previous["id"]
            assert current["parent_run_id"] == previous["run_id"]
            assert current["trigger_ref"] == previous["event_ref"]
        terminal = events[-1]
        metric = terminal["telemetry"]["metrics"]["elapsed_wall_ms"]
        assert metric["value"] == terminal["elapsed_ms"] and metric["provenance"] == "measured"
        assert terminal["telemetry"]["metrics"]["peak_ram_bytes"]["value"] is None
        output_id = UUID(terminal["output_artifact_ref"].removeprefix("artifact:"))
        output = artifacts.read(app_env.context, output_id)
        assert hashlib.sha256(output).hexdigest() == terminal["output_sha256"]
        retained = json.loads(output)
        assert retained["cog_receipt"] == outcome.receipt.to_dict()
        input_id = UUID(terminal["input_artifact_ref"].removeprefix("artifact:"))
        input_bytes = artifacts.read(app_env.context, input_id)
        assert hashlib.sha256(input_bytes).hexdigest() == terminal["trigger_input_sha256"]
        input_value = json.loads(input_bytes)
        from potbot.cogs.contracts import TypedCogValue

        assert (
            TypedCogValue(input_value["type_ref"], input_value["value"]).fingerprint
            == outcome.receipt.input_hash
        )
    assert ExecutionEventService(app_env.other_db).events(app_env.other_context)["items"] == []

    # Existing API and WebSocket replay expose the SAME ledger/telemetry, no new log.
    def authenticate(value):
        if value != "Bearer fixture-token":
            raise ScopeDeniedError("Fixture authentication denied")
        return app_env.context

    app = create_app(
        task_service=object(),
        job_service=object(),
        authenticate=authenticate,
        execution_events=ExecutionEventService(app_env.db),
    )
    with TestClient(app) as api:
        response = api.get(
            "/v1/executions/events?limit=7", headers={"Authorization": "Bearer fixture-token"}
        )
        assert response.status_code == 200
        assert response.json()["data"]["items"] == records[:7]
        run_id = outcomes[0].receipt.invocation_id
        expected = [e for e in records if e["run_id"] == str(run_id)]
        with api.websocket_connect(f"/v1/executions/{run_id}/stream") as stream:
            stream.send_json({"type": "authenticate", "token": "fixture-token", "after": 0})
            assert stream.receive_json()["type"] == "ready"
            assert [stream.receive_json()["event"] for _ in expected] == expected
    evidence = {
        "result": "passed",
        "mode": "real_PostgreSQL_sealed_SYN_Cogs_Linux_sandbox",
        "source_sha256": dict(identity.source_sha256),
        "adapter": f"debugpy {DEBUGPY_VERSION}",
        "observations": observations,
        "events": records,
        "receipts": [x.receipt.to_dict() for x in outcomes],
        "sandbox_identity": host.exit.isolation_identity,
        "owned_processes_stopped": True,
    }
    # CI retains public-safe authored-fixture evidence; operational credentials never enter it.
    destination = Path(os.environ.get("POTBOT_DEBUGGER_EVIDENCE_DIR", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    (
        destination / ("python-cog-exception.json" if fail else "python-cog-walkthrough.json")
    ).write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
