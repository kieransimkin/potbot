"""PostgreSQL admission receipts with a worker double and controlled Syn identity."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.child_plans import ChildWorkStep, InstalledChildPlan
from potbot.controller.cog_capability_policy import (
    Capability,
    CogCapabilityGrant,
    CogCapabilityPolicy,
    CogPolicyDenied,
    Denial,
    installation_hash,
)
from potbot.controller.execution_cogs import ExecutionCogRuntime
from potbot.controller.tasks import TaskService
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.semantics.service import SynService
from potbot.tools.execution_contracts import WORK_KINDS, ExecutionPolicy, WorkBinding
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.semantic_contracts import QUERY, RESULTS
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres


def test_cog_policy_checks_installed_work_before_dispatch(app_env, tmp_path, monkeypatch):
    _, artifacts, _, _, state, _ = setup(app_env, tmp_path)
    actor, db, repo = app_env.context, app_env.db, app_env.repository_id
    tasks = TaskService(db)
    task = tasks.submit(
        actor,
        repo,
        ContractSpec(
            objective="Test a scoped operator grant",
            baseline_ref="git:" + "a" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Check permission", "Record reason"),),
            change_scope=(),
            capabilities=("read", "network", "external_disclosure"),
            verification_policy="checks-v1",
            budget=(("tokens", 0), ("calls", 3), ("tool_ms", 6000), ("concurrency", 1)),
        ),
        "cog-policy",
    )
    episode = UUID(task["task_id"])
    revision = UUID(task["current_contract"]["id"])
    tasks.transition(
        actor, episode, 0, TaskStatus.INITIALIZING, "Initialize", idempotency_key="init"
    )
    tasks.transition(actor, episode, 1, TaskStatus.RUNNING, "Run", idempotency_key="run")
    registry = BoundCogRegistry.provenance()
    sealed = SynService(db, artifacts).import_snapshot(actor, repo, "cog-policy", registry.snapshot)
    policy = ExecutionPolicy("a" * 64, "b" * 64, state["content_digest"], "fixture")

    def operation(kind, name, effect="read_only"):
        return SandboxedCogOperation(
            WorkBinding(kind, f"syn://thing/{name}", QUERY, RESULTS, "c" * 64, effect, policy),
            (f"/usr/local/libexec/{name}",),
            "d" * 64,
        )

    read = operation("tool", "policy-search")
    delete = operation("tool", "policy-delete", "destructive")
    execute = operation("script", "policy-script")
    parent = operation("workflow", "policy-workflow")
    child = operation("api", "policy-api")
    agent, node = uuid4(), "e" * 64
    native = {
        "profile": "potbot.syn-cog-native-binding/1",
        "workspace_id": str(actor.workspace_id),
        "agent_id": str(agent),
        "node": node,
    }
    runs = []
    clock = [1000]
    monkeypatch.setattr("potbot.controller.cog_capability_policy.time", lambda: clock[0])

    class Worker(SandboxedCogExecutor):
        def __init__(self):
            pass

        def bind_context(self, *_):
            pass

        def syn_probe(self, *_):
            pass

        def syn_provenance(self, *_):
            return dict(native)

        def authorize_syn(self, *_):
            return None

        async def execute(self, op, source, cog, _actor, _agent, *, syn_authorization):
            runs.append((op.binding.operation_ref, cog.invocation_id))
            return TypedCogValue(
                RESULTS,
                {
                    "schema_version": 1,
                    "query_hash": source.fingerprint,
                    "corpus_hash": "a" * 64,
                    "items": [],
                    "complete": True,
                },
            )

    def grant(op, *capabilities):
        return CogCapabilityGrant(installation_hash(op), frozenset(capabilities), 1200)

    grants = CogCapabilityPolicy(
        actor,
        agent,
        node,
        policy.policy_revision_hash,
        (
            grant(read, Capability.READ),
            grant(parent, Capability.READ),
            grant(execute, Capability.READ, Capability.CODE_EXECUTION),
            grant(child, Capability.READ, Capability.NETWORK, Capability.EXTERNAL_DISCLOSURE),
        ),
    )
    plan = InstalledChildPlan(
        parent.binding.kind,
        parent.binding.operation_ref,
        (ChildWorkStep("lookup", child.binding.kind, child.binding.operation_ref),),
    )

    def controller(*, ops=(read, delete, execute, parent, child), admit=lambda *_: None):
        return ExecutionCogRuntime(
            database=db,
            artifacts=artifacts,
            actor=actor,
            snapshot_id=UUID(sealed["scope"]["snapshot_id"]),
            task_revision_id=revision,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=policy,
            operations=ops,
            admit=admit,
            sandbox_executor=Worker(),
            sandbox_agent_id=agent,
            child_plans=(plan,),
            capability_policy=grants,
        )

    def invoke(runtime, op, identity):
        signature = WORK_COGS[WORK_KINDS.index(op.binding.kind)]
        source = TypedCogValue(
            signature.input_type_ref,
            {
                "schema_version": 1,
                "operation_ref": op.binding.operation_ref,
                "input": {
                    "type_ref": QUERY,
                    "value": {
                        "schema_version": 1,
                        "text": "private fixture input",
                        "limit": 1,
                    },
                },
            },
        )
        context = CogRunContext(registry.snapshot.fingerprint, revision, state["id"], identity)
        return asyncio.run(runtime.invoke(signature, source, context))

    def history(run):
        with db.transaction(actor) as conn:
            return conn.execute(
                "SELECT stage,telemetry FROM syn_execution_events WHERE workspace_id=%s "
                "AND run_id=%s ORDER BY sequence",
                (actor.workspace_id, run),
            ).fetchall()

    runtime = controller()
    allowed = uuid4()
    assert invoke(runtime, read, allowed).receipt.invocation_id == allowed
    assert history(allowed)[0]["telemetry"]["implementation"]["capability_policy"] == {
        "sha256": grants.fingerprint,
        "capabilities": ["read"],
        "decision": "allow",
    }
    for op, reason in ((delete, Denial.INSTALLATION), (execute, Denial.TASK)):
        denied = uuid4()
        with pytest.raises(CogPolicyDenied, match=reason.value):
            invoke(runtime, op, denied)
        (event,) = history(denied)
        assert event["stage"] == "failed" and event["telemetry"]["phase"] == "admission_denied"
        assert event["telemetry"]["reason_class"] == reason.value
        assert "private fixture input" not in str(event["telemetry"])

    changed = replace(read, executable_sha256="f" * 64)
    with pytest.raises(CogPolicyDenied, match=Denial.INSTALLATION.value):
        invoke(controller(ops=(changed, delete, execute, parent, child)), changed, uuid4())
    native["node"] = "f" * 64
    with pytest.raises(CogPolicyDenied, match=Denial.AGENT.value):
        invoke(runtime, read, uuid4())
    native["node"] = node

    # The child has its own operator grant and the task accepts it, but the parent grants read only.
    workflow = uuid4()
    with pytest.raises(CogPolicyDenied, match=Denial.PARENT.value):
        invoke(runtime, parent, workflow)
    assert runs == [(read.binding.operation_ref, allowed), (parent.binding.operation_ref, workflow)]
    with db.transaction(actor) as conn:
        nested = conn.execute(
            "SELECT e.stage,e.telemetry FROM execution_run_contracts c "
            "JOIN syn_execution_events e USING (workspace_id,run_id) "
            "WHERE c.workspace_id=%s AND c.parent_run_id=%s",
            (actor.workspace_id, workflow),
        ).fetchall()
    (child_event,) = nested
    assert child_event["stage"] == "failed"
    assert child_event["telemetry"]["reason_class"] == Denial.PARENT.value

    # Expiry between the first policy decision and the worker admission fences launch.
    stale = uuid4()
    with pytest.raises(CogPolicyDenied, match=Denial.EXPIRED.value):
        invoke(controller(admit=lambda *_: clock.__setitem__(0, 1200)), read, stale)
    assert all(run != stale for _, run in runs)
    assert history(stale)[-1]["telemetry"]["reason_class"] == Denial.EXPIRED.value
