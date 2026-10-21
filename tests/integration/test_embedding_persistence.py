"""Real pgvector, bounded budget dispatch, scope, and stale-source behavior.

The provider here is deliberately synthetic; live Qwen qualification is the
separate explicit `potbot embeddings smoke` command.
"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.memory.embedding_profile import EmbeddingBatch, EmbeddingProfile
from potbot.memory.embeddings import EmbeddingService
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

pytestmark = pytest.mark.postgres


class SyntheticProvider:
    profile = EmbeddingProfile()
    calls = 0
    hook = None

    def count_tokens(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> int:
        return sum(len(text.split()) for text in texts)

    def encode(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> EmbeddingBatch:
        self.calls += 1
        if self.hook:
            self.hook()
        vectors = []
        for text in texts:
            v = [0.0] * 1024
            v[0 if "database" in text.lower() else 1] = 1.0
            vectors.append(tuple(v))
        return EmbeddingBatch(tuple(vectors), self.count_tokens(texts, kind), 1, "synthetic")


def running_task(app_env) -> UUID:
    spec = ContractSpec(
        "Investigate database cleanup",
        "fixture-baseline",
        DeliverableType.INVESTIGATION,
        (Requirement("R1", "Retrieve useful sources", "Sources are scoped and ranked"),),
        (),
        ("embed",),
        "v1",
        (("tokens", 10000), ("calls", 20), ("tool_ms", 1000000), ("concurrency", 1)),
    )
    service = TaskService(app_env.db)
    task = service.submit(app_env.context, app_env.repository_id, spec, "embedding-task")
    identity = UUID(task["task_id"])
    task = service.transition(
        app_env.context,
        identity,
        task["update_token"],
        TaskStatus.INITIALIZING,
        "fixture initialization",
        idempotency_key="init",
    )
    service.transition(
        app_env.context,
        identity,
        task["update_token"],
        TaskStatus.RUNNING,
        "fixture ready",
        idempotency_key="run",
    )
    return identity


def service_for(app_env, provider=None):
    provider = provider or SyntheticProvider()
    return EmbeddingService(
        app_env.db, ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root)), provider
    )


def test_exact_vector_retrieval_idempotency_spaces_and_deletion(app_env) -> None:
    episode = running_task(app_env)
    provider = SyntheticProvider()
    service = service_for(app_env, provider)
    ctx = app_env.context
    space = service.register_space(ctx)
    first = service.register_source(
        ctx,
        app_env.repository_id,
        "cleanup",
        "Close a database connection",
        metadata={"kind": "workflow", "revision": "v1"},
    )
    second = service.register_source(
        ctx, app_env.repository_id, "styles", "Use CSS colors for a web page"
    )
    repeated = service.register_source(
        ctx,
        app_env.repository_id,
        "cleanup",
        "Close a database connection",
        metadata={"kind": "workflow", "revision": "v1"},
    )
    assert repeated["id"] == first["id"]
    vector = service.index_source(ctx, episode, first["id"], space["id"])
    assert service.index_source(ctx, episode, first["id"], space["id"])["id"] == vector["id"]
    assert provider.calls == 1
    service.index_source(ctx, episode, second["id"], space["id"])
    result = service.search(
        ctx, episode, app_env.repository_id, space["id"], "database close", limit=2
    )
    assert [r["id"] for r in result] == [first["id"], second["id"]]
    assert result[0]["distance"] == pytest.approx(0.0)
    alternate = service.register_space(ctx, replace(provider.profile, generation="v2"))
    with pytest.raises(InvalidInputError, match="incompatible"):
        service.search(ctx, episode, app_env.repository_id, alternate["id"], "database")
    provider2 = SyntheticProvider()
    provider2.profile = replace(provider.profile, generation="v2")
    assert (
        service_for(app_env, provider2).search(
            ctx, episode, app_env.repository_id, alternate["id"], "database"
        )
        == []
    )
    service.retire_source(ctx, app_env.repository_id, "cleanup")
    assert [
        r["id"]
        for r in service.search(ctx, episode, app_env.repository_id, space["id"], "database")
    ] == [second["id"]]
    usage = BudgetLedger(app_env.db).inspect(ctx, episode)
    assert usage["used"]["calls"] == 5 and usage["held"].get("calls", 0) == 0
    with app_env.other_db.transaction(app_env.other_context) as conn:
        assert (
            conn.execute("SELECT * FROM embedding_vectors WHERE id=%s", (vector["id"],)).fetchone()
            is None
        )
    with pytest.raises(ScopeDeniedError):
        service.register_source(ctx, app_env.other_repository_id, "private", "database secret")


def test_retirement_during_encoding_rejects_stale_publication_but_charges_compute(app_env) -> None:
    episode = running_task(app_env)
    provider = SyntheticProvider()
    service = service_for(app_env, provider)
    space = service.register_space(app_env.context)
    source = service.register_source(
        app_env.context, app_env.repository_id, "obsolete", "database source"
    )
    provider.hook = lambda: service.retire_source(
        app_env.context, app_env.repository_id, "obsolete"
    )
    with pytest.raises(RevisionConflictError):
        service.index_source(app_env.context, episode, source["id"], space["id"])
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 0
    assert BudgetLedger(app_env.db).inspect(app_env.context, episode)["used"]["calls"] == 1


def test_provider_failure_holds_uncertain_usage_and_pause_prevents_dispatch(app_env) -> None:
    episode = running_task(app_env)
    provider = SyntheticProvider()
    service = service_for(app_env, provider)
    space = service.register_space(app_env.context)
    source = service.register_source(
        app_env.context, app_env.repository_id, "connection", "database source"
    )
    tasks = TaskService(app_env.db)
    current = tasks.inspect(app_env.context, episode)
    tasks.control(
        app_env.context,
        episode,
        current["update_token"],
        "pause",
        "user pause",
        idempotency_key="pause",
    )
    with pytest.raises(StateMismatchError):
        service.index_source(app_env.context, episode, source["id"], space["id"])
    assert provider.calls == 0
    current = tasks.inspect(app_env.context, episode)
    tasks.control(
        app_env.context,
        episode,
        current["update_token"],
        "resume",
        "resume",
        idempotency_key="resume",
    )

    def crash():
        raise RuntimeError("provider lost")

    provider.hook = crash
    with pytest.raises(RuntimeError):
        service.index_source(app_env.context, episode, source["id"], space["id"])
    usage = BudgetLedger(app_env.db).inspect(app_env.context, episode)
    assert usage["held"]["calls"] == 1 and usage["used"].get("calls", 0) == 0
