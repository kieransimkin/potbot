"""Strict, explicit SYN Cog signatures and deterministic transformation receipts.

A Cog is an instance under the native ``syn://type/cog`` hierarchy. It maps a
*single declared input data type* to a *single declared output data type*.
Compound inputs must be declared as data types, rather than silently acquiring
other inputs. Checking Cog output never grants permission to dispatch actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynRef, parse_type_ref, type_identity

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_VALUE_BYTES = 8 * 1024 * 1024


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InvalidInputError(f"{label} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class CogSignature:
    """Versioned installed Cog identity, not a claim of code attestation."""

    cog_ref: SynRef
    cog_type_ref: str
    input_type_ref: str
    output_type_ref: str
    handler_ref: str
    implementation_revision: int
    requires_controller_scope: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cog_ref, SynRef) or self.cog_ref.kind != "cog":
            raise InvalidInputError("Cog identity must be a syn://cog/... instance")
        for ref in (self.cog_type_ref, self.input_type_ref, self.output_type_ref):
            parse_type_ref(ref)
        if (
            not isinstance(self.handler_ref, str)
            or not re.fullmatch(r"potbot\.cogs\.[a-z0-9_.]+", self.handler_ref)
            or type(self.implementation_revision) is not int
            or not 1 <= self.implementation_revision <= 1_000_000
            or type(self.requires_controller_scope) is not bool
        ):
            raise InvalidInputError("Cog implementation binding is invalid")

    @property
    def binding_hash(self) -> str:
        # Exact registered handler + immutable version; this is a BINDING hash,
        # not a cryptographic hash of all transitively executed Python source.
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "cog_ref": str(self.cog_ref),
            "cog_type_ref": self.cog_type_ref,
            "input_type_ref": self.input_type_ref,
            "output_type_ref": self.output_type_ref,
            "handler_ref": self.handler_ref,
            "implementation_revision": self.implementation_revision,
            "requires_controller_scope": self.requires_controller_scope,
        }


@dataclass(frozen=True, slots=True)
class TypedCogValue:
    type_ref: str
    value: Any
    source_artifact_id: UUID | None = None

    def __post_init__(self) -> None:
        parse_type_ref(self.type_ref)
        # The generic Cog transport permits finite JSON values; the individual
        # installed handler additionally checks its representation contract.
        if len(canonical_json(self.value).encode("utf-8")) > _MAX_VALUE_BYTES:
            raise InvalidInputError("Cog payload exceeds its byte limit")
        if self.source_artifact_id is not None and not isinstance(self.source_artifact_id, UUID):
            raise InvalidInputError("Cog source artifact identity must be a UUID")

    @property
    def fingerprint(self) -> str:
        return content_hash({"type": type_identity(self.type_ref), "value": self.value})


@dataclass(frozen=True, slots=True)
class CodingScope:
    """Controller-supplied check inputs; never deserialize from model output.

    This scope check is *not* the final repository/usage/verifier admission. The
    caller must load these values from its authoritative pinned handoff.
    """

    implementation_id: UUID
    base_state_id: UUID
    contract_revision_id: UUID
    permitted_paths: tuple[str, ...]
    known_checks: tuple[str, ...]
    evidence_ids: tuple[UUID, ...] = ()
    query_profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            not isinstance(getattr(self, name), UUID)
            for name in ("implementation_id", "base_state_id", "contract_revision_id")
        ):
            raise InvalidInputError("Coding scope requires UUID identities")
        if any(
            not isinstance(getattr(self, name), tuple)
            for name in ("permitted_paths", "known_checks", "evidence_ids", "query_profiles")
        ):
            raise InvalidInputError("Coding scope requires immutable sequences")
        if any(not isinstance(ref, UUID) for ref in self.evidence_ids):
            raise InvalidInputError("Coding evidence references must be UUIDs")
        for name in ("permitted_paths", "known_checks", "query_profiles"):
            values = getattr(self, name)
            if (
                len(values) > 128
                or any(
                    not isinstance(value, str) or not value or "\x00" in value for value in values
                )
                or len(set(values)) != len(values)
            ):
                raise InvalidInputError(f"Coding scope {name} is invalid")

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "implementation_id": str(self.implementation_id),
                "base_state_id": str(self.base_state_id),
                "contract_revision_id": str(self.contract_revision_id),
                "permitted_paths": self.permitted_paths,
                "known_checks": self.known_checks,
                "evidence_ids": [str(value) for value in self.evidence_ids],
                "query_profiles": self.query_profiles,
            }
        )


@dataclass(frozen=True, slots=True)
class CogRunContext:
    """Immutable snapshot and controller-operation bindings for one chain."""

    cog_snapshot_hash: str
    task_revision_id: UUID
    repository_state_id: UUID
    invocation_id: UUID
    upstream_receipt_hash: str | None = None

    def __post_init__(self) -> None:
        _hash(self.cog_snapshot_hash, "Cog snapshot")
        for name in ("task_revision_id", "repository_state_id", "invocation_id"):
            if not isinstance(getattr(self, name), UUID):
                raise InvalidInputError("Cog context identities must be UUIDs")
        if self.upstream_receipt_hash is not None:
            _hash(self.upstream_receipt_hash, "upstream receipt")


@dataclass(frozen=True, slots=True)
class CogReceipt:
    """Transformation observation. It is NOT approval to execute or publish."""

    cog_ref: str
    cog_type_ref: str
    binding_hash: str
    cog_snapshot_hash: str
    task_revision_id: UUID
    repository_state_id: UUID
    invocation_id: UUID
    input_type_ref: str
    input_hash: str
    output_type_ref: str
    output_hash: str
    input_artifact_id: UUID | None
    upstream_receipt_hash: str | None
    scope_hash: str | None = None

    def __post_init__(self) -> None:
        SynRef(self.cog_ref)
        parse_type_ref(self.cog_type_ref)
        parse_type_ref(self.input_type_ref)
        parse_type_ref(self.output_type_ref)
        for name in ("binding_hash", "cog_snapshot_hash", "input_hash", "output_hash"):
            _hash(getattr(self, name), name)
        if self.upstream_receipt_hash is not None:
            _hash(self.upstream_receipt_hash, "upstream receipt")
        if self.scope_hash is not None:
            _hash(self.scope_hash, "scope")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "cog_ref": self.cog_ref,
            "cog_type_ref": self.cog_type_ref,
            "binding_hash": self.binding_hash,
            "cog_snapshot_hash": self.cog_snapshot_hash,
            "task_revision_id": str(self.task_revision_id),
            "repository_state_id": str(self.repository_state_id),
            "invocation_id": str(self.invocation_id),
            "input_type_ref": self.input_type_ref,
            "input_hash": self.input_hash,
            "output_type_ref": self.output_type_ref,
            "output_hash": self.output_hash,
            "input_artifact_id": str(self.input_artifact_id) if self.input_artifact_id else None,
            "upstream_receipt_hash": self.upstream_receipt_hash,
            "scope_hash": self.scope_hash,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class CogOutcome:
    value: TypedCogValue
    receipt: CogReceipt


def check_cog_input(signature: CogSignature, source: TypedCogValue) -> None:
    if type_identity(signature.input_type_ref) != type_identity(source.type_ref):
        raise StateMismatchError("Cog input type does not match its declared port")


def check_cog_output(signature: CogSignature, output: TypedCogValue) -> None:
    if type_identity(signature.output_type_ref) != type_identity(output.type_ref):
        raise ScopeDeniedError("Cog handler returned data under the wrong type")
