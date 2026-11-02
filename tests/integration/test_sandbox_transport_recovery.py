"""Real PostgreSQL durability; the sandbox executor boundary is a labelled double."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot import __version__
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkRun
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.execution_journal import (
    TRANSPORT_DISPATCH_PHASE,
    PinnedExecutionJournal,
    inspect_sandbox_transport_effect,
)
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError
from potbot.semantics.service import SynService
from potbot.tools.execution_contracts import WORK_KINDS, ExecutionPolicy, WorkBinding
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.semantic_contracts import QUERY, RESULTS
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres


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
