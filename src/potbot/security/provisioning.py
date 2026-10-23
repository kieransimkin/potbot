"""Public-only verification of operator-assigned accounts and Agent proofs.

Derivation, passwords and private keys remain in the native custody process.
Verified membership is not a grant to execute a Potbot task.
"""

from __future__ import annotations

import time
from typing import Any

from . import public

AGENT_PROFILE = "syn-agent-account-v1"
_ASSIGNMENT = frozenset(("account_number", "instance_id", "group_id", "group_epoch"))
_MEMBERSHIP = _ASSIGNMENT | {"schema_version", "profile", "operator", "agent", "signature"}
_PROOF = frozenset(
    (
        "schema_version",
        "profile",
        "membership",
        "request_id",
        "payload_sha256",
        "issued_at",
        "expires_at",
        "signature",
    )
)
_REQUEST = frozenset(
    ("group_id", "group_epoch", "request_id", "payload_sha256", "validity_seconds")
)


def validate_assignment(value: dict[str, Any]) -> dict[str, Any]:
    data = public._object(value, _ASSIGNMENT, "Agent assignment")
    public._integer(data["account_number"], 0, 2**31 - 1, "account number")
    public._integer(data["group_epoch"], 0, 2**31 - 1, "group epoch")
    for name in ("instance_id", "group_id"):
        public._string(data[name], public._UUID, name)
    return data


def verify_membership(
    value: dict[str, Any], *, expected_operator_hash: str, expected_agent_hash: str | None = None
) -> dict[str, Any]:
    data = public._object(public.load_public_document(value), _MEMBERSHIP, "Agent membership")
    public._constant(data["schema_version"], 1, "Agent membership version")
    public._constant(data["profile"], AGENT_PROFILE, "Agent account profile")
    validate_assignment({name: data[name] for name in _ASSIGNMENT})
    operator = public.verify_public_identity(
        data["operator"], expected_signer_hash=expected_operator_hash
    )
    agent_data = data["agent"]
    if not isinstance(agent_data, dict):
        raise public.PublicVerificationError("Agent membership requires a public account key")
    agent = public.verify_public_identity(
        agent_data, expected_signer_hash=expected_agent_hash or agent_data.get("signer_hash", "")
    )
    for descriptor in (data["operator"], agent_data):
        public._constant(
            descriptor["type_ref"], "synny://type/public-identity-descriptor", "identity Type"
        )
    if operator.signer_hash == agent.signer_hash:
        raise public.PublicVerificationError("Agent identity cannot be the operator base identity")
    unsigned = {name: item for name, item in data.items() if name != "signature"}
    public._verify_ed25519(
        operator.public_key,
        public._base64(data["signature"], 64, "membership signature"),
        b"syn-agent-membership-v1\0" + public.canonical_json_bytes(unsigned),
        "membership",
    )
    return data


def validate_agent_request(value: dict[str, Any]) -> dict[str, Any]:
    data = public._object(value, _REQUEST, "Agent proof request")
    public._string(data["group_id"], public._UUID, "group identity")
    public._string(data["request_id"], public._UUID, "request identity")
    public._string(data["payload_sha256"], public._HASH, "payload digest")
    public._integer(data["group_epoch"], 0, 2**31 - 1, "group epoch")
    public._integer(data["validity_seconds"], 1, 300, "validity interval")
    return data


def verify_agent_proof(
    value: dict[str, Any],
    *,
    expected_operator_hash: str,
    expected_agent_hash: str,
    group_id: str,
    group_epoch: int,
    payload_sha256: str,
    now: int | None = None,
) -> dict[str, Any]:
    data = public._object(public.load_public_document(value), _PROOF, "Agent proof")
    public._constant(data["schema_version"], 1, "Agent proof version")
    public._constant(data["profile"], "syn-agent-proof-v1", "Agent proof profile")
    membership = verify_membership(
        data["membership"],
        expected_operator_hash=expected_operator_hash,
        expected_agent_hash=expected_agent_hash,
    )
    request = validate_agent_request(
        {
            "group_id": group_id,
            "group_epoch": group_epoch,
            "request_id": data["request_id"],
            "payload_sha256": payload_sha256,
            "validity_seconds": 1,
        }
    )
    if membership["group_id"] != request["group_id"] or membership["group_epoch"] != group_epoch:
        raise public.PublicVerificationError("Agent proof is for a different group or epoch")
    if data["payload_sha256"] != payload_sha256:
        raise public.PublicVerificationError("Agent proof does not match the exact payload")
    issued = public._integer(data["issued_at"], 0, 2**31 * 3600 - 1, "issue time")
    expires = public._integer(
        data["expires_at"], issued + 1, min(issued + 300, 2**31 * 3600 - 1), "expiry time"
    )
    instant = (
        int(time.time())
        if now is None
        else public._integer(now, 0, 2**31 * 3600 - 1, "current time")
    )
    if not issued <= instant < expires:
        raise public.PublicVerificationError("Agent proof is outside its validity interval")
    unsigned = {name: item for name, item in data.items() if name != "signature"}
    agent = public.verify_public_identity(
        membership["agent"], expected_signer_hash=expected_agent_hash
    )
    public._verify_ed25519(
        agent.public_key,
        public._base64(data["signature"], 64, "Agent signature"),
        b"syn-agent-proof-v1\0" + public.canonical_json_bytes(unsigned),
        "Agent proof",
    )
    return data
