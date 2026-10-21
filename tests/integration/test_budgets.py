"""Concurrent resource reservations and conservative post-crash accounting."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from uuid import UUID

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.errors import BudgetUnavailableError, InvalidInputError, StateMismatchError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus

pytestmark = pytest.mark.postgres


def running_task(env: Any, *, calls: int = 1, concurrency: int = 2) -> UUID:
    contract = ContractSpec(
        objective="Investigate",
        baseline_ref="git:" + "a" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(Requirement("R1", "Identify the cause", "Cite a reproduction"),),
        change_scope=(),
        capabilities=("test",),
        verification_policy="repro-v1",
        budget=(
            ("tokens", 100),
            ("calls", calls),
            ("tool_ms", 1000),
            ("concurrency", concurrency),
            ("search_calls", 3),
        ),
    )
    service = TaskService(env.db)
    task = service.submit(env.context, env.repository_id, contract, "task")
    identifier = UUID(task["task_id"])
    service.transition(
        env.context, identifier, 0, TaskStatus.INITIALIZING, "Init", idempotency_key="init"
    )
    service.transition(
        env.context, identifier, 1, TaskStatus.RUNNING, "Ready", idempotency_key="run"
    )
    return identifier


def test_two_real_workers_cannot_reserve_the_final_allowance_twice(app_env: Any) -> None:
    task_id = running_task(app_env)
    barrier = Barrier(2)

    def attempt(key: str) -> str:
        barrier.wait(timeout=10)
        try:
            BudgetLedger(app_env.db).reserve(app_env.context, task_id, {"calls": 1}, key)
            return "reserved"
        except BudgetUnavailableError:
            return "unavailable"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("worker-a", "worker-b")))
    assert sorted(results) == ["reserved", "unavailable"]
    ledger = BudgetLedger(app_env.db).inspect(app_env.context, task_id)
    assert ledger["held"]["calls"] == 1
    assert ledger["available"]["calls"] == 0


def test_complete_settlement_releases_only_confirmed_unused_allowance(app_env: Any) -> None:
    task_id = running_task(app_env, calls=5)
    ledger = BudgetLedger(app_env.db)
    reservation = ledger.reserve(
        app_env.context, task_id, {"tokens": 80, "calls": 3, "tool_ms": 1000}, "first"
    )
    identifier = UUID(reservation["id"])
    with pytest.raises(InvalidInputError):
        ledger.settle(app_env.context, identifier, {"tokens": 20}, "synthetic")
    assert ledger.inspect(app_env.context, task_id)["held"]["tokens"] == 80
    actual = {"tokens": 20, "calls": 1, "tool_ms": 400}
    settled = ledger.settle(app_env.context, identifier, actual, "synthetic")
    assert ledger.settle(app_env.context, identifier, actual, "synthetic") == settled
    snapshot = ledger.inspect(app_env.context, task_id)
    assert snapshot["used"] == actual
    assert snapshot["held"] == {}
    assert snapshot["available"]["tokens"] == 80
    assert snapshot["available"]["concurrency"] == 2
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM budget_usage").fetchone()["n"] == 1


def test_crash_uncertainty_remains_charged_until_measured_reconciliation(app_env: Any) -> None:
    task_id = running_task(app_env, calls=3, concurrency=1)
    reservation = BudgetLedger(app_env.db).reserve(app_env.context, task_id, {"calls": 1}, "worker")
    identifier = UUID(reservation["id"])
    BudgetLedger(app_env.db).mark_indeterminate(app_env.context, identifier, "Worker disappeared")
    restarted = BudgetLedger(app_env.db)
    assert restarted.inspect(app_env.context, task_id)["held"] == {"calls": 1, "concurrency": 1}
    with pytest.raises(BudgetUnavailableError):
        restarted.reserve(app_env.context, task_id, {"calls": 1}, "new-worker")
    with pytest.raises(StateMismatchError):
        restarted.release(
            app_env.context, identifier, "Pretend nothing ran", confirmed_not_started=True
        )
    restarted.settle(app_env.context, identifier, {"calls": 1}, "measured")
    restarted.reserve(app_env.context, task_id, {"calls": 1}, "new-worker")


def test_release_requires_no_execution_confirmation_and_custom_limits_are_atomic(
    app_env: Any,
) -> None:
    task_id = running_task(app_env)
    ledger = BudgetLedger(app_env.db)
    reservation = ledger.reserve(app_env.context, task_id, {"search_calls": 3}, "search")
    identifier = UUID(reservation["id"])
    with pytest.raises(InvalidInputError):
        ledger.release(app_env.context, identifier, "Unknown", confirmed_not_started=False)
    with pytest.raises(BudgetUnavailableError):
        ledger.reserve(app_env.context, task_id, {"search_calls": 1}, "too-many")
    ledger.release(
        app_env.context, identifier, "Cancelled before dispatch", confirmed_not_started=True
    )
    assert ledger.inspect(app_env.context, task_id)["available"]["search_calls"] == 3
    with pytest.raises(InvalidInputError):
        ledger.reserve(app_env.context, task_id, {"unconfigured": 1}, "unknown-dimension")


def test_measured_overrun_is_durable_and_exhausts_future_admission(app_env: Any) -> None:
    task_id = running_task(app_env)
    ledger = BudgetLedger(app_env.db)
    reservation = ledger.reserve(app_env.context, task_id, {"tokens": 90}, "generation")
    ledger.settle(app_env.context, UUID(reservation["id"]), {"tokens": 110}, "provider_reported")
    assert ledger.inspect(app_env.context, task_id)["used"]["tokens"] == 110
    assert (
        TaskService(app_env.db).inspect(app_env.context, task_id)["lifecycle"] == "budget_exhausted"
    )
    with pytest.raises(StateMismatchError):
        ledger.reserve(app_env.context, task_id, {"calls": 1}, "later")
