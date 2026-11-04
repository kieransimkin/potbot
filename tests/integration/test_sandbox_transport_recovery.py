"""Real PostgreSQL durability; the sandbox executor boundary is a labelled double."""

import asyncio
import hashlib
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot import __version__
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.budgets import BudgetLedger
from potbot.controller.cog_work import CogWorkRun
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.execution_journal import (
    TRANSPORT_DISPATCH_PHASE,
    PinnedExecutionJournal,
    inspect_sandbox_transport_effect,
)
from potbot.controller.tasks import TaskService
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.semantics.service import SynService
from potbot.tools import queue_transport_worker
from potbot.tools.execution_contracts import WORK_KINDS, ExecutionPolicy, WorkBinding
from potbot.tools.queue_transport import EXECUTABLE, install_queue_transport
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.semantic_contracts import QUERY, RESULTS
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres


@pytest.mark.sandbox
def test_real_stdio_queue_reconciles_receipts_and_settles_once(app_env, tmp_path, monkeypatch):
    """Use the existing executor and real Bubblewrap transport in a disposable fixture."""
    fixture, artifacts, states, env, _, _ = setup(app_env, tmp_path)
    actor = app_env.context
    state = states.capture(actor, app_env.repository_id, fixture.path, env, committed_ref="HEAD")
    tasks = TaskService(app_env.db)
    task = tasks.submit(
        actor,
        app_env.repository_id,
        ContractSpec(
            objective="Verify queue recovery",
            baseline_ref="git:" + "a" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Read queue", "Record durable outcome"),),
            change_scope=(),
            capabilities=("test",),
            verification_policy="checks-v1",
            budget=(("tokens", 0), ("calls", 3), ("tool_ms", 15000), ("concurrency", 1)),
        ),
        "queue-task",
    )
    episode = UUID(task["task_id"])
    tasks.transition(
        actor, episode, 0, TaskStatus.INITIALIZING, "Init", idempotency_key="queue-init"
    )
    tasks.transition(actor, episode, 1, TaskStatus.RUNNING, "Run", idempotency_key="queue-run")
    revision = UUID(task["current_contract"]["id"])
    registry = BoundCogRegistry.provenance(queue_transport=True)
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        actor,
        app_env.repository_id,
        "queue-transport-v1",
        registry.snapshot,
    )
    snapshot = UUID(sealed["scope"]["snapshot_id"])
    policy = ExecutionPolicy(
        "a" * 64,
        "b" * 64,
        state["content_digest"],
        __version__,
        max_wall_ms=5000,
    )
    home, rootfs, private = (
        tmp_path / name for name in ("queue-home", "queue-rootfs", "queue-private")
    )
    agent_home = home / "agent"
    binary = rootfs / EXECUTABLE.lstrip("/")
    for path in (agent_home, binary.parent, private):
        path.mkdir(parents=True)
    binary.write_bytes(Path(queue_transport_worker.__file__).read_bytes())
    binary.chmod(0o555)
    target, agent = uuid4(), uuid4()
    operation = install_queue_transport(
        target, hashlib.sha256(binary.read_bytes()).hexdigest(), policy
    )
    runtime_pin = SharedWorkspaceRuntime("b" * 64, rootfs, home, "f" * 64)
    leases = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    leases.pin_image(
        actor.workspace_id,
        digest=runtime_pin.image_digest,
        image_ref="ghcr.io/test/system@sha256:" + runtime_pin.image_digest,
        build_commit="c" * 40,
        platform="linux/amd64",
        rootfs_path=rootfs,
        qualification_sha256=runtime_pin.qualification_sha256,
    )
    native_sandbox = ProcessSupervisor()
    launched = []

    def run(request, *, cancelled=None):
        # Hosted CI supplies the normal Bubblewrap/seccomp sandbox. This
        # fixture projects the pinned script into it without a provisioned S46
        # image, registered native Syn release, cgroup or audited operator.
        actual = replace(
            request,
            argv=(sys.executable, "-I", str(binary), *request.argv[1:]),
            guest_uid=None,
            guest_gid=None,
            cog_log_parent=None,
            read_only_roots=(binary.parent,),
        )
        observed = native_sandbox.run(actual, cancelled=cancelled)
        assert observed.dispatched and observed.processes_stopped, observed
        launched.append(request.argv[1])
        return observed

    initialization = run(
        ProcessRequest(
            (EXECUTABLE, "init", str(target)),
            agent_home,
            home,
            sandbox_root=home,
        )
    )
    assert initialization.exit_code == 0, initialization
    assert (agent_home / queue_transport_worker.DB).is_file()

    class KernelFixtureExecutor(SandboxedCogExecutor):
        def __init__(self):
            self.runtime, self.ledger = runtime_pin, leases
            registered = SimpleNamespace(
                workspace_id=actor.workspace_id,
                agent_id=agent,
                directory="agent",
                uid=555,
                gid=69,
            )
            self.homes = SimpleNamespace(lookup=lambda *_: registered)
            self.supervisor = SimpleNamespace(run=run)
            self.syn_mesh = SynWorkerNodeMesh(interval_seconds=0.05)
            self.development_syn_fixture = True
            self._context_binding = None

        def bind_context(self, database, context, operations):
            assert database is app_env.db and context == actor and operations == (operation,)

    def runtime(authorized=True):
        return ExecutionCogRuntime(
            database=app_env.db,
            artifacts=artifacts,
            actor=actor,
            snapshot_id=snapshot,
            task_revision_id=revision,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=policy,
            operations=(operation,),
            admit=lambda *_: None,
            sandbox_executor=KernelFixtureExecutor(),
            sandbox_agent_id=agent,
            admit_transport_recovery=(lambda _: None) if authorized else None,
        )

    def invocation(effect_id):
        signature = WORK_COGS[WORK_KINDS.index("api")]
        source = TypedCogValue(
            signature.input_type_ref,
            {
                "schema_version": 1,
                "operation_ref": operation.binding.operation_ref,
                "input": {
                    "type_ref": queue_transport_worker.REQUEST,
                    "value": {"effect_id": str(effect_id), "message": "A durable test message"},
                },
            },
        )
        context = CogRunContext(registry.snapshot.fingerprint, revision, state["id"], uuid4())
        return signature, source, context

    budgets = BudgetLedger(app_env.db)
    original = PinnedExecutionJournal.append

    def lose_intent_ack(self, context, event):
        result = original(self, context, event)
        if event.stage == "started":
            raise OSError("intent committed before dispatch")
        return result

    prepared = invocation(uuid4())
    with monkeypatch.context() as fault:
        fault.setattr(PinnedExecutionJournal, "append", lose_intent_ack)
        with pytest.raises(OSError, match="intent committed"):
            asyncio.run(runtime().invoke(*prepared))
    released = asyncio.run(runtime().reconcile_transport_effect(prepared[2].invocation_id))
    assert released["queue_state"] == "cancelled" and released["target_receipt"] is None
    assert launched == ["init"]
    assert budgets.inspect(actor, episode)["used"] == budgets.inspect(actor, episode)["held"] == {}

    def lose_worker_receipt(self, outcome):
        raise OSError("controller lost the worker result after its target committed")

    effect_id = uuid4()
    args = invocation(effect_id)
    with monkeypatch.context() as fault:
        fault.setattr(CogWorkRun, "complete", lose_worker_receipt)
        with pytest.raises(OSError, match="target committed"):
            asyncio.run(runtime().invoke(*args))
    assert launched == ["init", "apply"]
    assert budgets.inspect(actor, episode)["held"] == {
        "calls": 1,
        "concurrency": 1,
        "tool_ms": 5000,
    }
    run_id = args[2].invocation_id
    with pytest.raises(ScopeDeniedError, match="explicit operator"):
        asyncio.run(runtime(False).reconcile_transport_effect(run_id))
    resolved = asyncio.run(runtime().reconcile_transport_effect(run_id))
    assert resolved["queue_state"] == "applied" and resolved["external_effect_verified"]
    assert asyncio.run(runtime().reconcile_transport_effect(run_id)) == resolved
    assert launched == ["init", "apply", "resolve"]
    assert budgets.inspect(actor, episode)["used"] == {"calls": 1, "tool_ms": 5000}
    assert budgets.inspect(actor, episode)["held"] == {}
    with pytest.raises(ScopeDeniedError, match="already has an attempt"):
        asyncio.run(runtime().invoke(*invocation(effect_id)))
    assert launched == ["init", "apply", "resolve"]

    def lose_marker_ack(self, context, event):
        result = original(self, context, event)
        if TRANSPORT_DISPATCH_PHASE in event.telemetry_json:
            raise OSError("dispatch marker committed before worker")
        return result

    cancelled_id = uuid4()
    cancelled = invocation(cancelled_id)
    with monkeypatch.context() as fault:
        fault.setattr(PinnedExecutionJournal, "append", lose_marker_ack)
        with pytest.raises(IndeterminateExecutionError):
            asyncio.run(runtime().invoke(*cancelled))
    result = asyncio.run(runtime().reconcile_transport_effect(cancelled[2].invocation_id))
    assert result["queue_state"] == "cancelled"
    assert not result["external_effect_verified"]
    assert launched == ["init", "apply", "resolve", "resolve"]
    assert budgets.inspect(actor, episode)["used"] == {"calls": 2, "tool_ms": 10000}
    assert budgets.inspect(actor, episode)["held"] == {}
    normal_id = uuid4()
    normal = invocation(normal_id)
    result = asyncio.run(runtime().invoke(*normal))
    assert result.value.value["result"]["value"]["outcome"] == "applied"
    assert budgets.inspect(actor, episode)["used"] == {"calls": 3, "tool_ms": 15000}
    assert budgets.inspect(actor, episode)["held"] == {}
    with sqlite3.connect(agent_home / queue_transport_worker.DB) as target_db:
        records = target_db.execute("SELECT effect_id,outcome,message FROM queue").fetchall()
    assert set(records) == {
        (str(effect_id), "applied", "A durable test message"),
        (str(cancelled_id), "cancelled", None),
        (str(normal_id), "applied", "A durable test message"),
    }


def test_transport_effect_recovery_across_worker_and_journal_faults(app_env, tmp_path, monkeypatch):
    fixture, artifacts, states, env, _, _ = setup(app_env, tmp_path)
    state = states.capture(
        app_env.context, app_env.repository_id, fixture.path, env, committed_ref="HEAD"
    )
    registry = BoundCogRegistry.provenance()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "transport-recovery-v1", registry.snapshot
    )
    snapshot = UUID(sealed["scope"]["snapshot_id"])
    policy = ExecutionPolicy("a" * 64, "b" * 64, state["content_digest"], __version__)
    task, agent = uuid4(), uuid4()
    signature = WORK_COGS[WORK_KINDS.index("api")]
    source = TypedCogValue(QUERY, {"schema_version": 1, "text": "Controlled write", "limit": 1})
    attempts = []

    def inspect(run_id):
        return inspect_sandbox_transport_effect(app_env.db, artifacts, app_env.context, run_id)

    class FixtureWorker(SandboxedCogExecutor):
        """Simulate only admitted worker RPC, not kernel or native Syn qualification."""

        def __init__(self, mode):
            self.mode = mode

        def bind_context(self, database, actor, operations):
            assert database is app_env.db and actor == app_env.context
            assert all(isinstance(op, SandboxedCogOperation) for op in operations)

        def syn_probe(self, actor, agent_id):
            assert actor == app_env.context and agent_id == agent

        def syn_provenance(self, actor, agent_id):
            self.syn_probe(actor, agent_id)
            return None

        def authorize_syn(self, op, value, context, actor, agent_id):
            self.syn_probe(actor, agent_id)
            if self.mode == "deny":
                raise ScopeDeniedError("Fixture grant refused before dispatch")
            return None

        async def execute(self, op, value, context, actor, agent_id, *, syn_authorization=None):
            self.syn_probe(actor, agent_id)
            assert isinstance(op, SandboxedCogOperation)
            observed = inspect(context.invocation_id)
            assert observed["dispatch_marker_recorded"] is True
            assert observed["state"] == "indeterminate"
            attempts.append(context.invocation_id)
            if self.mode == "lose_effect_ack":
                raise OSError("Fixture response lost after effect")
            return TypedCogValue(
                RESULTS,
                {
                    "schema_version": 1,
                    "query_hash": source.fingerprint,
                    "corpus_hash": "c" * 64,
                    "items": [],
                    "complete": True,
                },
            )

    def invoke(lane, context, mode="normal"):
        operation = SandboxedCogOperation(
            WorkBinding(
                "api",
                f"syn://thing/transport-fixture-{lane}",
                QUERY,
                RESULTS,
                "d" * 64,
                "non_idempotent_write",
                policy,
            ),
            ("/usr/local/libexec/fixture-worker",),
            "e" * 64,
        )
        runtime = ExecutionCogRuntime(
            database=app_env.db,
            artifacts=artifacts,
            actor=app_env.context,
            snapshot_id=snapshot,
            task_revision_id=context.task_revision_id,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=policy,
            operations=(operation,),
            admit=lambda *_: None,
            sandbox_executor=FixtureWorker(mode),
            sandbox_agent_id=agent,
        )
        request = TypedCogValue(
            signature.input_type_ref,
            {
                "schema_version": 1,
                "operation_ref": operation.binding.operation_ref,
                "input": {"type_ref": QUERY, "value": source.value},
            },
        )
        return asyncio.run(runtime.invoke(signature, request, context))

    def new_context():
        return CogRunContext(registry.snapshot.fingerprint, task, state["id"], uuid4())

    original_append = PinnedExecutionJournal.append

    def missing_intent(self, actor, event):
        if event.stage == "started":
            raise OSError("Fixture database unavailable before intent")
        return original_append(self, actor, event)

    before_intent = new_context()
    with monkeypatch.context() as fault:
        fault.setattr(PinnedExecutionJournal, "append", missing_intent)
        with pytest.raises(OSError, match="before intent"):
            invoke("before-intent", before_intent)
    assert not attempts
    with pytest.raises(ScopeDeniedError):
        inspect(before_intent.invocation_id)

    denied = new_context()
    with pytest.raises(ScopeDeniedError, match="before dispatch"):
        invoke("denied", denied, mode="deny")
    assert inspect(denied.invocation_id)["state"] == "stopped_before_dispatch"
    assert not attempts

    def lost_marker_ack(self, actor, event):
        result = original_append(self, actor, event)
        if TRANSPORT_DISPATCH_PHASE in event.telemetry_json:
            raise OSError("Fixture crash after marker commit, before executor entry")
        return result

    before_launch = new_context()
    with monkeypatch.context() as fault:
        fault.setattr(PinnedExecutionJournal, "append", lost_marker_ack)
        with pytest.raises(IndeterminateExecutionError):
            invoke("marker-ack", before_launch)
    assert not attempts
    assert inspect(before_launch.invocation_id)["state"] == "indeterminate"
    with pytest.raises(ScopeDeniedError, match="unresolved durable attempt"):
        invoke("marker-ack", new_context())

    lost = new_context()
    with pytest.raises(OSError, match="after effect"):
        invoke("lost", lost, mode="lose_effect_ack")
    assert inspect(lost.invocation_id)["state"] == "indeterminate"
    # A new runtime, emitter, task and invocation cannot clear the stable lane.
    with pytest.raises(ScopeDeniedError, match="unresolved durable attempt"):
        invoke("lost", replace(new_context(), task_revision_id=uuid4()))
    assert attempts == [lost.invocation_id]
    with pytest.raises(ScopeDeniedError, match="unavailable"):
        inspect_sandbox_transport_effect(
            app_env.other_db, artifacts, app_env.other_context, lost.invocation_id
        )

    original_complete = CogWorkRun.complete

    def lose_completed_ack(self, outcome):
        original_complete(self, outcome)
        raise OSError("Fixture controller response lost after receipt commit")

    completed = new_context()
    with monkeypatch.context() as fault:
        fault.setattr(CogWorkRun, "complete", lose_completed_ack)
        with pytest.raises(OSError, match="after receipt commit"):
            invoke("denied", completed)
    receipt = inspect(completed.invocation_id)
    assert receipt["state"] == "receipt_recorded"
    assert receipt["receipt_ref"] and len(receipt["receipt_sha256"]) == 64
    assert receipt["external_effect_verified"] is False
    assert receipt["automatic_retry_permitted"] is False
    with pytest.raises(ScopeDeniedError):
        invoke("denied", completed)
    assert attempts == [lost.invocation_id, completed.invocation_id]
