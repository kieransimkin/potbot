"""End-to-end SYN identity, inventory, provenance and named-vector contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator

from potbot.controller.budgets import BudgetLedger
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, SynType
from potbot.memory.embedding_profile import EmbeddingBatch
from potbot.persistence.artifacts import ArtifactIntegrityError, ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import RepositoryStore, provision_principal
from potbot.persistence.syn_schema import WORKSPACE_TABLES
from potbot.semantics import service as service_module
from potbot.semantics.catalog import data_type_ref, extension_types
from potbot.semantics.service import SynService
from tests.integration.test_named_embeddings import NamedViewProvider, setup_service

pytestmark = pytest.mark.postgres

ROOT = SynRef("syn://type/test-root")
LEFT = SynRef("syn://type/test-left")
RIGHT = SynRef("syn://type/test-right")
LEAF = SynRef("syn://type/test-leaf")
EMPTY = SynRef("syn://type/test-empty")


def _type(ref: SynRef, parents: tuple[SynRef, ...] = ()) -> SynType:
    return SynType(ref, ref.slug, parents, None, ())


def _object(ref: str, target: SynRef, **values: Any) -> SynObject:
    return SynObject(
        SynRef(ref),
        target,
        canonical_json({"id": ref, "typeRef": str(target), "name": ref, **values}),
    )


def _snapshot(
    *, objects: tuple[SynObject, ...] | None = None, label: str = "synthetic SYN fixture"
) -> SynSnapshot:
    return SynSnapshot(
        label,
        "complete",
        (
            _type(ROOT),
            _type(LEFT, (ROOT,)),
            _type(RIGHT, (ROOT,)),
            # The redundant direct edge is legal and must not duplicate inventory.
            _type(LEAF, (LEFT, RIGHT, ROOT)),
            _type(EMPTY),
        ),
        objects
        if objects is not None
        else (
            _object("syn://cog/alpha", LEFT),
            _object("syn://thing/a", ROOT),
            _object("syn://thing/a-1", LEAF),
            _object("syn://thing/a_1", RIGHT),
            _object("syn://thing/aa", LEFT),
            _object("syn://thing/z", LEAF),
        ),
    )


def _service(app_env: Any) -> SynService:
    return SynService(
        app_env.db, ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    )


def _import(
    app_env: Any,
    service: SynService,
    snapshot: SynSnapshot | None = None,
    namespace: str = "synthetic",
) -> tuple[UUID, dict[str, Any]]:
    result = service.import_snapshot(
        app_env.context, app_env.repository_id, namespace, snapshot or _snapshot()
    )
    return UUID(result["scope"]["snapshot_id"]), result


def _counts(app_env: Any) -> dict[str, int]:
    with app_env.db.transaction(app_env.context) as connection:
        return {
            table: connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                *WORKSPACE_TABLES,
                "artifacts",
                "embedding_sources",
                "embedding_source_heads",
            )
        }


def _refs(result: dict[str, Any]) -> list[str]:
    return [item["ref"] for item in result["items"]]


def test_import_is_idempotent_and_preserves_lossless_public_bytes(app_env: Any) -> None:
    service = _service(app_env)
    raw = (
        '{ "id" : "syn://thing/precision", "typeRef" : "syn://type/test-leaf",'
        ' "name" : "café", "large":9007199254740993123456789,'
        ' "decimal":0.1000000000000000000000000001, "exponent":1.2300e+45,'
        ' "negativeZero":-0, "enabled":true, "escaped":"\\u0041", "escapedNul":"\\u0000" }\n'
    )
    obj = SynObject(SynRef("syn://thing/precision"), LEAF, raw)
    snapshot = _snapshot(objects=(obj,))
    identity, imported = _import(app_env, service, snapshot)
    before = _counts(app_env)
    assert (
        service.import_snapshot(app_env.context, app_env.repository_id, "synthetic", snapshot)
        == imported
    )
    assert _counts(app_env) == before
    assert imported["snapshot"]["source_hash"] == snapshot.fingerprint
    assert imported["type_count"] == len(snapshot.types) + len(extension_types())
    assert imported["object_count"] == 1
    assert imported["coverage"] == "imported_snapshot_only"

    effective = service.artifacts.read(app_env.context, UUID(imported["snapshot"]["artifact_id"]))
    assert hashlib.sha256(effective).hexdigest() == imported["snapshot"]["content_hash"]
    assert json.loads(effective)["source"] == snapshot.to_dict()
    schema_path = Path(__file__).resolve().parents[2] / "vocab/syn-effective-snapshot.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text())).validate(json.loads(effective))
    resolved = service.resolve(app_env.context, identity, str(obj.ref))
    assert resolved["payload_json"] == raw
    assert resolved["artifact_digest"] == hashlib.sha256(raw.encode()).hexdigest()
    assert resolved["byte_length"] == len(raw.encode())
    assert service.artifacts.read(app_env.context, UUID(resolved["artifact_id"])) == raw.encode()
    assert resolved["semantic_type_ref"] == str(LEAF)
    assert resolved["primary_data_type"] == "dt:syn.object_record"
    assert resolved["semantic_assertion_origin"] == "source_report"
    assert resolved["named_embeddings"] == {}
    with app_env.db.transaction(app_env.context) as connection:
        source = connection.execute(
            "SELECT source_key,metadata,artifact_id FROM embedding_sources WHERE id=%s",
            (UUID(resolved["source_id"]),),
        ).fetchone()
        assert source["source_key"] == (
            f"syn:{imported['scope']['namespace_id']}:{identity}:{obj.ref}"
        )
        assert source["metadata"]["source_url"] == obj.ref
        assert source["metadata"]["revision"] == imported["snapshot"]["content_hash"]
        assert str(source["artifact_id"]) == resolved["artifact_id"]


def test_mid_import_artifact_failure_leaves_no_visible_namespace_or_members(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(app_env)
    backend = service.artifacts.backend
    original = backend.publish
    calls = 0

    def failing_publish(workspace_id: UUID, data: bytes) -> Any:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise ArtifactIntegrityError("synthetic publication failure")
        return original(workspace_id, data)

    monkeypatch.setattr(backend, "publish", failing_publish)
    with pytest.raises(ArtifactIntegrityError, match="synthetic publication failure"):
        _import(app_env, service)
    assert calls == 4
    assert all(count == 0 for count in _counts(app_env).values())
    # Backend bytes can remain unreferenced, as in normal ArtifactStore rollback;
    # no SQL record or namespace makes a partial import accessible.
    monkeypatch.setattr(backend, "publish", original)
    identity, _ = _import(app_env, service)
    assert service.list_objects(app_env.context, identity)["object_count"] == 6


def test_missing_assigned_type_is_rejected_before_any_publication(app_env: Any) -> None:
    service = _service(app_env)
    missing = _object("syn://thing/missing", SynRef("syn://type/absent"))
    with pytest.raises(InvalidInputError, match="missing SYN type"):
        _import(app_env, service, _snapshot(objects=(missing,)))
    assert all(count == 0 for count in _counts(app_env).values())


@pytest.mark.parametrize("namespace", ("invalid\x00namespace", "invalid\ud800namespace"))
def test_namespace_rejects_non_postgres_text_before_database_writes(
    app_env: Any, namespace: str
) -> None:
    service = _service(app_env)
    with pytest.raises(InvalidInputError, match="namespace"):
        _import(app_env, service, namespace=namespace)
    assert all(count == 0 for count in _counts(app_env).values())


def test_changed_effective_catalog_does_not_reuse_the_original_snapshot(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(app_env)
    snapshot = _snapshot(objects=())
    first_id, first = _import(app_env, service, snapshot)
    original = service_module.merge_types
    integer_alias = data_type_ref("dt:value.integer")

    def changed_catalog(value: SynSnapshot) -> tuple[SynType, ...]:
        return tuple(
            replace(record, definition="Synthetic later catalog definition")
            if record.ref == integer_alias
            else record
            for record in original(value)
        )

    monkeypatch.setattr(service_module, "merge_types", changed_catalog)
    later_id, later = _import(app_env, service, snapshot)
    assert first_id != later_id
    assert first["snapshot"]["source_hash"] == later["snapshot"]["source_hash"]
    assert first["snapshot"]["catalog_hash"] != later["snapshot"]["catalog_hash"]
    assert first["snapshot"]["content_hash"] != later["snapshot"]["content_hash"]
    assert (
        service.resolve(app_env.context, first_id, str(integer_alias))["payload_json"]
        != (service.resolve(app_env.context, later_id, str(integer_alias))["payload_json"])
    )


def test_diamond_inventory_is_unique_exhaustive_and_stable_across_pages(app_env: Any) -> None:
    service = _service(app_env)
    snapshot = _snapshot()
    identity, _ = _import(app_env, service, snapshot)
    exact = service.list_objects(app_env.context, identity, type_ref=str(ROOT))
    assert _refs(exact) == ["syn://thing/a"]
    assert exact["complete"] and exact["next_cursor"] is None
    options = {"type_ref": str(ROOT), "include_subtypes": True, "page_size": 2}
    first = service.list_objects(app_env.context, identity, **options)
    expected = sorted(str(obj.ref) for obj in snapshot.objects)
    assert _refs(first) == expected[:2]
    assert first["total_count"] == 6 and first["delivered_count"] == 2
    assert not first["complete"]
    later_snapshot = _snapshot(
        label="newer immutable snapshot",
        objects=(*snapshot.objects, _object("syn://cog/aaa", ROOT)),
    )
    later_id, _ = _import(app_env, service, later_snapshot)
    assert later_id != identity

    cursor = UUID(first["next_cursor"])
    second = service.list_objects(app_env.context, identity, cursor=cursor, **options)
    replay = service.list_objects(app_env.context, identity, cursor=cursor, **options)
    assert second["items"] == replay["items"]
    assert second["delivered_count"] == replay["delivered_count"] == 4
    third = service.list_objects(
        app_env.context, identity, cursor=UUID(second["next_cursor"]), **options
    )
    assert _refs(first) + _refs(second) + _refs(third) == expected
    assert len(set(expected)) == 6
    assert third["total_count"] == third["delivered_count"] == 6
    assert third["complete"] and third["next_cursor"] is None
    assert service.list_objects(app_env.context, later_id)["total_count"] == 7


def test_cursor_binds_snapshot_query_page_size_and_authenticated_principal(app_env: Any) -> None:
    service = _service(app_env)
    identity, _ = _import(app_env, service)
    later_id, _ = _import(app_env, service, _snapshot(label="later"))
    options = {"type_ref": str(ROOT), "include_subtypes": True, "page_size": 2}
    first = service.list_objects(app_env.context, identity, **options)
    cursor = UUID(first["next_cursor"])
    for target_snapshot, changes in (
        (later_id, {}),
        (identity, {"type_ref": str(LEFT)}),
        (identity, {"include_subtypes": False}),
        (identity, {"page_size": 1}),
        (identity, {"primary_data_type": "dt:syn.object_record"}),
    ):
        with pytest.raises(ScopeDeniedError, match="cursor"):
            service.list_objects(
                app_env.context, target_snapshot, cursor=cursor, **(options | changes)
            )
    with app_env.other_db.transaction(app_env.other_context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    shared_context = provision_principal(
        app_env.admin_dsn, app_env.schema, role, app_env.context.workspace_id
    )
    other_service = SynService(
        app_env.other_db, ArtifactStore(app_env.other_db, service.artifacts.backend)
    )
    assert other_service.list_objects(shared_context, identity)["total_count"] == 6
    with pytest.raises(ScopeDeniedError, match="cursor"):
        other_service.list_objects(shared_context, identity, cursor=cursor, **options)
    with pytest.raises(ScopeDeniedError):
        service.list_objects(app_env.context, identity, cursor=uuid4(), **options)


def test_representation_aliases_are_canonical_and_do_not_retype_semantic_assertions(
    app_env: Any,
) -> None:
    service = _service(app_env)
    integer_alias = data_type_ref("dt:value.integer")
    obj = _object("syn://thing/asserted-integer", integer_alias)
    identity, _ = _import(app_env, service, _snapshot(objects=(_snapshot().objects[0], obj)))
    first = service.list_objects(
        app_env.context, identity, primary_data_type="dt:syn.object_record", page_size=1
    )
    second = service.list_objects(
        app_env.context,
        identity,
        primary_data_type=str(data_type_ref("dt:syn.object_record")),
        page_size=1,
        cursor=UUID(first["next_cursor"]),
    )
    assert second["complete"] and second["total_count"] == 2
    assert first["query"] == second["query"]
    assert (
        service.list_objects(app_env.context, identity, primary_data_type="dt:record")[
            "total_count"
        ]
        == 0
    )
    assert (
        service.list_objects(
            app_env.context, identity, primary_data_type="dt:record", include_subtypes=True
        )["total_count"]
        == 2
    )
    assert (
        service.list_objects(
            app_env.context, identity, primary_data_type="dt:value.integer", include_subtypes=True
        )["total_count"]
        == 0
    )
    native = service.list_objects(app_env.context, identity, type_ref=str(integer_alias))
    assert _refs(native) == [str(obj.ref)]
    assert native["items"][0]["primary_data_type"] == "dt:syn.object_record"

    definition = service.resolve(app_env.context, identity, str(integer_alias))
    assert definition["kind"] == "type"
    assert definition["semantic_type_ref"] is None
    assert definition["semantic_assertion_origin"] is None
    assert definition["primary_data_type"] == "dt:syn.type_definition"
    assert (
        service.list_objects(app_env.context, identity, primary_data_type="dt:syn.type_definition")[
            "total_count"
        ]
        == 0
    )


def test_equivalent_type_schemes_share_records_sources_artifacts_and_named_vectors(
    app_env: Any,
) -> None:
    embeddings, provider, episode = setup_service(app_env)
    service = SynService(app_env.db, embeddings.artifacts)
    context = app_env.context
    identity, _ = _import(app_env, service)
    space = embeddings.register_space(context, embedding_name="type_text")
    protected = str(data_type_ref("dt:value.integer"))
    assert protected == "synny://type/potbot-data-value-integer"
    pairs = (
        (str(LEFT), "synny://type/test-left"),
        (protected, "syn://type/potbot-data-value-integer"),
    )
    for canonical, alternate in pairs:
        indexed = service.index(
            context,
            identity,
            alternate,
            embeddings,
            episode,
            space["id"],
            embedding_name="type_text",
        )
        repeated = service.index(
            context,
            identity,
            canonical,
            embeddings,
            episode,
            space["id"],
            embedding_name="type_text",
        )
        assert indexed == repeated and indexed["ref"] == canonical
        resolved = service.resolve(context, identity, canonical)
        alias_resolved = service.resolve(context, identity, alternate)
        assert resolved["requested_ref"] == canonical
        assert alias_resolved["requested_ref"] == alternate
        assert {key: value for key, value in resolved.items() if key != "requested_ref"} == {
            key: value for key, value in alias_resolved.items() if key != "requested_ref"
        }
        assert resolved["ref"] == canonical
        assert resolved["source_id"] == indexed["source_id"]
        assert resolved["artifact_id"] == indexed["artifact_id"]
        assert len(resolved["named_embeddings"]["type_text"]) == 1
        assert service.catalog(context, identity, type_ref=canonical) == service.catalog(
            context, identity, type_ref=alternate
        )
    assert provider.calls == 2


def test_equivalent_type_filter_spellings_can_continue_the_same_cursor(app_env: Any) -> None:
    service = _service(app_env)
    identity, _ = _import(app_env, service)
    options = {
        "type_ref": str(ROOT),
        "primary_data_type": str(data_type_ref("dt:syn.object_record")),
        "include_subtypes": True,
        "page_size": 2,
    }
    first = service.list_objects(app_env.context, identity, **options)
    cursor = UUID(first["next_cursor"])
    canonical = service.list_objects(app_env.context, identity, cursor=cursor, **options)
    equivalent = service.list_objects(
        app_env.context,
        identity,
        cursor=cursor,
        **(
            options
            | {
                "type_ref": "synny://type/test-root",
                "primary_data_type": "syn://type/potbot-data-syn-object-record",
            }
        ),
    )
    assert equivalent["query"] == canonical["query"] == first["query"]
    assert equivalent["items"] == canonical["items"]
    assert equivalent["total_count"] == canonical["total_count"] == 6
    assert equivalent["delivered_count"] == canonical["delivered_count"] == 4


def test_import_canonicalizes_type_identity_without_rewriting_public_payload(app_env: Any) -> None:
    service = _service(app_env)
    wire = _snapshot(objects=()).to_dict()
    for record in wire["types"]:
        if record["ref"] == str(LEFT):
            record["ref"] = "synny://type/test-left"
            record["parents"] = ["synny://type/test-root"]
    native_alias = "synny://type/test-left"
    protected_alias = "syn://type/potbot-data-value-integer"
    for slug, type_ref in (("native-alias", native_alias), ("protected-alias", protected_alias)):
        ref = f"syn://thing/{slug}"
        wire["objects"].append(
            {
                "ref": ref,
                "type_ref": type_ref,
                "payload_json": canonical_json({"id": ref, "typeRef": type_ref, "name": slug}),
            }
        )
    snapshot = SynSnapshot.from_dict(wire)
    identity, _ = _import(app_env, service, snapshot)
    expected_types = (str(LEFT), str(data_type_ref("dt:value.integer")))
    for record, expected in zip(snapshot.objects, expected_types, strict=True):
        resolved = service.resolve(app_env.context, identity, str(record.ref))
        assert resolved["semantic_type_ref"] == expected
        assert resolved["payload_json"] == record.payload_json
        assert json.loads(resolved["payload_json"])["typeRef"] == str(record.type_ref)
        assert _refs(
            service.list_objects(app_env.context, identity, type_ref=str(record.type_ref))
        ) == [str(record.ref)]
    catalog = service.catalog(app_env.context, identity, type_ref=native_alias)
    assert catalog["types"][0]["ref"] == str(LEFT)
    assert catalog["types"][0]["parents"] == [str(ROOT)]


def test_namespace_repository_and_workspace_identity_do_not_drift(app_env: Any) -> None:
    service = _service(app_env)
    obj = _object("syn://thing/shared", LEFT, value="first namespace")
    first_id, first = _import(app_env, service, _snapshot(objects=(obj,)))
    other_repository = RepositoryStore(app_env.db).create(
        app_env.context, name="second repository", source_uri="fixture://other-repository"
    )
    with pytest.raises(RevisionConflictError, match="another repository"):
        service.import_snapshot(
            app_env.context, other_repository["id"], "synthetic", _snapshot(objects=(obj,))
        )
    changed = _object(str(obj.ref), RIGHT, value="second namespace")
    second = service.import_snapshot(
        app_env.context, other_repository["id"], "other", _snapshot(objects=(changed,))
    )
    second_id = UUID(second["scope"]["snapshot_id"])
    assert first["scope"]["namespace_id"] != second["scope"]["namespace_id"]
    assert (
        service.resolve(app_env.context, first_id, str(obj.ref))["payload_json"] == obj.payload_json
    )
    assert service.resolve(app_env.context, second_id, str(obj.ref))["payload_json"] == (
        changed.payload_json
    )
    with pytest.raises(ScopeDeniedError):
        service.import_snapshot(
            app_env.context, app_env.other_repository_id, "foreign", _snapshot()
        )
    other_service = SynService(
        app_env.other_db,
        ArtifactStore(app_env.other_db, LocalArtifactBackend(app_env.artifact_root)),
    )
    foreign = other_service.import_snapshot(
        app_env.other_context, app_env.other_repository_id, "synthetic", _snapshot(objects=(obj,))
    )
    for operation in (
        lambda: other_service.resolve(app_env.other_context, first_id, str(obj.ref)),
        lambda: other_service.list_objects(app_env.other_context, first_id),
        lambda: service.resolve(
            app_env.context, UUID(foreign["scope"]["snapshot_id"]), str(obj.ref)
        ),
    ):
        with pytest.raises(ScopeDeniedError):
            operation()


class RankedProvider(NamedViewProvider):
    """An excluded record scores above an eligible one, exposing post-limit filters."""

    def encode(self, texts: tuple[str, ...], kind: Literal["query", "document"]) -> EmbeddingBatch:
        result = super().encode(texts, kind)
        near = (1.0, *(0.0 for _ in range(1023)))
        far = (0.0, 1.0, *(0.0 for _ in range(1022)))
        return replace(
            result,
            vectors=tuple(
                far if kind == "document" and '"rank":"far"' in text else near for text in texts
            ),
        )


def test_named_search_filters_snapshot_and_exact_type_before_vector_ranking(app_env: Any) -> None:
    embeddings, provider, episode = setup_service(app_env, RankedProvider())
    service = SynService(app_env.db, embeddings.artifacts)
    context = app_env.context
    space = embeddings.register_space(context, embedding_name="semantic_text")
    embeddings.register_space(context)
    allowed = _object("syn://thing/allowed", LEFT, rank="far")
    excluded = _object("syn://thing/excluded", RIGHT, rank="near")
    snapshot = _snapshot(objects=(allowed, excluded))
    identity, _ = _import(app_env, service, snapshot)
    later_id, _ = _import(app_env, service, replace(snapshot, source_label="another snapshot"))
    selected = service.index(
        context,
        identity,
        str(allowed.ref),
        embeddings,
        episode,
        space["id"],
        embedding_name="semantic_text",
    )
    for target, ref in ((identity, excluded.ref), (later_id, allowed.ref)):
        service.index(
            context,
            target,
            str(ref),
            embeddings,
            episode,
            space["id"],
            embedding_name="semantic_text",
        )
    service.index(context, identity, str(allowed.ref), embeddings, episode, space["id"])
    unscoped = embeddings.search(
        context,
        episode,
        app_env.repository_id,
        space["id"],
        "find",
        embedding_name="semantic_text",
        limit=1,
    )
    assert unscoped[0]["metadata"]["source_url"] == str(excluded.ref)
    result = service.search(
        context,
        identity,
        "find",
        embeddings,
        episode,
        space["id"],
        embedding_name="semantic_text",
        limit=1,
        type_ref=str(LEFT),
    )
    assert result["candidate_count"] == 1
    assert _refs(result) == [str(allowed.ref)]
    assert result["items"][0]["embedding"]["vector_id"] == selected["embedding"]["id"]
    assert result["items"][0]["embedding"]["embedding_name"] == "semantic_text"
    assert result["items"][0]["source_id"] == selected["source_id"]
    assert result["embedding_name"] == "semantic_text" and result["exhaustive"] is False
    assert provider.calls == 6


def test_shared_bytes_keep_source_scoped_manifests_and_retirement_preserves_inventory(
    app_env: Any,
) -> None:
    embeddings, _, episode = setup_service(app_env)
    service = SynService(app_env.db, embeddings.artifacts)
    context = app_env.context
    space = embeddings.register_space(context, embedding_name="semantic_text")
    obj = _object("syn://thing/shared-bytes", LEAF)
    snapshot = _snapshot(objects=(obj,))
    first_id, first = _import(app_env, service, snapshot)
    second_id, _ = _import(app_env, service, replace(snapshot, source_label="second snapshot"))
    indexed = [
        service.index(
            context,
            identity,
            str(obj.ref),
            embeddings,
            episode,
            space["id"],
            embedding_name="semantic_text",
        )
        for identity in (first_id, second_id)
    ]
    assert indexed[0]["artifact_id"] == indexed[1]["artifact_id"]
    assert indexed[0]["source_id"] != indexed[1]["source_id"]
    artifact_manifest = embeddings.artifacts.embedding_manifest(
        context, UUID(indexed[0]["artifact_id"])
    )
    assert len(artifact_manifest["named_embeddings"]["semantic_text"]) == 2
    for identity, expected in zip((first_id, second_id), indexed, strict=True):
        resolved = service.resolve(context, identity, str(obj.ref))
        references = resolved["named_embeddings"]["semantic_text"]
        assert len(references) == 1 and references[0]["source_id"] == expected["source_id"]
        assert references[0]["encoding_input_hash"] == resolved["artifact_digest"]
    before = service.list_objects(context, first_id)
    embeddings.retire_source(
        context,
        app_env.repository_id,
        f"syn:{first['scope']['namespace_id']}:{first_id}:{obj.ref}",
    )
    after = service.list_objects(context, first_id)
    assert before == after
    resolved_first = service.resolve(context, first_id, str(obj.ref))
    assert resolved_first["payload_json"] == obj.payload_json
    assert resolved_first["named_embeddings"] == {}
    assert service.resolve(context, second_id, str(obj.ref))["named_embeddings"]["semantic_text"]
    assert (
        service.search(
            context,
            first_id,
            "find",
            embeddings,
            episode,
            space["id"],
            embedding_name="semantic_text",
        )["items"]
        == []
    )


def test_empty_exact_candidates_do_not_dispatch_or_charge_query_embedding(app_env: Any) -> None:
    embeddings, provider, episode = setup_service(app_env)
    service = SynService(app_env.db, embeddings.artifacts)
    context = app_env.context
    identity, _ = _import(app_env, service)
    space = embeddings.register_space(context, embedding_name="semantic_text")
    before = BudgetLedger(app_env.db).inspect(context, episode)
    for filters in ({"type_ref": str(EMPTY)}, {"primary_data_type": "dt:value.integer"}):
        result = service.search(
            context,
            identity,
            "find",
            embeddings,
            episode,
            space["id"],
            embedding_name="semantic_text",
            **filters,
        )
        assert result["items"] == [] and result["candidate_count"] == 0
    assert provider.calls == 0
    assert BudgetLedger(app_env.db).inspect(context, episode) == before


def test_type_definition_vectors_are_separate_from_object_inventory(app_env: Any) -> None:
    embeddings, _, episode = setup_service(app_env)
    service = SynService(app_env.db, embeddings.artifacts)
    context = app_env.context
    identity, imported = _import(app_env, service)
    space = embeddings.register_space(context, embedding_name="type_text")
    indexed = service.index(
        context, identity, str(LEFT), embeddings, episode, space["id"], embedding_name="type_text"
    )
    assert indexed["primary_data_type"] == "dt:syn.type_definition"
    assert indexed["semantic_type_ref"] is None
    result = service.search(
        context,
        identity,
        "find a type",
        embeddings,
        episode,
        space["id"],
        embedding_name="type_text",
        record_kind="types",
    )
    assert result["candidate_count"] == imported["type_count"]
    assert _refs(result) == [str(LEFT)]
    assert result["items"][0]["semantic_type_ref"] is None
    assert (
        service.search(
            context,
            identity,
            "find an object",
            embeddings,
            episode,
            space["id"],
            embedding_name="type_text",
        )["items"]
        == []
    )
    with pytest.raises(InvalidInputError, match="instance type filters"):
        service.search(
            context,
            identity,
            "find",
            embeddings,
            episode,
            space["id"],
            embedding_name="type_text",
            record_kind="types",
            type_ref=str(ROOT),
        )


def test_snapshot_repository_and_named_view_must_match_before_dispatch(app_env: Any) -> None:
    embeddings, provider, episode = setup_service(app_env)
    service = SynService(app_env.db, embeddings.artifacts)
    context = app_env.context
    space = embeddings.register_space(context)
    obj = _object("syn://thing/foreign-repository", ROOT)
    other_repository = RepositoryStore(app_env.db).create(
        context,
        name="other embedding repository",
        source_uri="fixture://other-embedding-repository",
    )
    imported = service.import_snapshot(
        context, other_repository["id"], "other", _snapshot(objects=(obj,))
    )
    identity = UUID(imported["scope"]["snapshot_id"])
    for operation in (
        lambda: service.index(context, identity, str(obj.ref), embeddings, episode, space["id"]),
        lambda: service.search(context, identity, "find", embeddings, episode, space["id"]),
    ):
        with pytest.raises(ScopeDeniedError):
            operation()
    own_id, _ = _import(app_env, service, _snapshot(objects=(obj,)))
    with pytest.raises(ScopeDeniedError, match="named embedding"):
        service.index(
            context,
            own_id,
            str(obj.ref),
            embeddings,
            episode,
            space["id"],
            embedding_name="unregistered",
        )
    assert provider.calls == 0
    assert BudgetLedger(app_env.db).inspect(context, episode)["used"].get("calls", 0) == 0


def test_unknown_references_and_invalid_filters_never_create_placeholders(app_env: Any) -> None:
    service = _service(app_env)
    identity, _ = _import(app_env, service)
    before = _counts(app_env)
    for ref in ("syn://thing/absent", "syn://type/absent"):
        with pytest.raises(ScopeDeniedError, match="reference"):
            service.resolve(app_env.context, identity, ref)
    for options in (
        {"type_ref": "syn://type/absent"},
        {"type_ref": "syn://thing/a"},
        {"primary_data_type": "dt:unknown"},
        {"page_size": True},
        {"page_size": 0},
        {"page_size": 1001},
        {"include_subtypes": 1},
        {"cursor": "not-an-opaque-uuid"},
    ):
        with pytest.raises(InvalidInputError):
            service.list_objects(app_env.context, identity, **options)
    assert _counts(app_env) == before


def test_actual_database_catalog_retains_every_native_definition_and_parent_edge(
    app_env: Any,
) -> None:
    # This committed types-only fixture was independently checked against the
    # original dump's COPY ref/name/edge sets. Tests never load or execute that
    # dump, and do not require any of its object or credential records.
    fixture = Path(__file__).resolve().parents[2] / "vocab/syn-database-types.snapshot.json"
    snapshot = SynSnapshot.from_json(fixture.read_text(encoding="utf-8"))
    assert snapshot.catalog_mode == "complete" and snapshot.objects == ()
    assert len(snapshot.types) == 529
    assert sum(len(record.parents) for record in snapshot.types) == 859
    assert sum(len(record.properties) for record in snapshot.types) == 0
    expected = {
        str(record.ref): record.to_dict() for record in (*snapshot.types, *extension_types())
    }
    assert len(expected) == 576
    assert expected["syn://type/computational-procedure"]["parents"] == [
        "syn://type/computational-method"
    ]
    service = _service(app_env)
    identity, imported = _import(app_env, service, snapshot, namespace="actual-database-types")
    assert imported["type_count"] == 576 and imported["object_count"] == 0
    catalog = service.catalog(app_env.context, identity)
    assert catalog["selected_count"] == 576
    assert [row["ref"] for row in catalog["types"]] == sorted(expected)
    assert {row["ref"]: row for row in catalog["types"]} == expected
    summary = service.catalog(app_env.context, identity, summary=True)
    assert summary["selected_count"] == 576 and "types" not in summary

    expected_edges = {
        (str(record.ref), str(parent))
        for record in (*snapshot.types, *extension_types())
        for parent in record.parents
    }
    with app_env.db.transaction(app_env.context) as connection:
        rows = connection.execute(
            "SELECT child_ref,parent_ref FROM syn_type_parents "
            "WHERE workspace_id=%s AND snapshot_id=%s",
            (app_env.context.workspace_id, identity),
        ).fetchall()
        assert {(row["child_ref"], row["parent_ref"]) for row in rows} == expected_edges
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM syn_type_ancestry "
                "WHERE workspace_id=%s AND snapshot_id=%s "
                "AND descendant_ref LIKE 'syn://type/%%'",
                (app_env.context.workspace_id, identity),
            ).fetchone()["n"]
            == 3377
        )
    representatives = (
        "syn://type/thing",
        "syn://type/academic-feed-and-api-feed-and-atom-feed-and-repository-feed",
        "syn://type/computational-procedure",
    )
    for ref in representatives:
        resolved = service.resolve(app_env.context, identity, ref)
        assert resolved["kind"] == "type" and resolved["semantic_type_ref"] is None
        assert json.loads(resolved["payload_json"]) == expected[ref]
        selected = service.catalog(app_env.context, identity, type_ref=ref)
        assert selected["selected_count"] == 1 and selected["types"] == [expected[ref]]
    inventory = service.list_objects(app_env.context, identity)
    assert inventory["items"] == [] and inventory["total_count"] == 0
    assert inventory["complete"] and inventory["next_cursor"] is None
