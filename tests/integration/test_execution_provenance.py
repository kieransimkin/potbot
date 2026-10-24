"""Real PostgreSQL, registered Cogs, subprocesses, Ed25519 and REST/WebSocket replay."""

import asyncio
import cProfile
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from potbot import __version__
from potbot.api.server import create_app
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import REPLAY, SEAL, WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkMonitor
from potbot.controller.execution_cogs import ExecutionCogRuntime, HostOperation
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.execution_journal import PinnedExecutionJournal
from potbot.controller.execution_receipts import ExecutionReceiptService
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import canonical_json, content_hash
from potbot.security.public import PublicVerificationError, verify_signature_bundle_digest
from potbot.semantics.catalog import merge_types
from potbot.semantics.service import SynService
from potbot.tools.execution_contracts import (
    RECEIPT,
    REPLAY_REQUEST,
    REPLAY_RESULT,
    REQUEST,
    RESULT,
    SEAL_REQUEST,
    SEAL_RESULT,
    WORK_KINDS,
    ExecutionPolicy,
    WorkBinding,
)
from potbot.tools.execution_schemas import schemas
from potbot.tools.semantic_contracts import QUERY, RESULTS
from tests.integration.test_repository_states import setup
from tests.unit.test_debugger_cogs_live import assert_lifecycles
from tests.unit.test_mcp_stdio import serve
from tests.unit.test_public_identity import _bundle, _resign

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "src/potbot/tools/execution_schemas.py",
    "src/potbot/cogs/execution.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/controller/execution_journal.py",
    "src/potbot/controller/execution_receipts.py",
    "src/potbot/controller/execution_events.py",
    "src/potbot/controller/cog_work.py",
    "src/potbot/tools/execution_contracts.py",
    "src/potbot/persistence/execution_provenance_schema.py",
)


def test_real_signed_causal_work_cogs_and_readonly_receipt_replay(app_env, tmp_path):
    fixture, artifacts, states, env, _, _ = setup(app_env, tmp_path)
    state = states.capture(
        app_env.context, app_env.repository_id, fixture.path, env, committed_ref="HEAD"
    )
    registry = BoundCogRegistry.provenance()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "execution-provenance-v1", registry.snapshot
    )
    snapshot_id = UUID(sealed["scope"]["snapshot_id"])
    policy = ExecutionPolicy(
        "a" * 64,
        "b" * 64,
        state["content_digest"],
        __version__,
        host_clock_offset_ms=-120000,
        host_clock_uncertainty_ms=25,
    )
    signed = []

    def sign(request):
        bundle = _bundle(issued=int(time.time()))
        bundle["envelope"].update(
            {
                k: request[k]
                for k in (
                    "workspace_id",
                    "audience",
                    "domain",
                    "payload_type_ref",
                    "payload_sha256",
                    "request_id",
                )
            }
        )
        _resign(bundle)
        signed.append(bundle)
        return bundle

    signer_hash = _bundle()["identity"]["signer_hash"]
    receipt_service = ExecutionReceiptService(
        app_env.db,
        artifacts,
        app_env.context,
        policy=policy,
        signer_hash=signer_hash,
        known_types=frozenset(str(t.ref) for t in merge_types(registry.snapshot)),
        sign=sign,
        cursor_ttl_seconds=300,
    )
    operations, effects, process_ids, profiling, outcomes = [], [], [], [], []
    original = TypedCogValue(QUERY, {"schema_version": 1, "text": "Python provenance", "limit": 4})

    def result(text):
        return TypedCogValue(
            RESULTS,
            {
                "schema_version": 1,
                "query_hash": original.fingerprint,
                "corpus_hash": "d" * 64,
                "items": [{"id": "fixture", "text": text, "source_sha256": "e" * 64}],
                "complete": True,
            },
        )

    def operation_ref(kind):
        return f"syn://thing/fixture-{kind.replace('_', '-')}"

    async def simple(call, value):
        effects.append(call.work.signature.cog_ref.slug)
        call.progress("fixture_operation_completed")
        return result("Scripted host fixture; no live provider/browser/device qualification")

    async def script(call, value):
        effects.append("actual-python-cli")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'sum':7,'pid':os.getpid()}))",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        process_ids.append(proc.pid)
        try:
            output, errors = await asyncio.wait_for(proc.communicate(), timeout=5)
            assert proc.returncode == 0 and not errors and json.loads(output)["sum"] == 7
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        call.progress("python_cli_exited")
        return result("Actual Python CLI returned 7 and exited")

    async def mcp(call, value):
        effects.append("actual-mcp-stdio")
        async for client, catalog in serve():
            response = await client.call_tool(
                "add",
                {"a": 3, "b": 4},
                pinned_catalog_hash=catalog.fingerprint,
                pinned_tool_hash=catalog.tool("add").fingerprint,
            )
            assert response.structured_content == {"sum": 7}
        call.progress("mcp_stdio_child_exited")
        return result("Actual MCP stdio tool returned 7 and exited")

    async def profile(call, value):
        effects.append("actual-cprofile")

        def workload():
            return sum(i * i for i in range(100000))

        start = time.perf_counter_ns()
        plain = workload()
        plain_ns = time.perf_counter_ns() - start
        profiler = cProfile.Profile()
        start = time.perf_counter_ns()
        measured = profiler.runcall(workload)
        profiled_ns = time.perf_counter_ns() - start
        assert plain == measured and profiler.getstats()
        profiling.append(
            {
                "plain_ns": plain_ns,
                "profiled_ns": profiled_ns,
                "difference_ns": profiled_ns - plain_ns,
                "output_hash": content_hash(plain),
                "sample_count": 1,
                "qualification": "single_controller_fixture_measurement",
                "target_ram_bytes": None,
                "uncertainty": "single_sample_noise_not_estimated",
            }
        )
        call.progress("cprofile_output_compared")
        return result(canonical_json(profiling[-1]))

    async def subagent(call, value):
        child = await call.child("tool", operation_ref("tool"), value)
        outcomes.append(child)
        return result("Nested scripted subagent observed its child tool")

    async def workflow(call, value):
        script_result = await call.child("script", operation_ref("script"), value)
        outcomes.append(script_result)
        terminal = ExecutionEventService(app_env.db).events(
            app_env.context, run_id=script_result.receipt.invocation_id
        )["items"][-1]
        for kind in WORK_KINDS:
            if kind in {"workflow", "script"}:
                continue
            child = await call.child(
                kind,
                operation_ref(kind),
                value,
                links=(UUID(terminal["id"]),) if kind == "mcp_tool" else (),
            )
            outcomes.append(child)
        return result("Completed a causal fixture family; original effects are never replayed")

    for kind in WORK_KINDS:
        handler = {
            "script": script,
            "mcp_tool": mcp,
            "maintenance": profile,
            "subagent": subagent,
            "workflow": workflow,
        }.get(kind, simple)
        operations.append(
            HostOperation(
                WorkBinding(
                    kind,
                    operation_ref(kind),
                    QUERY,
                    RESULTS,
                    content_hash({"fixture_handler": kind}),
                    "read_only",
                    policy,
                ),
                handler,
            )
        )
    revoked = []

    def admit(*args):
        if revoked:
            raise ScopeDeniedError("Fixture grant revoked")

    runtime = ExecutionCogRuntime(
        database=app_env.db,
        artifacts=artifacts,
        actor=app_env.context,
        snapshot_id=snapshot_id,
        task_revision_id=uuid4(),
        repository_state_id=state["id"],
        emitter_run_id=uuid4(),
        policy=policy,
        operations=tuple(operations),
        admit=admit,
        receipts=receipt_service,
    )

    def invoke(signature, value):
        context = CogRunContext(
            registry.snapshot.fingerprint, runtime.task_revision, state["id"], uuid4()
        )
        outcome = asyncio.run(
            registry.invoke(
                str(signature.cog_ref),
                TypedCogValue(signature.input_type_ref, value),
                context,
                controller=runtime,
            )
        )
        outcomes.append(outcome)
        return outcome

    root = invoke(
        WORK_COGS[WORK_KINDS.index("workflow")],
        {
            "schema_version": 1,
            "operation_ref": operation_ref("workflow"),
            "input": {"type_ref": original.type_ref, "value": original.value},
        },
    )
    read = ExecutionEventService(app_env.db)
    root_events = read.events(app_env.context, run_id=root.receipt.invocation_id)["items"]
    expected = {
        "schema_version": 1,
        "root_run_id": str(root.receipt.invocation_id),
        "expected_input_sha256": root_events[0]["trigger_input_sha256"],
        "expected_policy_hash": policy.fingerprint,
    }
    receipt = invoke(SEAL, expected).value.value
    assert receipt["trace_complete"] and receipt["run_count"] == 18 and len(signed) == 1
    assert invoke(SEAL, expected).value.value == receipt and len(signed) == 1
    replay = {
        "schema_version": 1,
        "receipt_id": receipt["receipt_id"],
        "expected_input_sha256": expected["expected_input_sha256"],
        "expected_policy_hash": policy.fingerprint,
        "limit": 8,
    }
    effect_count = len(effects)
    pages, cursor, replayed = [], None, []
    while True:
        page = invoke(REPLAY, {**replay, **({"cursor": cursor} if cursor else {})}).value.value
        pages.append(page)
        replayed.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(replayed) == receipt["event_count"] and len(effects) == effect_count
    assert len({e["event_ref"] for e in replayed}) == len(replayed)
    assert [e["sequence"] for e in replayed] == sorted(e["sequence"] for e in replayed)
    assert receipt_service.replay(replay)["items"] == pages[0]["items"]
    with app_env.db.transaction(app_env.context) as connection:
        receipt_row = connection.execute(
            "SELECT body_artifact_id FROM execution_provenance_receipts WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(receipt["receipt_id"])),
        ).fetchone()
    manifest = json.loads(artifacts.read(app_env.context, receipt_row["body_artifact_id"]))
    root_source = json.loads(
        artifacts.read(
            app_env.context, UUID(root_events[0]["input_artifact_ref"].removeprefix("artifact:"))
        )
    )["value"]
    for ref, value in (
        (REQUEST, root_source),
        (RESULT, root.value.value),
        (SEAL_REQUEST, expected),
        (SEAL_RESULT, receipt),
        (REPLAY_REQUEST, replay),
        (REPLAY_RESULT, pages[0]),
        (RECEIPT, manifest),
    ):
        typed = TypedCogValue(ref, value)
        assert runtime.catalog.decode(runtime.catalog.encode(typed)) == typed
    assert len(canonical_json(pages[0]).encode("utf-8")) <= 16384
    for key in ("expected_input_sha256", "expected_policy_hash"):
        with pytest.raises(StateMismatchError):
            receipt_service.replay({**replay, key: "f" * 64})
    with pytest.raises(ScopeDeniedError):
        receipt_service.replay({**replay, "cursor": str(uuid4())})
    expiring_service = ExecutionReceiptService(
        app_env.db,
        artifacts,
        app_env.context,
        policy=policy,
        signer_hash=signer_hash,
        known_types=receipt_service.verification.known_type_refs,
        sign=sign,
        cursor_ttl_seconds=1,
    )
    expiring = expiring_service.replay(replay)["next_cursor"]
    time.sleep(1.1)
    with pytest.raises(ScopeDeniedError):
        receipt_service.replay({**replay, "cursor": expiring})
    # Historical verification remains valid; this is not fresh request acceptance.
    signature = signed[0]
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle_digest(
            signature,
            receipt["body_sha256"],
            policy=receipt_service.verification,
            now=int(time.time()) + 200,
        )
    assert receipt_service.replay(replay)["signature_status"] == "valid_at_seal"
    altered = json.loads(canonical_json(signature))
    altered["envelope"]["payload_sha256"] = "f" * 64
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle_digest(
            altered, receipt["body_sha256"], policy=receipt_service.verification
        )
    foreign = ExecutionReceiptService(
        app_env.other_db,
        artifacts,
        app_env.other_context,
        policy=policy,
        signer_hash=signer_hash,
        known_types=receipt_service.verification.known_type_refs,
        sign=sign,
    )
    with pytest.raises(ScopeDeniedError):
        foreign.replay(replay)
    fields = {f.name for f in dataclass_fields(ExecutionEventInput)}
    forged = {k: v for k, v in root_events[-1].items() if k in fields}
    for key in ("run_id", "emitter_run_id", "parent_run_id", "parent_event_id"):
        if forged.get(key):
            forged[key] = UUID(forged[key])
    forged["telemetry_json"] = canonical_json(root_events[-1]["telemetry"])
    with pytest.raises(ScopeDeniedError):
        read.append(app_env.context, ExecutionEventInput(**forged))
    # Legacy producers also cannot append to a pinned run by changing its Event Type.
    with pytest.raises(ScopeDeniedError):
        read.append(
            app_env.context,
            replace(
                ExecutionEventInput(**forged), event_type_ref="synny://type/workflow-execution"
            ),
        )
    installed = WORK_COGS[WORK_KINDS.index("workflow")]
    source = TypedCogValue(
        installed.input_type_ref,
        json.loads(
            artifacts.read(
                app_env.context,
                UUID(root_events[0]["input_artifact_ref"].removeprefix("artifact:")),
            )
        )["value"],
    )
    context = CogRunContext(
        registry.snapshot.fingerprint, runtime.task_revision, state["id"], uuid4()
    )

    def journal_for(context=context, snapshot=snapshot_id):
        return PinnedExecutionJournal(
            app_env.db,
            artifacts,
            app_env.context,
            snapshot_id=snapshot,
            signature=installed,
            source=source,
            context=context,
            binding=operations[WORK_KINDS.index("workflow")].binding,
            emitter=runtime.emitter,
        )

    journal = journal_for()
    initial = {k: v for k, v in root_events[0].items() if k in fields}
    initial["run_id"], initial["emitter_run_id"] = context.invocation_id, runtime.emitter
    initial["trigger_kind"], initial["trigger_ref"] = (
        "api_request",
        f"cog-request:{context.invocation_id}",
    )
    initial["telemetry_json"] = canonical_json(root_events[0]["telemetry"])
    start = ExecutionEventInput(**initial)
    for invalid in (
        replace(start, emitter_run_id=uuid4()),
        replace(start, subject_version="2"),
        replace(start, event_type_ref="syn://type/unknown-event"),
        replace(start, parent_event_id=uuid4(), parent_run_id=context.invocation_id),
    ):
        with pytest.raises(ScopeDeniedError):
            journal.append(app_env.context, invalid)
    with pytest.raises(StateMismatchError):
        journal.append(app_env.context, replace(start, trigger_input_sha256="f" * 64))
    with pytest.raises(ScopeDeniedError):
        journal_for(replace(context, cog_snapshot_hash="f" * 64))
    with pytest.raises(ScopeDeniedError):
        journal_for(snapshot=uuid4()).append(app_env.context, start)
    monitor = CogWorkMonitor(
        journal,
        artifacts,
        app_env.context,
        emitter_run_id=runtime.emitter,
        event_type_ref=start.event_type_ref,
    )
    partial = monitor.begin(installed, source, context)
    partial.progress("fixture_paused_before_completion")
    with pytest.raises(ScopeDeniedError):
        receipt_service.seal({**expected, "root_run_id": str(context.invocation_id)})
    partial.fail(uncertain=False, reason="FixtureStopped")
    with pytest.raises(ScopeDeniedError):
        journal.append(app_env.context, start)

    write_effects = []

    async def failed_write(call, value):
        write_effects.append("dispatched")
        call.progress("external_write_dispatched")
        raise OSError("Fixture lost confirmation after dispatch")

    write_operation = HostOperation(
        WorkBinding(
            "tool",
            "syn://thing/fixture-failed-write",
            QUERY,
            RESULTS,
            "f" * 64,
            "non_idempotent_write",
            policy,
        ),
        failed_write,
    )
    writer = ExecutionCogRuntime(
        database=app_env.db,
        artifacts=artifacts,
        actor=app_env.context,
        snapshot_id=snapshot_id,
        task_revision_id=runtime.task_revision,
        repository_state_id=state["id"],
        emitter_run_id=runtime.emitter,
        policy=policy,
        operations=(write_operation,),
        admit=admit,
    )
    write_context = replace(context, invocation_id=uuid4())
    tool = WORK_COGS[WORK_KINDS.index("tool")]
    write_source = TypedCogValue(
        tool.input_type_ref,
        {
            "schema_version": 1,
            "operation_ref": write_operation.binding.operation_ref,
            "input": {"type_ref": QUERY, "value": original.value},
        },
    )
    with pytest.raises(OSError):
        asyncio.run(
            registry.invoke(str(tool.cog_ref), write_source, write_context, controller=writer)
        )
    assert write_effects == ["dispatched"]
    with pytest.raises(ScopeDeniedError):
        asyncio.run(
            registry.invoke(
                str(tool.cog_ref),
                write_source,
                replace(write_context, invocation_id=uuid4()),
                controller=writer,
            )
        )
    assert write_effects == ["dispatched"]
    assert (
        read.events(app_env.context, run_id=write_context.invocation_id)["items"][-1]["stage"]
        == "indeterminate"
    )

    async def mutating_handler(call, value):
        value.value["text"] = "mutated"
        return result("This output must not complete the original invocation")

    mutator = ExecutionCogRuntime(
        database=app_env.db,
        artifacts=artifacts,
        actor=app_env.context,
        snapshot_id=snapshot_id,
        task_revision_id=runtime.task_revision,
        repository_state_id=state["id"],
        emitter_run_id=runtime.emitter,
        policy=policy,
        operations=(
            HostOperation(
                replace(write_operation.binding, effect_class="read_only"), mutating_handler
            ),
        ),
        admit=admit,
    )
    mutate_context = replace(write_context, invocation_id=uuid4())
    with pytest.raises(StateMismatchError):
        asyncio.run(
            registry.invoke(str(tool.cog_ref), write_source, mutate_context, controller=mutator)
        )
    mutated_events = read.events(app_env.context, run_id=mutate_context.invocation_id)["items"]
    assert mutated_events[-1]["stage"] == "failed"
    assert (
        json.loads(
            artifacts.read(
                app_env.context,
                UUID(mutated_events[0]["input_artifact_ref"].removeprefix("artifact:")),
            )
        )["value"]
        == write_source.value
    )

    class UnavailableArtifacts:
        def __getattr__(self, name):
            return getattr(artifacts, name)

        def put(self, *args, **kwargs):
            raise OSError("Fixture artifact storage unavailable before dispatch")

    unavailable = ExecutionCogRuntime(
        database=app_env.db,
        artifacts=UnavailableArtifacts(),
        actor=app_env.context,
        snapshot_id=snapshot_id,
        task_revision_id=runtime.task_revision,
        repository_state_id=state["id"],
        emitter_run_id=runtime.emitter,
        policy=policy,
        operations=(write_operation,),
        admit=admit,
    )
    with pytest.raises(OSError):
        asyncio.run(
            registry.invoke(
                str(tool.cog_ref),
                write_source,
                replace(write_context, invocation_id=uuid4()),
                controller=unavailable,
            )
        )
    assert write_effects == ["dispatched"]
    with pytest.raises(ScopeDeniedError):
        asyncio.run(
            registry.invoke(
                str(tool.cog_ref),
                write_source,
                replace(write_context, invocation_id=uuid4()),
                controller=unavailable,
            )
        )
    revoked.append(True)
    with pytest.raises(ScopeDeniedError):
        invoke(
            WORK_COGS[0],
            {
                "schema_version": 1,
                "operation_ref": operation_ref("agent"),
                "input": {"type_ref": QUERY, "value": original.value},
            },
        )
    assert len(effects) == effect_count
    events, after = [], 0
    while True:
        page = read.events(app_env.context, after=after, limit=17)
        events.extend(page["items"])
        after = page["next_cursor"]
        if not page["has_more"]:
            break
    assert_lifecycles(events, outcomes)
    for outcome in outcomes:
        first = next(e for e in events if e["run_id"] == str(outcome.receipt.invocation_id))
        assert outcome.receipt.input_artifact_id == UUID(
            first["input_artifact_ref"].removeprefix("artifact:")
        )
    assert {e["telemetry"]["work_kind"] for e in events if e["stage"] == "completed"} == {
        *WORK_KINDS,
        "seal",
        "replay",
    }
    assert all(
        e["elapsed_ms"] >= 0 and e["telemetry"]["clock_offset_ms"] == -120000 for e in events
    )
    assert len(root_events[-1]["telemetry"]["child_runs"]) == 16
    assert ExecutionEventService(app_env.other_db).events(app_env.other_context)["items"] == []
    app = create_app(
        task_service=object(),
        job_service=object(),
        authenticate=lambda _: app_env.context,
        execution_events=read,
    )
    with TestClient(app) as api:
        assert (
            api.get(
                "/v1/executions/events?limit=200", headers={"Authorization": "Bearer fixture"}
            ).json()["data"]["items"]
            == events[:200]
        )
        with api.websocket_connect(f"/v1/executions/{root.receipt.invocation_id}/stream") as stream:
            stream.send_json({"type": "authenticate", "token": "fixture", "after": 0})
            assert stream.receive_json()["type"] == "ready"
            assert [stream.receive_json()["event"] for _ in root_events] == root_events
    trace = json.loads(
        artifacts.read(
            app_env.context, UUID(receipt["trace_artifact_ref"].removeprefix("artifact:"))
        )
    )
    assert any(r["scope"]["links"] for r in trace["runs"])
    destination = Path(os.environ.get("POTBOT_PROVENANCE_EVIDENCE_DIR", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "execution-provenance.json").write_text(
        json.dumps(
            {
                "result": "passed",
                "version": __version__,
                "source_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "implementation_source": {
                    p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCE_FILES
                },
                "qualification": "real_SYN_Ember_PostgreSQL_Ed25519_CLI_MCP_REST_WebSocket_fixture",
                "catalog_hash": registry.snapshot.fingerprint,
                "wire_schema_hashes": {ref: runtime.catalog.schema_hash(ref) for ref in schemas()},
                "receipt": receipt,
                "trace": trace,
                "events": events,
                "replay_pages": len(pages),
                "original_effects": effect_count,
                "effects_after_replay": len(effects),
                "actual_cli_process_ids": process_ids,
                "profiling": profiling,
                "limits": "Other work kinds are scripted host fixtures; no live model, browser or hardware qualification",
                "signature": "synthetic_test_keys_only",
                "rest_readback": "passed",
                "websocket_readback": "passed",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
