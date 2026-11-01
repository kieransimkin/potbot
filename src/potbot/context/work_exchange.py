"""Typed work input/result over the existing context pipe, never a host callback.

The controller selects the input and output port before spawn. The guest may
return a bounded result, but cannot select an operation, actor or child command.
"""

from __future__ import annotations

import json
from typing import Any

from potbot.context.rpc_contract import (
    ContextRequest, OperationRequest, SkillRequest, WorkRequest, canonical_uuid,
)
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

MAX_WORK_BYTES = 16 * 1024


def bounded_work(value: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        if not 1 <= len(raw) <= MAX_WORK_BYTES:
            raise ValueError
        result = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidInputError("Typed work exceeds its transport contract") from None
    if not isinstance(result, dict):
        raise InvalidInputError("Typed work needs an object")
    return result


class ContextWorkExchange:
    def __init__(self, input_envelope: dict[str, Any], output_type_ref: str, *,
                 allow_mutations: bool = True) -> None:
        if (not isinstance(output_type_ref, str) or not output_type_ref.startswith(("syn://", "synny://"))
                or len(output_type_ref) > 1024):
            raise InvalidInputError("Work result requires its installed semantic port")
        if type(allow_mutations) is not bool:
            raise InvalidInputError("Invalid work mutation allowance")
        self.allow_mutations = allow_mutations
        self._input = bounded_work(input_envelope)
        if set(self._input) != {"schema_version", "operation_ref", "input", "invocation_id"}:
            raise InvalidInputError("Unexpected controller work envelope")
        if (type(self._input["schema_version"]) is not int or self._input["schema_version"] != 1
                or not isinstance(self._input["operation_ref"], str)
                or not self._input["operation_ref"].startswith(("syn://", "synny://"))
                or not isinstance(self._input["input"], dict)
                or set(self._input["input"]) != {"type_ref", "value"}
                or not isinstance(self._input["input"]["type_ref"], str)):
            raise InvalidInputError("Invalid controller work envelope")
        canonical_uuid(self._input["invocation_id"])
        self._port = output_type_ref
        self._result: dict[str, Any] | None = None

    def admit(self, request: OperationRequest) -> None:
        """A read-only installed operation cannot gain writes via its side protocol."""
        mutates = ((isinstance(request, ContextRequest) and request.method == "context.update")
                   or (isinstance(request, SkillRequest) and request.mutates))
        if mutates and not self.allow_mutations:
            raise ScopeDeniedError("Read-only work cannot mutate context or publish skills")

    @property
    def result(self) -> dict[str, Any] | None:
        return bounded_work(self._result) if self._result is not None else None

    def execute(self, store: Any, grant: Any, request: WorkRequest) -> dict[str, Any]:
        if not isinstance(request, WorkRequest):
            raise ScopeDeniedError("Not a typed work request")
        if request.method == "work.input":
            if self._result is not None:
                raise StateMismatchError("Typed work is already complete")
            store.admit_work_read(grant)
            return {"revision": 1, "work": bounded_work(self._input)}
        assert request.result is not None
        output = bounded_work(request.result)
        if (set(output) != {"schema_version", "type_ref", "value"}
                or type(output["schema_version"]) is not int or output["schema_version"] != 1
                or output["type_ref"] != self._port):
            raise ScopeDeniedError("Typed work changed its installed output port")
        receipt = store.complete_work(grant, request)
        self._result = output
        return receipt
