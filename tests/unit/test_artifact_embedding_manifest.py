"""Named-vector wire records preserve source/space multiplicity and reject ambiguity."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from potbot.domain.embeddings import (
    DEFAULT_EMBEDDING_NAME,
    SOURCE_TEXT_INPUT_FORMAT,
    ArtifactEmbeddingManifest,
    NamedEmbeddingReference,
    validate_embedding_name,
)
from potbot.domain.errors import InvalidInputError


def reference(**changes: object) -> NamedEmbeddingReference:
    values = {
        "embedding_name": DEFAULT_EMBEDDING_NAME,
        "view_id": str(UUID(int=1)),
        "vector_id": str(UUID(int=2)),
        "source_id": str(UUID(int=3)),
        "repository_id": str(UUID(int=4)),
        "source_key": "workflow/cleanup",
        "source_content_hash": "b" * 64,
        "space_id": str(UUID(int=5)),
        "profile_hash": "c" * 64,
        "encoding_input_hash": "a" * 64,
        "input_format": SOURCE_TEXT_INPUT_FORMAT,
        "dimensions": 1024,
        "metric": "cosine",
        "generation": "standard-v1",
        "usage_origin": "synthetic",
    }
    values.update(changes)
    return NamedEmbeddingReference.from_dict(values)


def manifest(*references: NamedEmbeddingReference) -> ArtifactEmbeddingManifest:
    return ArtifactEmbeddingManifest(
        UUID(int=6),
        UUID(int=7),
        "a" * 64,
        10,
        "text/plain; charset=utf-8",
        {DEFAULT_EMBEDDING_NAME: references} if references else {},
    )


@pytest.mark.parametrize("name", ["general_text", "code.text-v1", "a", "a" * 64, "x2"])
def test_embedding_names_are_exact_canonical_labels(name: str) -> None:
    assert validate_embedding_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        None,
        1,
        True,
        "",
        "A",
        "1code",
        "a" * 65,
        "two words",
        "é",
        "a_",
        "a..b",
        "a_-b",
        "a/b",
        "a\n",
        " a",
        "a ",
        "a\x00",
    ],
)
def test_invalid_embedding_names_are_rejected_without_normalization(name: object) -> None:
    with pytest.raises(InvalidInputError):
        validate_embedding_name(name)


def test_manifest_roundtrip_preserves_distinct_sources_spaces_and_stable_order() -> None:
    first = reference()
    second_source = reference(
        vector_id=str(UUID(int=8)),
        source_id=str(UUID(int=9)),
        source_key="docs/cleanup",
        source_content_hash="d" * 64,
    )
    second_space = reference(
        vector_id=str(UUID(int=10)),
        space_id=str(UUID(int=11)),
        view_id=str(UUID(int=12)),
        profile_hash="e" * 64,
        generation="v2",
    )
    value = manifest(second_space, first, second_source)
    encoded = value.to_dict()
    refs = encoded["named_embeddings"][DEFAULT_EMBEDDING_NAME]
    assert len(refs) == 3
    assert {row["source_id"] for row in refs} == {
        str(first.source_id),
        str(second_source.source_id),
    }
    assert {row["space_id"] for row in refs} == {str(first.space_id), str(second_space.space_id)}
    assert value.to_json() == manifest(first, second_source, second_space).to_json()
    assert ArtifactEmbeddingManifest.from_dict(encoded).to_dict() == encoded
    assert ArtifactEmbeddingManifest.from_json(value.to_json()).to_dict() == encoded
    assert NamedEmbeddingReference.from_json(first.to_json()) == first
    assert manifest().to_dict()["named_embeddings"] == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"dimensions": True},
        {"dimensions": 768},
        {"dimensions": 1024.0},
        {"metric": "dot"},
        {"input_format": "generated-summary"},
        {"generation": " "},
        {"usage_origin": []},
        {"usage_origin": "estimated"},
        {"source_key": ""},
        {"profile_hash": "A" * 64},
        {"encoding_input_hash": "short"},
        {"view_id": "00000000-0000-0000-0000-00000000ABCD"},
        {"source_id": UUID(int=1)},
        {"unexpected": "field"},
    ],
)
def test_invalid_reference_fields_fail_the_typed_contract(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidInputError):
        reference(**changes)


def test_manifest_rejects_ambiguous_or_mismatched_materializations() -> None:
    first = reference()
    with pytest.raises(InvalidInputError, match="duplicate"):
        manifest(first, first)
    with pytest.raises(InvalidInputError, match="duplicate"):
        manifest(first, replace(first, vector_id=UUID(int=99)))
    with pytest.raises(InvalidInputError, match="map key"):
        manifest(replace(first, embedding_name="different_name"))
    with pytest.raises(InvalidInputError, match="artifact bytes"):
        manifest(replace(first, encoding_input_hash="f" * 64))
    with pytest.raises(InvalidInputError):
        ArtifactEmbeddingManifest.from_dict({**manifest().to_dict(), "byte_length": True})
    with pytest.raises(InvalidInputError):
        ArtifactEmbeddingManifest.from_dict({**manifest().to_dict(), "extra": "field"})
    with pytest.raises(InvalidInputError):
        ArtifactEmbeddingManifest.from_dict(
            {
                **manifest().to_dict(),
                "named_embeddings": {DEFAULT_EMBEDDING_NAME: []},
            }
        )


def test_duplicate_json_names_are_rejected_before_they_can_overwrite_references() -> None:
    encoded = manifest(reference()).to_json()
    duplicate = encoded.replace(
        '"named_embeddings":{', '"named_embeddings":{},"named_embeddings":{'
    )
    with pytest.raises(InvalidInputError, match="duplicate JSON"):
        ArtifactEmbeddingManifest.from_json(duplicate)


def test_published_fixture_matches_the_strict_manifest_contract() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "vocab" / "example-artifact-embedding-manifest.json"
    )
    fixture = json.loads(path.read_text())
    assert ArtifactEmbeddingManifest.from_dict(fixture).to_dict() == fixture
    assert len(fixture["named_embeddings"][DEFAULT_EMBEDDING_NAME]) == 3
    assert {
        item["usage_origin"] for refs in fixture["named_embeddings"].values() for item in refs
    } == {"synthetic"}


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    path = Path(__file__).resolve().parents[2] / "vocab" / "artifact-embedding-manifest.schema.json"
    schema = json.loads(path.read_text())
    assert schema["$id"] == "urn:potbot:schema:artifact-embedding-manifest:1"
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_json_schema_validates_fixture_and_typed_runtime_output(schema_validator) -> None:
    path = (
        Path(__file__).resolve().parents[2] / "vocab" / "example-artifact-embedding-manifest.json"
    )
    schema_validator.validate(json.loads(path.read_text()))
    schema_validator.validate(manifest().to_dict())
    value = ArtifactEmbeddingManifest.from_json(manifest(reference()).to_json())
    schema_validator.validate(value.to_dict())


@pytest.mark.parametrize(
    "violation",
    [
        "unknown_root",
        "unknown_reference",
        "missing_reference",
        "empty_group",
        "unsupported_format",
        "noncanonical_uuid",
    ],
)
def test_json_schema_rejects_unsupported_shapes(schema_validator, violation: str) -> None:
    value = manifest(reference()).to_dict()
    ref = value["named_embeddings"][DEFAULT_EMBEDDING_NAME][0]
    if violation == "unknown_root":
        value["unknown"] = True
    elif violation == "unknown_reference":
        ref["unknown"] = True
    elif violation == "missing_reference":
        del ref["view_id"]
    elif violation == "empty_group":
        value["named_embeddings"][DEFAULT_EMBEDDING_NAME] = []
    elif violation == "unsupported_format":
        ref["input_format"] = "unimplemented_projection"
    else:
        ref["source_id"] = "00000000-0000-0000-0000-00000000ABCD"
    with pytest.raises(ValidationError):
        schema_validator.validate(value)


@pytest.mark.parametrize("name", ["GeneralText", "a\n", "é", "a..b", "a" * 65])
def test_json_schema_rejects_noncanonical_map_and_reference_names(
    schema_validator, name: str
) -> None:
    value = manifest(reference()).to_dict()
    refs = value["named_embeddings"].pop(DEFAULT_EMBEDDING_NAME)
    value["named_embeddings"][name] = refs
    with pytest.raises(ValidationError):
        schema_validator.validate(value)
    value["named_embeddings"] = {DEFAULT_EMBEDDING_NAME: refs}
    refs[0]["embedding_name"] = name
    with pytest.raises(ValidationError):
        schema_validator.validate(value)


@pytest.mark.parametrize(
    "violation", ["map_key_mismatch", "input_hash_mismatch", "duplicate_binding"]
)
def test_cross_field_materialization_rules_remain_runtime_checks(
    schema_validator, violation
) -> None:
    # JSON Schema checks shape. Typed validation checks these cross-field rules;
    # checked database ownership/current-source visibility belong to ArtifactStore.
    value = manifest(reference()).to_dict()
    refs = value["named_embeddings"][DEFAULT_EMBEDDING_NAME]
    if violation == "map_key_mismatch":
        refs[0]["embedding_name"] = "another_name"
    elif violation == "input_hash_mismatch":
        refs[0]["encoding_input_hash"] = "f" * 64
    else:
        refs.append({**refs[0], "vector_id": str(UUID(int=99))})
    schema_validator.validate(value)
    with pytest.raises(InvalidInputError):
        ArtifactEmbeddingManifest.from_dict(value)
