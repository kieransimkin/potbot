"""Semantic inputs remain bounded, precisely typed proposals without authority."""

import copy
import json
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, SchemaViolationError
from potbot.semantics import SemanticRegistry, SemanticValidator, parse_scalar_literal
from potbot.semantics.registry import REGISTRY_DOCUMENTS, strict_json
from potbot.semantics.validation import ABSENT

VOCAB = Path(__file__).resolve().parents[2] / "vocab"


@pytest.fixture
def registry() -> SemanticRegistry:
    return SemanticRegistry.load(VOCAB)


@pytest.fixture
def validator(registry) -> SemanticValidator:
    return SemanticValidator(registry)


def bundle() -> dict:
    return json.loads((VOCAB / "example-bundle.json").read_text())


def changed_registry(name, change):
    documents = {filename: (VOCAB / filename).read_bytes() for filename in REGISTRY_DOCUMENTS}
    document = json.loads(documents[name])
    change(document)
    documents[name] = json.dumps(document).encode()
    return SemanticRegistry.from_documents(documents)


def test_exact_catalog_inventory_and_abstract_subtypes(registry):
    actual = registry.inventory("data_type")
    assert {item["id"] for item in actual} == {
        item["id"] for item in json.loads((VOCAB / "data-types.json").read_text())["types"]
    }
    assert len(actual) == 45
    assert sum(not item["abstract"] for item in actual) == 39
    assert len(registry.inventory("entity_type")) == 25
    assert len(registry.inventory("predicate")) == 16
    assert len(registry.inventory("concept")) == 16
    assert len(registry.inventory("retrieval_profile")) == 7
    assert "dt:agent.run" in registry.descendants("dt:agent")
    assert "dt:file.agents_md" not in registry.descendants("dt:agent")
    assert registry.require_type("dt:agent")["abstract"] is True
    with pytest.raises(SchemaViolationError):
        registry.require_type("dt:agent", assignable=True)
    with pytest.raises(SchemaViolationError):
        registry.require_type("dt:invented")
    with pytest.raises(InvalidInputError):
        registry.inventory("invented")
    actual[0]["definition"] = "caller mutation"
    assert registry.inventory("data_type")[0]["definition"] != "caller mutation"


def test_catalog_rejects_omissions_duplicate_ids_unknown_refs_and_cycles():
    cases = [
        ("data-types.json", lambda d: d["types"].pop()),
        ("data-types.json", lambda d: d["types"].append(copy.deepcopy(d["types"][0]))),
        ("data-types.json", lambda d: d["types"][0].update(is_a=["dt:agent.run"])),
        ("data-types.json", lambda d: d["types"][1].update(is_a=["dt:no_such_type"])),
        (
            "vocabulary.json",
            lambda d: d["predicates"][0]["roles"]["key"].update(entity_types=["invented"]),
        ),
        (
            "vocabulary.json",
            lambda d: d["context_fields"][0].update(selector_entity_type="invented"),
        ),
        (
            "semantic-bundle.schema.json",
            lambda d: d["properties"]["frames"]["items"].update(
                {"$ref": "https://untrusted.invalid/schema"}
            ),
        ),
    ]
    for name, mutation in cases:
        with pytest.raises(SchemaViolationError):
            changed_registry(name, mutation)


@pytest.mark.parametrize("source", ['{"x":1,"x":2}', "NaN", "Infinity", "{} trailing", b"\xff"])
def test_strict_json_rejects_ambiguous_or_invalid_sources(source):
    with pytest.raises(SchemaViolationError):
        strict_json(source)


def test_lossless_numeric_lexemes_preserve_type_and_precision(validator):
    values = [parse_scalar_literal(value) for value in ["1", "1.0", '"1"', "true", "null"]]
    assert [value.term["term"] for value in values] == [
        "integer",
        "decimal",
        "string",
        "boolean",
        "null",
    ]
    assert values[1].source_lexeme == "1.0" and values[1].term["value"] == "1"
    assert len({value.term_json for value in values}) == 5
    precise = parse_scalar_literal("123456789012345678901234567890.123456789012345678901")
    assert precise.term["value"] == precise.source_lexeme
    assert parse_scalar_literal("-0").term == {"term": "integer", "value": "0"}
    assert parse_scalar_literal("1e2").term == {"term": "decimal", "value": "100"}
    unknown = validator.validate_term(
        {"term": "unknown", "expected_type": "integer", "reason": "not_inspected"}
    )
    assert unknown != values[-1].term
    assert ABSENT is not None
    for term in [
        {"term": "integer", "value": True},
        {"term": "boolean", "value": 1},
        {"term": "decimal", "value": "1.0"},
    ]:
        with pytest.raises(SchemaViolationError):
            validator.validate_term(term)


@pytest.mark.parametrize("source", ["1e10000", "1e-10000", "[1]", "{}", "NaN", "01"])
def test_literal_resource_and_scalar_boundaries(source):
    with pytest.raises(SchemaViolationError):
        parse_scalar_literal(source)


def test_shipped_envelopes_and_invalid_cases(validator):
    parsed = validator.validate_bundle((VOCAB / "example-bundle.json").read_bytes())
    assert parsed.admission_status == "untrusted_proposal"
    assert "authenticated_ownership" in parsed.pending_checks
    assert parsed.payload == bundle()
    for case in json.loads((VOCAB / "invalid-schema-examples.json").read_text()):
        assert case["expected_schema_valid"] is False
        with pytest.raises(SchemaViolationError):
            validator.validate_bundle(case["payload"])


def test_modality_polarity_conditions_and_authentication_are_not_coerced(validator):
    must = bundle()
    must["frames"] = [must["frames"][0]]
    must["frames"][0]["polarity"] = "negative"
    may = copy.deepcopy(must)
    may["frames"][0]["force"] = "may"
    first, second = validator.validate_bundle(must), validator.validate_bundle(may)
    assert first.payload_json != second.payload_json
    assert (
        first.payload["frames"][0]["when"] != first.payload["frames"][0]["applicability"]["guard"]
    )
    forged = copy.deepcopy(must)
    forged["frames"][0]["authority"] = "controller"
    with pytest.raises(SchemaViolationError):
        validator.validate_bundle(forged)
    partial = bundle()
    partial["frames"][-1]["coverage"] = {"status": "complete", "unresolved": []}
    with pytest.raises(SchemaViolationError):
        validator.validate_bundle(partial)


def test_guard_limits_and_operand_signatures_precede_evaluation(validator):
    guard = {"op": "const", "value": "true"}
    for _ in range(8):
        guard = {"op": "not", "child": guard}
    with pytest.raises(SchemaViolationError):
        validator.validate_guard(guard)
    with pytest.raises(SchemaViolationError):
        validator.validate_guard({"op": "all", "children": [{"op": "const", "value": "true"}] * 33})
    with pytest.raises(SchemaViolationError):
        validator.validate_guard(
            {
                "op": "eq",
                "left": {"field": "environment.stage"},
                "right": {"term": "string", "value": "production"},
            }
        )
    with pytest.raises(SchemaViolationError):
        validator.validate_guard(
            {
                "op": "gt",
                "left": {"field": "environment.stage"},
                "right": {"term": "enum", "domain": "environment_stage", "value": "production"},
            }
        )
    for malformed in [
        {"op": []},
        {"op": "not", "child": 1},
        {"op": "all", "children": "oops"},
        {"op": "in", "values": 1},
        {"op": "eq", "left": {"field": []}, "right": {"term": "integer", "value": "1"}},
    ]:
        with pytest.raises(SchemaViolationError):
            validator.validate_guard(malformed)


def test_interval_utf8_and_conjunction_boundaries(validator):
    value = bundle()
    value["frames"] = value["frames"][:2]
    value["frames"][0]["scope"]["valid_time"] = {
        "kind": "interval",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:00:00Z",
    }
    with pytest.raises(SchemaViolationError):
        validator.validate_bundle(value)
    with pytest.raises(SchemaViolationError):
        validator.validate_term({"term": "string", "value": "🦇" * 1025})
    value = bundle()
    value["frames"] = [value["frames"][0]]
    second = copy.deepcopy(value["frames"][0])
    second["local_id"] = "second"
    value["frames"].append(second)
    value["conjunction_groups"] = [
        {
            "local_id": "joined",
            "member_claim_ids": [value["frames"][0]["local_id"], "second"],
            "source_target": {"kind": "event", "id": "00000000-0000-0000-0000-000000000014"},
            "source_span": "Both independently asserted conditions",
            "coverage": {"status": "complete", "unresolved": []},
        }
    ]
    assert validator.validate_bundle(value).payload["conjunction_groups"]
    for frame in value["frames"]:
        frame["polarity"] = "negative"
    with pytest.raises(SchemaViolationError):
        validator.validate_bundle(value)
    value["frames"] = []
    value["conjunction_groups"] = []
    value["unmapped_content"] = [
        {
            "source_target": {"kind": "event", "id": "00000000-0000-0000-0000-000000000014"},
            "reason": "coupled_statement",
            "summary": "Do not do A and B together",
        }
    ]
    assert (
        validator.validate_bundle(value).payload["unmapped_content"][0]["summary"]
        == "Do not do A and B together"
    )
