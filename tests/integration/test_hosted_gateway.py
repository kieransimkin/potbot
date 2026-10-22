"""Real SQL hosted admission/accounting with HTTP mocks; no paid calls or live keys."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest

import potbot.controller.job_runner as runner_module
from potbot.controller.budgets import BudgetLedger
from potbot.controller.job_runner import HostedJobRunner
from potbot.controller.jobs import JobService
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.models.hosted_gateway import HostedInferenceGateway
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.service_credentials import ServiceCredentials

pytestmark = pytest.mark.postgres
KEY = "hosted-provider-secret-canary-not-real-123456789"
OTHER = "private-api-service-canary-not-real-987654321"


def sse(value: dict) -> bytes:
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def wire(texts=("model output",), *, usage=(12, 3), complete=True) -> bytes:
    parts = [
        sse(
            {
                "id": "mock-response",
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {"content": text}}],
            }
        )
        for text in texts
    ]
    if complete:
        parts.append(sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
        if usage is not None:
            parts.append(
                sse(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": usage[0],
                            "completion_tokens": usage[1],
                            "total_tokens": sum(usage),
                        },
                    }
                )
            )
        parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


def hosted_contract(*, money=100000):
    return ContractSpec(
        objective="Hosted approach comparison",
        baseline_ref="git:" + "1" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(Requirement("R1", "Compare approaches", "Retain useful observations"),),
        change_scope=("src/app.py",),
        capabilities=("test",),
        verification_policy="tests-v1",
        budget=(
            ("tokens", 1000000),
            ("calls", 8),
            ("tool_ms", 1000),
            ("concurrency", 2),
            ("inference_ms", 120000),
            ("money_microunits", money),
        ),
    )


def rig(
    env,
    *,
    response=None,
    money=100000,
    prompt="private task prompt",
    claim=True,
    roles=("reasoning",),
    selected_models=None,
):
    tasks = TaskService(env.db)
    task = tasks.submit(env.context, env.repository_id, hosted_contract(money=money), "hosted-task")
    task_id = UUID(task["task_id"])
    task = tasks.start_generation(
        env.context, task_id, task["update_token"], "fixture admission", idempotency_key="start"
    )
    jobs = JobService(env.db)
    job = jobs.submit(
        env.context,
        {
            "job_type": "model.generate",
            "episode_id": str(task_id),
            "input": {"prompt": prompt, "max_output_tokens": 16, "timeout_seconds": 30},
            "selected_models": selected_models
            or [{"provider": "openai", "model": "fixture-model", "role": role} for role in roles],
        },
        "hosted-job",
    )
    lease = jobs.claim(env.context, "hosted-worker", lease_seconds=300) if claim else None
    policy = HostedPolicy(
        allow_prompt_disclosure=True,
        rules=(RateRule("openai", "fixture-model", 1000000, 2000000, protocol="chat_completions"),),
    )
    requests = []
    callbacks = SimpleNamespace(before_response=None)

    def handler(request):
        requests.append(request)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + KEY
        if callbacks.before_response is not None:
            callbacks.before_response(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=wire() if response is None else response,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    credentials = ServiceCredentials(environment={"OPENAI_API_KEY": KEY})
    gateway = HostedInferenceGateway(
        env.db, policy, credentials.resolve, http_client=http, protected_values=(OTHER,)
    )
    output = []

    def publish(text):
        output.append(text)
        jobs.output(
            env.context, lease, text, provider="openai", model="fixture-model", role="reasoning"
        )

    return SimpleNamespace(
        env=env,
        tasks=tasks,
        task=task,
        task_id=task_id,
        jobs=jobs,
        job=job,
        lease=lease,
        policy=policy,
        gateway=gateway,
        http=http,
        requests=requests,
        callbacks=callbacks,
        output=output,
        publish=publish,
    )


def generate(value, *, cancelled=lambda: False):
    return value.gateway.generate(
        value.env.context, value.lease, 0, on_delta=value.publish, cancelled=cancelled
    )


def invocation_id(value):
    return uuid5(
        NAMESPACE_URL, f"potbot:hosted:{value.env.context.workspace_id}:{value.lease.job_id}:0"
    )


def ledger(value):
    return BudgetLedger(value.env.db).inspect(value.env.context, value.task_id)


def test_success_replay_and_policy_conflict_do_not_duplicate_calls_or_accounting(app_env) -> None:
    value = rig(app_env)
    try:
        first = generate(value)
        assert first["status"] == "completed" and first["qualified"] is False
        assert first["accounting"] == "conservative_estimate"
        assert first["accounted_amounts"]["money_microunits"] == 18
        assert ledger(value)["used"]["tokens"] == 15 and ledger(value)["held"] == {}
        assert generate(value) == first and len(value.requests) == 1
        assert "".join(value.output) == "model output"
        changed = HostedInferenceGateway(
            app_env.db,
            replace(value.policy, max_cost_microunits=999999),
            lambda reference: KEY,
            http_client=value.http,
        )
        with pytest.raises(RevisionConflictError):
            changed.generate(
                app_env.context, value.lease, 0, on_delta=value.publish, cancelled=lambda: False
            )
        with app_env.db.transaction(app_env.context) as conn:
            assert conn.execute("SELECT count(*) AS n FROM hosted_requests").fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM budget_usage").fetchone()["n"] == 1
        assert len(value.requests) == 1
    finally:
        value.http.close()


def test_usage_overrun_is_retained_and_exhausts_task(app_env) -> None:
    value = rig(app_env, response=wire(usage=(1200, 1000)), money=2000)
    try:
        result = generate(value)
        assert result["accounted_amounts"]["money_microunits"] == 3200
        assert ledger(value)["used"]["money_microunits"] == 3200
        assert ledger(value)["held"] == {}
        assert (
            value.tasks.inspect(app_env.context, value.task_id)["lifecycle"] == "budget_exhausted"
        )
    finally:
        value.http.close()


def test_unknown_usage_retains_reservation_until_explicit_reconciliation(app_env) -> None:
    value = rig(app_env, response=wire(usage=None))
    try:
        result = generate(value)
        assert result["status"] == "indeterminate" and result["usage"] is None
        assert result["accounting"] == "pending_reconciliation"
        assert ledger(value)["held"]["calls"] == 1 and ledger(value)["used"] == {}
        assert generate(value) == result and len(value.requests) == 1
        actual = {"calls": 1, "tokens": 15, "inference_ms": 20, "money_microunits": 18}
        with pytest.raises(StateMismatchError):
            value.gateway.reconcile(app_env.context, invocation_id(value), actual=actual)
        value.jobs.finish(app_env.context, value.lease, "indeterminate", error_code="indeterminate")
        reconciled = value.gateway.reconcile(app_env.context, invocation_id(value), actual=actual)
        assert reconciled["accounting"] == "provider_reported" and reconciled["executable"] is False
        value.gateway.reconcile(app_env.context, invocation_id(value), actual=actual)
        assert ledger(value)["held"] == {} and ledger(value)["used"]["calls"] == 1
        with pytest.raises(StateMismatchError):
            value.gateway.reconcile(
                app_env.context, invocation_id(value), actual={**actual, "tokens": 999}
            )
        with app_env.db.transaction(app_env.context) as conn:
            assert conn.execute("SELECT count(*) AS n FROM budget_usage").fetchone()["n"] == 1
    finally:
        value.http.close()


def test_pre_http_cancellation_never_calls_provider_or_spends_budget(app_env) -> None:
    value = rig(app_env)
    try:
        result = generate(value, cancelled=lambda: True)
        assert result["status"] == "rejected" and result["error_code"] == "cancelled"
        assert value.requests == [] and ledger(value)["held"] == {} and ledger(value)["used"] == {}
        assert value.jobs.inspect(app_env.context, value.lease.job_id)["dispatched_at"] is None
    finally:
        value.http.close()


def test_cancelling_job_is_denied_before_any_invocation(app_env) -> None:
    value = rig(app_env)
    try:
        value.jobs.cancel(app_env.context, value.lease.job_id, "cancel-before-admission")
        with pytest.raises(OperationCancelledError):
            generate(value)
        assert value.requests == [] and ledger(value)["held"] == {}
        with app_env.db.transaction(app_env.context) as conn:
            assert conn.execute("SELECT count(*) AS n FROM hosted_requests").fetchone()["n"] == 0
    finally:
        value.http.close()


def revise(value):
    task = value.tasks.inspect(value.env.context, value.task_id)
    contract = ContractSpec.from_dict(task["current_contract"]["content"])
    return value.tasks.revise(
        value.env.context,
        value.task_id,
        UUID(task["current_contract"]["id"]),
        replace(contract, objective="Changed objective"),
        "hosted-revision",
    )


def test_queued_revision_cannot_run_after_task_changes(app_env) -> None:
    value = rig(app_env)
    try:
        revise(value)
        with pytest.raises(StateMismatchError):
            generate(value)
        assert value.requests == [] and ledger(value)["held"] == {}
    finally:
        value.http.close()


def test_revision_change_after_reservation_releases_only_confirmed_unsent_work(
    app_env, monkeypatch
) -> None:
    value = rig(app_env)
    original = value.gateway.budgets.reserve

    def reserve_then_revise(*args, **kwargs):
        result = original(*args, **kwargs)
        revise(value)
        return result

    monkeypatch.setattr(value.gateway.budgets, "reserve", reserve_then_revise)
    try:
        result = generate(value)
        assert result["status"] == "rejected" and result["error_code"] == "state_mismatch"
        assert result["accounting"] == "released" and value.requests == []
        assert ledger(value)["held"] == {} and ledger(value)["used"] == {}
    finally:
        value.http.close()


def test_dispatch_rechecks_lease_deadline_after_task_lock(app_env, monkeypatch) -> None:
    value = rig(app_env)
    original = value.gateway._task_current
    checks = 0

    def current_then_expired(conn, context, lease):
        nonlocal checks
        original(conn, context, lease)
        checks += 1
        if checks == 2:
            # Model a deadline passing while the final task lock was acquired,
            # after the earlier lease read, without a slow wall-clock sleep.
            conn.execute(
                "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, lease.job_id),
            )

    monkeypatch.setattr(value.gateway, "_task_current", current_then_expired)
    try:
        result = generate(value)
        assert checks == 2
        assert result["status"] == "rejected" and result["error_code"] == "revision_conflict"
        assert result["accounting"] == "released" and value.requests == []
        assert ledger(value)["held"] == {} and ledger(value)["used"] == {}
        with app_env.db.transaction(app_env.context) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM hosted_invocation_events WHERE kind='dispatched'"
                ).fetchone()["n"]
                == 0
            )
    finally:
        value.http.close()


def test_secret_fragments_are_redacted_before_progress_storage(app_env) -> None:
    text = "visible " + KEY + " and " + OTHER + " tail \U0001f600"
    chunks = tuple(text[i : i + 5] for i in range(0, len(text), 5))
    value = rig(app_env, response=wire(chunks))
    try:
        result = generate(value)
        assert result["status"] == "completed"
        output = "".join(value.output)
        assert output == "visible [REDACTED] and [REDACTED] tail \U0001f600"
        events = value.jobs.events(app_env.context)["items"]
        assert KEY not in json.dumps(events) and OTHER not in json.dumps(events)
        with app_env.db.transaction(app_env.context) as conn:
            retained = conn.execute("SELECT request,policy FROM hosted_requests").fetchall()
        assert KEY not in str(retained) and OTHER not in str(retained)
    finally:
        value.http.close()


def test_interrupted_stream_preserves_safe_pending_tail_and_holds_usage(app_env) -> None:
    value = rig(app_env, response=wire(("short safe tail",), complete=False))
    try:
        result = generate(value)
        assert (
            result["status"] == "indeterminate" and result["accounting"] == "pending_reconciliation"
        )
        assert "".join(value.output) == "short safe tail"
        assert ledger(value)["held"]["calls"] == 1
    finally:
        value.http.close()


def test_service_secret_in_prompt_is_denied_before_hosted_journal_or_http(app_env) -> None:
    value = rig(app_env, prompt="Do not disclose " + KEY)
    try:
        with pytest.raises(DomainError) as caught:
            generate(value)
        assert caught.value.code.value == "scope_denied"
        assert value.requests == [] and ledger(value)["held"] == {}
        with app_env.db.transaction(app_env.context) as conn:
            assert conn.execute("SELECT count(*) AS n FROM hosted_requests").fetchone()["n"] == 0
    finally:
        value.http.close()


def test_hosted_inspection_is_scoped_and_hides_prompt(app_env) -> None:
    value = rig(app_env)
    try:
        generate(value)
        state = value.gateway.inspect(app_env.context, invocation_id(value))
        assert "private task prompt" not in json.dumps(state)
        other = HostedInferenceGateway(
            app_env.other_db, value.policy, lambda reference: KEY, http_client=value.http
        )
        with pytest.raises(ScopeDeniedError):
            other.inspect(app_env.other_context, invocation_id(value))
    finally:
        value.http.close()


class WorkerDied(BaseException):
    """Simulate process loss, bypassing ordinary exception-to-result handling."""


def test_crash_after_budget_commit_recovers_reservation_without_http_retry(
    app_env, monkeypatch
) -> None:
    value = rig(app_env)
    original = value.gateway.budgets.reserve

    def committed_then_died(*args, **kwargs):
        original(*args, **kwargs)
        raise WorkerDied()

    monkeypatch.setattr(value.gateway.budgets, "reserve", committed_then_died)
    try:
        with pytest.raises(WorkerDied):
            generate(value)
        monkeypatch.setattr(value.gateway.budgets, "reserve", original)
        state = value.gateway.inspect(app_env.context, invocation_id(value))
        assert state["reservation_id"] is not None
        assert value.jobs.inspect(app_env.context, value.lease.job_id)["result"] is None
        discovered = value.gateway.list_for_job(app_env.context, value.lease.job_id)
        assert discovered["job_id"] == str(value.lease.job_id)
        assert [item["id"] for item in discovered["items"]] == [str(invocation_id(value))]
        assert discovered["items"][0]["reservation_id"] == state["reservation_id"]
        assert "private task prompt" not in json.dumps(discovered)
        foreign = HostedInferenceGateway(
            app_env.other_db, value.policy, lambda _reference: KEY, http_client=value.http
        )
        with pytest.raises(ScopeDeniedError):
            foreign.list_for_job(app_env.other_context, value.lease.job_id)
        assert generate(value)["status"] == "indeterminate" and value.requests == []
        assert ledger(value)["held"]["calls"] == 1
        value.jobs.finish(app_env.context, value.lease, "indeterminate", error_code="indeterminate")
        value.gateway.reconcile(
            app_env.context,
            invocation_id(value),
            actual={
                "calls": 0,
                "tokens": 0,
                "inference_ms": 0,
                "money_microunits": 0,
            },
        )
        assert ledger(value)["held"] == {} and ledger(value)["used"]["calls"] == 0
    finally:
        value.http.close()


def test_crash_after_settlement_replays_observation_without_double_charging(
    app_env, monkeypatch
) -> None:
    value = rig(app_env)
    original = value.gateway.budgets.settle

    def settled_then_died(*args, **kwargs):
        original(*args, **kwargs)
        raise WorkerDied()

    monkeypatch.setattr(value.gateway.budgets, "settle", settled_then_died)
    try:
        with pytest.raises(WorkerDied):
            generate(value)
        assert len(value.requests) == 1 and ledger(value)["used"]["calls"] == 1
        monkeypatch.setattr(value.gateway.budgets, "settle", original)
        replay = generate(value)
        assert replay["status"] == "completed" and replay["accounting"] == "conservative_estimate"
        assert len(value.requests) == 1 and ledger(value)["used"]["calls"] == 1
        assert "".join(value.output) == "model output"
        with app_env.db.transaction(app_env.context) as conn:
            assert conn.execute("SELECT count(*) AS n FROM budget_usage").fetchone()["n"] == 1
    finally:
        value.http.close()


def test_start_generation_is_atomic_idempotent_and_requires_current_submitted_task(app_env):
    service = TaskService(app_env.db)
    submitted = service.submit(
        app_env.context, app_env.repository_id, hosted_contract(), "lifecycle-task"
    )
    task_id = UUID(submitted["task_id"])
    reason = "Allow budgeted model generation"
    with pytest.raises(RevisionConflictError):
        service.start_generation(
            app_env.context, task_id, submitted["update_token"] + 1, reason, idempotency_key="stale"
        )
    assert service.inspect(app_env.context, task_id) == submitted
    started = service.start_generation(
        app_env.context, task_id, submitted["update_token"], reason, idempotency_key="start-task"
    )
    assert started["lifecycle"] == "running"
    assert started["update_token"] == submitted["update_token"] + 2
    assert (
        service.start_generation(
            app_env.context,
            task_id,
            submitted["update_token"],
            reason,
            idempotency_key="start-task",
        )
        == started
    )
    with pytest.raises(RevisionConflictError):
        service.start_generation(
            app_env.context,
            task_id,
            submitted["update_token"],
            "Changed reason",
            idempotency_key="start-task",
        )
    with pytest.raises(StateMismatchError):
        service.start_generation(
            app_env.context,
            task_id,
            started["update_token"],
            reason,
            idempotency_key="running-task",
        )
    with app_env.db.transaction(app_env.context) as conn:
        events = conn.execute(
            "SELECT event_type,details FROM evidence_events WHERE episode_id=%s ORDER BY recorded_at,id",
            (task_id,),
        ).fetchall()
    assert [event["event_type"] for event in events] == [
        "task_submitted",
        "hosted_generation_started",
        "task_state_changed",
        "task_state_changed",
    ]
    assert events[1]["details"]["execution_authorized"] is False
    assert [event["details"]["to"] for event in events[2:]] == ["initializing", "running"]
    terminal = service.transition(
        app_env.context,
        task_id,
        started["update_token"],
        TaskStatus.FAILED,
        "Test terminal task",
        idempotency_key="terminal-task",
    )
    with pytest.raises(StateMismatchError):
        service.start_generation(
            app_env.context,
            task_id,
            terminal["update_token"],
            reason,
            idempotency_key="terminal-start",
        )
    assert (
        service.start_generation(
            app_env.context,
            task_id,
            submitted["update_token"],
            reason,
            idempotency_key="start-task",
        )
        == started
    )
    assert service.inspect(app_env.context, task_id)["lifecycle"] == "failed"


def test_start_generation_requires_explicit_positive_hosted_budgets(app_env):
    service = TaskService(app_env.db)
    for dimension in ("inference_ms", "money_microunits"):
        for value in (None, 0):
            contract = hosted_contract()
            budget = dict(contract.budget)
            if value is None:
                budget.pop(dimension)
            else:
                budget[dimension] = value
            identity = f"{dimension}-{value}"
            submitted = service.submit(
                app_env.context,
                app_env.repository_id,
                replace(contract, budget=tuple(budget.items())),
                identity,
            )
            with pytest.raises(InvalidInputError):
                service.start_generation(
                    app_env.context,
                    UUID(submitted["task_id"]),
                    submitted["update_token"],
                    "Invalid hosted budget",
                    idempotency_key=identity,
                )
            assert service.inspect(app_env.context, UUID(submitted["task_id"])) == submitted
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM evidence_events WHERE event_type='hosted_generation_started'"
            ).fetchone()["n"]
            == 0
        )


def test_runner_completes_same_model_in_two_roles_and_retains_usage_and_output(app_env):
    value = rig(app_env, claim=False, roles=("reasoning", "coding"))
    runner = HostedJobRunner(value.jobs, value.gateway)
    try:
        result = runner.run_once(app_env.context, "coordinator")
        assert result["state"] == "succeeded" and result["result"]["completed_models"] == 2
        assert len(result["result"]["invocation_ids"]) == 2 and len(value.requests) == 2
        assert result["result"]["usage"]["tokens"] == 30
        assert result["result"]["usage"]["money_microunits"] == 36
        assert result["result"]["accounting"] == "conservative_estimate"
        assert ledger(value)["used"]["calls"] == 2 and ledger(value)["held"] == {}
        events = value.jobs.events(app_env.context, UUID(value.job["id"]))["items"]
        outputs = [event["data"] for event in events if event["event_type"] == "job.output"]
        assert [(output["role"], output["text"]) for output in outputs] == [
            ("reasoning", "model output"),
            ("coding", "model output"),
        ]
        assert events[-1]["data"]["job"]["state"] == "succeeded"
        assert value.tasks.inspect(app_env.context, value.task_id)["lifecycle"] == "running"
        assert runner.run_once(app_env.context, "coordinator") is None
        assert len(value.requests) == 2
    finally:
        value.http.close()


def test_runner_preserves_completed_model_when_next_provider_credential_is_missing(app_env):
    value = rig(
        app_env,
        claim=False,
        selected_models=[
            {"provider": "openai", "model": "fixture-model", "role": "reasoning"},
            {"provider": "anthropic", "model": "fixture-claude", "role": "coding"},
        ],
    )
    value.gateway.policy = replace(
        value.policy,
        rules=(*value.policy.rules, RateRule("anthropic", "fixture-claude", 1000000, 2000000)),
    )
    # A deterministic empty store ensures the user's native vault and ambient
    # provider environment cannot supply the deliberately absent second key.
    credentials = ServiceCredentials(
        environment={"OPENAI_API_KEY": KEY}, store=SimpleNamespace(get=lambda _name: None)
    )
    value.gateway.secret_resolver = credentials.resolve
    try:
        result = HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
        assert result["state"] == "failed" and result["error_code"] == "capability_unavailable"
        assert result["result"]["completed_models"] == 1
        identities = result["result"]["invocation_ids"]
        assert len(identities) == 1
        assert result["result"]["usage"]["tokens"] == 15
        assert result["result"]["usage"]["money_microunits"] == 18
        assert result["result"]["accounting"] == "conservative_estimate"
        assert ledger(value)["used"]["calls"] == 1 and ledger(value)["held"] == {}
        assert len(value.requests) == 1
        invocation = value.gateway.inspect(app_env.context, UUID(identities[0]))
        assert invocation["status"] == "completed" and invocation["role"] == "reasoning"
        events = value.jobs.events(app_env.context, UUID(value.job["id"]))["items"]
        outputs = [event["data"] for event in events if event["event_type"] == "job.output"]
        assert [(output["role"], output["text"]) for output in outputs] == [
            ("reasoning", "model output")
        ]
        with app_env.db.transaction(app_env.context) as conn:
            assert conn.execute("SELECT count(*) AS n FROM hosted_requests").fetchone()["n"] == 1
        assert (
            HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
            is None
        )
    finally:
        value.http.close()


def test_runner_preserves_prior_receipts_when_next_observation_cannot_be_persisted(
    app_env, monkeypatch
):
    value = rig(app_env, claim=False, roles=("reasoning", "coding"))
    original = value.gateway._state

    def fail_second_observation(context, identity, state, **kwargs):
        result = kwargs.get("result")
        if state == "observed" and result["role"] == "coding":
            raise RuntimeError("private-persistence-exception-canary")
        return original(context, identity, state, **kwargs)

    monkeypatch.setattr(value.gateway, "_state", fail_second_observation)
    try:
        result = HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
        assert result["state"] == "indeterminate" and result["error_code"] == "internal_error"
        assert result["result"]["completed_models"] == 1
        identities = result["result"]["invocation_ids"]
        assert len(identities) == 2 and len(value.requests) == 2
        assert result["result"]["usage"]["tokens"] == 15
        assert result["result"]["usage"]["money_microunits"] == 18
        assert result["result"]["accounting"] == "pending_reconciliation"
        assert ledger(value)["used"]["calls"] == 1 and ledger(value)["held"]["calls"] == 1
        invocations = value.gateway.list_for_job(app_env.context, UUID(value.job["id"]))["items"]
        assert [invocation["id"] for invocation in invocations] == identities
        assert [invocation["status"] for invocation in invocations] == ["completed", "dispatched"]
        events = value.jobs.events(app_env.context, UUID(value.job["id"]))["items"]
        assert "private-persistence-exception-canary" not in json.dumps(
            [result, events, invocations]
        )
        assert (
            HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
            is None
        )
        assert len(value.requests) == 2
    finally:
        value.http.close()


@pytest.mark.parametrize("publication", ["output", "progress"])
def test_runner_retains_completed_receipts_when_publication_fails(
    app_env, monkeypatch, publication
):
    value = rig(app_env, claim=False, roles=("reasoning", "coding"))
    original_progress = value.jobs.progress
    failures = []
    # Keep the small text delta buffered until the post-generation flush. This
    # isolates failure after known settlement from interrupted provider usage.
    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=lambda: 0.0))

    def failed_output(*args, **kwargs):
        failures.append("output")
        raise RuntimeError("private-publication-error-canary")

    def failed_progress(context, lease, stage, *args, **kwargs):
        if stage == "model_completed":
            failures.append("progress")
            raise RuntimeError("private-publication-error-canary")
        return original_progress(context, lease, stage, *args, **kwargs)

    if publication == "output":
        monkeypatch.setattr(value.jobs, "output", failed_output)
    else:
        monkeypatch.setattr(value.jobs, "progress", failed_progress)
    try:
        result = HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
        assert failures and set(failures) == {publication}
        assert result["state"] == "indeterminate" and result["error_code"] == "internal_error"
        assert result["result"]["completed_models"] == 1
        identities = result["result"]["invocation_ids"]
        assert len(identities) == 1
        assert result["result"]["usage"]["tokens"] == 15
        assert result["result"]["usage"]["money_microunits"] == 18
        assert result["result"]["accounting"] == "conservative_estimate"
        assert ledger(value)["used"]["calls"] == 1 and ledger(value)["held"] == {}
        assert value.gateway.inspect(app_env.context, UUID(identities[0]))["status"] == "completed"
        events = value.jobs.events(app_env.context, UUID(value.job["id"]))["items"]
        assert "private-publication-error-canary" not in json.dumps([result, events])
        assert len(value.requests) == 1
        assert (
            HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
            is None
        )
        assert len(value.requests) == 1
    finally:
        value.http.close()


def test_runner_confirms_cancellation_before_http_without_spending(app_env, monkeypatch):
    value = rig(app_env, claim=False)
    original = value.jobs.claim

    def claim_then_cancel(*args, **kwargs):
        lease = original(*args, **kwargs)
        assert lease is not None
        value.jobs.cancel(app_env.context, lease.job_id, "cancel-at-claim")
        return lease

    monkeypatch.setattr(value.jobs, "claim", claim_then_cancel)
    try:
        result = HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
        assert result["state"] == "cancelled" and result["dispatched_at"] is None
        assert result["result"]["invocation_ids"] == []
        assert result["result"]["completed_models"] == 0
        assert value.requests == [] and ledger(value)["held"] == {} and ledger(value)["used"] == {}
    finally:
        value.http.close()


@pytest.mark.parametrize("action", ["job_cancel", "task_pause", "task_revise"])
def test_runner_stops_after_remote_dispatch_and_retains_uncertain_usage(
    app_env, monkeypatch, action
):
    value = rig(app_env, claim=False)
    original = value.gateway.generate
    cancellation_probe = None

    def observe_cancellation(*args, **kwargs):
        nonlocal cancellation_probe
        cancellation_probe = kwargs["cancelled"]
        return original(*args, **kwargs)

    def request_stop(request):
        if action == "job_cancel":
            value.jobs.cancel(app_env.context, UUID(value.job["id"]), "cancel-remote")
        elif action == "task_pause":
            value.tasks.control(
                app_env.context,
                value.task_id,
                value.task["update_token"],
                "pause",
                "Pause during generation",
                idempotency_key="pause-remote",
            )
        else:
            revise(value)
        deadline = time.monotonic() + 5
        wait = threading.Event()
        while not cancellation_probe() and time.monotonic() < deadline:
            wait.wait(0.01)

    monkeypatch.setattr(value.gateway, "generate", observe_cancellation)
    value.callbacks.before_response = request_stop
    try:
        result = HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
        assert cancellation_probe(), "Worker heartbeat did not revoke streaming permission"
        assert result["state"] == "indeterminate" and result["dispatched_at"] is not None
        assert result["cancellation_requested"] is (action == "job_cancel")
        assert result["result"]["accounting"] == "pending_reconciliation"
        assert result["result"]["completed_models"] == 0
        assert ledger(value)["held"]["calls"] == 1 and ledger(value)["used"] == {}
        assert len(value.requests) == 1
        assert (
            HostedJobRunner(value.jobs, value.gateway).run_once(app_env.context, "coordinator")
            is None
        )
        assert len(value.requests) == 1
    finally:
        value.http.close()
