"""Database enforcement for scoped, immutable SYN snapshot publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
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
from potbot.persistence.syn_schema import WORKSPACE_TABLES

pytestmark = pytest.mark.postgres

LEGACY_CHECKSUMS = (
    "dad7fcb6afd1720b427fb27bc18dfb182f0428112d78696554c7c941d1b04a7d",
    "56ee825cfe2e989dc29c8f520bbca85ba4f314691ef750e2a50712afc300576b",
    "309b25df22966997cd26693cb99e374b9bb9aaf19d5bc86b09d5113e90ea9a47",
    "65bfab18f6bb93d788bd220387780544f93c1b236a11c774d1f6e28602ea8941",
    "5456f844a46117a94a61b50b9e44896890ef25b3a8870d1b29e243aed9bcfc92",
)
ROOT_TYPE = "syn://type/root"
CHILD_TYPE = "syn://type/child"
OBJECT_REF = "syn://thing/sample"


@dataclass(frozen=True)
class SnapshotFixture:
    context: WorkspaceContext
    id: UUID
    namespace_id: UUID
    repository_id: UUID
    content_hash: str


def _store(app_env: Any) -> ArtifactStore:
    return ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))


def _stage(
    connection: Any,
    app_env: Any,
    *,
    type_count: int = 2,
    object_count: int = 1,
) -> SnapshotFixture:
    context = app_env.context
    identity, namespace_id = uuid4(), uuid4()
    payload = canonical_json({"fixture_snapshot": str(identity)}).encode()
    artifact = _store(app_env).put_in_transaction(connection, context, payload, "application/json")
    connection.execute(
        "INSERT INTO syn_namespaces(workspace_id,id,name,repository_id) VALUES (%s,%s,%s,%s)",
        (context.workspace_id, namespace_id, str(namespace_id), app_env.repository_id),
    )
    connection.execute(
        "INSERT INTO syn_snapshots"
        "(workspace_id,id,namespace_id,repository_id,content_hash,source_hash,catalog_hash,"
        "source_label,catalog_mode,artifact_id,type_count,object_count) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'synthetic fixture','complete',%s,%s,%s)",
        (
            context.workspace_id,
            identity,
            namespace_id,
            app_env.repository_id,
            artifact["digest"],
            content_hash({"source": str(identity)}),
            content_hash({"catalog": str(identity)}),
            artifact["id"],
            type_count,
            object_count,
        ),
    )
    return SnapshotFixture(
        context, identity, namespace_id, app_env.repository_id, artifact["digest"]
    )


def _source(
    connection: Any,
    app_env: Any,
    snapshot: SnapshotFixture,
    ref: str,
    text: str,
    *,
    repository_id: UUID | None = None,
    source_key: str | None = None,
    metadata_override: dict[str, str] | None = None,
) -> tuple[UUID, UUID]:
    from psycopg.types.json import Jsonb

    metadata = {"source_url": ref, "revision": snapshot.content_hash}
    metadata.update(metadata_override or {})
    artifact = _store(app_env).put_in_transaction(
        connection, snapshot.context, text.encode(), "application/json"
    )
    source_id = uuid4()
    connection.execute(
        "INSERT INTO embedding_sources"
        "(workspace_id,id,repository_id,source_key,artifact_id,content_hash,content,metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            snapshot.context.workspace_id,
            source_id,
            repository_id or snapshot.repository_id,
            source_key or f"syn:{snapshot.namespace_id}:{snapshot.id}:{ref}",
            artifact["id"],
            content_hash({"text": text, "metadata": metadata}),
            text,
            Jsonb(metadata),
        ),
    )
    return source_id, artifact["id"]


def _type(
    connection: Any,
    app_env: Any,
    snapshot: SnapshotFixture,
    ref: str,
    parents: tuple[str, ...] = (),
    *,
    source: tuple[UUID, UUID] | None = None,
) -> None:
    from psycopg.types.json import Jsonb

    definition = {
        "ref": ref,
        "name": ref.rsplit("/", 1)[-1],
        "parents": list(parents),
        "definition": None,
        "properties": [],
        "metadata_json": "{}",
    }
    source_id, artifact_id = source or _source(
        connection, app_env, snapshot, ref, canonical_json(definition)
    )
    connection.execute(
        "INSERT INTO syn_types"
        "(workspace_id,snapshot_id,repository_id,ref,definition,source_id,artifact_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            snapshot.context.workspace_id,
            snapshot.id,
            snapshot.repository_id,
            ref,
            Jsonb(definition),
            source_id,
            artifact_id,
        ),
    )


def _object(
    connection: Any,
    app_env: Any,
    snapshot: SnapshotFixture,
    *,
    ref: str = OBJECT_REF,
    type_ref: str = CHILD_TYPE,
    kind: str = "thing",
    source: tuple[UUID, UUID] | None = None,
) -> None:
    text = canonical_json({"id": ref, "typeRef": type_ref, "name": "Synthetic object"})
    source_id, artifact_id = source or _source(connection, app_env, snapshot, ref, text)
    connection.execute(
        "INSERT INTO syn_objects"
        "(workspace_id,snapshot_id,repository_id,ref,kind,type_ref,source_id,artifact_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            snapshot.context.workspace_id,
            snapshot.id,
            snapshot.repository_id,
            ref,
            kind,
            type_ref,
            source_id,
            artifact_id,
        ),
    )


def _edge(connection: Any, snapshot: SnapshotFixture, child: str, parent: str) -> None:
    connection.execute(
        "INSERT INTO syn_type_parents(workspace_id,snapshot_id,child_ref,parent_ref) "
        "VALUES (%s,%s,%s,%s)",
        (snapshot.context.workspace_id, snapshot.id, child, parent),
    )


def _ancestry(connection: Any, snapshot: SnapshotFixture, child: str, parent: str) -> None:
    connection.execute(
        "INSERT INTO syn_type_ancestry(workspace_id,snapshot_id,descendant_ref,ancestor_ref) "
        "VALUES (%s,%s,%s,%s)",
        (snapshot.context.workspace_id, snapshot.id, child, parent),
    )


def _members(connection: Any, app_env: Any, snapshot: SnapshotFixture) -> None:
    _type(connection, app_env, snapshot, ROOT_TYPE)
    _type(connection, app_env, snapshot, CHILD_TYPE, (ROOT_TYPE,))
    _edge(connection, snapshot, CHILD_TYPE, ROOT_TYPE)
    for child, parent in (
        (ROOT_TYPE, ROOT_TYPE),
        (CHILD_TYPE, CHILD_TYPE),
        (CHILD_TYPE, ROOT_TYPE),
    ):
        _ancestry(connection, snapshot, child, parent)
    _object(connection, app_env, snapshot)


def _seal(connection: Any, snapshot: SnapshotFixture) -> None:
    connection.execute(
        "INSERT INTO syn_snapshot_seals(workspace_id,snapshot_id) VALUES (%s,%s)",
        (snapshot.context.workspace_id, snapshot.id),
    )


def _published(app_env: Any) -> SnapshotFixture:
    with app_env.db.transaction(app_env.context) as connection:
        snapshot = _stage(connection, app_env)
        _members(connection, app_env, snapshot)
        _seal(connection, snapshot)
    return snapshot


def _cursor(connection: Any, snapshot: SnapshotFixture, principal_id: UUID) -> UUID:
    identity = uuid4()
    connection.execute(
        "INSERT INTO syn_inventory_cursors"
        "(workspace_id,id,snapshot_id,principal_id,query_hash,last_ref,"
        "delivered_count,total_count,page_size) VALUES (%s,%s,%s,%s,%s,%s,1,1,1)",
        (
            snapshot.context.workspace_id,
            identity,
            snapshot.id,
            principal_id,
            content_hash({"type_ref": ROOT_TYPE, "include_subtypes": True}),
            OBJECT_REF,
        ),
    )
    return identity


def test_syn_migration_preserves_all_five_shipped_checksums() -> None:
    assert tuple(item.checksum for item in application_migrations()[:5]) == LEGACY_CHECKSUMS


def test_populated_v5_upgrade_preserves_sources_and_named_views(app_env: Any) -> None:
    from psycopg.types.json import Jsonb

    schema = "syn_upgrade"
    revisions = application_migrations()
    assert migrate(app_env.admin_dsn, schema, revisions[:5]) == (1, 2, 3, 4, 5)
    workspace_id = provision_workspace(app_env.admin_dsn, schema, "legacy-syn-upgrade")
    with app_env.db.transaction(app_env.context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    context = provision_principal(app_env.admin_dsn, schema, role, workspace_id)
    database = Database(app_env.db.dsn, schema)
    repository = RepositoryStore(database).create(
        context, name="legacy", source_uri="fixture://syn-upgrade"
    )
    artifact = ArtifactStore(database, LocalArtifactBackend(app_env.artifact_root / "upgrade")).put(
        context, b"Legacy source", "text/plain"
    )
    with database.transaction(context) as connection:
        source_id, space_id = uuid4(), uuid4()
        connection.execute(
            "INSERT INTO embedding_sources"
            "(workspace_id,id,repository_id,source_key,artifact_id,content_hash,content,metadata) "
            "VALUES (%s,%s,%s,'legacy-source',%s,%s,'Legacy source',%s)",
            (
                workspace_id,
                source_id,
                repository["id"],
                artifact["id"],
                content_hash({"text": "Legacy source", "metadata": {}}),
                Jsonb({}),
            ),
        )
        connection.execute(
            "INSERT INTO embedding_source_heads"
            "(workspace_id,id,repository_id,source_key,source_id,generation,active) "
            "VALUES (%s,%s,%s,'legacy-source',%s,1,true)",
            (workspace_id, uuid4(), repository["id"], source_id),
        )
        connection.execute(
            "INSERT INTO embedding_spaces"
            "(workspace_id,id,content_hash,content,dimensions,metric) "
            "VALUES (%s,%s,%s,%s,1024,'cosine')",
            (workspace_id, space_id, content_hash({"legacy": True}), Jsonb({"legacy": True})),
        )
        connection.execute(
            "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id) "
            "VALUES (%s,%s,'code',%s)",
            (workspace_id, uuid4(), space_id),
        )
        tables = ("artifacts", "embedding_sources", "embedding_source_heads", "embedding_views")
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
    with admin_connection(app_env.admin_dsn, schema) as connection:
        history = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert migrate(app_env.admin_dsn, schema) == tuple(item.version for item in revisions)
    assert migrate(app_env.admin_dsn, schema) == tuple(item.version for item in revisions)
    with admin_connection(app_env.admin_dsn, schema) as connection:
        assert (
            connection.execute(
                "SELECT * FROM schema_migrations WHERE version<=5 ORDER BY version"
            ).fetchall()
            == history
        )
    with database.transaction(context) as connection:
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        connection.execute(
            "INSERT INTO syn_namespaces(workspace_id,id,name,repository_id) VALUES (%s,%s,%s,%s)",
            (workspace_id, uuid4(), "new-table-grants", repository["id"]),
        )


def test_publication_is_complete_and_inventory_is_exact(app_env: Any) -> None:
    snapshot = _published(app_env)
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM syn_snapshot_seals").fetchone()["n"] == 1
        )
        assert (
            connection.execute(
                "SELECT ref FROM syn_objects WHERE snapshot_id=%s AND type_ref=%s ORDER BY ref",
                (snapshot.id, ROOT_TYPE),
            ).fetchall()
            == []
        )
        assert connection.execute(
            "SELECT o.ref FROM syn_objects o JOIN syn_type_ancestry a "
            "ON a.workspace_id=o.workspace_id AND a.snapshot_id=o.snapshot_id "
            "AND a.descendant_ref=o.type_ref "
            "WHERE o.snapshot_id=%s AND a.ancestor_ref=%s ORDER BY o.ref",
            (snapshot.id, ROOT_TYPE),
        ).fetchall() == [{"ref": OBJECT_REF}]
        assert connection.execute("SELECT count(*) AS n FROM syn_types").fetchone()["n"] == 2


def test_both_type_schemes_support_bounded_native_instance_assignments(app_env: Any) -> None:
    refs = ("syn://type/" + "a" * 255, "synny://type/" + "b" * 255)
    with app_env.db.transaction(app_env.context) as connection:
        snapshot = _stage(connection, app_env, object_count=2)
        for ref in refs:
            _type(connection, app_env, snapshot, ref)
            _ancestry(connection, snapshot, ref, ref)
        _object(connection, app_env, snapshot, type_ref=refs[1])
        _object(connection, app_env, snapshot, ref="syn://thing/native", type_ref=refs[0])
        _seal(connection, snapshot)
    with app_env.db.transaction(app_env.context) as connection:
        assert {
            row["ref"] for row in connection.execute("SELECT ref FROM syn_types").fetchall()
        } == set(refs)
        assert connection.execute(
            "SELECT ref,kind,type_ref FROM syn_objects WHERE ref=%s", (OBJECT_REF,)
        ).fetchone() == {
            "ref": OBJECT_REF,
            "kind": "thing",
            "type_ref": refs[1],
        }


@pytest.mark.parametrize("first_scheme", ("syn", "synny"))
def test_alternate_type_scheme_cannot_create_a_second_slug_identity(
    app_env: Any, first_scheme: str
) -> None:
    other_scheme = "synny" if first_scheme == "syn" else "syn"
    with pytest.raises(RevisionConflictError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env, type_count=2, object_count=0)
            _type(connection, app_env, snapshot, f"{first_scheme}://type/same-type")
            _type(connection, app_env, snapshot, f"{other_scheme}://type/same-type")


@pytest.mark.parametrize(
    "ref",
    (
        "synny://thing/sample",
        "SYNNY://type/sample",
        "synny://type/Sample",
        "synny://type/" + "a" * 256,
    ),
)
def test_potbot_type_scheme_does_not_relax_kind_case_or_slug_bounds(app_env: Any, ref: str) -> None:
    with pytest.raises(InvalidInputError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env, type_count=1, object_count=0)
            _type(connection, app_env, snapshot, ref)


def test_unsealed_snapshot_cannot_commit_and_rolls_back_all_members(app_env: Any) -> None:
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env)
            _members(connection, app_env, snapshot)
    with app_env.db.transaction(app_env.context) as connection:
        for table in (*WORKSPACE_TABLES, "artifacts", "embedding_sources"):
            assert connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


@pytest.mark.parametrize("defect", ("count", "parent_definition", "cycle", "missing", "extra"))
def test_seal_rejects_inconsistent_snapshot_graph(app_env: Any, defect: str) -> None:
    with pytest.raises(InvalidInputError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env, type_count=3 if defect == "count" else 2)
            root_parents = (CHILD_TYPE,) if defect == "cycle" else ()
            _type(connection, app_env, snapshot, ROOT_TYPE, root_parents)
            _type(connection, app_env, snapshot, CHILD_TYPE, (ROOT_TYPE,))
            if defect != "parent_definition":
                _edge(connection, snapshot, CHILD_TYPE, ROOT_TYPE)
            if defect == "cycle":
                _edge(connection, snapshot, ROOT_TYPE, CHILD_TYPE)
            for child, parent in ((ROOT_TYPE, ROOT_TYPE), (CHILD_TYPE, CHILD_TYPE)):
                _ancestry(connection, snapshot, child, parent)
            if defect != "missing":
                _ancestry(connection, snapshot, CHILD_TYPE, ROOT_TYPE)
            if defect == "extra":
                _ancestry(connection, snapshot, ROOT_TYPE, CHILD_TYPE)
            _object(connection, app_env, snapshot)
            _seal(connection, snapshot)


def test_published_membership_cannot_grow_in_any_table(app_env: Any) -> None:
    snapshot = _published(app_env)
    attempts = (
        lambda connection: _type(connection, app_env, snapshot, "syn://type/later"),
        lambda connection: _object(connection, app_env, snapshot, ref="syn://thing/later"),
        lambda connection: _edge(connection, snapshot, ROOT_TYPE, CHILD_TYPE),
        lambda connection: _ancestry(connection, snapshot, ROOT_TYPE, CHILD_TYPE),
        lambda connection: _seal(connection, snapshot),
    )
    for attempt in attempts:
        with pytest.raises(RevisionConflictError):
            with app_env.db.transaction(app_env.context) as connection:
                attempt(connection)


def test_parallel_writer_cannot_join_an_unpublished_snapshot(app_env: Any) -> None:
    with app_env.db.transaction(app_env.context) as publishing:
        snapshot = _stage(publishing, app_env)
        _members(publishing, app_env, snapshot)
        with pytest.raises(ScopeDeniedError):
            with app_env.db.transaction(app_env.context) as concurrent:
                _ancestry(concurrent, snapshot, ROOT_TYPE, CHILD_TYPE)
        _seal(publishing, snapshot)


def test_temporary_table_cannot_hide_publication_from_insert_guard(app_env: Any) -> None:
    snapshot = _published(app_env)
    with pytest.raises(RevisionConflictError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "CREATE TEMPORARY TABLE syn_snapshot_seals(workspace_id uuid,snapshot_id uuid)"
            )
            _ancestry(connection, snapshot, ROOT_TYPE, CHILD_TYPE)


@pytest.mark.parametrize("defect", ("repository", "artifact", "snapshot_type"))
def test_source_and_type_associations_cannot_cross_scopes(app_env: Any, defect: str) -> None:
    other_repository = RepositoryStore(app_env.db).create(
        app_env.context, name="same-workspace-other-repository", source_uri="fixture://different"
    )
    _published(app_env)
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env, type_count=1)
            _type(connection, app_env, snapshot, ROOT_TYPE)
            source_id, artifact_id = _source(
                connection,
                app_env,
                snapshot,
                OBJECT_REF,
                '{"name":"Synthetic object"}',
                repository_id=other_repository["id"] if defect == "repository" else None,
            )
            if defect == "artifact":
                artifact_id = _store(app_env).put_in_transaction(
                    connection, app_env.context, b"different bytes", "application/octet-stream"
                )["id"]
            _object(
                connection,
                app_env,
                snapshot,
                type_ref=CHILD_TYPE if defect == "snapshot_type" else ROOT_TYPE,
                source=(source_id, artifact_id),
            )


@pytest.mark.parametrize("defect", ("source_key", "source_url", "revision"))
def test_member_source_provenance_is_snapshot_specific(app_env: Any, defect: str) -> None:
    with pytest.raises(InvalidInputError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env, type_count=1, object_count=0)
            source = _source(
                connection,
                app_env,
                snapshot,
                ROOT_TYPE,
                "{}",
                source_key="syn:another-snapshot" if defect == "source_key" else None,
                metadata_override={defect: "wrong"} if defect != "source_key" else None,
            )
            _type(connection, app_env, snapshot, ROOT_TYPE, source=source)


@pytest.mark.parametrize(
    ("ref", "kind"),
    (
        ("syn://thing/" + "a" * 256, "thing"),
        ("syn://thing/noncanonical-", "thing"),
        ("syn://thing/noncanonical\n", "thing"),
        ("syn://thing/uppercaseA", "thing"),
        ("syn://thing/sample", "cog"),
        ("syn://type/sample", "type"),
        ("synny://thing/sample", "thing"),
        ("Syn://thing/sample", "thing"),
    ),
)
def test_storage_kind_and_ref_constraints(app_env: Any, ref: str, kind: str) -> None:
    with pytest.raises(InvalidInputError):
        with app_env.db.transaction(app_env.context) as connection:
            snapshot = _stage(connection, app_env, type_count=1)
            _type(connection, app_env, snapshot, ROOT_TYPE)
            _object(connection, app_env, snapshot, ref=ref, kind=kind, type_ref=ROOT_TYPE)


def test_tables_force_scope_and_immutability_even_for_privileged_writer(app_env: Any) -> None:
    import psycopg

    snapshot = _published(app_env)
    with app_env.db.transaction(app_env.context) as connection:
        _cursor(connection, snapshot, app_env.context.principal_id)
        for table in WORKSPACE_TABLES:
            assert connection.execute(
                "SELECT has_table_privilege(session_user,%s,'SELECT') AS can_select,"
                "has_table_privilege(session_user,%s,'INSERT') AS can_insert,"
                "has_table_privilege(session_user,%s,'UPDATE') AS can_update,"
                "has_table_privilege(session_user,%s,'DELETE') AS can_delete",
                (table, table, table, table),
            ).fetchone() == {
                "can_select": True,
                "can_insert": True,
                "can_update": False,
                "can_delete": False,
            }
    with app_env.other_db.transaction(app_env.other_context) as connection:
        for table in WORKSPACE_TABLES:
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == []
    with admin_connection(app_env.admin_dsn) as connection:
        security = connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='potbot' AND c.relname=ANY(%s)",
            (list(WORKSPACE_TABLES),),
        ).fetchall()
        assert len(security) == len(WORKSPACE_TABLES)
        assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in security)
    for table in WORKSPACE_TABLES:
        for statement in (
            f"UPDATE {table} SET workspace_id=workspace_id",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(ScopeDeniedError):
                with app_env.db.transaction(app_env.context) as connection:
                    connection.execute(statement)
            with pytest.raises(psycopg.Error) as failure:
                with admin_connection(app_env.admin_dsn) as connection:
                    connection.execute(statement)
            assert failure.value.sqlstate == "55000"
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute(
                "INSERT INTO syn_namespaces(workspace_id,id,name,repository_id) VALUES (%s,%s,%s,%s)",
                (
                    app_env.other_context.workspace_id,
                    uuid4(),
                    "foreign",
                    app_env.other_repository_id,
                ),
            )


def test_cursors_are_bound_to_authenticated_principal_even_in_shared_workspace(
    app_env: Any,
) -> None:
    snapshot = _published(app_env)
    with app_env.other_db.transaction(app_env.other_context) as connection:
        other_role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    shared_context = provision_principal(
        app_env.admin_dsn, "potbot", other_role, app_env.context.workspace_id
    )
    with app_env.db.transaction(app_env.context) as connection:
        cursor_id = _cursor(connection, snapshot, app_env.context.principal_id)
    with app_env.other_db.transaction(shared_context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM syn_snapshots").fetchone()["n"] == 1
        assert (
            connection.execute(
                "SELECT * FROM syn_inventory_cursors WHERE id=%s", (cursor_id,)
            ).fetchall()
            == []
        )
    with pytest.raises(ScopeDeniedError):
        with app_env.other_db.transaction(shared_context) as connection:
            _cursor(connection, snapshot, app_env.context.principal_id)
    with app_env.other_db.transaction(shared_context) as connection:
        connection.execute(
            "SELECT set_config('potbot.principal_id',%s,true)",
            (str(app_env.context.principal_id),),
        )
        assert connection.execute("SELECT * FROM syn_inventory_cursors").fetchall() == []
