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
from potbot.controller.child_plans import ChildWorkStep, InstalledChildPlan
from potbot.controller.cog_work import CogWorkMonitor
from potbot.controller.execution_cogs import ExecutionCall, ExecutionCogRuntime
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
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
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
    "src/potbot/controller/child_plans.py",
    "src/potbot/controller/execution_journal.py",
    "src/potbot/controller/execution_receipts.py",
    "src/potbot/controller/execution_events.py",
    "src/potbot/controller/cog_work.py",
    "src/potbot/tools/execution_contracts.py",
    "src/potbot/tools/sandbox_cog_worker.py",
    "src/potbot/persistence/execution_provenance_schema.py",
    "tests/integration/test_execution_provenance.py",
)


def test_real_signed_causal_work_cogs_and_readonly_receipt_replay(app_env, tmp_path, monkeypatch):
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

    class FixtureSandboxTransport(SandboxedCogExecutor):
        """Only the audited worker RPC is simulated in this PostgreSQL test.

        This is intentionally not a kernel/Bubblewrap qualification. The real
        worker, executable hash, UID, lease and syscall audit have independent
        S46 + live-host gates; no production admission is relaxed here.
        """

        def __init__(self, mode: str = "normal"):
            self.mode = mode
            self.dispatched = []

        def bind_context(self, database, actor, installed):
            assert database is app_env.db and actor == app_env.context
            assert all(isinstance(item, SandboxedCogOperation) for item in installed)

        def syn_probe(self, actor, agent):
            assert actor == app_env.context and agent == sandbox_agent

        def authorize_syn(self, op, value, context, actor, agent):
            self.syn_probe(actor, agent)
            return None  # Explicit transport double, no native Syn qualification.

        def syn_provenance(self, actor, agent):
            self.syn_probe(actor, agent)
            return {
                "profile": "potbot.syn-cog-native-binding/1",
                "fixture": "synthetic-public-pins",
                "manifest_sha256": "1" * 64,
                "node": "2" * 64,
                "boot_id": "3" * 32,
            }

        async def execute(self, op, value, context, actor, agent, *, syn_authorization=None):
            assert actor == app_env.context and agent == sandbox_agent
            assert isinstance(op, SandboxedCogOperation)
            self.dispatched.append((op.binding.kind, context.invocation_id))
            if op.binding.operation_ref == "syn://thing/fixture-failed-write":
                if self.mode == "write":
                    write_effects.append("dispatched")
                    raise OSError("Fixture lost confirmation after dispatch")
                if self.mode == "mutate":
                    value.value["text"] = "mutated"
                    return result("This output must not complete the invocation")
                raise AssertionError("unspecified test worker")
            effects.append(op.binding.kind)
            if op.binding.kind == "script":
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
            elif op.binding.kind == "mcp_tool":
                async for client, catalog in serve():
                    response = await client.call_tool(
                        "add",
                        {"a": 3, "b": 4},
                        pinned_catalog_hash=catalog.fingerprint,
                        pinned_tool_hash=catalog.tool("add").fingerprint,
                    )
                    assert response.structured_content == {"sum": 7}
            elif op.binding.kind == "maintenance":

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
                        "qualification": "host_fixture_transport_double_not_sandbox",
                        "target_ram_bytes": None,
                        "uncertainty": "single_sample_noise_not_estimated",
                    }
                )
            return result("Recorded sandbox transport response for " + op.binding.kind)

    for kind in WORK_KINDS:
        operations.append(
            SandboxedCogOperation(
                WorkBinding(
                    kind,
                    operation_ref(kind),
                    QUERY,
                    RESULTS,
                    content_hash({"fixture_handler": kind}),
                    "read_only",
                    policy,
                ),
                ("/usr/local/libexec/fixture-worker",),
                content_hash({"fixture_binary": kind}),
            )
        )
    steps = [ChildWorkStep("script", "script", operation_ref("script"))]
    for kind in WORK_KINDS:
        if kind not in {"script", "workflow"}:
            steps.append(
                ChildWorkStep(
                    kind,
                    kind,
                    operation_ref(kind),
                    links=("script",) if kind == "mcp_tool" else (),
                )
            )
    child_plans = (
        InstalledChildPlan("workflow", operation_ref("workflow"), tuple(steps)),
        InstalledChildPlan(
            "subagent",
            operation_ref("subagent"),
            (ChildWorkStep("tool", "tool", operation_ref("tool")),),
        ),
    )
    sandbox_agent = uuid4()
    sandbox_executor = FixtureSandboxTransport()
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
        sandbox_executor=sandbox_executor,
        sandbox_agent_id=sandbox_agent,
        child_plans=child_plans,
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

    original_child = ExecutionCall._child

    async def record_real_journal_child(call, signature, source, links):
        outcome = await original_child(call, signature, source, links)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(ExecutionCall, "_child", record_real_journal_child)

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
    assert all(
        event["telemetry"]["implementation"]["syn_native"]["manifest_sha256"] == "1" * 64
        for event in root_events
    )
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
    write_operation = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/fixture-failed-write",
            QUERY,
            RESULTS,
            "f" * 64,
            "non_idempotent_write",
            policy,
        ),
        ("/usr/local/libexec/fixture-worker",),
        "e" * 64,
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
        sandbox_executor=FixtureSandboxTransport("write"),
        sandbox_agent_id=sandbox_agent,
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
            SandboxedCogOperation(
                replace(write_operation.binding, effect_class="read_only"),
                write_operation.argv,
                write_operation.executable_sha256,
            ),
        ),
        admit=admit,
        sandbox_executor=FixtureSandboxTransport("mutate"),
        sandbox_agent_id=sandbox_agent,
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
        sandbox_executor=FixtureSandboxTransport("write"),
        sandbox_agent_id=sandbox_agent,
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
                "qualification": "real_SYN_Ember_PostgreSQL_Ed25519_REST_WebSocket_scripted_worker_transport",
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
                "limits": "Sandbox subprocess/UID/lease/audit are transport doubles in this PostgreSQL fixture; no live model, browser, Xpra, host or hardware qualification",
                "signature": "synthetic_test_keys_only",
                "rest_readback": "passed",
                "websocket_readback": "passed",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
