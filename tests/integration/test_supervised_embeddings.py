"""Real SQL accounting and publication fencing with real blocked subprocesses."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.errors import StateMismatchError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.memory.embedding_profile import EmbeddingBatch
from potbot.memory.embeddings import EmbeddingService
from potbot.memory.supervised_encoder import SupervisedEncodingFailure, SupervisedQwenEncoder
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.tools.supervisor import ProcessResult, ProcessSupervisor

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]


def running_task(app_env) -> UUID:
    task_service = TaskService(app_env.db)
    specification = ContractSpec(
        "Verify supervised embedding publication",
        "fixture-base",
        DeliverableType.INVESTIGATION,
        (Requirement("R1", "Bound embedding work", "No stale vector is published"),),
        (),
        ("embed",),
        "v1",
        (("tokens", 10000), ("calls", 10), ("tool_ms", 100000), ("concurrency", 1)),
    )
    task = task_service.submit(app_env.context, app_env.repository_id, specification, "task")
    identity = UUID(task["task_id"])
    for status in (TaskStatus.INITIALIZING, TaskStatus.RUNNING):
        task = task_service.transition(
            app_env.context,
            identity,
            task["update_token"],
            status,
            "fixture",
            idempotency_key=status.value,
        )
    return identity


class BlockedStageSupervisor(ProcessSupervisor):
    """Replace only the trusted worker computation with a deterministic slow fixture."""

    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage
        self.last_result = None

    def run(self, request, **kwargs):
        code = f"import time,sys;print({self.stage!r},file=sys.stderr,flush=True);time.sleep(120)"
        request = replace(request, argv=(sys.executable, "-I", "-c", code))
        self.last_result = super().run(request, **kwargs)
        return self.last_result


def build_service(app_env, provider):
    service = EmbeddingService(
        app_env.db, ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root)), provider
    )
    space = service.register_space(app_env.context)
    source = service.register_source(
        app_env.context, app_env.repository_id, "worker-fixture", "The input bytes remain exact."
    )
    return service, space, source


@pytest.mark.parametrize("stage", ["tokenizer preparation", "model loading and inference"])
def test_hard_deadline_holds_uncertain_usage_and_never_publishes(
    app_env, tmp_path: Path, stage: str
) -> None:
    model = tmp_path / "fixture-model"
    model.mkdir()
    supervisor = BlockedStageSupervisor(stage)
    encoder = SupervisedQwenEncoder(model, supervisor=supervisor)
    service, space, source = build_service(app_env, encoder)
    episode = running_task(app_env)
    with pytest.raises(SupervisedEncodingFailure) as failure:
        service.index_source(
            app_env.context, episode, source["id"], space["id"], runtime_budget_ms=350
        )
    assert failure.value.dispatched
    assert supervisor.last_result.outcome == "timeout"
    assert stage.encode() in supervisor.last_result.stderr
    assert supervisor.last_result.elapsed_ms < 3000
    budget = BudgetLedger(app_env.db).inspect(app_env.context, episode)
    assert budget["held"]["calls"] == 1 and budget["used"].get("calls", 0) == 0
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 0
        event = conn.execute(
            "SELECT details FROM evidence_events WHERE event_type='embedding_worker_outcome'"
        ).fetchone()
        assert event["details"]["outcome"] == "timeout"
        assert event["details"]["dispatched"] is True
        assert event["details"]["profile_hash"] == encoder.profile.fingerprint


def test_confirmed_unstarted_worker_releases_unused_capacity(app_env, tmp_path: Path) -> None:
    service, space, source = build_service(app_env, SupervisedQwenEncoder(tmp_path / "missing"))
    episode = running_task(app_env)
    with pytest.raises(SupervisedEncodingFailure) as failure:
        service.index_source(app_env.context, episode, source["id"], space["id"])
    assert not failure.value.dispatched
    budget = BudgetLedger(app_env.db).inspect(app_env.context, episode)
    assert budget["held"].get("calls", 0) == 0 and budget["used"]["calls"] == 0
    assert budget["held"].get("tokens", 0) == 0 and budget["used"]["tokens"] == 0
    with app_env.db.transaction(app_env.context) as conn:
        event = conn.execute(
            "SELECT details FROM evidence_events WHERE event_type='embedding_worker_outcome'"
        ).fetchone()
        assert event["details"]["outcome"] == "infrastructure_error"
        assert event["details"]["dispatched"] is False


@pytest.mark.parametrize("outcome", ["completed", "inconclusive"])
def test_missing_stop_proof_holds_usage_and_blocks_publication(
    app_env, tmp_path: Path, outcome: str
) -> None:
    class UnconfirmedStopSupervisor(ProcessSupervisor):
        def run(self, request, **kwargs):
            # Simulate a lost startup acknowledgement plus failed final wait.
            # The parent must not equate missing acknowledgement with no work.
            return ProcessResult(
                outcome, 0, b"{}", b"", 1, False, False, False, "fixture-isolation"
            )

    encoder = SupervisedQwenEncoder(tmp_path, supervisor=UnconfirmedStopSupervisor())
    service, space, source = build_service(app_env, encoder)
    episode = running_task(app_env)
    with pytest.raises(SupervisedEncodingFailure) as failure:
        service.index_source(app_env.context, episode, source["id"], space["id"])
    assert failure.value.code == "indeterminate"
    assert not failure.value.result.processes_stopped
    budget = BudgetLedger(app_env.db).inspect(app_env.context, episode)
    assert budget["held"]["calls"] == 1 and budget["used"].get("calls", 0) == 0
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 0
        event = conn.execute(
            "SELECT details FROM evidence_events WHERE event_type='embedding_worker_outcome'"
        ).fetchone()
        assert event["details"]["processes_stopped"] is False


def test_late_worker_result_after_task_cancellation_is_fenced(app_env, tmp_path: Path) -> None:
    episode = running_task(app_env)

    class FinishesAfterCancellation(SupervisedQwenEncoder):
        def encode_supervised(self, texts, kind, *, timeout_ms, cancelled=None):
            tasks = TaskService(app_env.db)
            current = tasks.inspect(app_env.context, episode)
            tasks.control(
                app_env.context,
                episode,
                current["update_token"],
                "cancel",
                "cancel before publication",
                idempotency_key="cancel",
            )
            return EmbeddingBatch((tuple([1.0] + [0.0] * 1023),), 6, 1, "synthetic")

    service, space, source = build_service(app_env, FinishesAfterCancellation(tmp_path))
    with pytest.raises(StateMismatchError, match="fenced"):
        service.index_source(app_env.context, episode, source["id"], space["id"])
    budget = BudgetLedger(app_env.db).inspect(app_env.context, episode)
    assert budget["used"]["calls"] == 1 and budget["held"].get("calls", 0) == 0
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 0
