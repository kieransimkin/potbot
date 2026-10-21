"""Current named manifests retain source occurrences and enforce workspace scope."""

import hashlib
import json
from dataclasses import replace
from typing import Literal
from uuid import UUID, uuid4

import pytest

from potbot.controller.tasks import TaskService
from potbot.domain.embeddings import (
    DEFAULT_EMBEDDING_NAME,
    SOURCE_TEXT_INPUT_FORMAT,
    ArtifactEmbeddingManifest,
)
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.memory.embedding_profile import EmbeddingBatch, EmbeddingProfile
from potbot.memory.embeddings import EmbeddingService
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

pytestmark = pytest.mark.postgres


class SyntheticProvider:
    def __init__(self, profile: EmbeddingProfile | None = None) -> None:
        self.profile = profile or EmbeddingProfile()

    def count_tokens(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> int:
        return sum(len(text.split()) for text in texts)

    def encode(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> EmbeddingBatch:
        vector = (1.0,) + (0.0,) * 1023
        return EmbeddingBatch(
            tuple(vector for _ in texts), self.count_tokens(texts, kind), 1, "synthetic"
        )


def running_task(app_env) -> UUID:
    service = TaskService(app_env.db)
    task = service.submit(
        app_env.context,
        app_env.repository_id,
        ContractSpec(
            "Inspect named source embeddings",
            "fixture",
            DeliverableType.INVESTIGATION,
            (Requirement("R1", "Preserve source and space identities", "Check manifest"),),
            (),
            ("embed",),
            "v1",
            (("tokens", 20000), ("calls", 20), ("tool_ms", 1000000), ("concurrency", 1)),
        ),
        "manifest-task",
    )
    identity = UUID(task["task_id"])
    task = service.transition(
        app_env.context,
        identity,
        task["update_token"],
        TaskStatus.INITIALIZING,
        "fixture initialization",
        idempotency_key="manifest-init",
    )
    service.transition(
        app_env.context,
        identity,
        task["update_token"],
        TaskStatus.RUNNING,
        "fixture ready",
        idempotency_key="manifest-run",
    )
    return identity


def test_manifest_preserves_current_sources_multiple_names_and_spaces(app_env) -> None:
    context = app_env.context
    store = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    provider = SyntheticProvider()
    service = EmbeddingService(app_env.db, store, provider)
    task_id = running_task(app_env)
    space = service.register_space(context)
    alias = service.register_space(context, embedding_name="code_text")
    assert alias["id"] == space["id"]
    alternate_profile = replace(provider.profile, generation="manifest-alternate-v1")
    alternate_service = EmbeddingService(app_env.db, store, SyntheticProvider(alternate_profile))
    alternate_space = alternate_service.register_space(context)
    text = "Close database connections after use."
    first = service.register_source(
        context, app_env.repository_id, "workflow/cleanup", text, metadata={"kind": "workflow"}
    )
    second = service.register_source(
        context, app_env.repository_id, "docs/cleanup", text, metadata={"kind": "documentation"}
    )
    assert first["artifact_id"] == second["artifact_id"]
    assert first["content_hash"] != second["content_hash"]
    first_vector = service.index_source(context, task_id, first["id"], space["id"])
    second_vector = service.index_source(context, task_id, second["id"], space["id"])
    alias_vector = service.index_source(
        context, task_id, first["id"], space["id"], embedding_name="code_text"
    )
    alternate_vector = alternate_service.index_source(
        context, task_id, first["id"], alternate_space["id"]
    )
    artifact_id = first["artifact_id"]
    original_metadata = store.metadata(context, artifact_id)
    result = store.embedding_manifest(context, artifact_id)
    assert json.loads(json.dumps(result)) == result
    assert ArtifactEmbeddingManifest.from_dict(result).to_dict() == result
    assert result == store.embedding_manifest(context, artifact_id)
    assert list(result["named_embeddings"]) == ["code_text", DEFAULT_EMBEDDING_NAME]
    refs = result["named_embeddings"][DEFAULT_EMBEDDING_NAME]
    assert {ref["vector_id"] for ref in refs} == {
        str(first_vector["id"]),
        str(second_vector["id"]),
        str(alternate_vector["id"]),
    }
    assert len(refs) == 3
    assert result["named_embeddings"]["code_text"][0]["vector_id"] == str(alias_vector["id"])
    assert {ref["generation"] for ref in refs} == {
        provider.profile.generation,
        alternate_profile.generation,
    }
    expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert result["artifact_digest"] == expected_hash
    for entries in result["named_embeddings"].values():
        for ref in entries:
            assert ref["encoding_input_hash"] == expected_hash
            assert ref["input_format"] == SOURCE_TEXT_INPUT_FORMAT
            assert ref["usage_origin"] == "synthetic"
    assert store.metadata(context, artifact_id) == original_metadata
    assert store.read(context, artifact_id) == text.encode("utf-8")

    service.retire_source(context, app_env.repository_id, "workflow/cleanup")
    remaining = store.embedding_manifest(context, artifact_id)["named_embeddings"]
    assert list(remaining) == [DEFAULT_EMBEDDING_NAME]
    assert [ref["source_id"] for ref in remaining[DEFAULT_EMBEDDING_NAME]] == [str(second["id"])]
    service.register_source(context, app_env.repository_id, "docs/cleanup", "Updated source text")
    assert store.embedding_manifest(context, artifact_id)["named_embeddings"] == {}
    assert store.read(context, artifact_id) == text.encode("utf-8")


def test_manifest_unindexed_artifact_and_foreign_workspace_access(app_env) -> None:
    store = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    other_store = ArtifactStore(app_env.other_db, LocalArtifactBackend(app_env.artifact_root))
    artifact = store.put(app_env.context, b"not indexed")
    result = store.embedding_manifest(app_env.context, artifact["id"])
    assert result["named_embeddings"] == {}
    assert result["workspace_id"] == str(app_env.context.workspace_id)
    with pytest.raises(ScopeDeniedError):
        other_store.embedding_manifest(app_env.other_context, artifact["id"])
    with pytest.raises(ScopeDeniedError):
        store.embedding_manifest(app_env.context, uuid4())
