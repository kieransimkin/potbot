"""Local built-in Cog handlers, with no dynamic plugin loading or side effects.

Each implementation maps its declared type to exactly one other declared type.
No model-returned Cog reference can select an arbitrary Python callable.
"""

from __future__ import annotations

from collections.abc import Callable

from potbot.domain.coding import CODING_RESPONSE_SCHEMA
from potbot.domain.errors import InvalidInputError, SchemaViolationError, ScopeDeniedError
from potbot.domain.model_proposals import ACTION_RESPONSE_SCHEMA, ModelCodingResponse
from potbot.domain.serialization import canonical_json
from potbot.models.envelopes import parse_model_response, strict_json

from .contracts import CodingScope, TypedCogValue

# All type refs are declared in the companion pinned SYN snapshot. Source and
# target types are not inferred from payload contents or JSON's top-level shape.
COMPLETION_TEXT = "syn://type/potbot-provider-completion-text"
JSON_DOCUMENT = "synny://type/potbot-data-document-json"
MODEL_CODING_RESPONSE = "syn://type/potbot-model-coding-response"
MODEL_ACTION_FRAME = "syn://type/potbot-model-action-frame"
SCOPE_CHECKED_CODING = "syn://type/potbot-scope-checked-coding-response"


def text_to_json(value: TypedCogValue, scope: CodingScope | None) -> TypedCogValue:
    if scope is not None:
        raise ScopeDeniedError("Pure JSON parse Cog does not accept controller authority")
    if not isinstance(value.value, str):
        raise InvalidInputError("Completion text Cog requires text")
    decoded = strict_json(value.value)
    if not isinstance(decoded, dict):
        raise SchemaViolationError("Completion must be one JSON object")
    return TypedCogValue(JSON_DOCUMENT, decoded)


def json_to_coding(value: TypedCogValue, scope: CodingScope | None) -> TypedCogValue:
    if scope is not None:
        raise ScopeDeniedError("Structural coding Cog does not accept controller authority")
    if not isinstance(value.value, dict):
        raise InvalidInputError("Coding response Cog requires a JSON document")
    result = parse_model_response(
        canonical_json(value.value), response_schema=CODING_RESPONSE_SCHEMA
    )
    if not isinstance(result, ModelCodingResponse):
        raise SchemaViolationError("Coding Cog requires a coding result")
    return TypedCogValue(MODEL_CODING_RESPONSE, result.to_dict())


def json_to_action(value: TypedCogValue, scope: CodingScope | None) -> TypedCogValue:
    if scope is not None:
        raise ScopeDeniedError("Structural action Cog does not accept controller authority")
    if not isinstance(value.value, dict):
        raise InvalidInputError("Action response Cog requires a JSON document")
    result = parse_model_response(
        canonical_json(value.value), response_schema=ACTION_RESPONSE_SCHEMA
    )
    return TypedCogValue(MODEL_ACTION_FRAME, result.to_dict())


def coding_scope_check(value: TypedCogValue, scope: CodingScope | None) -> TypedCogValue:
    if scope is None:
        raise ScopeDeniedError("Coding scope Cog requires controller-supplied authority")
    if not isinstance(value.value, dict):
        raise InvalidInputError("Scope checking requires a coding response record")
    from potbot.domain.model_proposals import admit_coding_response

    response = ModelCodingResponse.from_dict(value.value)
    checked = admit_coding_response(
        response,
        implementation_id=scope.implementation_id,
        base_state_id=scope.base_state_id,
        contract_revision_id=scope.contract_revision_id,
        permitted_paths=scope.permitted_paths,
        known_checks=scope.known_checks,
        evidence_ids=scope.evidence_ids,
        query_profiles=scope.query_profiles,
    )
    return TypedCogValue(SCOPE_CHECKED_CODING, checked.to_dict())


BUILTIN_HANDLERS: dict[str, Callable[[TypedCogValue, CodingScope | None], TypedCogValue]] = {
    "potbot.cogs.builtin.text_to_json": text_to_json,
    "potbot.cogs.builtin.json_to_coding": json_to_coding,
    "potbot.cogs.builtin.json_to_action": json_to_action,
    "potbot.cogs.builtin.coding_scope_check": coding_scope_check,
}
