"""Bounded coding handoffs and explicitly scripted response envelopes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.execution import bounded_strings, relative_path
from potbot.domain.reuse import ReuseAssessment

MAX_PATCH_BYTES = 4 * 1024 * 1024
CODING_RESPONSE_SCHEMA = "coding-result-v1"
INABILITY_CODES = frozenset(
    {"insufficient_context", "unsupported", "out_of_scope", "no_valid_patch"}
)


def coding_path(value: object) -> str:
    path = relative_path(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise InvalidInputError("Coding paths cannot contain control characters.")
    if any(part.casefold() == ".git" for part in path.split("/")):
        raise ScopeDeniedError("Patch scope cannot include Git administrative paths.")
    return path


def within_scope(path: str, scope: tuple[str, ...]) -> bool:
    return any(path == allowed or path.startswith(allowed + "/") for allowed in scope)


@dataclass(frozen=True, slots=True)
class ImplementationSpec:
    permitted_paths: tuple[str, ...]
    expected_behavior: tuple[str, ...]
    required_checks: tuple[tuple[str, str], ...]
    reuse_assessment: ReuseAssessment
    search_receipt_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        bounded_strings(self.permitted_paths, "permitted_paths")
        bounded_strings(self.expected_behavior, "expected_behavior")
        if not self.permitted_paths or not self.expected_behavior:
            raise InvalidInputError(
                "A coding handoff requires bounded scope and expected behavior."
            )
        for path in self.permitted_paths:
            coding_path(path)
        if len(set(self.permitted_paths)) != len(self.permitted_paths):
            raise InvalidInputError("Coding scope cannot repeat paths.")
        if (
            not isinstance(self.required_checks, tuple)
            or not self.required_checks
            or len(self.required_checks) > 128
            or any(
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", pair[0])
                or not isinstance(pair[1], str)
                or not re.fullmatch(r"[0-9a-f]{64}", pair[1])
                for pair in self.required_checks
            )
            or len(dict(self.required_checks)) != len(self.required_checks)
        ):
            raise InvalidInputError(
                "Required checks must have distinct identities and definition digests."
            )
        if not isinstance(self.reuse_assessment, ReuseAssessment):
            raise InvalidInputError("Coding requires the scoped reuse/adapt/build review.")
        if (
            not isinstance(self.search_receipt_ids, tuple)
            or not self.search_receipt_ids
            or len(self.search_receipt_ids) > 128
            or any(not isinstance(identity, UUID) for identity in self.search_receipt_ids)
            or len(set(self.search_receipt_ids)) != len(self.search_receipt_ids)
        ):
            raise InvalidInputError(
                "Coding requires distinct scoped search-receipt artifact references."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "permitted_paths": list(self.permitted_paths),
            "expected_behavior": list(self.expected_behavior),
            "required_checks": [
                {"check_id": key, "definition_digest": digest}
                for key, digest in self.required_checks
            ],
            "reuse_assessment": self.reuse_assessment.to_dict(),
            "search_receipt_ids": list(map(str, self.search_receipt_ids)),
            "discovery_origin": "provided_receipts",
        }


@dataclass(frozen=True, slots=True)
class ScriptedCodingResponse:
    implementation_id: UUID
    base_state_id: UUID
    patch: bytes | None = None
    inability_code: str | None = None
    inability_reason: str | None = None
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.implementation_id, UUID) or not isinstance(self.base_state_id, UUID):
            raise InvalidInputError("A coding response must identify its handoff and exact base.")
        if (self.patch is None) == (self.inability_code is None):
            raise InvalidInputError(
                "A coding response must contain exactly one patch or classified inability."
            )
        bounded_strings(self.assumptions, "assumptions")
        if self.patch is not None:
            if not isinstance(self.patch, bytes) or not 1 <= len(self.patch) <= MAX_PATCH_BYTES:
                raise InvalidInputError("Patch output must be bounded bytes.")
            if self.inability_reason is not None:
                raise InvalidInputError("A patch response cannot also describe an inability.")
            try:
                self.patch.decode("utf-8")
            except UnicodeDecodeError:
                raise InvalidInputError("This adapter accepts UTF-8 text patches only.") from None
            if b"\x00" in self.patch:
                raise InvalidInputError("Patch output contains invalid binary data.")
        elif (
            self.inability_code not in INABILITY_CODES
            or not isinstance(self.inability_reason, str)
            or not self.inability_reason.strip()
            or len(self.inability_reason) > 8000
        ):
            raise InvalidInputError("An inability requires a known code and bounded explanation.")

    def to_dict(self, patch_artifact_id: UUID | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "response_schema": CODING_RESPONSE_SCHEMA,
            "implementation_id": str(self.implementation_id),
            "base_state_id": str(self.base_state_id),
            "outcome": "patch" if self.patch is not None else "inability",
            "patch_artifact_id": str(patch_artifact_id) if patch_artifact_id else None,
            "patch_sha256": hashlib.sha256(self.patch).hexdigest() if self.patch else None,
            "inability_code": self.inability_code,
            "inability_reason": self.inability_reason,
            "assumptions": list(self.assumptions),
            "producer_kind": "scripted_fixture",
        }
