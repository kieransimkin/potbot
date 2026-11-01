"""Real sealed catalogue regression tests; no substituted registry or validators."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs.artifact_ranges import READ
from potbot.cogs.browser import ACT, BROWSER_COGS, CAPTURE, OBSERVE, PICK
from potbot.cogs.contracts import TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import type_identity
from potbot.tools.browser_cog_contracts import EVIDENCE_RESULT, schemas
from potbot.tools.semantic_contracts import ToolSemanticCatalog


@pytest.fixture(scope="module")
def catalog():
    return ToolSemanticCatalog(BoundCogRegistry.browser().snapshot)


def request(command):
    return {"schema_version": 1, "session_id": "a" * 32, "command": command}


def test_every_installed_browser_port_has_its_exact_compiled_schema(catalog):
    compiled = schemas()
    ports = {ref for cog in BROWSER_COGS for ref in (cog.input_type_ref, cog.output_type_ref)}
    assert ports == set(compiled)
    for ref, schema in compiled.items():
        assert catalog.schema_hash(ref) == content_hash(schema)
        assert catalog.schema_hash(ref.replace("syn://", "synny://")) == content_hash(schema)


@pytest.mark.parametrize(
    ("signature", "command"),
    [
        (OBSERVE, {"op": "accessibility"}),
        (ACT, {"op": "role.click", "role": "button", "name": "Save"}),
        (PICK, {"op": "inspect.start"}),
        (CAPTURE, {"op": "sample", "image": True}),
    ],
)
def test_all_four_requests_round_trip_through_real_catalog(catalog, signature, command):
    for ref in (signature.input_type_ref, signature.input_type_ref.replace("syn://", "synny://")):
        source = TypedCogValue(ref, request(command))
        assert catalog.decode(catalog.encode(source)).fingerprint == source.fingerprint


def test_evidence_output_is_a_real_registered_port(catalog):
    value = {
        "schema_version": 1,
        "session_id": "a" * 32,
        "status": "recorded",
        "kind": "observation",
        "command_sha256": "b" * 64,
        "observation_artifact_id": str(uuid4()),
        "observation_sha256": "c" * 64,
        "image_artifact_id": None,
        "image_sha256": None,
        "manifest_artifact_id": str(uuid4()),
        "manifest_sha256": "d" * 64,
        "preview": None,
    }
    source = TypedCogValue(EVIDENCE_RESULT, value)
    assert catalog.decode(catalog.encode(source)).value == value
    with pytest.raises(InvalidInputError):
        catalog.validate(TypedCogValue(EVIDENCE_RESULT, {**value, "image_sha256": "forged"}))


@pytest.mark.parametrize(
    ("signature", "command"),
    [
        (OBSERVE, {"op": "role.click", "role": "button", "name": "Save"}),
        (ACT, {"op": "accessibility"}),
        (PICK, {"op": "input.keyboard", "action": "press", "key": "Enter"}),
        (CAPTURE, {"op": "sample", "image": False}),
        (OBSERVE, {"op": "cdp", "method": "Input.dispatchKeyEvent", "params": {"type": "keyUp"}}),
        (ACT, {"op": "cdp", "method": "DOM.getDocument"}),
        (OBSERVE, {"op": "cdp", "method": "Runtime.evaluate"}),
    ],
)
def test_command_effect_and_allowlist_validation_happens_at_the_semantic_port(
    catalog,
    signature,
    command,
):
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        catalog.validate(TypedCogValue(signature.input_type_ref, request(command)))


@pytest.mark.parametrize("profile", ["provenance", "artifact_ranges", "artifact_uploads"])
def test_other_catalog_profiles_do_not_acquire_browser_ports(profile):
    other = ToolSemanticCatalog(getattr(BoundCogRegistry, profile)().snapshot)
    with pytest.raises(InvalidInputError, match="Unknown or unregistered"):
        other.schema_hash(OBSERVE.input_type_ref)


def test_browser_profile_does_not_acquire_artifact_ports(catalog):
    with pytest.raises(InvalidInputError, match="Unknown or unregistered"):
        catalog.schema_hash(READ.input_type_ref)


@pytest.mark.parametrize(
    "replacement_schema",
    [
        {},
        {"$ref": "https://untrusted.invalid/schema.json"},
        {"type": "string"},
    ],
)
def test_metadata_cannot_replace_a_compiled_browser_validator(replacement_schema):
    snapshot = BoundCogRegistry.browser().snapshot
    changed = []
    found = False
    for row in snapshot.types:
        if type_identity(row.ref) == type_identity(OBSERVE.input_type_ref):
            metadata = json.loads(row.metadata_json)
            metadata["schema"] = replacement_schema
            row = replace(row, metadata_json=canonical_json(metadata))
            found = True
        changed.append(row)
    assert found
    with pytest.raises(InvalidInputError, match="Canonical schema differs"):
        ToolSemanticCatalog(replace(snapshot, types=tuple(changed)))


def test_schema_mutation_does_not_change_existing_or_new_catalog(catalog):
    before = catalog.schema_hash(OBSERVE.input_type_ref)
    changed = schemas()
    changed[OBSERVE.input_type_ref]["properties"]["session_id"] = {}
    assert catalog.schema_hash(OBSERVE.input_type_ref) == before
    assert (
        ToolSemanticCatalog(BoundCogRegistry.browser().snapshot).schema_hash(OBSERVE.input_type_ref)
        == before
    )
    with pytest.raises(InvalidInputError):
        catalog.decode(
            canonical_json(
                {
                    "type_ref": OBSERVE.input_type_ref,
                    "schema_hash": "0" * 64,
                    "value": request({"op": "state"}),
                }
            )
        )
