"""Real PostgreSQL queue concurrency, scope, cursor ordering and crash semantics."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID, uuid4

import pytest

import potbot.controller.jobs as job_module
from potbot.controller.jobs import JobService, _event
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement
from potbot.persistence.database import Database

pytestmark = pytest.mark.postgres


def request(env):
    contract = ContractSpec(
        objective="Compare model approaches",
        baseline_ref="git:" + "1" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(Requirement("R1", "Explain approach", "Preserve supporting evidence"),),
        change_scope=("src/app.py",),
        capabilities=("test",),
        verification_policy="tests-v1",
        budget=(("tokens", 10000), ("calls", 10), ("tool_ms", 1000), ("concurrency", 2)),
    )
    task = TaskService(env.db).submit(env.context, env.repository_id, contract, str(uuid4()))
    return {
        "job_type": "model.generate",
        "episode_id": task["task_id"],
        "input": {"prompt": "private-input-canary"},
        "selected_models": [{"provider": "openai", "model": "fixture-model", "role": "reasoning"}],
    }


def create(env, *, key="job"):
    service = JobService(env.db)
    return service, service.submit(env.context, request(env), key)


def test_idempotent_concurrent_submission_keeps_one_job_and_event(app_env) -> None:
    service = JobService(app_env.db)
    payload = request(app_env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.submit, app_env.context, payload, "same") for _ in range(2)]
        results = [f.result(timeout=10) for f in futures]
    assert results[0] == results[1]
    assert service.queue(app_env.context)["total"] == 1
    events = service.events(app_env.context)["items"]
    assert len(events) == 1 and events[0]["event_type"] == "job.queued"
    assert "private-input-canary" not in str(results) + str(events)
    changed = {**payload, "input": {"prompt": "different"}}
    with pytest.raises(RevisionConflictError):
        service.submit(app_env.context, changed, "same")


def test_event_failure_rolls_back_job_cursor_and_idempotency_record(app_env, monkeypatch) -> None:
    service = JobService(app_env.db)
    payload = request(app_env)

    def failed(*args, **kwargs):
        _event(*args, **kwargs)
        raise RuntimeError("failure before commit")

    monkeypatch.setattr(job_module, "_event", failed)
    with pytest.raises(RuntimeError):
        service.submit(app_env.context, payload, "rollback")
    assert service.queue(app_env.context)["total"] == 0
    assert service.events(app_env.context)["items"] == []
    monkeypatch.setattr(job_module, "_event", _event)
    service.submit(app_env.context, payload, "rollback")
    assert service.events(app_env.context)["items"][0]["sequence"] == 1


def test_concurrent_claims_are_distinct_and_expired_work_is_never_replayed(app_env) -> None:
    service, first = create(app_env)
    service.submit(app_env.context, request(app_env), "second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.claim, app_env.context, f"worker-{i}") for i in range(2)]
        leases = [f.result(timeout=10) for f in futures]
    assert None not in leases
    assert len({lease.job_id for lease in leases}) == 2
    assert service.claim(app_env.context, "third") is None
    lease = next(item for item in leases if str(item.job_id) == first["id"])
    service.mark_dispatched(app_env.context, lease)
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (lease.job_id,),
        )
    assert service.queue(app_env.context)["expired_leases"] == 1
    assert service.claim(app_env.context, "replacement") is None
    observed = service.inspect(app_env.context, lease.job_id)
    assert observed["state"] == "indeterminate" and observed["error_code"] == "lease_expired"
    assert service.cancelled(app_env.context, lease)
    for operation in (
        lambda: service.progress(app_env.context, lease, "stale"),
        lambda: service.renew(app_env.context, lease),
        lambda: service.finish(app_env.context, lease, "succeeded"),
    ):
        with pytest.raises(RevisionConflictError):
            operation()


def test_cancellation_distinguishes_queued_requested_and_confirmed_stop(app_env) -> None:
    service, job = create(app_env)
    queued = service.cancel(app_env.context, UUID(job["id"]), "cancel-queued")
    assert queued["state"] == "cancelled" and queued["dispatched_at"] is None
    assert service.claim(app_env.context, "worker") is None
    running = service.submit(app_env.context, request(app_env), "second")
    lease = service.claim(app_env.context, "worker")
    requested = service.cancel(app_env.context, UUID(running["id"]), "cancel-running")
    assert requested["state"] == "cancelling" and requested["finished_at"] is None
    assert service.cancelled(app_env.context, lease)
    with pytest.raises(OperationCancelledError):
        service.mark_dispatched(app_env.context, lease)
    confirmed = service.finish(app_env.context, lease, "cancelled", error_code="cancelled")
    assert confirmed["state"] == "cancelled" and confirmed["finished_at"] is not None
    assert service.cancel(app_env.context, UUID(running["id"]), "cancel-running") == requested


def test_fenced_progress_output_completion_and_reconnection(app_env) -> None:
    service, job = create(app_env)
    lease = service.claim(app_env.context, "worker")
    with pytest.raises(StateMismatchError):
        service.output(app_env.context, lease, "before-dispatch")
    with pytest.raises(StateMismatchError):
        service.finish(app_env.context, lease, "succeeded")
    with pytest.raises(RevisionConflictError):
        service.progress(
            app_env.context, replace(lease, generation=lease.generation + 1), "invalid"
        )
    old_expiry = lease.lease_until
    assert service.renew(app_env.context, lease, lease_seconds=60) > old_expiry
    service.mark_dispatched(app_env.context, lease)
    service.progress(app_env.context, lease, "generating", 1, 2)
    checkpoint = service.inspect(app_env.context, UUID(job["id"]))["event_cursor"]
    output = service.output(
        app_env.context, lease, "safe output", provider="openai", model="fixture-model"
    )
    with pytest.raises(InvalidInputError):
        service.output(app_env.context, lease, "bad source", provider="openai", model="different")
    result = {
        "invocation_ids": [str(uuid4())],
        "completed_models": 1,
        "accounting": "conservative_estimate",
        "usage": {"calls": 1},
    }
    done = service.finish(app_env.context, lease, "succeeded", result=result)
    assert done["result"] == result
    reconnected = JobService(Database(app_env.db.dsn))
    page = reconnected.events(app_env.context, UUID(job["id"]), after=checkpoint, limit=1)
    assert page["items"] == [output] and page["has_more"]
    rest = reconnected.events(app_env.context, UUID(job["id"]), after=page["next_cursor"])
    assert rest["items"][0]["event_type"] == "job.succeeded"
    assert service.inspect(app_env.context, UUID(job["id"]))["output_bytes"] == len("safe output")
    with pytest.raises(RevisionConflictError):
        service.finish(app_env.context, lease, "succeeded", result=result)


def test_workspace_scope_and_append_only_events_are_database_enforced(app_env) -> None:
    service, job = create(app_env)
    other = JobService(app_env.other_db)
    job_id = UUID(job["id"])
    for call in (
        lambda: other.inspect(app_env.other_context, job_id),
        lambda: other.events(app_env.other_context, job_id),
        lambda: other.cancel(app_env.other_context, job_id, "foreign"),
        lambda: service.inspect(app_env.other_context, job_id),
    ):
        with pytest.raises(ScopeDeniedError):
            call()
    assert other.list(app_env.other_context)["items"] == []
    assert other.events(app_env.other_context)["items"] == []
    assert other.queue(app_env.other_context)["total"] == 0
    with app_env.other_db.transaction(app_env.other_context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM job_events").fetchone()["n"] == 0
    with pytest.raises((RevisionConflictError, ScopeDeniedError)):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute("UPDATE job_events SET data='{}'::jsonb")


def test_workspace_cursor_cannot_commit_out_of_order_or_skip_replay(app_env, monkeypatch) -> None:
    service, first = create(app_env)
    second = service.submit(app_env.context, request(app_env), "second")
    leases = [service.claim(app_env.context, "one"), service.claim(app_env.context, "two")]
    second_lease = next(lease for lease in leases if str(lease.job_id) == second["id"])
    entered = Event()

    def observe(conn, context, job_id, event_type, data):
        if str(job_id) == second["id"]:
            entered.set()
        return _event(conn, context, job_id, event_type, data)

    monkeypatch.setattr(job_module, "_event", observe)
    before = service.events(app_env.context)["next_cursor"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (UUID(first["id"]),))
            held = _event(
                conn, app_env.context, UUID(first["id"]), "job.progress", {"stage": "held"}
            )
            future = pool.submit(service.progress, app_env.context, second_lease, "later")
            assert entered.wait(5)
            assert not future.done()
            assert service.events(app_env.context, after=before)["items"] == []
        future.result(timeout=10)
    replay = service.events(app_env.context, after=before)["items"]
    assert [event["sequence"] for event in replay] == [before + 1, before + 2]
    assert replay[0] == held and replay[1]["job_id"] == second["id"]
    assert service.events(app_env.context, after=replay[0]["sequence"])["items"] == replay[1:]


def test_pagination_queue_counts_invalid_cursors_and_terminal_task(app_env) -> None:
    service, _ = create(app_env)
    for i in range(3):
        service.submit(app_env.context, request(app_env), f"more-{i}")
    first = service.list(app_env.context, state="queued", limit=2)
    second = service.list(app_env.context, state="queued", limit=2, after=first["next_cursor"])
    assert len({row["id"] for row in first["items"] + second["items"]}) == 4
    assert first["has_more"] and second["next_cursor"] is None
    assert service.queue(app_env.context)["counts"]["queued"] == 4
    assert service.queue(app_env.context)["oldest_queued_at"] is not None
    for value in ("garbage", "!!!", "W10"):
        with pytest.raises(InvalidInputError):
            service.list(app_env.context, after=value)
    with pytest.raises(InvalidInputError):
        service.events(app_env.context, after=True)
    payload = request(app_env)
    TaskService(app_env.db).control(
        app_env.context,
        UUID(payload["episode_id"]),
        0,
        "cancel",
        "cancel fixture",
        idempotency_key="cancel-task",
    )
    with pytest.raises(StateMismatchError):
        service.submit(app_env.context, payload, "terminal")


def test_submission_freezes_task_revision_even_after_task_is_revised(app_env) -> None:
    service = JobService(app_env.db)
    payload = request(app_env)
    job = service.submit(app_env.context, payload, "revision-bound")
    tasks = TaskService(app_env.db)
    task_id = UUID(payload["episode_id"])
    task = tasks.inspect(app_env.context, task_id)
    contract = ContractSpec.from_dict(task["current_contract"]["content"])
    revised = tasks.revise(
        app_env.context,
        task_id,
        UUID(task["current_contract"]["id"]),
        replace(contract, objective="Different task requirements"),
        "revise-task",
    )
    assert revised["current_contract"]["id"] != job["contract_revision_id"]
    assert service.submit(app_env.context, payload, "revision-bound") == job
    lease = service.claim(app_env.context, "worker")
    assert str(lease.contract_revision_id) == job["contract_revision_id"]
    assert (
        service.inspect(app_env.context, UUID(job["id"]))["contract_revision_id"]
        == job["contract_revision_id"]
    )


def test_mutations_recheck_lease_expiry_in_the_write(app_env, monkeypatch) -> None:
    service, job = create(app_env)
    lease = service.claim(app_env.context, "worker")
    service.mark_dispatched(app_env.context, lease)
    original = JobService._lease_row

    def expire_after_read(conn, context, current):
        row = original(conn, context, current)
        conn.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (current.job_id,),
        )
        return row

    monkeypatch.setattr(JobService, "_lease_row", staticmethod(expire_after_read))
    for operation in (
        lambda: service.renew(app_env.context, lease),
        lambda: service.progress(app_env.context, lease, "later"),
        lambda: service.output(app_env.context, lease, "stale"),
        lambda: service.finish(app_env.context, lease, "succeeded"),
    ):
        with pytest.raises(RevisionConflictError):
            operation()
    observed = service.inspect(app_env.context, UUID(job["id"]))
    assert observed["state"] == "running" and observed["output_bytes"] == 0


def test_output_byte_limits_are_atomic_and_count_utf8_bytes(app_env) -> None:
    service, job = create(app_env)
    lease = service.claim(app_env.context, "worker")
    service.mark_dispatched(app_env.context, lease)
    cursor = service.events(app_env.context)["next_cursor"]
    with pytest.raises(InvalidInputError):
        service.output(app_env.context, lease, "\U0001f600" * (64 * 1024 // 4 + 1))
    assert service.events(app_env.context, after=cursor)["items"] == []
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute("UPDATE jobs SET output_bytes=4194303 WHERE id=%s", (UUID(job["id"]),))
    with pytest.raises(InvalidInputError):
        service.output(app_env.context, lease, "ab")
    assert service.events(app_env.context, after=cursor)["items"] == []
    assert service.inspect(app_env.context, UUID(job["id"]))["output_bytes"] == 4194303


def test_output_role_distinguishes_same_model_in_reasoning_and_coding(app_env) -> None:
    service = JobService(app_env.db)
    payload = request(app_env)
    payload["selected_models"].append({**payload["selected_models"][0], "role": "coding"})
    service.submit(app_env.context, payload, "two-roles")
    lease = service.claim(app_env.context, "worker")
    service.mark_dispatched(app_env.context, lease)
    first = service.output(
        app_env.context, lease, "plan", provider="openai", model="fixture-model", role="reasoning"
    )
    second = service.output(
        app_env.context, lease, "code", provider="openai", model="fixture-model", role="coding"
    )
    assert first["data"]["role"] == "reasoning" and second["data"]["role"] == "coding"
    with pytest.raises(InvalidInputError):
        service.output(app_env.context, lease, "bad", role="coding")
    with pytest.raises(InvalidInputError):
        service.output(
            app_env.context,
            lease,
            "bad",
            provider="openai",
            model="fixture-model",
            role="reviewing",
        )
    single = dict(payload, selected_models=[payload["selected_models"][0]])
    service.submit(app_env.context, single, "one-role")
    second_lease = service.claim(app_env.context, "worker-two")
    service.mark_dispatched(app_env.context, second_lease)
    with pytest.raises(InvalidInputError):
        service.output(
            app_env.context,
            second_lease,
            "bad",
            provider="openai",
            model="fixture-model",
            role="coding",
        )
