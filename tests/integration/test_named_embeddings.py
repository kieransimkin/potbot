"""Named views are isolated even when their vectors share one coordinate space."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Literal
from uuid import UUID

import pytest

from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.memory.embedding_profile import EmbeddingBatch, EmbeddingProfile
from potbot.memory.embeddings import EmbeddingService
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend

pytestmark = pytest.mark.postgres


class NamedViewProvider:
    """Synthetic vectors test SQL selection, never model quality."""

    def __init__(self, profile: EmbeddingProfile | None = None) -> None:
        self.profile = profile or EmbeddingProfile()
        self.calls = 0
        self.hook = None

    def count_tokens(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> int:
        return sum(len(text.split()) for text in texts)

    def encode(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> EmbeddingBatch:
        self.calls += 1
        if self.hook is not None:
            self.hook()
        vector = (1.0, *(0.0 for _ in range(1023)))
        return EmbeddingBatch(
            tuple(vector for _ in texts), self.count_tokens(texts, kind), 1, "synthetic"
        )


def setup_service(app_env, provider=None):
    provider = provider or NamedViewProvider()
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    service = EmbeddingService(app_env.db, artifacts, provider)
    spec = ContractSpec(
        "Check named retrieval",
        "named-fixture-baseline",
        DeliverableType.INVESTIGATION,
        (Requirement("R1", "Retrieve named views", "Name and space are isolated"),),
        (),
        ("embed",),
        "v1",
        (("tokens", 100000), ("calls", 50), ("tool_ms", 5000000), ("concurrency", 1)),
    )
    tasks = TaskService(app_env.db)
    task = tasks.submit(app_env.context, app_env.repository_id, spec, "named-task")
    episode = UUID(task["task_id"])
    for status in (TaskStatus.INITIALIZING, TaskStatus.RUNNING):
        task = tasks.transition(
            app_env.context,
            episode,
            task["update_token"],
            status,
            "synthetic named view fixture",
            idempotency_key=status.value,
        )
    return service, provider, episode


def test_names_share_profile_but_not_materialization_or_retrieval(app_env) -> None:
    service, provider, episode = setup_service(app_env)
    ctx = app_env.context
    general = service.register_space(ctx)
    named = service.register_space(ctx, embedding_name="workflow_text")
    assert named["id"] == general["id"]
    assert named["content_hash"] == general["content_hash"] == provider.profile.fingerprint
    assert named["view_id"] != general["view_id"]
    assert (
        service.register_space(ctx, embedding_name="workflow_text")["view_id"] == named["view_id"]
    )
    source = service.register_source(ctx, app_env.repository_id, "shared", "database workflow")
    general_vector = service.index_source(ctx, episode, source["id"], general["id"])
    named_vector = service.index_source(
        ctx, episode, source["id"], named["id"], embedding_name="workflow_text"
    )
    assert general_vector["id"] != named_vector["id"]
    assert named_vector["embedding_name"] == "workflow_text"
    assert named_vector["view_id"] == named["view_id"]
    assert named_vector["encoding_input_hash"] == hashlib.sha256(b"database workflow").hexdigest()
    assert named_vector["input_format"] == "source_text_v1"
    assert (
        service.index_source(
            ctx, episode, source["id"], named["id"], embedding_name="workflow_text"
        )
        == named_vector
    )
    assert provider.calls == 2

    only_named = service.register_source(
        ctx, app_env.repository_id, "workflow-only", "another database workflow"
    )
    service.index_source(
        ctx, episode, only_named["id"], named["id"], embedding_name="workflow_text"
    )
    general_results = service.search(ctx, episode, app_env.repository_id, general["id"], "find")
    assert [row["id"] for row in general_results] == [source["id"]]
    assert general_results[0]["embedding_name"] == "general_text"
    assert general_results[0]["vector_id"] == general_vector["id"]
    named_results = service.search(
        ctx,
        episode,
        app_env.repository_id,
        named["id"],
        "find",
        embedding_name="workflow_text",
    )
    assert {row["id"] for row in named_results} == {source["id"], only_named["id"]}
    assert all(row["embedding_name"] == "workflow_text" for row in named_results)
    assert all(row["profile_hash"] == provider.profile.fingerprint for row in named_results)
    assert provider.calls == 5


def test_named_space_registration_is_required_and_provider_must_match(app_env) -> None:
    service, provider, episode = setup_service(app_env)
    ctx = app_env.context
    space = service.register_space(ctx)
    source = service.register_source(ctx, app_env.repository_id, "item", "database source")
    with pytest.raises(ScopeDeniedError, match="named embedding"):
        service.index_source(ctx, episode, source["id"], space["id"], embedding_name="missing")
    with pytest.raises(ScopeDeniedError, match="named embedding"):
        service.search(
            ctx, episode, app_env.repository_id, space["id"], "find", embedding_name="missing"
        )
    assert provider.calls == 0
    assert BudgetLedger(app_env.db).inspect(ctx, episode)["used"].get("calls", 0) == 0

    alternate_profile = replace(provider.profile, generation="named-generation-2")
    alternate = service.register_space(ctx, alternate_profile)
    service.index_source(ctx, episode, source["id"], space["id"])
    with pytest.raises(InvalidInputError, match="incompatible"):
        service.search(ctx, episode, app_env.repository_id, alternate["id"], "find")
    assert provider.calls == 1
    other_provider = NamedViewProvider(alternate_profile)
    other_service = EmbeddingService(app_env.db, service.artifacts, other_provider)
    assert other_service.search(ctx, episode, app_env.repository_id, alternate["id"], "find") == []
    old_vector = service.index_source(ctx, episode, source["id"], space["id"])
    new_vector = other_service.index_source(ctx, episode, source["id"], alternate["id"])
    assert new_vector["id"] != old_vector["id"]
    assert new_vector["embedding_name"] == old_vector["embedding_name"] == "general_text"
    assert new_vector["profile_hash"] != old_vector["profile_hash"]
    assert [
        row["vector_id"]
        for row in service.search(ctx, episode, app_env.repository_id, space["id"], "find")
    ] == [old_vector["id"]]
    assert [
        row["vector_id"]
        for row in other_service.search(
            ctx, episode, app_env.repository_id, alternate["id"], "find"
        )
    ] == [new_vector["id"]]


def test_invalid_names_do_not_dispatch_or_write(app_env) -> None:
    service, provider, episode = setup_service(app_env)
    ctx = app_env.context
    space = service.register_space(ctx)
    source = service.register_source(ctx, app_env.repository_id, "item", "database source")
    for name in ("", "UPPER", "bad name", "x" * 65, "leading__separator", "trailing-", None):
        with pytest.raises(InvalidInputError):
            service.register_space(ctx, embedding_name=name)
        with pytest.raises(InvalidInputError):
            service.index_source(ctx, episode, source["id"], space["id"], embedding_name=name)
        with pytest.raises(InvalidInputError):
            service.search(
                ctx, episode, app_env.repository_id, space["id"], "find", embedding_name=name
            )
    assert provider.calls == 0
    with app_env.db.transaction(ctx) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_views").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 0


def test_source_supersession_and_retirement_exclude_all_names(app_env) -> None:
    service, provider, episode = setup_service(app_env)
    ctx = app_env.context
    first = service.register_space(ctx)
    second = service.register_space(ctx, embedding_name="workflow_text")
    source = service.register_source(ctx, app_env.repository_id, "item", "database original")
    for name in ("general_text", "workflow_text"):
        service.index_source(ctx, episode, source["id"], first["id"], embedding_name=name)
    successor = service.register_source(ctx, app_env.repository_id, "item", "database new")
    for name in ("general_text", "workflow_text"):
        assert (
            service.search(
                ctx, episode, app_env.repository_id, first["id"], "find", embedding_name=name
            )
            == []
        )
    provider.hook = lambda: service.retire_source(ctx, app_env.repository_id, "item")
    before = provider.calls
    with pytest.raises(RevisionConflictError, match="changed or was retired"):
        service.index_source(
            ctx, episode, successor["id"], second["id"], embedding_name="workflow_text"
        )
    assert provider.calls == before + 1
    with app_env.db.transaction(ctx) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 2
    assert BudgetLedger(app_env.db).inspect(ctx, episode)["used"]["calls"] == provider.calls


@pytest.mark.parametrize("operation", ["index", "search"])
@pytest.mark.parametrize("change", ["profile", "provider"])
def test_provider_change_during_encoding_never_enters_selected_space(
    app_env, operation, change
) -> None:
    service, provider, episode = setup_service(app_env)
    ctx = app_env.context
    profile = provider.profile
    space = service.register_space(ctx)
    source = service.register_source(ctx, app_env.repository_id, "stable", "database source")
    service.index_source(ctx, episode, source["id"], space["id"])
    target = service.register_source(ctx, app_env.repository_id, "new", "database new source")

    def change_identity():
        if change == "profile":
            provider.profile = replace(profile, generation="changed-during-dispatch")
        else:
            service.provider = NamedViewProvider(profile)

    provider.hook = change_identity
    with pytest.raises(InvalidInputError, match="provider or profile changed"):
        if operation == "index":
            service.index_source(ctx, episode, target["id"], space["id"])
        else:
            service.search(ctx, episode, app_env.repository_id, space["id"], "find")
    with app_env.db.transaction(ctx) as conn:
        assert conn.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 1
    usage = BudgetLedger(app_env.db).inspect(ctx, episode)
    assert usage["used"]["calls"] == 2
    assert usage["held"].get("calls", 0) == 0
    assert provider.calls == 2


def test_cli_selects_the_named_view_and_exports_the_artifact_manifest(
    app_env, monkeypatch, capsys
) -> None:
    from potbot.memory import local_encoder
    from potbot.memory.embedding_cli import main

    service, provider, episode = setup_service(app_env)
    source = service.register_source(
        app_env.context, app_env.repository_id, "cli-source", "database CLI source"
    )
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)
    monkeypatch.setattr(local_encoder, "QwenLocalEncoder", lambda *args: provider)
    common = [
        "--workspace-id",
        str(app_env.context.workspace_id),
        "--principal-id",
        str(app_env.context.principal_id),
        "--schema",
        app_env.schema,
        "--artifact-root",
        str(app_env.artifact_root),
    ]
    assert main(["register-space", *common, "--embedding-name", "cli_text"]) == 0
    space = json.loads(capsys.readouterr().out)
    assert space["embedding_name"] == "cli_text"
    selection = [
        "--episode-id",
        str(episode),
        "--space-id",
        space["id"],
        "--embedding-name",
        "cli_text",
    ]
    assert main(["index", *common, *selection, "--source-id", str(source["id"])]) == 0
    vector = json.loads(capsys.readouterr().out)
    assert vector["embedding_name"] == "cli_text"
    assert (
        main(
            [
                "search",
                *common,
                *selection,
                "--repository-id",
                str(app_env.repository_id),
                "--query",
                "find",
            ]
        )
        == 0
    )
    results = json.loads(capsys.readouterr().out)
    assert [row["vector_id"] for row in results] == [vector["id"]]
    assert main(["manifest", *common, "--artifact-id", str(source["artifact_id"])]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert list(manifest["named_embeddings"]) == ["cli_text"]
    assert manifest["named_embeddings"]["cli_text"][0]["vector_id"] == vector["id"]
