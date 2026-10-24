"""Live PostgreSQL RLS, fenced checkpoints and cross-process steering contracts."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.controller.reasoning_runtime import PostgresReasoningStore, PostgresSteeringInbox
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

pytestmark = pytest.mark.postgres


def _snapshot(status: str = "running") -> dict[str, Any]:
    return {"run_status": status, "revision": 0, "nodes": [], "steering": [],
            "spent_tokens": 0, "events": [
                {"sequence": 1, "type": "node.created", "subject": "a", "revision": 0}
            ]}


def test_reasoning_run_and_steering_survive_independent_transactions(app_env: Any) -> None:
    store = PostgresReasoningStore(app_env.db)
    inbox = PostgresSteeringInbox(app_env.db)
    run_id = uuid4().hex
    token = store.start(app_env.context, run_id)
    store.checkpoint(app_env.context, run_id, token, _snapshot())
    assert store.inspect(app_env.context, run_id)["checkpoint"] == 1
    assert len(store.events(app_env.context, run_id)) == 1

    first = inbox.submit(app_env.context, run_id, request_id="change-1", prompt="Try ARM")
    assert first["status"] == "queued"
    assert inbox.submit(app_env.context, run_id, request_id="change-1", prompt="Try ARM") == first
    with pytest.raises(InvalidInputError):
        inbox.submit(app_env.context, run_id, request_id="change-1", prompt="Use GPUs")
    pending = inbox.pending(app_env.context, run_id)
    assert len(pending) == 1 and pending[0]["prompt"] == "Try ARM"
    decision = inbox.resolve(app_env.context, run_id, first,
                             {"decision": "spawn_hypothesis", "explanation": "ARM now matters"})
    assert decision["status"] == "decided"
    assert inbox.get(app_env.context, run_id, "change-1")["decision"] == "spawn_hypothesis"
    assert not inbox.pending(app_env.context, run_id)

    inbox.seal(app_env.context, run_id)
    with pytest.raises(InvalidInputError):
        inbox.submit(app_env.context, run_id, request_id="change-2", prompt="New")
    store.checkpoint(app_env.context, run_id, token, _snapshot("completed"))
    assert store.inspect(app_env.context, run_id)["state"] == "completed"
    with pytest.raises(StateMismatchError):
        store.checkpoint(app_env.context, run_id, token, _snapshot())
    with pytest.raises(ScopeDeniedError):
        store.inspect(app_env.context, uuid4().hex)
    with pytest.raises(ScopeDeniedError):
        store.inspect(app_env.other_context, run_id)


def test_expired_writer_cannot_publish_late_results(app_env: Any) -> None:
    store = PostgresReasoningStore(app_env.db)
    run_id = uuid4().hex
    token = store.start(app_env.context, run_id)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE reasoning_runtime_runs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE workspace_id=%s AND id=%s",
            (app_env.context.workspace_id, UUID(hex=run_id)),
        )
    with pytest.raises(StateMismatchError):
        store.checkpoint(app_env.context, run_id, token, _snapshot())
    assert store.expire(app_env.context) == 1
    assert store.inspect(app_env.context, run_id)["state"] == "indeterminate"
    assert store.expire(app_env.context) == 0
