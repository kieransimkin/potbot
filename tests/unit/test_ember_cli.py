"""Offline catalog-pinned validation does not initialize database or model adapters."""

import json
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cli import main
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, SynType
from potbot.semantics.catalog import merge_types
from potbot.semantics.embers import (
    MAX_BUNDLE_BYTES,
    EmberBinding,
    EmberInstance,
    EmberRevision,
    EmberSlot,
    TypeEmberContract,
    build_bundle,
)

BASE = "syn://type/offline-subject"
VALUE = "syn://type/offline-value"
PROPERTY = "syn://type/offline-property"
OBJECT = "syn://cog/offline-fixture"
PUBLIC_SLOT = "syn://thing/offline-public-property"
PRIVATE_SLOT = "syn://thing/offline-private-property"


@pytest.fixture
def inputs(tmp_path):
    snapshot = SynSnapshot(
        "offline Ember validation fixture",
        "complete",
        tuple(SynType(SynRef(ref), ref, (), None, ()) for ref in (BASE, VALUE, PROPERTY)),
        (
            SynObject(
                SynRef(OBJECT),
                SynRef(BASE),
                canonical_json({"id": OBJECT, "typeRef": BASE, "name": "Offline subject"}),
            ),
        ),
    )
    public = EmberRevision.create("syn://ember/offline-public", PROPERTY, VALUE, "public-value")
    private = EmberRevision.create(
        "syn://ember/offline-private",
        PROPERTY,
        VALUE,
        "secret://fixture/offline-private",
        value_kind="private_handle",
        visibility="private",
    )
    bundle = build_bundle(
        merge_types(snapshot),
        workspace_id=uuid4(),
        namespace="offline",
        snapshot_id=uuid4(),
        type_contracts=(
            TypeEmberContract.create(
                BASE,
                (
                    EmberSlot(PUBLIC_SLOT, "Public property", PROPERTY, VALUE, 1, 1),
                    EmberSlot(
                        PRIVATE_SLOT,
                        "Private operation",
                        PROPERTY,
                        VALUE,
                        1,
                        1,
                        source="instance",
                        visibility="private",
                    ),
                ),
                (EmberBinding(PUBLIC_SLOT, (public.pointer,)),),
            ),
        ),
        embers=(public, private),
        instances=(
            EmberInstance.create(
                OBJECT,
                BASE,
                (EmberBinding(PRIVATE_SLOT, (private.pointer,)),),
            ),
        ),
    )
    source = tmp_path / "owner.json"
    catalog = tmp_path / "snapshot.json"
    source.write_text(bundle.to_json())
    catalog.write_text(snapshot.to_json())
    return bundle, snapshot, source, catalog


def _args(source, catalog):
    return ["syn", "embers", "validate", "--file", str(source), "--catalog-file", str(catalog)]


def test_offline_validation_receipt_defaults_to_a_public_digest(inputs, capsys, monkeypatch):
    bundle, snapshot, source, catalog = inputs
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    assert main(_args(source, catalog)) == 0
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result["valid"] is True
    assert result["projection"] == "public"
    assert result["conformance"] == "public_only"
    assert result["scope_source"] == "offline_bundle_claim"
    assert result["bundle_hash"] == bundle.public_projection(merge_types(snapshot)).fingerprint
    assert bundle.fingerprint not in text
    assert "secret://" not in text
    assert main([*_args(source, catalog), "--projection", "owner"]) == 0
    assert json.loads(capsys.readouterr().out)["bundle_hash"] == bundle.fingerprint


def test_validation_checks_catalog_pin_and_exact_snapshot_subject_types(inputs, capsys):
    _bundle, snapshot, source, catalog = inputs
    changed = replace(
        snapshot,
        types=snapshot.types
        + (SynType(SynRef("syn://type/unrelated-extra"), "Extra", (), None, ()),),
    )
    catalog.write_text(changed.to_json())
    assert main(_args(source, catalog)) == 2
    assert "catalog hash" in json.loads(capsys.readouterr().err)["message"]
    changed = replace(
        snapshot,
        objects=(
            SynObject(
                SynRef(OBJECT),
                SynRef(VALUE),
                canonical_json({"id": OBJECT, "typeRef": VALUE, "name": "Incorrect subject"}),
            ),
        ),
    )
    catalog.write_text(changed.to_json())
    assert main(_args(source, catalog)) == 2
    assert "subject" in json.loads(capsys.readouterr().err)["message"]


def test_validation_rejects_incomplete_owner_and_cannot_promote_public_to_owner(inputs, capsys):
    bundle, snapshot, source, catalog = inputs
    incomplete = replace(bundle, instances=(EmberInstance.create(OBJECT, BASE),))
    source.write_text(incomplete.to_json())
    assert main(_args(source, catalog)) == 2
    assert "cardinality" in json.loads(capsys.readouterr().err)["message"]
    public = bundle.public_projection(merge_types(snapshot))
    source.write_text(public.to_json())
    assert main(_args(source, catalog)) == 0
    assert json.loads(capsys.readouterr().out)["conformance"] == "public_only"
    assert main([*_args(source, catalog), "--projection", "owner"]) == 2
    assert "complete owner" in json.loads(capsys.readouterr().err)["message"]


def test_source_and_size_errors_do_not_echo_paths_or_contents(inputs, capsys):
    _bundle, _snapshot, source, catalog = inputs
    source.write_text("x" * (MAX_BUNDLE_BYTES + 1))
    assert main(_args(source, catalog)) == 2
    assert "byte limit" in json.loads(capsys.readouterr().err)["message"]
    source.unlink()
    assert main(_args(source, catalog)) == 2
    error = capsys.readouterr().err
    assert str(source) not in error
    assert json.loads(error)["error"] == "source_unavailable"


def test_offline_validation_never_imports_adapters_or_opens_network(inputs, tmp_path):
    _bundle, _snapshot, source, catalog = inputs
    program = """
import importlib.abc
import socket
import sys
blocked = {'psycopg', 'sqlalchemy', 'transformers', 'torch', 'requests', 'httpx'}
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in blocked or fullname.startswith(
            ('potbot.persistence', 'potbot.memory')
        ):
            raise AssertionError('unexpected adapter: ' + fullname)
def deny_network(*args, **kwargs):
    raise AssertionError('unexpected network')
sys.meta_path.insert(0, DenyAdapters())
socket.socket.connect = deny_network
socket.create_connection = deny_network
from potbot.cli import main
assert main(['syn', 'embers', 'validate', '--file', sys.argv[1], '--catalog-file', sys.argv[2]]) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(source), str(catalog)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"] is True
