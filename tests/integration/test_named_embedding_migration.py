"""Populated upgrades and database enforcement for immutable named vector views."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.memory.embedding_profile import EmbeddingProfile
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import (
    Database,
    RepositoryStore,
    admin_connection,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations

pytestmark = pytest.mark.postgres

# These are the shipped revisions before named views were added. A populated
# upgrade must accept the original ledger, not a retroactively edited migration.
LEGACY_CHECKSUMS = (
    "dad7fcb6afd1720b427fb27bc18dfb182f0428112d78696554c7c941d1b04a7d",
    "56ee825cfe2e989dc29c8f520bbca85ba4f314691ef750e2a50712afc300576b",
    "309b25df22966997cd26693cb99e374b9bb9aaf19d5bc86b09d5113e90ea9a47",
    "65bfab18f6bb93d788bd220387780544f93c1b236a11c774d1f6e28602ea8941",
)


def _insert_space(db: Database, context: WorkspaceContext, generation: str) -> UUID:
    from psycopg.types.json import Jsonb

    profile = replace(EmbeddingProfile(), generation=generation)
    identity = uuid4()
    with db.transaction(context) as connection:
        connection.execute(
            "INSERT INTO embedding_spaces"
            "(workspace_id,id,content_hash,content,dimensions,metric) "
            "VALUES (%s,%s,%s,%s,1024,'cosine')",
            (context.workspace_id, identity, profile.fingerprint, Jsonb(profile.to_dict())),
        )
    return identity


def _clone_vector(
    db: Database,
    context: WorkspaceContext,
    source_vector_id: UUID,
    embedding_name: str,
    space_id: UUID,
) -> UUID:
    identity = uuid4()
    with db.transaction(context) as connection:
        connection.execute(
            "INSERT INTO embedding_vectors"
            "(workspace_id,id,source_id,space_id,embedding,content_hash,"
            "reservation_id,usage_origin,embedding_name) "
            "SELECT workspace_id,%s,source_id,%s,embedding,content_hash,"
            "reservation_id,usage_origin,%s FROM embedding_vectors "
            "WHERE workspace_id=%s AND id=%s",
            (identity, space_id, embedding_name, context.workspace_id, source_vector_id),
        )
    return identity


def test_original_migration_checksums_are_preserved() -> None:
    assert tuple(item.checksum for item in application_migrations()[:4]) == LEGACY_CHECKSUMS


def test_populated_v4_upgrade_preserves_vectors_profiles_sources_and_usage(app_env: Any) -> None:
    from psycopg.types.json import Jsonb

    revisions = application_migrations()
    schema = "named_upgrade"
    assert migrate(app_env.admin_dsn, schema, revisions[:4]) == (1, 2, 3, 4)
    workspace_id = provision_workspace(app_env.admin_dsn, schema, "legacy-embedding-workspace")
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    context = provision_principal(app_env.admin_dsn, schema, role, workspace_id)
    db = Database(app_env.db.dsn, schema)
    repository = RepositoryStore(db).create(
        context, name="legacy-repository", source_uri="fixture://legacy-vectors"
    )
    space_id = _insert_space(db, context, "legacy-populated")
    unused_space_id = _insert_space(db, context, "legacy-unpopulated")
    text = "Close the database connection after committing the transaction."
    metadata = {"revision": "legacy-v1", "kind": "workflow"}
    source_hash = content_hash({"text": text, "metadata": metadata})
    artifact = ArtifactStore(db, LocalArtifactBackend(app_env.artifact_root / "upgrade")).put(
        context, text.encode(), "text/plain; charset=utf-8"
    )
    source_id, head_id, episode_id, reservation_id, vector_id = (uuid4() for _ in range(5))
    with db.transaction(context) as connection:
        connection.execute(
            "INSERT INTO embedding_sources"
            "(workspace_id,id,repository_id,source_key,artifact_id,content_hash,content,metadata) "
            "VALUES (%s,%s,%s,'legacy-source',%s,%s,%s,%s)",
            (
                workspace_id,
                source_id,
                repository["id"],
                artifact["id"],
                source_hash,
                text,
                Jsonb(metadata),
            ),
        )
        connection.execute(
            "INSERT INTO embedding_source_heads"
            "(workspace_id,id,repository_id,source_key,source_id,generation,active) "
            "VALUES (%s,%s,%s,'legacy-source',%s,3,true)",
            (workspace_id, head_id, repository["id"], source_id),
        )
        connection.execute(
            "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type) "
            "VALUES (%s,%s,%s,'investigation')",
            (workspace_id, episode_id, repository["id"]),
        )
        connection.execute(
            "INSERT INTO budget_reservations"
            "(workspace_id,id,episode_id,reservation_key,amounts,status,actual,origin) "
            "VALUES (%s,%s,%s,'legacy-encoding',%s,'settled',%s,'synthetic')",
            (
                workspace_id,
                reservation_id,
                episode_id,
                Jsonb({"tokens": 100, "calls": 1}),
                Jsonb({"tokens": 12, "calls": 1}),
            ),
        )
        connection.execute(
            "INSERT INTO budget_usage"
            "(workspace_id,id,episode_id,reservation_id,amounts,origin) "
            "VALUES (%s,%s,%s,%s,%s,'synthetic')",
            (
                workspace_id,
                uuid4(),
                episode_id,
                reservation_id,
                Jsonb({"tokens": 12, "calls": 1}),
            ),
        )
        connection.execute(
            "INSERT INTO embedding_vectors"
            "(workspace_id,id,source_id,space_id,embedding,content_hash,reservation_id,usage_origin) "
            "VALUES (%s,%s,%s,%s,%s::public.vector,%s,%s,'synthetic')",
            (
                workspace_id,
                vector_id,
                source_id,
                space_id,
                json.dumps([1.0] + [0.0] * 1023),
                source_hash,
                reservation_id,
            ),
        )
        tables = (
            "artifacts",
            "embedding_spaces",
            "embedding_sources",
            "embedding_source_heads",
            "budget_reservations",
            "budget_usage",
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in tables
        }
        original_vector = connection.execute("SELECT * FROM embedding_vectors").fetchone()
    with admin_connection(app_env.admin_dsn, schema) as connection:
        original_history = connection.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()

    expected_versions = tuple(item.version for item in revisions)
    assert migrate(app_env.admin_dsn, schema) == expected_versions
    assert migrate(app_env.admin_dsn, schema) == expected_versions
    with admin_connection(app_env.admin_dsn, schema) as connection:
        assert (
            connection.execute(
                "SELECT * FROM schema_migrations WHERE version<=4 ORDER BY version"
            ).fetchall()
            == original_history
        )
    with db.transaction(context) as connection:
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert connection.execute("SELECT * FROM embedding_vectors").fetchone() == {
            **original_vector,
            "embedding_name": "general_text",
        }
        views = connection.execute(
            "SELECT id,embedding_name,space_id,input_format FROM embedding_views"
        ).fetchall()
        assert {(row["id"], row["space_id"]) for row in views} == {
            (space_id, space_id),
            (unused_space_id, unused_space_id),
        }
        assert all(
            row["embedding_name"] == "general_text" and row["input_format"] == "source_text_v1"
            for row in views
        )
        # The updated default-name conflict target deduplicates the old row.
        duplicate = connection.execute(
            "INSERT INTO embedding_vectors"
            "(workspace_id,id,source_id,space_id,embedding,content_hash,reservation_id,usage_origin) "
            "SELECT workspace_id,%s,source_id,space_id,embedding,content_hash,"
            "reservation_id,usage_origin FROM embedding_vectors WHERE id=%s "
            "ON CONFLICT(workspace_id,source_id,embedding_name,space_id) DO NOTHING RETURNING id",
            (uuid4(), vector_id),
        ).fetchone()
        assert duplicate is None
        # Migration regrants the new table to already provisioned logins.
        connection.execute(
            "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
            "VALUES (%s,%s,'code',%s)",
            (workspace_id, uuid4(), space_id),
        )

    second_vector = _clone_vector(db, context, vector_id, "code", space_id)
    assert second_vector != vector_id
    with pytest.raises(RevisionConflictError):
        _clone_vector(db, context, vector_id, "code", space_id)
    for name, target_space in (("unregistered", space_id), ("code", unused_space_id)):
        with pytest.raises(ScopeDeniedError):
            _clone_vector(db, context, vector_id, name, target_space)
    with db.transaction(context) as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM embedding_vectors").fetchone()["n"] == 2
        )


def test_named_views_enforce_scope_grants_and_immutable_bindings(app_env: Any) -> None:
    import psycopg

    context = app_env.context
    space_id = _insert_space(app_env.db, context, "scope-check")
    other_space_id = _insert_space(app_env.other_db, app_env.other_context, "other-scope")
    view_id = uuid4()
    with app_env.db.transaction(context) as connection:
        connection.execute(
            "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
            "VALUES (%s,%s,'general_text',%s)",
            (context.workspace_id, view_id, space_id),
        )
        privileges = connection.execute(
            "SELECT has_table_privilege(session_user,'embedding_views','SELECT') AS can_select,"
            "has_table_privilege(session_user,'embedding_views','INSERT') AS can_insert,"
            "has_table_privilege(session_user,'embedding_views','UPDATE') AS can_update,"
            "has_table_privilege(session_user,'embedding_views','DELETE') AS can_delete"
        ).fetchone()
        assert privileges == {
            "can_select": True,
            "can_insert": True,
            "can_update": False,
            "can_delete": False,
        }
    with app_env.other_db.transaction(app_env.other_context) as connection:
        assert connection.execute("SELECT * FROM embedding_views").fetchall() == []
        assert (
            connection.execute("SELECT * FROM embedding_views WHERE id=%s", (view_id,)).fetchone()
            is None
        )
    for workspace_id, target_space in (
        (context.workspace_id, other_space_id),
        (app_env.other_context.workspace_id, other_space_id),
    ):
        with pytest.raises(ScopeDeniedError):
            with app_env.db.transaction(context) as connection:
                connection.execute(
                    "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
                    "VALUES (%s,%s,'code',%s)",
                    (workspace_id, uuid4(), target_space),
                )
    for statement in (
        "UPDATE embedding_views SET embedding_name='changed' WHERE id=%s",
        "DELETE FROM embedding_views WHERE id=%s",
    ):
        with pytest.raises(ScopeDeniedError):
            with app_env.db.transaction(context) as connection:
                connection.execute(statement, (view_id,))
        # Grants alone are insufficient: the immutable trigger also guards a
        # privileged SQL writer that has UPDATE/DELETE permissions.
        with pytest.raises(psycopg.Error) as failure:
            with admin_connection(app_env.admin_dsn) as connection:
                connection.execute(statement, (view_id,))
        assert failure.value.sqlstate == "55000"
    with app_env.db.transaction(context) as connection:
        assert connection.execute(
            "SELECT embedding_name,space_id FROM embedding_views WHERE id=%s", (view_id,)
        ).fetchone() == {"embedding_name": "general_text", "space_id": space_id}


def test_named_view_name_and_input_format_constraints(app_env: Any) -> None:
    context = app_env.context
    space_id = _insert_space(app_env.db, context, "name-validation")
    for name in ("a", "general_text", "code.ast-v2", "a" * 64):
        with app_env.db.transaction(context) as connection:
            connection.execute(
                "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
                "VALUES (%s,%s,%s,%s)",
                (context.workspace_id, uuid4(), name, space_id),
            )
    for name in ("", "A", "_a", "a__b", "a..b", "a--b", "a_", "a b", "a\n", "é", "a" * 65):
        with pytest.raises(InvalidInputError):
            with app_env.db.transaction(context) as connection:
                connection.execute(
                    "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
                    "VALUES (%s,%s,%s,%s)",
                    (context.workspace_id, uuid4(), name, space_id),
                )
    with pytest.raises(InvalidInputError):
        with app_env.db.transaction(context) as connection:
            connection.execute(
                "INSERT INTO embedding_views"
                "(workspace_id,id,embedding_name,space_id,input_format) "
                "VALUES (%s,%s,'unsupported',%s,'unversioned-summary')",
                (context.workspace_id, uuid4(), space_id),
            )
