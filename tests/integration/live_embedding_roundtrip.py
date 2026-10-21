"""Opt-in live Qwen + PostgreSQL test, outside the default test filename pattern.

Run explicitly with POTBOT_LIVE_MODEL_DIR and POTBOT_TEST_DSN. Missing local
weights are failures, never skips. No model download occurs during this test.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.memory.embedding_profile import EmbeddingProfile
from potbot.memory.embeddings import EmbeddingService
from potbot.memory.local_encoder import QwenLocalEncoder
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

pytestmark = pytest.mark.postgres


def test_live_qwen_vector_roundtrip(app_env) -> None:
    model_dir = os.environ.get("POTBOT_LIVE_MODEL_DIR")
    assert model_dir, "POTBOT_LIVE_MODEL_DIR must point at the pinned offline model"
    profile = replace(EmbeddingProfile(), max_input_tokens=256)
    service = EmbeddingService(
        app_env.db,
        ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root)),
        QwenLocalEncoder(Path(model_dir), profile),
    )
    ctx = app_env.context
    tasks = TaskService(app_env.db)
    contract = ContractSpec(
        "Verify standard local semantic retrieval",
        "live-fixture-v1",
        DeliverableType.INVESTIGATION,
        (Requirement("R1", "Find database cleanup evidence", "Correct source ranks first"),),
        (),
        ("embed",),
        "v1",
        (("tokens", 10000), ("calls", 10), ("tool_ms", 300000), ("concurrency", 1)),
    )
    task = tasks.submit(ctx, app_env.repository_id, contract, "live-embedding-task")
    episode_id = UUID(task["task_id"])
    task = tasks.transition(
        ctx,
        episode_id,
        task["update_token"],
        TaskStatus.INITIALIZING,
        "live local fixture",
        idempotency_key="init",
    )
    tasks.transition(
        ctx,
        episode_id,
        task["update_token"],
        TaskStatus.RUNNING,
        "local encoder ready",
        idempotency_key="run",
    )
    space = service.register_space(ctx)
    useful = service.register_source(
        ctx,
        app_env.repository_id,
        "database-cleanup",
        "A Python context manager closes its database connection when the block exits.",
        metadata={"kind": "workflow", "revision": "fixture-v1"},
    )
    distractor = service.register_source(
        ctx,
        app_env.repository_id,
        "css-colors",
        "A CSS stylesheet controls colors, layout, and fonts.",
        metadata={"kind": "document", "revision": "fixture-v1"},
    )
    first = service.index_source(ctx, episode_id, useful["id"], space["id"])
    assert service.index_source(ctx, episode_id, useful["id"], space["id"])["id"] == first["id"]
    service.index_source(ctx, episode_id, distractor["id"], space["id"])
    results = service.search(
        ctx,
        episode_id,
        app_env.repository_id,
        space["id"],
        "How can I ensure a database connection is closed?",
        limit=2,
    )
    assert [row["id"] for row in results] == [useful["id"], distractor["id"]]
    assert all(0 <= row["distance"] <= 2 for row in results)
    service.retire_source(ctx, app_env.repository_id, "database-cleanup")
    after = service.search(
        ctx,
        episode_id,
        app_env.repository_id,
        space["id"],
        "How can I ensure a database connection is closed?",
        limit=2,
    )
    assert [row["id"] for row in after] == [distractor["id"]]
    with app_env.db.transaction(ctx) as conn:
        vectors = conn.execute(
            "SELECT public.vector_dims(embedding) AS dimensions,usage_origin FROM embedding_vectors"
        ).fetchall()
        usage = conn.execute("SELECT origin FROM budget_usage").fetchall()
        assert vectors == [{"dimensions": 1024, "usage_origin": "measured"}] * 2
        assert usage == [{"origin": "measured"}] * 4
    budget = BudgetLedger(app_env.db).inspect(ctx, episode_id)
    assert budget["used"]["calls"] == 4
    assert all(amount == 0 for amount in budget["held"].values())
    with app_env.other_db.transaction(app_env.other_context) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 0
    report = {
        "schema_version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "profile": profile.to_dict(),
        "profile_hash": profile.fingerprint,
        "test": "live_qwen_pgvector_roundtrip",
        "result": "passed",
        "database_role": "restricted normal application login",
        "source_count": 2,
        "dimensions": 1024,
        "query_top_source": "database-cleanup",
        "cosine_distances": [row["distance"] for row in results],
        "checks": [
            "offline pinned model manifest integrity",
            "measured local CPU float32 encoding",
            "exact pgvector retrieval",
            "same-input idempotency",
            "retirement excluded from subsequent query",
            "cross-workspace RLS exclusion",
            "measured budget settlement",
        ],
        "budget_usage": budget["used"],
        "qualification": "integration smoke; not a retrieval benchmark or deployment promotion",
    }
    destination = os.environ.get("POTBOT_LIVE_REPORT")
    if destination:
        Path(destination).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, sort_keys=True))
