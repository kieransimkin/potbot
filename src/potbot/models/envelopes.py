"""Strict model response framing and bounded repair policy.

Constrained generation is an optional transport capability, never a substitute
for local parsing and controller admission. This module performs no inference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from potbot.domain.coding import CODING_RESPONSE_SCHEMA, INABILITY_CODES
from potbot.domain.errors import DomainError, InvalidInputError, SchemaViolationError
from potbot.domain.model_proposals import (
    ACTION_RESPONSE_SCHEMA,
    MAX_ACTIONS,
    MAX_INFORMATION_NEEDS,
    ControllerIdentity,
    ModelActionFrame,
    ModelCodingResponse,
)

MAX_MODEL_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 16_384


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaViolationError("Model response contains a duplicate JSON key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SchemaViolationError("Model response contains a non-finite JSON number.")


def _integer(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        raise SchemaViolationError("Model response contains an oversized integer.")
    parsed = int(value)
    if not -(2**63) <= parsed < 2**63:
        raise SchemaViolationError("Model response contains an oversized integer.")
    return parsed


def _floating(value: str) -> float:
    # The declared envelopes have no floating-point fields. Rejecting these here
    # also prevents overflow (1e999) from bypassing JSON's constant callback.
    raise SchemaViolationError("Model response schema does not permit floating-point values.")


def strict_json(raw: bytes | str, *, max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES) -> object:
    """Parse one complete bounded UTF-8 JSON value with no salvage heuristics."""
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_MODEL_OUTPUT_BYTES:
        raise InvalidInputError("Model output byte bound is invalid.")
    try:
        if isinstance(raw, bytes):
            if len(raw) > max_output_bytes:
                raise SchemaViolationError("Model response exceeds its byte limit.")
            text = raw.decode("utf-8")
        elif isinstance(raw, str):
            if len(raw.encode("utf-8")) > max_output_bytes:
                raise SchemaViolationError("Model response exceeds its byte limit.")
            text = raw
        else:
            raise SchemaViolationError("Model response must be UTF-8 text.")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
            parse_int=_integer,
            parse_float=_floating,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise SchemaViolationError(
            "Model response is not one complete UTF-8 JSON document."
        ) from None
    stack = [(value, 1)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_NODES:
            raise SchemaViolationError("Model response exceeds its structural bounds.")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
            stack.extend((key, depth + 1) for key in item)
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            # JSON \ud800 escapes can produce lone surrogates even from valid UTF-8.
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise SchemaViolationError("Model response contains invalid Unicode.") from None
    return value


def parse_model_response(
    raw: bytes | str,
    *,
    response_schema: str,
    complete: bool = True,
    max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES,
) -> ModelActionFrame | ModelCodingResponse:
    """Structural validation only; parsed records remain untrusted proposals."""
    if type(complete) is not bool:
        raise InvalidInputError("Transport completion must be an explicit boolean.")
    if response_schema not in {ACTION_RESPONSE_SCHEMA, CODING_RESPONSE_SCHEMA}:
        raise InvalidInputError("The requested response schema is not registered.")
    if not complete:
        raise SchemaViolationError("An incomplete model stream cannot publish a proposal.")
    value = strict_json(raw, max_output_bytes=max_output_bytes)
    try:
        if response_schema == ACTION_RESPONSE_SCHEMA:
            return ModelActionFrame.from_dict(value)
        return ModelCodingResponse.from_dict(value)
    except DomainError:
        raise SchemaViolationError(
            "Model output does not conform to its response contract."
        ) from None
    except (KeyError, TypeError, ValueError, RecursionError):
        raise SchemaViolationError(
            "Model output does not conform to its response contract."
        ) from None


@dataclass(frozen=True, slots=True)
class RepairPolicy:
    """The gateway records and reserves each attempt independently."""

    max_repairs: int = 1
    max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if type(self.max_repairs) is not int or not 0 <= self.max_repairs <= 4:
            raise InvalidInputError("Repair allowance must be between zero and four.")
        if (
            type(self.max_output_bytes) is not int
            or not 1 <= self.max_output_bytes <= MAX_MODEL_OUTPUT_BYTES
        ):
            raise InvalidInputError("Repair output byte limit is invalid.")

    @property
    def total_attempts(self) -> int:
        return self.max_repairs + 1

    def should_repair(
        self,
        attempts: int,
        error_code: str = "schema_violation",
        *,
        complete: bool = True,
    ) -> bool:
        if type(attempts) is not int or attempts < 1 or type(complete) is not bool:
            raise InvalidInputError("Repair decisions require a completed attempt count.")
        # Transport interruption/retries have a different reconciliation contract.
        return complete and error_code == "schema_violation" and attempts < self.total_attempts


def negotiate_output(*, supports_json_schema: bool, supports_json_object: bool = False) -> str:
    """Select only capabilities explicitly declared by the chosen transport."""
    if type(supports_json_schema) is not bool or type(supports_json_object) is not bool:
        raise InvalidInputError("Output negotiation requires explicit backend capabilities.")
    if supports_json_schema:
        return "json_schema"
    if supports_json_object:
        return "json_object"
    return "validated_envelope"


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _array_schema(items: dict[str, Any], limit: int = 128, *, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": limit}


def _definitions() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1, "maxLength": 8192}
    uuid = {
        "type": "string",
        "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    }
    unique_strings = {**_array_schema(text), "uniqueItems": True}
    uuids = {**_array_schema(uuid), "uniqueItems": True}
    scalar = {"type": ["string", "integer", "boolean", "null"], "maxLength": 4096}
    predicate_ref = {"$ref": "#/$defs/predicate"}
    predicate = {
        "oneOf": [
            _object_schema(
                {
                    "op": {"enum": ["all", "any"]},
                    "operands": _array_schema(predicate_ref, 16, minimum=1),
                }
            ),
            _object_schema(
                {"op": {"const": "not"}, "operands": _array_schema(predicate_ref, 1, minimum=1)}
            ),
            _object_schema({"op": {"enum": ["eq", "ne"]}, "field": text, "value": scalar}),
            _object_schema(
                {
                    "op": {"const": "in"},
                    "field": text,
                    "value": _array_schema(scalar, 32, minimum=1),
                }
            ),
            _object_schema({"op": {"const": "exists"}, "field": text}),
        ]
    }
    need = _object_schema(
        {
            "schema_version": {"const": 1},
            "question": {"type": "string", "minLength": 1, "maxLength": 8000},
            "pending_decision": {"type": "string", "minLength": 1, "maxLength": 8000},
            "alternatives": unique_strings,
            "required_evidence": {**unique_strings, "minItems": 1},
            "resolution_criteria": {**unique_strings, "minItems": 1},
            "scope": {"type": "string", "minLength": 1, "maxLength": 8000},
            "known_evidence": uuids,
            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
            "budget": {
                "type": "object",
                "maxProperties": 16,
                "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
            },
            "query_profiles": unique_strings,
        }
    )
    action = _object_schema(
        {
            "schema_version": {"const": 1},
            "kind": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,79}$"},
            "arguments": _array_schema(text),
            "working_directory": {"type": "string", "minLength": 1, "maxLength": 2000},
            "input_paths": _array_schema({"type": "string", "minLength": 1, "maxLength": 2000}),
            "output_paths": _array_schema({"type": "string", "minLength": 1, "maxLength": 2000}),
            "expected_output": {"type": "string", "minLength": 1, "maxLength": 4000},
            "stopping_condition": {"type": "string", "minLength": 1, "maxLength": 4000},
        }
    )
    model_action = _object_schema(
        {
            "proposal_id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,39}$"},
            "action": {"$ref": "#/$defs/action"},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 4000},
            "expected_observations": {**unique_strings, "minItems": 1, "maxItems": 16},
            "criterion_ids": unique_strings,
            "resource_class": {"type": "string", "minLength": 1, "maxLength": 80},
            "evidence_ids": uuids,
            "information_need_ids": uuids,
            "prior_dependencies": uuids,
            "dependencies": {**unique_strings, "maxItems": MAX_ACTIONS},
            "preconditions": _array_schema(predicate_ref, 16),
            "stop_conditions": _array_schema(predicate_ref, 16),
        }
    )
    return {
        "uuid": uuid,
        "predicate": predicate,
        "information_need": need,
        "action": action,
        "model_action": model_action,
    }


def response_contract(response_schema: str) -> dict[str, Any]:
    """Return the registered framing schema; local semantic checks remain mandatory.

    Backends may support only a subset of JSON Schema. Such a backend must select
    validated_envelope/json_object instead of silently claiming strict conformance.
    """
    definitions = _definitions()
    needs = _array_schema({"$ref": "#/$defs/information_need"}, MAX_INFORMATION_NEEDS)
    properties: dict[str, Any] = {
        "schema_version": {"const": 1},
        "response_schema": {"const": response_schema},
    }
    if response_schema == ACTION_RESPONSE_SCHEMA:
        properties.update(
            identity=_object_schema(
                {
                    field: {"$ref": "#/$defs/uuid"}
                    for field in ControllerIdentity.__dataclass_fields__
                }
            ),
            actions=_array_schema({"$ref": "#/$defs/model_action"}, MAX_ACTIONS),
            information_needs=needs,
        )
        schema = _object_schema(properties)
        schema["anyOf"] = [
            {"properties": {"actions": {"minItems": 1}}},
            {"properties": {"information_needs": {"minItems": 1}}},
        ]
    elif response_schema == CODING_RESPONSE_SCHEMA:
        # A small coder should receive only the definitions reachable from its
        # response contract, not the unrelated reasoner action/predicate grammar.
        definitions = {
            key: value for key, value in definitions.items() if key in {"uuid", "information_need"}
        }
        text = {"type": "string", "minLength": 1, "maxLength": 8192}
        properties.update(
            implementation_id={"$ref": "#/$defs/uuid"},
            base_state_id={"$ref": "#/$defs/uuid"},
            contract_revision_id={"$ref": "#/$defs/uuid"},
            patch={"type": ["string", "null"], "maxLength": 4 * 1024 * 1024},
            inability_code={"enum": [None, *sorted(INABILITY_CODES)]},
            inability_reason={"type": ["string", "null"], "maxLength": 8000},
            changed_files={
                **_array_schema({"type": "string", "minLength": 1, "maxLength": 2000}),
                "uniqueItems": True,
            },
            proposed_checks={**_array_schema(text), "uniqueItems": True},
            information_needs=needs,
            evidence_ids={**_array_schema({"$ref": "#/$defs/uuid"}), "uniqueItems": True},
            assumptions={**_array_schema(text), "uniqueItems": True},
        )
        schema = _object_schema(properties)
        schema["oneOf"] = [
            {
                "properties": {
                    "patch": {"type": "string", "minLength": 1},
                    "inability_code": {"type": "null"},
                    "inability_reason": {"type": "null"},
                    "changed_files": {"minItems": 1},
                }
            },
            {
                "properties": {
                    "patch": {"type": "null"},
                    "inability_code": {"type": "string"},
                    "inability_reason": {"type": "string", "minLength": 1},
                    "changed_files": {"maxItems": 0},
                }
            },
        ]
    else:
        raise InvalidInputError("The requested response schema is not registered.")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
        **schema,
    }
