"""Versioned execution summaries; transport validation never grants authority.

This deliberately exports no semantic claim, artifact reference, content digest,
free-form source text or credential. Richer exchanges need a new contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.execution_events import EXECUTION_STAGES, EXECUTION_TRIGGER_KINDS
from potbot.domain.serialization import canonical_json
from potbot.domain.syn_credentials import SynCredentialScope

EXECUTION_SUMMARY_CONTRACT = "urn:potbot:syn:execution-summary:v1"
EXECUTION_SUMMARY_CHANNEL = "potbot.execution-events.v1"
EXPORT_APPROVAL = "execution-metadata-to-immutable-ledger"
MAX_EXCHANGE_BYTES = 4096
_MAX_INTEGER = 9007199254740991


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidInputError("Syn exchange fields do not match the declared contract.")
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise InvalidInputError("Syn exchange identities must be canonical UUIDs.")
    try:
        identifier = UUID(value)
    except ValueError:
        raise InvalidInputError("Syn exchange identities must be canonical UUIDs.") from None
    if value != str(identifier):
        raise InvalidInputError("Syn exchange identities must be canonical UUIDs.")
    return identifier


def _integer(value: object, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= _MAX_INTEGER:
        raise InvalidInputError("Syn exchange numbers must be bounded exact integers.")


@dataclass(frozen=True, slots=True)
class SynExportPolicy:
    """Local operator approval for one fixed profile and selected service history.

    The namespace is operator-assigned, not a remotely attested server identity.
    Reapproval IDs stay local so reapproval cannot change an existing message.
    """

    policy_id: UUID
    scope: SynCredentialScope
    recipient_namespace_id: UUID

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_id, UUID)
            or not isinstance(self.scope, SynCredentialScope)
            or not isinstance(self.recipient_namespace_id, UUID)
        ):
            raise InvalidInputError("Syn export requires an explicit scoped policy.")

    @classmethod
    def from_dict(cls, value: object) -> SynExportPolicy:
        data = _object(
            value,
            {
                "schema_version",
                "contract",
                "approval",
                "policy_id",
                "installation_id",
                "workspace_id",
                "recipient_origin",
                "recipient_namespace_id",
            },
        )
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or data["contract"] != EXECUTION_SUMMARY_CONTRACT
            or data["approval"] != EXPORT_APPROVAL
        ):
            raise InvalidInputError(
                "Unsupported Syn export profile or missing disclosure approval."
            )
        return cls(
            _uuid(data["policy_id"]),
            SynCredentialScope.for_service(
                _uuid(data["installation_id"]),
                _uuid(data["workspace_id"]),
                data["recipient_origin"],
            ),
            _uuid(data["recipient_namespace_id"]),
        )

    def require_scope(self, scope: SynCredentialScope, workspace_id: UUID) -> None:
        if scope != self.scope or workspace_id != self.scope.workspace_id:
            raise ScopeDeniedError("Syn export policy does not approve this service/workspace.")


def validate_execution_summary(
    value: object, *, scope: SynCredentialScope | None = None
) -> dict[str, Any]:
    """Strict wire shape, without interpreting a source report as an admitted event."""
    data = _object(
        value, {"schema_version", "contract", "assertion", "source", "recipient", "execution"}
    )
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["contract"] != EXECUTION_SUMMARY_CONTRACT
        or data["assertion"] != "source_report"
    ):
        raise InvalidInputError("Unsupported Syn execution-summary contract.")
    source = _object(data["source"], {"installation_id", "workspace_id", "event_id", "sequence"})
    for name in ("installation_id", "workspace_id", "event_id"):
        _uuid(source[name])
    _integer(source["sequence"], minimum=1)
    recipient = _object(data["recipient"], {"namespace_id", "origin_sha256"})
    _uuid(recipient["namespace_id"])
    if (
        not isinstance(recipient["origin_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", recipient["origin_sha256"]) is None
    ):
        raise InvalidInputError("Syn execution summary requires an exact recipient origin.")
    if scope is not None and (
        source["installation_id"] != str(scope.installation_id)
        or source["workspace_id"] != str(scope.workspace_id)
        or recipient["origin_sha256"] != scope.recipient_sha256
    ):
        raise ScopeDeniedError("Syn execution summary differs from the configured scope.")
    execution = _object(
        data["execution"],
        {
            "run_id",
            "parent_event_id",
            "parent_run_id",
            "emitter_run_id",
            "trigger_kind",
            "stage",
            "elapsed_ms",
        },
    )
    for name in ("run_id", "emitter_run_id"):
        _uuid(execution[name])
    for name in ("parent_event_id", "parent_run_id"):
        if execution[name] is not None:
            _uuid(execution[name])
    if (execution["parent_event_id"] is None) != (execution["parent_run_id"] is None):
        raise InvalidInputError("Syn summary requires both causal parent identities.")
    if execution["parent_event_id"] == source["event_id"]:
        raise InvalidInputError("Syn summary cannot cause itself.")
    if (
        not isinstance(execution["stage"], str)
        or execution["stage"] not in EXECUTION_STAGES
        or not isinstance(execution["trigger_kind"], str)
        or execution["trigger_kind"] not in EXECUTION_TRIGGER_KINDS
    ):
        raise InvalidInputError("Unsupported Syn summary lifecycle or trigger kind.")
    if (
        execution["trigger_kind"]
        in EXECUTION_TRIGGER_KINDS - {"user_input", "api_request", "schedule", "webhook"}
        and execution["parent_event_id"] is None
    ):
        raise InvalidInputError("Child summary requires its immediate causal parent.")
    if execution["elapsed_ms"] is not None:
        _integer(execution["elapsed_ms"])
    if len(canonical_json(data).encode("utf-8")) > MAX_EXCHANGE_BYTES:
        raise InvalidInputError("Syn execution summary exceeds its byte limit.")
    return data


def project_execution_summary(event: Mapping[str, Any], policy: SynExportPolicy) -> dict[str, Any]:
    """Allowlist only fixed enums, exact IDs and bounded numbers from a local event.

    Never copy a source mapping: future local columns must remain private by default.
    This summary intentionally omits input evidence and semantic bindings; it cannot
    satisfy producer admission or authorize a receiving agent.
    """
    try:
        if type(event["schema_version"]) is not int or event["schema_version"] != 1:
            raise InvalidInputError("Unsupported local execution-event schema.")
        identifier = _uuid(event["id"])
        if event["event_ref"] != f"syn://event/{identifier}":
            raise InvalidInputError("Local execution event has inconsistent identity.")
        result = {
            "schema_version": 1,
            "contract": EXECUTION_SUMMARY_CONTRACT,
            "assertion": "source_report",
            "source": {
                "installation_id": str(policy.scope.installation_id),
                "workspace_id": str(policy.scope.workspace_id),
                "event_id": str(identifier),
                "sequence": event["sequence"],
            },
            "recipient": {
                "namespace_id": str(policy.recipient_namespace_id),
                "origin_sha256": policy.scope.recipient_sha256,
            },
            "execution": {
                name: event[name]
                for name in (
                    "run_id",
                    "parent_event_id",
                    "parent_run_id",
                    "emitter_run_id",
                    "trigger_kind",
                    "stage",
                    "elapsed_ms",
                )
            },
        }
    except KeyError:
        raise InvalidInputError("Local event lacks required export metadata.") from None
    return validate_execution_summary(result, scope=policy.scope)


def execution_summary_message_id(payload: dict[str, Any]) -> UUID:
    """Stable v1 delivery identity, separate from the preserved source event UUID."""
    data = validate_execution_summary(payload)
    return uuid5(
        NAMESPACE_URL,
        canonical_json(
            {
                "contract": data["contract"],
                "installation_id": data["source"]["installation_id"],
                "workspace_id": data["source"]["workspace_id"],
                "event_id": data["source"]["event_id"],
                "recipient": data["recipient"],
            }
        ),
    )
