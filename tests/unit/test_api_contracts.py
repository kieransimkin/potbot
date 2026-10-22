"""T02.4 shared API contracts: no metadata grants execution authority."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import (
    ApiCallResult,
    ApiCallScope,
    ApiOperation,
    validate_json,
    validated_service_origin,
)
from potbot.tools.capability_contracts import ToolInstallationDescriptor, ToolInvocationIntent


def operation(**changes: object) -> ApiOperation:
    values = dict(
        name="get_items",
        protocol="rest",
        effect_class="read_only",
        target="/items/{id}",
        method="GET",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            }
        ),
        output_schema_json=canonical_json(
            {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            }
        ),
        source_hash="a" * 64,
    )
    values.update(changes)
    return ApiOperation(**values)


def scope_for(
    op: ApiOperation, args: dict[str, object], *, approve=None, egress=None
) -> ApiCallScope:
    w = uuid4()
    installation = ToolInstallationDescriptor(
        installation_ref="syn://thing/test-api-installation",
        workspace_id=w,
        implementation_hash="b" * 64,
        configuration_hash="c" * 64,
        policy_revision_hash="d" * 64,
        permitted_scopes=("read",),
    )
    intent = ToolInvocationIntent(
        invocation_id=uuid4(),
        workspace_id=w,
        task_revision_id=uuid4(),
        installation_hash=installation.fingerprint,
        input_type_ref=op.input_type_ref,
        input_hash=content_hash(args),
        effect_class=op.effect_class,
        policy_revision_hash=installation.policy_revision_hash,
        max_wall_time_ms=op.timeout_ms,
        max_output_bytes=op.max_output_bytes,
        idempotency_key="api-test-001",
    )
    return ApiCallScope(
        installation,
        intent,
        operation_hash=op.fingerprint,
        authorize=approve or (lambda *_: None),
        egress=egress or (lambda *_: None),
    )


def test_rest_contract_and_fingerprint() -> None:
    op = operation()
    assert op.fingerprint == operation().fingerprint
    assert op.to_capability().protocol == "rest"
    assert op.to_capability().input_type_ref == op.input_type_ref
    assert op.to_capability().output_type_ref == op.output_type_ref
    assert op.to_capability().effect_class == "read_only"


def test_reject_incorrect_methods_and_dynamic_targets() -> None:
    for changes in (
        {"target": "https://evil.example/path"},
        {"target": "//evil.example"},
        {"target": "/../admin"},
        {"target": "/items/%2e%2e"},
        {"method": "DELETE"},
        {"max_pages": 21},
        {"timeout_ms": 0},
    ):
        with pytest.raises(InvalidInputError):
            operation(**changes)


def test_reject_unresolved_schema_reference_and_invalid_schema() -> None:
    for schema in (
        {"$ref": "https://example.org/a"},
        {"$ref": "#/$defs/x"},
        {"type": "unknown-type"},
    ):
        with pytest.raises(InvalidInputError):
            operation(output_schema_json=canonical_json(schema))


def test_declared_input_and_output_schema_are_enforced() -> None:
    op = operation()
    validate_json(op.input_schema_json, {"path": {"id": "12"}}, label="input")
    with pytest.raises(InvalidInputError):
        validate_json(
            op.input_schema_json,
            {"path": {"id": "1"}, "url": "https://evil.example"},
            label="input",
        )
    with pytest.raises(InvalidInputError):
        validate_json(op.output_schema_json, {"value": "wrong"}, label="output")


def test_authority_is_independent_of_type_and_metadata() -> None:
    op = operation()
    args = {"path": {"id": "1"}}
    scope = scope_for(op, args)
    scope.check(op, args)
    with pytest.raises(ScopeDeniedError):
        scope.check(op, {"path": {"id": "2"}})
    with pytest.raises(ScopeDeniedError):
        replace(scope, authorize=lambda *_: False).check(op, args)
    with pytest.raises(ScopeDeniedError):
        replace(
            scope, installation=replace(scope.installation, policy_revision_hash="1" * 64)
        ).check(op, args)
    with pytest.raises(ScopeDeniedError):
        replace(scope, intent=replace(scope.intent, workspace_id=uuid4())).check(op, args)


def test_observation_links_existing_intent_not_approval() -> None:
    op = operation()
    args = {"path": {"id": "a"}}
    scope = scope_for(op, args)
    result = ApiCallResult(
        op.fingerprint, "succeeded", {"value": 3}, None, http_status=200, attempts=1, pages=1
    )
    observation = result.to_observation(scope.intent, op)
    assert observation.status == "succeeded"
    assert observation.output_hash == content_hash({"value": 3})
    failed = ApiCallResult(
        op.fingerprint, "failed", None, "bad_schema", http_status=200, attempts=1
    )
    assert failed.to_observation(scope.intent, op).output_hash is None
    with pytest.raises(StateMismatchError):
        replace(result, operation_hash="c" * 64).to_observation(scope.intent, op)


def test_explicit_service_origin_restrictions() -> None:
    for url in (
        "http://api.example.org",
        "https://127.0.0.1",
        "https://api.example.org/path",
        "https://user:password@api.example.org",
        "https://api.example.org:444",
        "https://api.example.org/?key=abc",
        "https://localhost",
    ):
        with pytest.raises((InvalidInputError, ScopeDeniedError)):
            validated_service_origin(url)
    assert validated_service_origin("https://api.example.org") == "https://api.example.org"
    assert (
        validated_service_origin("http://127.0.0.1:8000", test_loopback=True)
        == "http://127.0.0.1:8000"
    )


def test_graphql_and_grpc_are_exactly_pinned() -> None:
    op = operation(
        name="lookup",
        protocol="graphql",
        target="/graphql",
        method="POST",
        graphql_document="query lookup($id: ID!) { lookup(id: $id) { id } }",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"variables": {"type": "object"}},
                "additionalProperties": False,
            }
        ),
    )
    assert op.protocol == "graphql"
    with pytest.raises(InvalidInputError):
        replace(op, graphql_document="mutation Lookup { deleteEverything }")
    grpc = operation(
        name="lookup", protocol="grpc", target="/example.Catalog/Lookup", method="UNARY"
    )
    assert grpc.target == "/example.Catalog/Lookup"
    with pytest.raises(InvalidInputError):
        replace(grpc, target="example.Catalog/Lookup")


def test_reject_operation_swap_after_admission() -> None:
    op = operation()
    args = {"path": {"id": "1"}}
    scope = scope_for(op, args)
    with pytest.raises(ScopeDeniedError):
        scope.check(replace(op, source_hash="f" * 64), args)


def test_nullable_json_success_has_a_real_output_hash() -> None:
    op = operation(output_schema_json=canonical_json({"type": "null"}))
    args = {"path": {"id": "1"}}
    scope = scope_for(op, args)
    result = ApiCallResult(op.fingerprint, "succeeded", None, None, attempts=1)
    assert result.to_observation(scope.intent, op).output_hash == content_hash(None)


def test_tampered_result_cannot_be_promoted_as_original_observation() -> None:
    op = operation()
    args = {"path": {"id": "1"}}
    scope = scope_for(op, args)
    result = ApiCallResult(op.fingerprint, "succeeded", {"value": 3}, None, attempts=1)
    original = result.fingerprint
    result.value["value"] = 99
    assert result.fingerprint == original
    with pytest.raises(StateMismatchError):
        result.to_observation(scope.intent, op)
