"""T01.2 — reusable semantic tool descriptors without implicit authority."""

from __future__ import annotations

import json

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.tools.capability_contracts import (
    MCP_TOOL_DEFINITION_TYPE,
    TOOL_DEFINITION_TYPE,
    ToolCapabilityDescriptor,
    ToolImplementationDescriptor,
)

SCHEMA = canonical_json({"type": "object", "properties": {"query": {"type": "string"}}})
DIGEST = "a" * 64


def fixture_capability(**changes: object) -> ToolCapabilityDescriptor:
    fields = {
        "name": "fixture.search",
        "definition_type_ref": TOOL_DEFINITION_TYPE,
        "input_type_ref": "synny://type/potbot-data-record",
        "output_type_ref": "synny://type/potbot-data-record",
        "protocol": "rest",
        "effect_class": "read_only",
        "input_schema_json": SCHEMA,
        "output_schema_json": SCHEMA,
        "description": "Safe read-only demonstration tool",
    }
    fields.update(changes)
    return ToolCapabilityDescriptor(**fields)


def test_descriptor_roundtrip_and_immutability() -> None:
    item = fixture_capability()
    restored = ToolCapabilityDescriptor.from_dict(item.to_dict())
    assert restored == item
    assert restored.fingerprint == item.fingerprint
    result = item.input_schema
    result["type"] = "tampered"
    assert item.input_schema["type"] == "object"
    assert item.fingerprint == restored.fingerprint
    assert item.to_dict() == restored.to_dict()


def test_existing_syn_hierarchy_contains_tool_definitions() -> None:
    fixture_capability().require_registered_types()
    fixture_capability(
        protocol="mcp", definition_type_ref=MCP_TOOL_DEFINITION_TYPE
    ).require_registered_types()
    with pytest.raises(InvalidInputError):
        fixture_capability(
            input_type_ref="syn://type/not-registered-17789"
        ).require_registered_types()
    with pytest.raises(InvalidInputError):
        fixture_capability(definition_type_ref="syn://type/cog").require_registered_types()


def test_invalid_shorthand_and_mismatched_mcp_type_rejected() -> None:
    with pytest.raises(InvalidInputError):
        fixture_capability(input_type_ref="dt:potbot-data-record")
    with pytest.raises(InvalidInputError):
        fixture_capability(definition_type_ref="tool-definition")
    with pytest.raises(InvalidInputError):
        fixture_capability(protocol="mcp")
    with pytest.raises(InvalidInputError):
        fixture_capability(protocol="filesystem")
    with pytest.raises(InvalidInputError):
        fixture_capability(effect_class="maybe_safe")


def test_schema_strings_must_be_canonical_bounded_json_objects() -> None:
    with pytest.raises(InvalidInputError):
        fixture_capability(input_schema_json='{"type":"object", "foo":1}')
    with pytest.raises(InvalidInputError):
        fixture_capability(input_schema_json='{"type":"x","type":"y"}')
    with pytest.raises(InvalidInputError):
        fixture_capability(output_schema_json='{"x":NaN}')
    with pytest.raises(InvalidInputError):
        fixture_capability(input_schema_json=canonical_json(list(range(8))))
    with pytest.raises(InvalidInputError):
        fixture_capability(input_schema_json='{"x":"' + "a" * 140_000 + '"}')


def test_implementation_is_separate_from_discovery_descriptor() -> None:
    capability = fixture_capability()
    implementation = ToolImplementationDescriptor(
        implementation_ref="syn://thing/fixture-search-v1",
        capability_hash=capability.fingerprint,
        package_digest=DIGEST,
        publisher="Fixture publisher",
        version="1.0.0",
    )
    assert ToolImplementationDescriptor.from_dict(implementation.to_dict()) == implementation
    implementation.assert_capability(capability)
    assert "authorized" not in json.dumps(implementation.to_dict())
    assert "executable" not in json.dumps(capability.to_dict())
    with pytest.raises(InvalidInputError):
        ToolImplementationDescriptor.from_dict({**implementation.to_dict(), "approval": True})
    with pytest.raises(ScopeDeniedError):
        implementation.assert_capability(fixture_capability(name="other.search"))
    with pytest.raises(InvalidInputError):
        ToolImplementationDescriptor(
            implementation_ref="syn://type/fixture",
            capability_hash=DIGEST,
            package_digest=DIGEST,
            publisher="Fixture",
            version="1",
        )
