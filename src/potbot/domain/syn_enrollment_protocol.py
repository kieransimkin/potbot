"""Public verification of SYN operator pulses, candidate offers and selections.

This is a *public-only* bridge contract. It never creates, derives, unwraps,
exports or signs using installation keys. SYN implements the corresponding
private-key operations and authenticated encrypted P2P transport.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.syn_agent_pledge import (
    INDEPENDENCE_CHOICES,
    IdentityCandidates,
    PledgeOffer,
    SignedPledgeOffer,
    _fixed_bytes,
    agent_key_path,
    public_key_hash,
)

PULSE_TYPE = "synny://type/syn-operator-pulse-v1"
PULSE_DOMAIN = b"syn.operator.pulse.v1\x00"
CANDIDATE_DOMAIN = b"syn.agent.candidates.v2\x00"
SELECTION_DOMAIN = b"syn.agent.independence.v2\x00"
SELECTION_TYPE = "synny://type/syn-agent-independence-selection-v2"
_MAX_PULSE_LIFETIME = 300


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _hex(value: object, size: int, name: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{size * 2}}}", value) is None:
        raise InvalidInputError(f"{name} must be canonical lowercase hex")
    return bytes.fromhex(value)


def signed_pledge_from_dict(record: object) -> SignedPledgeOffer:
    """Strictly decode a v2 encrypted-channel wire packet; NOT acceptance.

    The caller must run `SignedPledgeOffer.verify` against an authenticated
    fresh operator pulse and reserve it atomically before any enrollment.
    """
    if not isinstance(record, dict) or set(record) != {
        "offer", "temporary_public_key", "signature",
    }:
        raise InvalidInputError("signed SYN pledge has unsupported fields")
    return SignedPledgeOffer(
        PledgeOffer.from_dict(record["offer"]),
        _hex(record["temporary_public_key"], 32, "temporary public key"),
        _hex(record["signature"], 64, "pledge signature"),
    )


def _verify_ed25519(public_key: bytes, signature: bytes, payload: bytes) -> None:
    _fixed_bytes(public_key, 32, "verification key")
    _fixed_bytes(signature, 64, "signature")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise CapabilityUnavailableError("SYN public verification requires the security extra") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise ScopeDeniedError("SYN enrollment signature is invalid") from exc


@dataclass(frozen=True, slots=True)
class OperatorPulse:
    """Fresh signed operator broadcast; hardware identifiers NEVER appear here."""

    network_id: str
    master_public_key_hash: bytes
    challenge: bytes
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.network_id, str) or re.fullmatch(
            r"[a-z][a-z0-9-]{0,63}", self.network_id
        ) is None:
            raise InvalidInputError("SYN network identifier is invalid")
        _fixed_bytes(self.master_public_key_hash, 32, "operator base public key hash")
        _fixed_bytes(self.challenge, 32, "operator pulse challenge")
        if self.challenge == bytes(32):
            raise InvalidInputError("SYN pulse challenge cannot be zero")
        if (
            type(self.issued_at) is not int
            or type(self.expires_at) is not int
            or self.issued_at <= 0
            or not 1 <= self.expires_at - self.issued_at <= _MAX_PULSE_LIFETIME
        ):
            raise InvalidInputError("SYN operator pulse lifetime is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "type_ref": PULSE_TYPE,
            "network_id": self.network_id,
            "master_public_key_hash": self.master_public_key_hash.hex(),
            "challenge": self.challenge.hex(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def signed_bytes(self) -> bytes:
        return PULSE_DOMAIN + _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, record: object) -> OperatorPulse:
        if not isinstance(record, dict) or set(record) != {
            "schema_version", "type_ref", "network_id", "master_public_key_hash",
            "challenge", "issued_at", "expires_at",
        } or type(record["schema_version"]) is not int or record["schema_version"] != 1 or record["type_ref"] != PULSE_TYPE:
            raise InvalidInputError("SYN operator pulse has unsupported fields")
        return cls(
            record["network_id"],
            _hex(record["master_public_key_hash"], 32, "operator hash"),
            _hex(record["challenge"], 32, "pulse challenge"),
            record["issued_at"], record["expires_at"],
        )


@dataclass(frozen=True, slots=True)
class SignedOperatorPulse:
    pulse: OperatorPulse
    operator_public_key: bytes
    signature: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.pulse, OperatorPulse):
            raise InvalidInputError("signed pulse requires typed operator advertisement")
        if public_key_hash(_fixed_bytes(self.operator_public_key, 32, "operator key")) != (
            self.pulse.master_public_key_hash
        ):
            raise StateMismatchError("operator pulse signing key does not match master hash")
        _fixed_bytes(self.signature, 64, "operator pulse signature")

    def verify(self, *, master_hash: bytes, now: int, network_id: str) -> None:
        if self.pulse.network_id != network_id or self.pulse.master_public_key_hash != (
            _fixed_bytes(master_hash, 32, "expected master hash")
        ):
            raise ScopeDeniedError("operator pulse belongs to another master or network")
        if type(now) is not int or not self.pulse.issued_at <= now <= self.pulse.expires_at:
            raise StateMismatchError("operator pulse expired or not yet valid")
        _verify_ed25519(self.operator_public_key, self.signature, self.pulse.signed_bytes())

    def to_dict(self) -> dict[str, object]:
        return {
            "pulse": self.pulse.to_dict(),
            "operator_public_key": self.operator_public_key.hex(),
            "signature": self.signature.hex(),
        }

    @classmethod
    def from_dict(cls, record: object) -> SignedOperatorPulse:
        if not isinstance(record, dict) or set(record) != {
            "pulse", "operator_public_key", "signature",
        }:
            raise InvalidInputError("signed operator pulse has unsupported fields")
        return cls(
            OperatorPulse.from_dict(record["pulse"]),
            _hex(record["operator_public_key"], 32, "operator public key"),
            _hex(record["signature"], 64, "operator signature"),
        )


@dataclass(frozen=True, slots=True)
class SignedIdentityCandidates:
    """The operator signature authenticates all eight abbreviated offers."""

    candidates: IdentityCandidates
    operator_public_key: bytes
    signature: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, IdentityCandidates):
            raise InvalidInputError("candidate announcement requires eight typed choices")
        if public_key_hash(_fixed_bytes(self.operator_public_key, 32, "operator key")) != (
            self.candidates.offer.target_master_public_key_hash
        ):
            raise StateMismatchError("candidate signer does not match pledged master")
        _fixed_bytes(self.signature, 64, "operator candidate signature")

    def signed_bytes(self) -> bytes:
        return CANDIDATE_DOMAIN + _canonical(self.candidates.to_dict())

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(self.signed_bytes()).digest()

    def verify(self, *, offer: PledgeOffer, master_hash: bytes) -> None:
        if not isinstance(offer, PledgeOffer) or (
            self.candidates.pledge_offer_digest != hashlib.sha256(offer.signed_bytes()).digest()
            or self.candidates.offer != offer
            or self.candidates.offer.target_master_public_key_hash != _fixed_bytes(
                master_hash, 32, "master hash"
            )
        ):
            raise ScopeDeniedError("candidate announcement is not bound to the accepted pledge")
        _verify_ed25519(self.operator_public_key, self.signature, self.signed_bytes())

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": self.candidates.to_dict(),
            "operator_public_key": self.operator_public_key.hex(),
            "signature": self.signature.hex(),
        }

    @classmethod
    def from_dict(
        cls, record: object, *, verified_pledge: PledgeOffer,
    ) -> SignedIdentityCandidates:
        if not isinstance(record, dict) or set(record) != {
            "candidates", "operator_public_key", "signature",
        } or not isinstance(record["candidates"], dict):
            raise InvalidInputError("signed candidate announcement has unsupported fields")
        fields = record["candidates"]
        if set(fields) != {
            "schema_version", "type_ref", "pledge_offer_sha256",
            "target_master_public_key_hash", "operator_byte", "inheritance",
            "inheritance_hex", "short_public_key_hashes",
        } or type(fields["schema_version"]) is not int or fields["schema_version"] != 2:
            raise InvalidInputError("operator candidates are not a versioned v2 announcement")
        abbreviations = fields["short_public_key_hashes"]
        if not isinstance(abbreviations, list) or len(abbreviations) != 8:
            raise InvalidInputError("SYN candidate response requires exactly eight choices")
        from potbot.domain.syn_agent_pledge import calculate_inheritance

        if fields["inheritance"] != calculate_inheritance(
            verified_pledge.hardware_unique_id, fields["operator_byte"]
        ):
            raise ScopeDeniedError("operator inheritance differs from signed hardware pledge")
        typed = IdentityCandidates(
            verified_pledge, fields["operator_byte"], fields["inheritance"],
            tuple(_hex(x, 3, "candidate short hash") for x in abbreviations),
        )
        if typed.to_dict() != fields:
            raise StateMismatchError("candidate announcement is noncanonical or mismatched")
        return cls(
            typed, _hex(record["operator_public_key"], 32, "operator public key"),
            _hex(record["signature"], 64, "operator signature"),
        )


@dataclass(frozen=True, slots=True)
class IndependenceSelection:
    target_master_public_key_hash: bytes
    inheritance: int
    independence: int
    chosen_short_public_key_hash: bytes
    candidate_offer_digest: bytes

    def __post_init__(self) -> None:
        _fixed_bytes(self.target_master_public_key_hash, 32, "operator master hash")
        _fixed_bytes(self.chosen_short_public_key_hash, 3, "selected short public key hash")
        _fixed_bytes(self.candidate_offer_digest, 32, "candidate offer digest")
        if type(self.inheritance) is not int or not 0 <= self.inheritance <= (1 << 24) - 1:
            raise InvalidInputError("inheritance must be unsigned 24-bit")
        if type(self.independence) is not int or self.independence not in INDEPENDENCE_CHOICES:
            raise InvalidInputError("independence must be a choice from 1 through 8")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2, "type_ref": SELECTION_TYPE,
            "target_master_public_key_hash": self.target_master_public_key_hash.hex(),
            "inheritance": self.inheritance, "independence": self.independence,
            "chosen_short_public_key_hash": self.chosen_short_public_key_hash.hex(),
            "candidate_offer_digest": self.candidate_offer_digest.hex(),
        }

    def signed_bytes(self) -> bytes:
        return SELECTION_DOMAIN + _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, record: object) -> IndependenceSelection:
        if not isinstance(record, dict) or set(record) != {
            "schema_version", "type_ref", "target_master_public_key_hash", "inheritance",
            "independence", "chosen_short_public_key_hash", "candidate_offer_digest",
        } or type(record["schema_version"]) is not int or record["schema_version"] != 2 or record["type_ref"] != SELECTION_TYPE:
            raise InvalidInputError("independence selection has unsupported fields")
        return cls(
            _hex(record["target_master_public_key_hash"], 32, "master hash"),
            record["inheritance"], record["independence"],
            _hex(record["chosen_short_public_key_hash"], 3, "chosen hash"),
            _hex(record["candidate_offer_digest"], 32, "candidate digest"),
        )


@dataclass(frozen=True, slots=True)
class SignedIndependenceSelection:
    selection: IndependenceSelection
    temporary_public_key: bytes
    signature: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.selection, IndependenceSelection):
            raise InvalidInputError("signed selection requires a typed choice")
        _fixed_bytes(self.temporary_public_key, 32, "temporary public key")
        _fixed_bytes(self.signature, 64, "agent selection signature")

    def verify(
        self, *, candidates: SignedIdentityCandidates, expected_agent_public_key: bytes,
    ) -> str:
        if self.temporary_public_key != _fixed_bytes(
            expected_agent_public_key, 32, "expected temporary public key"
        ):
            raise ScopeDeniedError("selection signer differs from original pledge signer")
        values = self.selection
        if (
            values.target_master_public_key_hash != candidates.candidates.offer.target_master_public_key_hash
            or values.inheritance != candidates.candidates.inheritance
            or values.candidate_offer_digest != candidates.digest
            or candidates.candidates.short_public_key_hashes[values.independence - 1]
            != values.chosen_short_public_key_hash
        ):
            raise ScopeDeniedError("selection does not match the signed operator candidates")
        _verify_ed25519(self.temporary_public_key, self.signature, values.signed_bytes())
        return agent_key_path(values.inheritance, values.independence)

    def to_dict(self) -> dict[str, object]:
        return {
            "selection": self.selection.to_dict(),
            "temporary_public_key": self.temporary_public_key.hex(),
            "signature": self.signature.hex(),
        }

    @classmethod
    def from_dict(cls, record: object) -> SignedIndependenceSelection:
        if not isinstance(record, dict) or set(record) != {
            "selection", "temporary_public_key", "signature",
        }:
            raise InvalidInputError("signed agent selection has unsupported fields")
        return cls(
            IndependenceSelection.from_dict(record["selection"]),
            _hex(record["temporary_public_key"], 32, "agent temporary public key"),
            _hex(record["signature"], 64, "agent signature"),
        )
