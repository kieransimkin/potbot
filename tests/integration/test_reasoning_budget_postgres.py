"""PostgreSQL task-budget admission, terminal usage and uncertain inference."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.reasoning_budget import ReasoningBudgetJournal
from potbot.controller.reasoning_runtime import PostgresReasoningStore
from potbot.controller.tasks import TaskService
from potbot.domain.errors import StateMismatchError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.models.hosted_policy import HostedPolicy, RateRule

pytestmark = pytest.mark.postgres


def _task(env: Any) -> UUID:
    service = TaskService(env.db)
    contract = ContractSpec(
        objective="Check competing solutions",
        baseline_ref="git:" + "a" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(Requirement("R1", "Compare evidence", "Record final result"),),
        change_scope=(),
        capabilities=("test",),
        verification_policy="checks-v1",
        budget=(("tokens", 100000), ("calls", 10), ("tool_ms", 10000),
                ("concurrency", 3), ("inference_ms", 300000),
                ("money_microunits", 100000)),
    )
    task = service.submit(env.context, env.repository_id, contract,
                          "reasoning-budget-" + uuid4().hex)
    identity = UUID(task["task_id"])
    service.transition(env.context, identity, 0, TaskStatus.INITIALIZING,
                       "Init", idempotency_key="init-" + uuid4().hex)
    service.transition(env.context, identity, 1, TaskStatus.RUNNING,
                       "Running", idempotency_key="run-" + uuid4().hex)
    return identity


def _journal(env: Any) -> ReasoningBudgetJournal:
    policy = HostedPolicy(
        allow_prompt_disclosure=True,
        rules=(RateRule("openai", "reasoner-test", 100000, 200000,
                        protocol="responses"),),
    )
    return ReasoningBudgetJournal(env.db, policy)


def _prepare(env: Any, task_id: UUID, run_id: str, token: UUID) -> tuple[Any, Any]:
    journal = _journal(env)
    ticket = journal.prepare(
        env.context, run_id=run_id, node_id=str(uuid4()), lease_token=token,
        episode_id=task_id, provider="openai", model="reasoner-test",
        protocol="responses", prompt_hash="e" * 64,
        system="Follow instructions", prompt="Give a JSON answer",
        max_output_tokens=128, timeout_seconds=20,
    )
    return journal, ticket


def test_reasoning_budget_success_records_usage_once(app_env: Any) -> None:
    task_id = _task(app_env)
    run_id = uuid4().hex
    token = PostgresReasoningStore(app_env.db).start(app_env.context, run_id)
    journal, ticket = _prepare(app_env, task_id, run_id, token)
    assert BudgetLedger(app_env.db).inspect(app_env.context, task_id)["held"]["calls"] == 1
    journal.dispatch(app_env.context, ticket, token)
    journal.observe(app_env.context, ticket, {
        "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
    }, elapsed_ms=220, rate_provider="openai", rate_model="reasoner-test")
    journal.settle_observed(app_env.context, ticket.identity)
    amounts = BudgetLedger(app_env.db).inspect(app_env.context, task_id)
    assert amounts["used"]["calls"] == 1
    assert amounts["used"]["tokens"] == 14
    assert amounts["used"]["inference_ms"] == 220
    assert amounts["held"] == {}
    receipts = journal.list_for_run(app_env.context, run_id)
    assert len(receipts) == 1 and receipts[0]["status"] == "settled"
    assert "prompt" not in receipts[0] and "credential" not in receipts[0]


def test_reasoning_budget_interrupted_call_remains_held(app_env: Any) -> None:
    task_id = _task(app_env)
    run_id = uuid4().hex
    store = PostgresReasoningStore(app_env.db)
    token = store.start(app_env.context, run_id)
    journal, ticket = _prepare(app_env, task_id, run_id, token)
    journal.dispatch(app_env.context, ticket, token)
    journal.uncertain(app_env.context, ticket)
    assert journal.list_for_run(app_env.context, run_id)[0]["status"] == "indeterminate"
    assert BudgetLedger(app_env.db).inspect(app_env.context, task_id)["held"]["calls"] == 1
    with pytest.raises(StateMismatchError):
        journal.settle_observed(app_env.context, ticket.identity)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE reasoning_runtime_runs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(hex=run_id)),
        )
    assert store.expire(app_env.context) == 1
    assert BudgetLedger(app_env.db).inspect(app_env.context, task_id)["held"]["calls"] == 1


def test_failed_dispatch_releases_only_confirmed_unsent_budget(app_env: Any) -> None:
    task_id = _task(app_env)
    run_id = uuid4().hex
    store = PostgresReasoningStore(app_env.db)
    token = store.start(app_env.context, run_id)
    journal, ticket = _prepare(app_env, task_id, run_id, token)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE reasoning_runtime_runs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(hex=run_id)),
        )
    with pytest.raises(StateMismatchError):
        journal.dispatch(app_env.context, ticket, token)
    journal.uncertain(app_env.context, ticket)
    assert journal.list_for_run(app_env.context, run_id)[0]["status"] == "released"
    assert BudgetLedger(app_env.db).inspect(app_env.context, task_id)["held"] == {}
