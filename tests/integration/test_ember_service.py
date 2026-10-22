"""Scoped durable Ember views, concurrent replay and the actual owner RLS boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, SynType
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import admin_connection, provision_principal
from potbot.semantics.catalog import merge_types
from potbot.semantics.ember_service import EmberContractService
from potbot.semantics.embers import (
    EmberBinding,
    EmberContractBundle,
    EmberInstance,
    EmberRevision,
    EmberSlot,
    TypeEmberContract,
    build_bundle,
)
from potbot.semantics.service import SynService
from tests.integration.conftest import AppEnvironment

pytestmark = pytest.mark.postgres

SUBJECT = "syn://cog/fixture-lock"
BASE = "syn://type/fixture-cog"
CHILD = "syn://type/fixture-special-cog"
PROPERTY = "syn://type/fixture-property-ember"
SPECIAL_PROPERTY = "syn://type/fixture-special-property-ember"
RECORD = "syn://type/fixture-record"
HANDLE = "syn://type/fixture-operation-handle"
CODE_SLOT = "syn://thing/fixture-implementation"
KEY_SLOT = "syn://thing/fixture-verification-key"
HANDLE_SLOT = "syn://thing/fixture-signing-operation"
VERIFICATION_KEY = "42" * 32
PRIVATE_HANDLE = "secret://fixture/owner-only-signing-operation"


@dataclass(frozen=True)
class Rig:
    service: EmberContractService
    snapshot_id: UUID
    snapshot: SynSnapshot
    bundle: EmberContractBundle
    types: tuple[SynType, ...]


def _prepare(app_env: AppEnvironment) -> Rig:
    types = tuple(
        SynType(SynRef(ref), ref.rsplit("/", 1)[-1], tuple(SynRef(p) for p in parents), None, ())
        for ref, parents in (
            (BASE, ()),
            (CHILD, (BASE,)),
            (PROPERTY, ()),
            (SPECIAL_PROPERTY, (PROPERTY,)),
            (RECORD, ()),
            (HANDLE, ()),
        )
    )
    snapshot = SynSnapshot(
        "Ember contract integration fixture",
        "complete",
        types,
        (
            SynObject(
                SynRef(SUBJECT),
                SynRef(CHILD),
                canonical_json({"id": SUBJECT, "typeRef": CHILD, "name": "Fixture lock"}),
            ),
        ),
    )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    result = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "ember-test", snapshot
    )
    snapshot_id = UUID(result["scope"]["snapshot_id"])
    types = merge_types(snapshot)
    code = EmberRevision.create(
        "syn://ember/fixture-implementation",
        PROPERTY,
        RECORD,
        {"algorithm": "Ed25519", "implementation_digest": "a" * 64},
    )
    key = EmberRevision.create(
        "syn://ember/fixture-verification-key",
        SPECIAL_PROPERTY,
        RECORD,
        {"algorithm": "Ed25519", "verification_key": VERIFICATION_KEY},
    )
    handle = EmberRevision.create(
        "syn://ember/fixture-private-operation",
        PROPERTY,
        HANDLE,
        PRIVATE_HANDLE,
        value_kind="private_handle",
        visibility="private",
    )
    contract = TypeEmberContract.create(
        BASE,
        (
            EmberSlot(CODE_SLOT, "Implementation", PROPERTY, RECORD, 1, 1, override="fixed"),
            EmberSlot(KEY_SLOT, "Verification key", PROPERTY, RECORD, 1, 1, source="instance"),
            EmberSlot(
                HANDLE_SLOT,
                "Signing operation",
                PROPERTY,
                HANDLE,
                1,
                1,
                source="instance",
                visibility="private",
            ),
        ),
        (EmberBinding(CODE_SLOT, (code.pointer,)),),
    )
    instance = EmberInstance.create(
        SUBJECT,
        CHILD,
        (EmberBinding(KEY_SLOT, (key.pointer,)), EmberBinding(HANDLE_SLOT, (handle.pointer,))),
    )
    bundle = build_bundle(
        types,
        workspace_id=app_env.context.workspace_id,
        namespace="ember-test",
        snapshot_id=snapshot_id,
        type_contracts=(contract,),
        embers=(code, key, handle),
        instances=(instance,),
    )
    return Rig(EmberContractService(app_env.db), snapshot_id, snapshot, bundle, types)


def _counts(app_env: AppEnvironment) -> dict[str, int]:
    with app_env.db.transaction(app_env.context) as connection:
        return {
            table: connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "syn_ember_public_bundles",
                "syn_ember_owner_bundles",
                "artifacts",
                "embedding_sources",
                "embedding_source_heads",
                "command_results",
            )
        }


def test_owner_and_public_views_are_separate_immutable_records(app_env: AppEnvironment) -> None:
    rig = _prepare(app_env)
    before = _counts(app_env)
    imported = rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle)
    assert rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle) == imported
    bundle_id = UUID(imported["bundle_id"])
    public, _ = rig.service.read_bundle(app_env.context, bundle_id)
    owner, _ = rig.service.read_bundle(app_env.context, bundle_id, projection="owner")
    assert owner.to_dict() == rig.bundle.to_dict()
    assert public == owner.public_projection(rig.types)
    assert imported["public_hash"] == public.fingerprint != owner.fingerprint
    text = public.to_json()
    assert VERIFICATION_KEY in text
    assert PRIVATE_HANDLE not in text
    assert owner.fingerprint not in text
    for private in (ember for ember in owner.embers if ember.visibility == "private"):
        assert private.ref not in text
        assert private.revision not in text
    resolved = rig.service.resolve(app_env.context, bundle_id, SUBJECT)
    assert resolved["conformance"] == "public_only"
    assert HANDLE_SLOT not in json.dumps(resolved)
    complete = rig.service.resolve(app_env.context, bundle_id, SUBJECT, projection="owner")
    assert complete["conformance"] == "complete"
    assert PRIVATE_HANDLE in json.dumps(complete)
    after = _counts(app_env)
    assert after == {
        **before,
        "syn_ember_public_bundles": 1,
        "syn_ember_owner_bundles": 1,
    }


def test_same_private_document_concurrently_imports_once(app_env: AppEnvironment) -> None:
    rig = _prepare(app_env)
    barrier = Barrier(2)

    def publish(_: int) -> dict[str, object]:
        barrier.wait(timeout=10)
        return rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(publish, range(2)))
    assert results[0] == results[1]
    counts = _counts(app_env)
    assert counts["syn_ember_public_bundles"] == counts["syn_ember_owner_bundles"] == 1


def test_hidden_changes_do_not_change_public_document_or_digest(app_env: AppEnvironment) -> None:
    rig = _prepare(app_env)
    original = rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle)
    prior_handle = next(e for e in rig.bundle.embers if e.visibility == "private")
    changed_handle = EmberRevision.create(
        "syn://ember/different-private-id",
        PROPERTY,
        HANDLE,
        "secret://fixture/replaced-operation",
        value_kind="private_handle",
        visibility="private",
    )
    changed = replace(
        rig.bundle,
        embers=tuple(e for e in rig.bundle.embers if e is not prior_handle) + (changed_handle,),
        instances=(
            EmberInstance.create(
                SUBJECT,
                CHILD,
                tuple(
                    EmberBinding(b.slot_ref, (changed_handle.pointer,))
                    if b.slot_ref == HANDLE_SLOT
                    else b
                    for b in rig.bundle.instances[0].bindings
                ),
            ),
        ),
    )
    updated = rig.service.import_bundle(app_env.context, rig.snapshot_id, changed)
    assert original["bundle_id"] != updated["bundle_id"]
    assert original["public_hash"] == updated["public_hash"]
    first, _ = rig.service.read_bundle(app_env.context, UUID(original["bundle_id"]))
    second, _ = rig.service.read_bundle(app_env.context, UUID(updated["bundle_id"]))
    assert first == second


def test_owner_rls_resists_same_workspace_reads_inserts_and_principal_spoofing(
    app_env: AppEnvironment,
) -> None:
    from psycopg.types.json import Jsonb

    rig = _prepare(app_env)
    imported = rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle)
    identity = UUID(imported["bundle_id"])
    other = EmberContractService(app_env.other_db)
    with pytest.raises(ScopeDeniedError):
        other.read_bundle(app_env.other_context, identity)
    with app_env.other_db.transaction(app_env.other_context) as connection:
        role = connection.execute("SELECT session_user AS role").fetchone()["role"]
    shared = provision_principal(
        app_env.admin_dsn, app_env.schema, role, app_env.context.workspace_id
    )
    public, _ = other.read_bundle(shared, identity)
    assert public.projection == "public"
    with pytest.raises(ScopeDeniedError):
        other.read_bundle(shared, identity, projection="owner")
    with pytest.raises(ScopeDeniedError):
        other.read_bundle(app_env.context, identity, projection="owner")
    with app_env.other_db.transaction(shared) as connection:
        assert connection.execute("SELECT * FROM syn_ember_owner_bundles").fetchall() == []
        connection.execute(
            "SELECT set_config('potbot.principal_id',%s,true)",
            (str(app_env.context.principal_id),),
        )
        assert connection.execute("SELECT * FROM syn_ember_owner_bundles").fetchall() == []
    with pytest.raises(ScopeDeniedError):
        with app_env.other_db.transaction(shared) as connection:
            connection.execute(
                "INSERT INTO syn_ember_owner_bundles "
                "(workspace_id,id,snapshot_id,owner_principal_id,owner_hash,document) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    shared.workspace_id,
                    identity,
                    rig.snapshot_id,
                    app_env.context.principal_id,
                    rig.bundle.fingerprint,
                    Jsonb(rig.bundle.to_dict()),
                ),
            )


def test_import_rejects_wrong_scope_subject_type_missing_requirements_and_referents(
    app_env: AppEnvironment,
) -> None:
    rig = _prepare(app_env)
    instance = rig.bundle.instances[0]
    missing_handle = EmberInstance.create(
        SUBJECT, CHILD, tuple(b for b in instance.bindings if b.slot_ref != HANDLE_SLOT)
    )
    missing_referent = EmberRevision.create(
        "syn://ember/missing-referent",
        PROPERTY,
        BASE,
        "syn://cog/absent",
        value_kind="object_ref",
    )
    wrong_referent = EmberRevision.create(
        "syn://ember/wrong-referent",
        PROPERTY,
        RECORD,
        SUBJECT,
        value_kind="object_ref",
    )
    variants = (
        (replace(rig.bundle, scope=replace(rig.bundle.scope, snapshot_id=uuid4())), "scope"),
        (replace(rig.bundle, scope=replace(rig.bundle.scope, namespace="other")), "scope"),
        (
            replace(
                rig.bundle,
                instances=(EmberInstance.create("syn://cog/absent", CHILD, instance.bindings),),
            ),
            "subject",
        ),
        (
            replace(
                rig.bundle, instances=(EmberInstance.create(SUBJECT, BASE, instance.bindings),)
            ),
            "subject",
        ),
        (replace(rig.bundle, instances=(missing_handle,)), "cardinality"),
        (replace(rig.bundle, embers=rig.bundle.embers + (missing_referent,)), "referent"),
        (replace(rig.bundle, embers=rig.bundle.embers + (wrong_referent,)), "referent Type"),
    )
    for value, message in variants:
        with pytest.raises(InvalidInputError, match=message):
            rig.service.import_bundle(app_env.context, rig.snapshot_id, value)
        assert _counts(app_env)["syn_ember_public_bundles"] == 0
    with pytest.raises(ScopeDeniedError):
        rig.service.import_bundle(app_env.context, uuid4(), rig.bundle)
    with pytest.raises(InvalidInputError, match="complete owner"):
        rig.service.import_bundle(
            app_env.context, rig.snapshot_id, rig.bundle.public_projection(rig.types)
        )
    # A represented object can have a narrower actual Type than its declared range.
    valid_reference = EmberRevision.create(
        "syn://ember/valid-subtype-referent", PROPERTY, BASE, SUBJECT, value_kind="object_ref"
    )
    valid = replace(rig.bundle, embers=rig.bundle.embers + (valid_reference,))
    assert rig.service.import_bundle(app_env.context, rig.snapshot_id, valid)["bundle_id"]


def test_contracts_without_instances_are_validated_before_any_write(
    app_env: AppEnvironment,
) -> None:
    rig = _prepare(app_env)
    changed = EmberRevision.create("syn://ember/incompatible-default", PROPERTY, RECORD, "changed")
    child = TypeEmberContract.create(CHILD, defaults=(EmberBinding(CODE_SLOT, (changed.pointer,)),))
    invalid = replace(
        rig.bundle,
        type_contracts=rig.bundle.type_contracts + (child,),
        embers=rig.bundle.embers + (changed,),
        instances=(),
    )
    with pytest.raises(InvalidInputError, match="fixed default"):
        rig.service.import_bundle(app_env.context, rig.snapshot_id, invalid)
    assert _counts(app_env)["syn_ember_owner_bundles"] == 0


def test_database_forces_scope_projection_and_immutability_and_read_checks_digest(
    app_env: AppEnvironment,
) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    rig = _prepare(app_env)
    imported = rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle)
    identity = UUID(imported["bundle_id"])
    public = rig.bundle.public_projection(rig.types)
    with admin_connection(app_env.admin_dsn) as connection:
        rows = connection.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('syn_ember_public_bundles','syn_ember_owner_bundles')"
        ).fetchall()
        assert len(rows) == 2
        assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows)
    for table in ("syn_ember_public_bundles", "syn_ember_owner_bundles"):
        with pytest.raises(psycopg.Error) as caught:
            with admin_connection(app_env.admin_dsn) as connection:
                connection.execute(f"DELETE FROM {table} WHERE id=%s", (identity,))
        assert caught.value.sqlstate == "55000"
    for document in ({}, rig.bundle.to_dict(), {**public.to_dict(), "scope": {}}):
        with pytest.raises(InvalidInputError):
            with app_env.db.transaction(app_env.context) as connection:
                connection.execute(
                    "INSERT INTO syn_ember_public_bundles "
                    "(workspace_id,id,snapshot_id,public_hash,document) VALUES(%s,%s,%s,%s,%s)",
                    (
                        app_env.context.workspace_id,
                        uuid4(),
                        rig.snapshot_id,
                        public.fingerprint,
                        Jsonb(document),
                    ),
                )
    corrupt_id = uuid4()
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute(
            "INSERT INTO syn_ember_public_bundles "
            "(workspace_id,id,snapshot_id,public_hash,document) VALUES(%s,%s,%s,%s,%s)",
            (
                app_env.context.workspace_id,
                corrupt_id,
                rig.snapshot_id,
                "0" * 64,
                Jsonb(public.to_dict()),
            ),
        )
    with pytest.raises(InvalidInputError, match="fingerprint"):
        rig.service.read_bundle(app_env.context, corrupt_id)


def test_cli_import_export_and_resolve_default_to_public(
    app_env: AppEnvironment,
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    from potbot.cli import main

    rig = _prepare(app_env)
    source = tmp_path / "owner.json"
    source.write_text(rig.bundle.to_json())
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)
    scope = [
        "--workspace-id",
        str(app_env.context.workspace_id),
        "--principal-id",
        str(app_env.context.principal_id),
    ]
    assert (
        main(
            [
                "syn",
                "embers",
                "import",
                *scope,
                "--snapshot-id",
                str(rig.snapshot_id),
                "--file",
                str(source),
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    bundle_arg = ["--bundle-id", imported["bundle_id"]]
    assert main(["syn", "embers", "export", *scope, *bundle_arg]) == 0
    public_text = capsys.readouterr().out
    public = EmberContractBundle.from_json(public_text)
    assert public.projection == "public"
    assert PRIVATE_HANDLE not in public_text
    assert VERIFICATION_KEY in public_text
    assert main(["syn", "embers", "resolve", *scope, *bundle_arg, "--ref", SUBJECT]) == 0
    assert json.loads(capsys.readouterr().out)["conformance"] == "public_only"
    assert main(["syn", "embers", "export", *scope, *bundle_arg, "--projection", "owner"]) == 0
    assert EmberContractBundle.from_json(capsys.readouterr().out).to_dict() == rig.bundle.to_dict()
    assert (
        main(
            [
                "syn",
                "embers",
                "list",
                *scope,
                *bundle_arg,
                "--type-ref",
                PROPERTY,
                "--include-subtypes",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["projection"] == "public"
    assert inventory["total_count"] == 2
    assert len(inventory["items"]) == 1
    assert PRIVATE_HANDLE not in json.dumps(inventory)


def test_exact_type_inventory_is_alias_aware_private_to_owner_and_bundle_scoped(
    app_env: AppEnvironment,
) -> None:
    rig = _prepare(app_env)
    imported = rig.service.import_bundle(app_env.context, rig.snapshot_id, rig.bundle)
    identity = UUID(imported["bundle_id"])
    public = rig.service.list_embers(app_env.context, identity, type_ref=PROPERTY)
    alias = rig.service.list_embers(
        app_env.context, identity, type_ref=PROPERTY.replace("syn://", "synny://")
    )
    assert alias == public
    assert public["coverage"] == "selected_bundle"
    assert public["projection"] == "public"
    assert public["total_count"] == 1
    assert PRIVATE_HANDLE not in json.dumps(public)
    owner = rig.service.list_embers(
        app_env.context, identity, type_ref=PROPERTY, projection="owner"
    )
    assert owner["total_count"] == 2
    assert PRIVATE_HANDLE in json.dumps(owner)
    descendants = rig.service.list_embers(
        app_env.context, identity, type_ref=PROPERTY, include_subtypes=True, projection="owner"
    )
    assert descendants["total_count"] == 3
    assert [(e["ref"], e["revision"]) for e in descendants["items"]] == sorted(
        (e["ref"], e["revision"]) for e in descendants["items"]
    )
    page = rig.service.list_embers(
        app_env.context,
        identity,
        type_ref=PROPERTY,
        include_subtypes=True,
        projection="owner",
        limit=1,
        offset=1,
    )
    assert page["items"] == descendants["items"][1:2]
    assert page["total_count"] == 3
    for parameters in ({"limit": True}, {"limit": 1001}, {"offset": -1}):
        with pytest.raises(InvalidInputError):
            rig.service.list_embers(app_env.context, identity, type_ref=PROPERTY, **parameters)
    with pytest.raises(ScopeDeniedError):
        EmberContractService(app_env.other_db).list_embers(
            app_env.other_context, identity, type_ref=PROPERTY, projection="owner"
        )
