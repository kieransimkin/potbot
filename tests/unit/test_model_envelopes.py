"""Strict transport framing and finite repair, independent of live providers."""

import json
from uuid import uuid4

import jsonschema
import pytest

from potbot.domain.coding import CODING_RESPONSE_SCHEMA
from potbot.domain.errors import InvalidInputError, SchemaViolationError
from potbot.domain.execution import ActionProposal
from potbot.domain.model_proposals import (
    ACTION_RESPONSE_SCHEMA,
    ControllerIdentity,
    ModelActionFrame,
    ModelActionProposal,
    ModelCodingResponse,
)
from potbot.models.envelopes import (
    RepairPolicy,
    negotiate_output,
    parse_model_response,
    response_contract,
    strict_json,
)


def action_response() -> ModelActionFrame:
    return ModelActionFrame(
        ControllerIdentity(*(uuid4() for _ in range(6))),
        (
            ModelActionProposal(
                "inspect",
                ActionProposal("inspect"),
                "Find reusable dependencies.",
                ("Dependency inventory",),
            ),
        ),
    )


def coding_response() -> ModelCodingResponse:
    return ModelCodingResponse(
        uuid4(),
        uuid4(),
        uuid4(),
        patch=b"--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        changed_files=("a.py",),
    )


@pytest.mark.parametrize("response", [action_response(), coding_response()])
def test_registered_contract_and_parser_agree_on_valid_output(
    response: ModelActionFrame | ModelCodingResponse,
) -> None:
    payload = response.to_dict()
    schema = response_contract(payload["response_schema"])
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(payload, schema)
    raw = json.dumps(payload).encode("utf-8")
    assert parse_model_response(raw, response_schema=payload["response_schema"]) == response


@pytest.mark.parametrize(
    "raw",
    [
        b"Here is the action: {}",
        b"```json\n{}\n```",
        b"{} trailing text",
        b"{}{}",
        b'{"x":1,"x":2}',
        b'{"outer":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e999}',
        b'{"x":0.5}',
        b'{"x":99999999999999999999999}',
        b'{"x":9223372036854775808}',
        b'{"x":"\xff"}',
        b'{"x":"\\ud800"}',
        b'{"x":',
    ],
)
def test_strict_json_never_salvages_or_normalizes_dangerous_output(raw: bytes) -> None:
    with pytest.raises(SchemaViolationError):
        strict_json(raw)


def test_output_bytes_depth_and_nodes_are_bounded() -> None:
    with pytest.raises(SchemaViolationError, match="byte limit"):
        strict_json(b'{"x":"long"}', max_output_bytes=4)
    with pytest.raises(SchemaViolationError, match="structural"):
        strict_json("[" * 34 + "0" + "]" * 34)
    with pytest.raises(SchemaViolationError, match="structural"):
        strict_json(json.dumps([0] * 16_384))
    with pytest.raises(InvalidInputError):
        strict_json("{}", max_output_bytes=True)


@pytest.mark.parametrize("raw", [b"null", b"[]", b'"inspect"', b"true", b"1", b"{}"])
def test_json_without_complete_contract_is_not_a_proposal(raw: bytes) -> None:
    with pytest.raises(SchemaViolationError):
        parse_model_response(raw, response_schema=ACTION_RESPONSE_SCHEMA)


def test_incomplete_transport_cannot_publish_even_a_valid_json_prefix() -> None:
    raw = json.dumps(action_response().to_dict())
    with pytest.raises(SchemaViolationError, match="incomplete"):
        parse_model_response(raw, response_schema=ACTION_RESPONSE_SCHEMA, complete=False)


def test_response_schema_is_bound_to_request() -> None:
    raw = json.dumps(coding_response().to_dict())
    with pytest.raises(SchemaViolationError):
        parse_model_response(raw, response_schema=ACTION_RESPONSE_SCHEMA)
    with pytest.raises(InvalidInputError):
        parse_model_response(raw, response_schema="invented")


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"patch": 123},
        {"producer_kind": "controller"},
        {"inability_code": "unsupported", "inability_reason": "Also cannot comply"},
        {"evidence_ids": ["invented"]},
        {"changed_files": ["../outside.py"]},
    ],
)
def test_coding_structural_rejection_is_classified_as_schema_violation(change: dict) -> None:
    raw = coding_response().to_dict()
    raw.update(change)
    with pytest.raises(SchemaViolationError):
        parse_model_response(json.dumps(raw), response_schema=CODING_RESPONSE_SCHEMA)


def test_duplicate_identity_echo_is_not_last_value_wins() -> None:
    payload = json.dumps(action_response().to_dict())
    payload = payload.replace(
        '"schema_version": 1', '"schema_version": 999, "schema_version": 1', 1
    )
    with pytest.raises(SchemaViolationError, match="duplicate"):
        parse_model_response(payload, response_schema=ACTION_RESPONSE_SCHEMA)


def test_transport_negotiation_never_assumes_constrained_output() -> None:
    assert negotiate_output(supports_json_schema=False) == "validated_envelope"
    assert negotiate_output(supports_json_schema=False, supports_json_object=True) == "json_object"
    assert negotiate_output(supports_json_schema=True, supports_json_object=True) == "json_schema"
    with pytest.raises(InvalidInputError):
        negotiate_output(supports_json_schema=1)


def test_repair_allowance_is_finite_and_transport_interruption_is_not_schema_repair() -> None:
    policy = RepairPolicy(max_repairs=1)
    assert policy.total_attempts == 2
    assert policy.should_repair(1)
    assert not policy.should_repair(2)
    assert not policy.should_repair(1, complete=False)
    assert not policy.should_repair(1, "timeout")
    assert not RepairPolicy(max_repairs=0).should_repair(1)
    with pytest.raises(InvalidInputError):
        policy.should_repair(0)


@pytest.mark.parametrize("maximum", [-1, 5, True, 1.5])
def test_repair_policy_cannot_supply_unlimited_or_ambiguous_allowance(maximum: int) -> None:
    with pytest.raises(InvalidInputError):
        RepairPolicy(max_repairs=maximum)


def test_repair_returns_one_structural_result_and_caller_retains_raw_attempts() -> None:
    # The gateway, not this pure parser, owns persistence and budget accounting.
    outputs = [
        b"Prose pretending to execute an action",
        json.dumps(action_response().to_dict()).encode(),
    ]
    retained = []
    accepted = []
    policy = RepairPolicy(1)
    for number, raw in enumerate(outputs, 1):
        retained.append(raw)
        try:
            accepted.append(parse_model_response(raw, response_schema=ACTION_RESPONSE_SCHEMA))
            break
        except SchemaViolationError:
            if not policy.should_repair(number):
                break
    assert len(accepted) == 1
    assert retained == outputs


def test_schema_calls_return_independent_documents() -> None:
    first = response_contract(ACTION_RESPONSE_SCHEMA)
    first["properties"]["identity"] = {}
    assert (
        response_contract(ACTION_RESPONSE_SCHEMA)["properties"]["identity"]["additionalProperties"]
        is False
    )
