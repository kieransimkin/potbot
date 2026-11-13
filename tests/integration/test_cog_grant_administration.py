"""Exercise the operator commands and SQL isolation with genuine restricted logins."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID, uuid4

import pytest

from potbot.cogs.contracts import TypedCogValue
from potbot.controller.budgets import BudgetLedger
from potbot.controller.cog_capability_policy import (
    Capability,
    CogCapabilityGrant,
    CogCapabilityPolicy,
    CogPolicyDenied,
    Denial,
    installation_hash,
)
from potbot.controller.cog_grant_store import current_cog_policy, publish_cog_policy
from potbot.controller.jobs import JobService
from potbot.controller.native_cog_dispatch import NativeCogDispatcher
from potbot.controller.tasks import TaskService
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.persistence.admin_cli import main
from potbot.persistence.database import (
    Database,
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.semantic_contracts import QUERY, RESULTS
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres


def test_enrollment_pin_migration_preserves_existing_grant_fingerprint(app_env):
    schema = "grant_before_enrollment_pins"
    assert migrate(app_env.admin_dsn, schema, application_migrations()[:36])[-1] == 36
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    workspace = provision_workspace(app_env.admin_dsn, schema, "Existing grant workspace")
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    agent = uuid4()
    legacy = content_hash(
        {
            "profile": "potbot.cog-capability-policy/1",
            "workspace_id": str(workspace),
            "principal_id": str(actor.principal_id),
            "agent_id": str(agent),
            "node_sha256": "a" * 64,
            "revision_sha256": "b" * 64,
            "grants": [["c" * 64, ["read"], 2**31]],
        }
    )
    with admin_connection(app_env.admin_dsn, schema) as connection:
        connection.execute(
            "INSERT INTO cog_grant_revisions(workspace_id,principal_id,agent_id,generation,"
            "node_sha256,revision_sha256,policy_sha256,grants) VALUES (%s,%s,%s,1,%s,%s,%s,%s::jsonb)",
            (
                workspace,
                actor.principal_id,
                agent,
                "a" * 64,
                "b" * 64,
                legacy,
                json.dumps(
                    [
                        {
                            "installation_sha256": "c" * 64,
                            "capabilities": ["read"],
                            "expires_at": 2**31,
                        }
                    ]
                ),
            ),
        )
    assert migrate(app_env.admin_dsn, schema)[-1] == 37
    with Database(app_env.db.dsn, schema).transaction(actor) as connection:
        published = current_cog_policy(connection, actor, agent)
        assert published is not None
        assert published.policy.fingerprint == legacy
        assert published.policy.enrollment_sha256 is None


def test_cog_grants_operator_publication_and_principal_isolation(
    app_env, tmp_path, monkeypatch, capsys
):
    actor, agent = app_env.context, uuid4()
    monkeypatch.setenv("POTBOT_ADMIN_DSN", app_env.admin_dsn)
    scope = [
        "--workspace-id",
        str(actor.workspace_id),
        "--principal-id",
        str(actor.principal_id),
        "--agent-id",
        str(agent),
    ]

    def show():
        assert main(["cog-grants-show", *scope]) == 0
        return json.loads(capsys.readouterr().out)["result"]

    document = tmp_path / "grants.json"
    allow = [{"installation_sha256": "a" * 64, "capabilities": ["read"], "expires_at": 2**31}]

    def publish(value, expected, enrollment=None):
        document.write_text(json.dumps(value), encoding="utf-8")
        pin = ["--enrollment-sha256", enrollment] if enrollment else []
        status = main(
            [
                "cog-grants-publish",
                *scope,
                "--node-sha256",
                "b" * 64,
                "--revision-sha256",
                "c" * 64,
                "--grants-file",
                str(document),
                "--expected-generation",
                str(expected),
                *pin,
            ]
        )
        return status, json.loads(capsys.readouterr().out)

    assert show() == {"generation": 0, "grants": []}
    status, issued = publish(allow, 0)
    assert status == 0 and issued["result"]["generation"] == 1
    assert publish(allow, 0) == (0, issued)
    status, conflict = publish([], 0)
    assert status == 1 and conflict["error"]["code"] == "revision_conflict"
    assert show() == issued["result"]

    with app_env.other_db.transaction(app_env.other_context) as connection:
        other_role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    peer = provision_principal(app_env.admin_dsn, app_env.schema, other_role, actor.workspace_id)
    with app_env.other_db.transaction(peer) as connection:
        assert current_cog_policy(connection, actor, agent) is None
        assert (
            connection.execute("SELECT count(*) AS rows FROM cog_grant_revisions").fetchone()[
                "rows"
            ]
            == 0
        )
    restarted = Database(app_env.db.dsn, app_env.schema)
    with restarted.transaction(actor) as connection:
        published = current_cog_policy(connection, actor, agent)
        assert published is not None and published.to_dict() == issued["result"]
    with pytest.raises(ScopeDeniedError), restarted.transaction(actor) as connection:
        connection.execute("UPDATE cog_grant_revisions SET grants='[]'")
    with pytest.raises(ScopeDeniedError), restarted.transaction(actor) as connection:
        connection.execute(
            "INSERT INTO cog_grant_revisions SELECT workspace_id,principal_id,agent_id,2,"
            "node_sha256,revision_sha256,policy_sha256,grants,published_by,published_at "
            "FROM cog_grant_revisions"
        )

    status, revoked = publish([], 1)
    assert status == 0 and revoked["result"]["generation"] == 2
    assert show() == revoked["result"] and show()["grants"] == []
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        history = connection.execute(
            "SELECT generation,grants,published_by FROM cog_grant_revisions ORDER BY generation"
        ).fetchall()
    assert [(row["generation"], row["grants"]) for row in history] == [(1, allow), (2, [])]
    assert all(row["published_by"] == issued["result"]["published_by"] for row in history)
    assert published is not None
    contender = replace(
        published.policy, grants=(replace(published.policy.grants[0], expires_at=2**31 + 1),)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(publish_cog_policy, app_env.admin_dsn, app_env.schema, policy, 2)
            for policy in (published.policy, contender)
        ]
        winners = []
        for future in pending:
            try:
                winners.append(future.result(timeout=10))
            except RevisionConflictError:
                pass
    (winner,) = winners
    assert winner.generation == 3 and show() == winner.to_dict()
    status, pinned = publish(allow, 3, "d" * 64)
    assert status == 0 and pinned["result"]["enrollment_sha256"] == "d" * 64
    assert publish(allow, 3, "d" * 64) == (0, pinned)
    status, downgrade = publish(allow, 4)
    assert status == 1 and downgrade["error"]["code"] == "invalid_input"
    assert show() == pinned["result"]


def test_hosted_cog_grants_gate_dispatch_and_budget(app_env, tmp_path):
    _, _, _, _, state, _ = setup(app_env, tmp_path)
    actor, db, agent = app_env.context, app_env.db, uuid4()
    profile, node = "a" * 64, "b" * 64
    op = SandboxedCogOperation(
        WorkBinding(
            "tool",
            "syn://thing/granted-job",
            QUERY,
            RESULTS,
            "c" * 64,
            "read_only",
            ExecutionPolicy(
                profile, "d" * 64, state["content_digest"], "fixture", max_wall_ms=1000
            ),
        ),
        ("/usr/local/libexec/granted-job",),
        "c" * 64,
    )
    tasks = TaskService(db)
    task = tasks.submit(
        actor,
        app_env.repository_id,
        ContractSpec(
            objective="Verify a queued read",
            baseline_ref="git:" + "1" * 40,
            deliverable_type=DeliverableType.INVESTIGATION,
            requirements=(Requirement("R1", "Verify admission", "Retain receipt"),),
            change_scope=(),
            capabilities=("read", "cog.execute:" + op.binding.operation_ref),
            verification_policy="checks-v1",
            budget=(("tokens", 0), ("calls", 2), ("tool_ms", 2000), ("concurrency", 1)),
        ),
        "hosted-grant",
    )
    episode = UUID(task["task_id"])
    tasks.transition(
        actor, episode, 0, TaskStatus.INITIALIZING, "Initialize", idempotency_key="init"
    )
    tasks.transition(actor, episode, 1, TaskStatus.RUNNING, "Run", idempotency_key="run")
    jobs = JobService(db)
    worker_calls = []
    during_syn_admission = None
    enrollment = "f" * 64

    class Worker(SandboxedCogExecutor):
        def __init__(self):
            pass

        def authorize_syn(self, *_):
            if during_syn_admission is not None:
                during_syn_admission()
            return object()

        def syn_provenance(self, *_):
            return {
                "profile": "potbot.syn-cog-native-binding/1",
                "workspace_id": str(actor.workspace_id),
                "agent_id": str(agent),
                "node": node,
                "enrollment_sha256": enrollment,
            }

        async def execute(self, operation, *_args, **_kwargs):
            worker_calls.append(operation.binding.operation_ref)
            return TypedCogValue(RESULTS, {"items": []})

    def enqueue(key):
        jobs.submit(
            actor,
            {
                "job_type": "cog.execute",
                "episode_id": str(episode),
                "selected_models": [],
                "input": {
                    "agent_id": str(agent),
                    "repository_state_id": str(state["id"]),
                    "operation_ref": op.binding.operation_ref,
                    "profile_sha256": profile,
                    "type_ref": QUERY,
                    "value": {"text": "query"},
                },
            },
            key,
        )
        lease = jobs.claim(actor, "operator-worker", kinds=("cog.execute",))
        assert lease is not None
        return lease

    def dispatcher():
        return NativeCogDispatcher(
            Worker(), (op,), frozenset({agent}), profile, persistent_cog_grants=True
        )

    grant = CogCapabilityPolicy(
        actor,
        agent,
        node,
        profile,
        (CogCapabilityGrant(installation_hash(op), frozenset({Capability.READ}), 2**31),),
        enrollment_sha256=enrollment,
    )
    issued = publish_cog_policy(app_env.admin_dsn, app_env.schema, grant, 0)
    allowed = enqueue("allowed")
    result = dispatcher().run(jobs, actor, allowed, Event())
    jobs.finish(actor, allowed, "succeeded", result=result)
    balance = BudgetLedger(db).inspect(actor, episode)
    assert balance["used"] == {"calls": 1, "tool_ms": 1000} and balance["held"] == {}
    (dispatch,) = [e for e in jobs.events(actor)["items"] if e["event_type"] == "job.dispatched"]
    assert dispatch["data"]["capability_policy"] == {
        "generation": 1,
        "sha256": issued.policy.fingerprint,
        "decision": "allow",
        "capabilities": ["read"],
    }
    enrolled = enrollment
    enrollment = "e" * 64
    wrong_epoch = enqueue("wrong-epoch")
    with pytest.raises(CogPolicyDenied, match=Denial.ENROLLMENT.value):
        dispatcher().run(jobs, actor, wrong_epoch, Event())
    assert jobs.inspect(actor, wrong_epoch.job_id)["dispatched_at"] is None
    jobs.finish(actor, wrong_epoch, "failed", error_code="scope_denied")
    enrollment = enrolled

    def revoke():
        publish_cog_policy(app_env.admin_dsn, app_env.schema, replace(grant, grants=()), 1)

    during_syn_admission = revoke
    denied = enqueue("denied")
    with pytest.raises(CogPolicyDenied, match=Denial.REPLACED.value):
        dispatcher().run(jobs, actor, denied, Event())
    assert jobs.inspect(actor, denied.job_id)["dispatched_at"] is None
    jobs.finish(actor, denied, "failed", error_code="scope_denied")
    revoked = enqueue("revoked")
    with pytest.raises(CogPolicyDenied, match=Denial.INSTALLATION.value):
        dispatcher().run(jobs, actor, revoked, Event())
    assert jobs.inspect(actor, revoked.job_id)["dispatched_at"] is None
    assert worker_calls == [op.binding.operation_ref]
    assert BudgetLedger(db).inspect(actor, episode) == balance
