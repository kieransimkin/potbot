"""SYN wire compatibility, lossless public JSON, and bounded snapshot validation."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

import potbot.domain.syn as syn
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import (
    MAX_SYN_METADATA_BYTES,
    MAX_SYN_OBJECTS,
    MAX_SYN_PAYLOAD_BYTES,
    MAX_SYN_SNAPSHOT_BYTES,
    MAX_SYN_TYPES,
    SYN_KINDS,
    SynnyRef,
    SynObject,
    SynProperty,
    SynRef,
    SynSnapshot,
    SynType,
    parse_ref,
    parse_type_ref,
    type_identity,
)

TYPE_REF = SynRef("syn://type/example-document")
OBJECT_REF = SynRef("syn://thing/readme")
_VOCAB = Path(__file__).resolve().parents[2] / "vocab"


def definition(**changes: object) -> SynType:
    values = {
        "ref": str(TYPE_REF),
        "name": "Example document",
        "parents": [],
        "definition": None,
        "properties": [],
        "metadata_json": "{}",
    }
    values.update(changes)
    return SynType.from_dict(values)


def public_object(ref: str = str(OBJECT_REF), *, payload_json: str | None = None) -> SynObject:
    if payload_json is None:
        payload_json = json.dumps({"id": ref, "typeRef": str(TYPE_REF), "name": "README"})
    return SynObject(SynRef(ref), TYPE_REF, payload_json)


def snapshot(**changes: object) -> SynSnapshot:
    values = {
        "source_label": "Synthetic fixture",
        "catalog_mode": "complete",
        "types": [definition().to_dict()],
        "objects": [public_object().to_dict()],
        "schema_version": 1,
    }
    values.update(changes)
    return SynSnapshot.from_dict(values)


@pytest.mark.parametrize("kind", sorted(SYN_KINDS))
def test_exact_local_refs_roundtrip_all_ten_upstream_kinds(kind: str) -> None:
    value = f"syn://{kind}/a-b_c9"
    ref = SynRef(value)
    assert str(ref) == value
    assert ref.kind == kind
    assert ref.slug == "a-b_c9"
    assert SynRef(ref) == ref
    assert len(SYN_KINDS) == 10


@pytest.mark.parametrize("slug", ["0", "x", "under_score", "a" * 255])
def test_local_ref_slugs_accept_the_sql_boundary_without_normalization(slug: str) -> None:
    assert SynRef(f"syn://thing/{slug}").slug == slug


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        b"syn://thing/a",
        "",
        "syn://",
        "syn://Thing/a",
        "syn://thing/A",
        "SYN://thing/a",
        "syn:/thing/a",
        "syn://unknown/a",
        "syn://instance/thing/a",
        "syn://thing/a/b",
        "syn://thing/a.b",
        "syn://thing/a--b",
        "syn://thing/a__b",
        "syn://thing/a-_b",
        "syn://thing/-a",
        "syn://thing/a-",
        "syn://thing/a%20b",
        "syn://thing/a?version=2",
        "syn://thing/a#fragment",
        "syn://thing/a\n",
        "syn://thing/é",
        " syn://thing/a",
        "syn://thing/" + "a" * 256,
        "https://example.com/a",
        "../relative",
    ],
)
def test_nonlocal_or_malformed_refs_are_not_repaired(value: object) -> None:
    with pytest.raises(InvalidInputError):
        SynRef(value)


@pytest.mark.parametrize("slug", ["person", "potbot-data-file-agents-md", "a" * 255])
def test_synny_type_refs_preserve_spelling_and_share_type_identity(slug: str) -> None:
    value = f"synny://type/{slug}"
    ref = SynnyRef(value)
    assert str(ref) == value
    assert ref.kind == "type" and ref.slug == slug
    assert type(parse_ref(value)) is SynnyRef
    assert parse_type_ref(value) == ref
    assert type_identity(ref) == type_identity(f"syn://type/{slug}") == slug
    assert not isinstance(ref, SynRef)
    with pytest.raises(InvalidInputError):
        SynRef(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "syn://type/data",
        "SYNNY://type/data",
        "synny://Type/data",
        "synny://type/Data",
        "synny://thing/data",
        "synny://credential/key",
        "synny://type/data/child",
        "synny://type/data?revision=1",
        "synny://type/data#fragment",
        "synny://type/a--b",
        "synny://type/a%20b",
        "synny://type/a\n",
        "synny://type/" + "a" * 256,
    ],
)
def test_synny_parser_only_accepts_exact_type_uris(value: object) -> None:
    with pytest.raises(InvalidInputError):
        SynnyRef(value)


def test_shared_ref_parser_preserves_both_schemes_and_requires_type_for_identity() -> None:
    assert type(parse_ref(str(TYPE_REF))) is SynRef
    assert type(parse_ref(str(OBJECT_REF))) is SynRef
    assert parse_type_ref(str(TYPE_REF)) == TYPE_REF
    for value in ("synny://thing/a", "SYNNY://type/a", "dt:record", "https://example.com/a"):
        with pytest.raises(InvalidInputError):
            parse_ref(value)
    with pytest.raises(InvalidInputError):
        parse_type_ref(str(OBJECT_REF))
    with pytest.raises(InvalidInputError):
        type_identity(str(OBJECT_REF))


def test_type_fields_roundtrip_both_schemes_without_changing_native_object_identity() -> None:
    alias = SynnyRef("synny://type/potbot-data-agent-definition-coding")
    parent = SynnyRef("synny://type/potbot-data-agent-definition")
    value = SynType(
        alias, "Coding agent definition", (parent,), None, (SynProperty("source", TYPE_REF),)
    )
    roundtrip = SynType.from_json(value.to_json())
    assert roundtrip == value
    assert type(roundtrip.ref) is SynnyRef and type(roundtrip.parents[0]) is SynnyRef
    assert type(roundtrip.properties[0].type_ref) is SynRef
    prop = SynProperty("representation", alias)
    assert type(SynProperty.from_dict(prop.to_dict()).type_ref) is SynnyRef
    payload = json.dumps({"id": str(OBJECT_REF), "typeRef": str(alias), "name": "Source claim"})
    obj = SynObject.from_dict(
        {"ref": str(OBJECT_REF), "type_ref": str(alias), "payload_json": payload}
    )
    assert type(obj.ref) is SynRef and obj.ref == OBJECT_REF
    assert type(obj.type_ref) is SynnyRef and obj.payload_json == payload
    with pytest.raises(InvalidInputError, match="native"):
        SynObject(alias, TYPE_REF, payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("outer_scheme,payload_scheme", [("syn", "synny"), ("synny", "syn")])
def test_object_type_aliases_agree_without_changing_source_spellings_or_payload_bytes(
    outer_scheme: str, payload_scheme: str
) -> None:
    outer = parse_type_ref(f"{outer_scheme}://type/example-document")
    payload_ref = f"{payload_scheme}://type/example-document"
    payload = (
        f' {{"id":"{OBJECT_REF}","typeRef":"{payload_ref}","name":"README","confidence":0.7500}} \n'
    )
    value = SynObject(OBJECT_REF, outer, payload)
    roundtrip = SynObject.from_json(value.to_json())
    assert roundtrip.type_ref == outer
    assert roundtrip.payload_json == payload
    for wrong_type in ("synny://type/other", str(OBJECT_REF), "SYNNY://type/example-document"):
        with pytest.raises(InvalidInputError):
            replace(value, payload_json=payload.replace(payload_ref, wrong_type))
    with pytest.raises(InvalidInputError):
        replace(value, payload_json=payload.replace(str(OBJECT_REF), "syn://thing/other"))


def test_alias_duplicate_types_and_parents_are_rejected_without_rewriting_valid_records() -> None:
    original = definition()
    alias = replace(original, ref=SynnyRef(f"synny://type/{TYPE_REF.slug}"))
    assert SynType.from_json(alias.to_json()).ref == alias.ref
    with pytest.raises(InvalidInputError, match="duplicate refs"):
        snapshot(types=[original.to_dict(), alias.to_dict()], objects=[])
    with pytest.raises(InvalidInputError, match="duplicate parent"):
        definition(parents=[str(TYPE_REF), str(alias.ref)])


def test_property_cardinality_defaults_are_explicit_on_the_wire() -> None:
    value = SynProperty("related", TYPE_REF)
    assert value.to_dict() == {
        "name": "related",
        "type_ref": str(TYPE_REF),
        "minimum": 1,
        "maximum": 1,
    }
    assert SynProperty.from_json(value.to_json()) == value
    assert SynProperty("optional", TYPE_REF, 0, 2**63 - 1).maximum == 2**63 - 1


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum": True},
        {"maximum": False},
        {"minimum": 1.0},
        {"maximum": Decimal(1)},
        {"minimum": -1},
        {"minimum": 2, "maximum": 1},
        {"maximum": None},
        {"maximum": 2**63},
        {"name": " "},
        {"name": "x" * 1025},
        {"type_ref": "syn://thing/a"},
        {"unknown": "field"},
    ],
)
def test_property_cardinality_and_shape_are_strict(changes: dict[str, object]) -> None:
    value = SynProperty("related", TYPE_REF).to_dict()
    value.update(changes)
    with pytest.raises(InvalidInputError):
        SynProperty.from_dict(value)


def test_typed_constructors_do_not_coerce_strings_or_mutable_sequences() -> None:
    with pytest.raises(InvalidInputError, match="typed SynRef"):
        SynProperty("related", str(TYPE_REF))  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        replace(definition(), parents=[])  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        replace(definition(), properties=[])  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        replace(snapshot(), types=[])  # type: ignore[arg-type]


def test_type_record_rejects_duplicate_edges_and_properties_but_defers_graph_validation() -> None:
    parent = SynRef("syn://type/not-in-this-snapshot")
    value = definition(parents=[str(parent)])
    assert value.parents == (parent,)
    # Missing parents and even self edges require the merged graph to be checked.
    assert definition(parents=[str(TYPE_REF)]).parents == (TYPE_REF,)
    with pytest.raises(InvalidInputError, match="duplicate parent"):
        replace(value, parents=(parent, parent))
    prop = SynProperty("related", TYPE_REF)
    with pytest.raises(InvalidInputError, match="duplicate property"):
        replace(value, properties=(prop, replace(prop, maximum=2)))
    with pytest.raises(InvalidInputError):
        definition(ref="syn://thing/not-a-type")


def test_exact_payload_and_metadata_bytes_survive_roundtrip_and_hashing() -> None:
    payload = (
        '{ "id": "syn://thing/readme", "typeRef": "syn://type/example-document", '
        '"name": "README é", "integer":900719925474099312345678901234567890, '
        '"decimal":0.123456789012345678901234567890, "negative_zero":-0.00E+001, '
        '"large_exponent":1e10000 }\n'
    )
    metadata = ' { "confidence":0.7500, "origin":"é" } '
    item = public_object(payload_json=payload)
    value = snapshot(types=[definition(metadata_json=metadata).to_dict()], objects=[item.to_dict()])
    roundtrip = SynSnapshot.from_json(value.to_json())
    assert roundtrip.objects[0].payload_json.encode("utf-8") == payload.encode("utf-8")
    assert roundtrip.types[0].metadata_json == metadata
    assert SynObject.from_json(item.to_json()) == item
    assert (
        roundtrip.fingerprint
        == hashlib.sha256(canonical_json(value.to_dict()).encode("utf-8")).hexdigest()
    )
    changed = replace(value, objects=(replace(item, payload_json=payload.rstrip()),))
    assert changed.fingerprint != value.fingerprint


def test_snapshot_preserves_imported_order_without_claiming_graph_order() -> None:
    first = public_object("syn://thing/a")
    second = public_object("syn://thing/b")
    original = snapshot(objects=[second.to_dict(), first.to_dict()])
    assert original.objects == (second, first)
    assert SynSnapshot.from_json(original.to_json()).objects == (second, first)
    assert original.fingerprint != replace(original, objects=(first, second)).fingerprint


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "syn://thing/different", "typeRef": str(TYPE_REF), "name": "README"},
        {"id": str(OBJECT_REF), "typeRef": "syn://type/different", "name": "README"},
        {"id": str(OBJECT_REF), "typeRef": str(TYPE_REF)},
        {"id": str(OBJECT_REF), "typeRef": str(TYPE_REF), "name": " "},
        {"id": str(OBJECT_REF), "typeRef": str(TYPE_REF), "name": 1},
        {"id": 1, "typeRef": str(TYPE_REF), "name": "README"},
    ],
)
def test_public_payload_identity_and_name_must_match(fields: dict[str, object]) -> None:
    with pytest.raises(InvalidInputError):
        public_object(payload_json=json.dumps(fields))


def test_type_refs_cannot_be_published_as_instance_objects() -> None:
    with pytest.raises(InvalidInputError, match="definitions"):
        public_object(str(TYPE_REF))


@pytest.mark.parametrize(
    "extra",
    [
        '"privateData":"SECRET-DO-NOT-PRINT"',
        '"nested":{"private_data":null}',
        '"nested":[{"nested":[{"pri\\u0076ateData":"secret"}]}]',
        '"nested":{"duplicate":1,"duplicate":2}',
        '"number":NaN',
        '"number":Infinity',
        '"number":-Infinity',
        '"number":1e99999999999999999999999999999999999999999999',
    ],
)
def test_public_json_rejects_recursive_private_fields_duplicate_keys_and_nonfinite(
    extra: str,
) -> None:
    payload = public_object().payload_json[:-1] + "," + extra + "}"
    with pytest.raises(InvalidInputError) as error:
        public_object(payload_json=payload)
    assert "SECRET-DO-NOT-PRINT" not in str(error.value)
    with pytest.raises(InvalidInputError):
        definition(metadata_json="{" + extra + "}")


@pytest.mark.parametrize("value", ["null", "[]", "1", '"text"', "{", "{}{}", "\ufeff{}"])
def test_public_json_must_be_one_strict_object(value: str) -> None:
    with pytest.raises(InvalidInputError):
        definition(metadata_json=value)
    with pytest.raises(InvalidInputError):
        public_object(payload_json=value)


def test_excessively_nested_json_fails_as_a_domain_error() -> None:
    value = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    with pytest.raises(InvalidInputError, match="nested"):
        definition(metadata_json=value)


def test_json_depth_guard_respects_strings_and_escaped_quotes() -> None:
    metadata = json.dumps({"value": '[{"' * 2000 + '\\"' * 100})
    assert definition(metadata_json=metadata).metadata_json == metadata
    at_limit = '{"nested":' + "[" * (syn.MAX_SYN_JSON_DEPTH - 1) + "0"
    at_limit += "]" * (syn.MAX_SYN_JSON_DEPTH - 1) + "}"
    assert definition(metadata_json=at_limit).metadata_json == at_limit
    with pytest.raises(InvalidInputError, match="nested"):
        definition(metadata_json=at_limit.replace("0", "[0]"))


def test_public_strings_are_checked_in_utf8_bytes_not_character_count() -> None:
    assert MAX_SYN_METADATA_BYTES == MAX_SYN_PAYLOAD_BYTES == 2_000_000
    oversized = '{"text":"' + "é" * 1_000_000 + '"}'
    assert len(oversized) < MAX_SYN_METADATA_BYTES
    with pytest.raises(InvalidInputError, match="byte limit"):
        definition(metadata_json=oversized)
    with pytest.raises(InvalidInputError, match="byte limit"):
        public_object(payload_json=oversized)
    with pytest.raises(InvalidInputError, match="UTF-8"):
        definition(metadata_json='{"value":"\ud800"}')


def test_sql_scalar_nuls_fail_before_publication_while_escaped_public_json_survives() -> None:
    with pytest.raises(InvalidInputError, match="U\\+0000"):
        definition(name="Document\x00")
    with pytest.raises(InvalidInputError, match="U\\+0000"):
        definition(definition="Description\x00")
    with pytest.raises(InvalidInputError, match="U\\+0000"):
        SynProperty("related\x00", TYPE_REF)
    with pytest.raises(InvalidInputError, match="U\\+0000"):
        snapshot(source_label="Source\x00")
    payload = json.dumps({"id": str(OBJECT_REF), "typeRef": str(TYPE_REF), "name": "A\x00B"})
    metadata = '{"value":"A\\u0000B"}'
    value = snapshot(
        types=[definition(metadata_json=metadata).to_dict()],
        objects=[public_object(payload_json=payload).to_dict()],
    )
    roundtrip = SynSnapshot.from_json(value.to_json())
    assert roundtrip.objects[0].payload_json == payload
    assert roundtrip.types[0].metadata_json == metadata


@pytest.mark.parametrize("field,limit", [("types", MAX_SYN_TYPES), ("objects", MAX_SYN_OBJECTS)])
def test_snapshot_count_bounds_fail_before_attempting_item_parsing(field: str, limit: int) -> None:
    value = snapshot().to_dict()
    value[field] = [None] * (limit + 1)
    with pytest.raises(InvalidInputError, match=f"at most {limit}"):
        SynSnapshot.from_dict(value)


def test_snapshot_rejects_duplicates_unknown_keys_and_unsupported_modes() -> None:
    value = snapshot()
    with pytest.raises(InvalidInputError, match="duplicate refs"):
        replace(value, objects=value.objects * 2)
    with pytest.raises(InvalidInputError, match="duplicate refs"):
        replace(value, types=value.types * 2)
    for changes in [
        {"catalog_mode": "latest"},
        {"catalog_mode": []},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"source_label": " "},
        {"source_label": "a" * 2001},
        {"unknown": "field"},
        {"types": {}},
        {"objects": ()},
    ]:
        with pytest.raises(InvalidInputError):
            SynSnapshot.from_dict({**value.to_dict(), **changes})


def test_snapshot_json_detects_duplicate_keys_before_they_can_be_overwritten() -> None:
    encoded = snapshot().to_json()
    root_duplicate = encoded.replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    nested_duplicate = encoded.replace(
        '"metadata_json":"{}"', '"metadata_json":"{}","metadata_json":"{}"'
    )
    for value in (root_duplicate, nested_duplicate):
        with pytest.raises(InvalidInputError, match="duplicate JSON"):
            SynSnapshot.from_json(value)
    for version in ("1.0", "NaN", "Infinity"):
        with pytest.raises(InvalidInputError):
            SynSnapshot.from_json(
                encoded.replace('"schema_version":1', f'"schema_version":{version}')
            )


def test_snapshot_input_and_canonical_export_have_the_same_hard_byte_bound(monkeypatch) -> None:
    assert MAX_SYN_SNAPSHOT_BYTES == 64 * 1024 * 1024
    value = snapshot()
    encoded = value.to_json()
    monkeypatch.setattr(syn, "MAX_SYN_SNAPSHOT_BYTES", len(encoded.encode("utf-8")))
    assert SynSnapshot.from_json(encoded) == value
    with pytest.raises(InvalidInputError, match="envelope.*byte limit"):
        SynSnapshot.from_json(encoded + " ")
    # A caller constructing typed records directly must pass the same export check.
    with pytest.raises(InvalidInputError, match="record.*byte limit"):
        replace(value, source_label=value.source_label + "é")


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads((_VOCAB / "syn-snapshot.schema.json").read_text())
    assert schema["$id"] == "urn:potbot:schema:syn-snapshot:1"
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_schema_fixture_and_runtime_roundtrip_agree(schema_validator) -> None:
    fixture_text = (_VOCAB / "example-syn-snapshot.json").read_text()
    fixture = json.loads(fixture_text)
    schema_validator.validate(fixture)
    parsed = SynSnapshot.from_json(fixture_text)
    assert parsed.to_dict() == fixture
    assert "0.123456789012345678901234567890" in parsed.objects[0].payload_json
    schema_validator.validate(SynSnapshot.from_json(parsed.to_json()).to_dict())
    schema_validator.validate(
        snapshot(types=[], objects=[], catalog_mode="extend_builtin").to_dict()
    )


@pytest.mark.parametrize("scheme", ["syn", "synny"])
def test_schema_supports_both_type_schemes_with_separate_slug_bounds(
    schema_validator, scheme: str
) -> None:
    value = snapshot(objects=[]).to_dict()
    value["types"][0]["ref"] = f"{scheme}://type/" + "a" * 255
    schema_validator.validate(value)
    assert SynSnapshot.from_dict(value).types[0].ref == value["types"][0]["ref"]
    value["types"][0]["ref"] += "a"
    with pytest.raises(ValidationError):
        schema_validator.validate(value)
    with pytest.raises(InvalidInputError):
        SynSnapshot.from_dict(value)


def test_schema_keeps_native_object_identity_when_semantic_type_is_synny(schema_validator) -> None:
    value = snapshot().to_dict()
    alias = "synny://type/potbot-data-file-agents-md"
    value["objects"][0]["type_ref"] = alias
    value["objects"][0]["payload_json"] = json.dumps(
        {"id": str(OBJECT_REF), "typeRef": alias, "name": "Reported type"}
    )
    schema_validator.validate(value)
    assert SynSnapshot.from_dict(value).objects[0].ref == OBJECT_REF
    value["objects"][0]["ref"] = "synny://type/readme"
    with pytest.raises(ValidationError):
        schema_validator.validate(value)


@pytest.mark.parametrize(
    "violation",
    [
        "unknown_root",
        "unknown_type",
        "unknown_object",
        "unknown_property",
        "missing_metadata",
        "missing_default",
        "bad_catalog_mode",
        "type_as_object",
        "uppercase_ref",
        "ref_newline",
        "overlong_slug",
        "overlong_type_slug",
        "boolean_cardinality",
        "negative_cardinality",
        "source_label_nul",
        "type_name_nul",
        "definition_nul",
        "property_name_nul",
    ],
)
def test_schema_rejects_supported_shape_violations(schema_validator, violation: str) -> None:
    value = snapshot().to_dict()
    type_value, object_value = value["types"][0], value["objects"][0]
    type_value["properties"] = [SynProperty("related", TYPE_REF).to_dict()]
    prop = type_value["properties"][0]
    if violation == "unknown_root":
        value["unexpected"] = True
    elif violation == "unknown_type":
        type_value["unexpected"] = True
    elif violation == "unknown_object":
        object_value["unexpected"] = True
    elif violation == "unknown_property":
        prop["unexpected"] = True
    elif violation == "missing_metadata":
        del type_value["metadata_json"]
    elif violation == "missing_default":
        del prop["minimum"]
    elif violation == "bad_catalog_mode":
        value["catalog_mode"] = "latest"
    elif violation == "type_as_object":
        object_value["ref"] = str(TYPE_REF)
    elif violation == "uppercase_ref":
        object_value["ref"] = "SYN://thing/a"
    elif violation == "ref_newline":
        object_value["ref"] += "\n"
    elif violation == "overlong_slug":
        object_value["ref"] = "syn://om/" + "a" * 256
    elif violation == "overlong_type_slug":
        type_value["ref"] = "syn://type/" + "a" * 256
    elif violation == "boolean_cardinality":
        prop["maximum"] = True
    elif violation == "negative_cardinality":
        prop["minimum"] = -1
    elif violation == "source_label_nul":
        value["source_label"] = "Source\x00"
    elif violation == "type_name_nul":
        type_value["name"] = "Name\x00"
    elif violation == "definition_nul":
        type_value["definition"] = "Definition\x00"
    else:
        prop["name"] = "Property\x00"
    with pytest.raises(ValidationError):
        schema_validator.validate(value)


@pytest.mark.parametrize(
    "violation",
    [
        "payload_mismatch",
        "private_field",
        "duplicate_ref",
        "duplicate_type_alias",
        "duplicate_parent_alias",
        "inverted_cardinality",
    ],
)
def test_schema_annotations_do_not_replace_runtime_semantic_validation(
    schema_validator, violation: str
) -> None:
    value = snapshot().to_dict()
    if violation == "payload_mismatch":
        value["objects"][0]["payload_json"] = public_object("syn://thing/other").payload_json
    elif violation == "private_field":
        value["types"][0]["metadata_json"] = '{"nested":[{"privateData":null}]}'
    elif violation == "duplicate_ref":
        value["objects"].append(dict(value["objects"][0]))
    elif violation == "duplicate_type_alias":
        value["types"].append({**value["types"][0], "ref": f"synny://type/{TYPE_REF.slug}"})
    elif violation == "duplicate_parent_alias":
        value["types"][0]["parents"] = [str(TYPE_REF), f"synny://type/{TYPE_REF.slug}"]
    else:
        value["types"][0]["properties"] = [
            {"name": "related", "type_ref": str(TYPE_REF), "minimum": 2, "maximum": 1}
        ]
    # Embedded JSON and cross-field constraints are intentionally runtime checks.
    # Merged graph validity and database ownership remain with their service owners.
    schema_validator.validate(value)
    with pytest.raises(InvalidInputError):
        SynSnapshot.from_dict(value)
