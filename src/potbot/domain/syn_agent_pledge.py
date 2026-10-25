"""Public SYN agent-pledge protocol math; SYN exclusively owns keys and enrollment.

This module cannot generate keys, accept pledges, reserve inheritance indices,
issue operator signatures, or provision agents.  It permits a Potbot client to
validate the public wire contract without access to any installation secret.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)

BASE_ADDRESS_PATH = "m/6969'/420'/0'/0/0"
ROOT_ADDRESS_PATH = "m/6969'/420'"
PLEDGE_OFFER_TYPE = "synny://type/syn-agent-pledge-offer-v1"
CANDIDATE_TYPE = "synny://type/syn-agent-identity-candidates-v1"
SELECTION_TYPE = "synny://type/syn-agent-independence-selection-v1"
OFFER_SIGNATURE_DOMAIN = b"syn.agent.pledge.offer.v1\x00"
INHERITANCE_MAX = (1 << 24) - 1
INDEPENDENCE_CHOICES = tuple(range(1, 9))


def _fixed_bytes(value: object, length: int, field: str) -> bytes:
    if type(value) is not bytes or len(value) != length:
        raise InvalidInputError(f"{field} must be exactly {length} bytes")
    return value


def _hex_bytes(value: object, length: int, field: str) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) != length * 2
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise InvalidInputError(f"{field} must be lowercase {length}-byte hex")
    return bytes.fromhex(value)


def public_key_hash(raw_public_key: bytes) -> bytes:
    """SHA-256 of exactly the raw 32-byte Ed25519 public key, never an xpub."""
    return hashlib.sha256(_fixed_bytes(raw_public_key, 32, "Ed25519 public key")).digest()


def shorten_public_key_hash(full_public_key_hash: bytes) -> bytes:
    """Unbiased 24-bit abbreviation of a SHA-256 digest; NOT a trusted identity."""
    return _fixed_bytes(full_public_key_hash, 32, "public key hash")[:3]


def calculate_inheritance(temporary_public_key_hash: bytes, operator_byte: int) -> int:
    """Big-endian SHA-256(32-byte temporary key hash || operator octet)[:3].

    SHA-256 truncation is uniform in the usual cryptographic hash model; it
    avoids the bias of reducing a larger integer modulo a non-power-of-two.
    The value MUST be collision-reserved by SYN in durable operator state.
    """
    digest = _fixed_bytes(temporary_public_key_hash, 32, "temporary public key hash")
    if type(operator_byte) is not int or not 0 <= operator_byte <= 255:
        raise InvalidInputError("operator choice must be one unsigned byte")
    return int.from_bytes(hashlib.sha256(digest + bytes((operator_byte,))).digest()[:3], "big")


def inheritance_hex(inheritance: int) -> str:
    if type(inheritance) is not int or not 0 <= inheritance <= INHERITANCE_MAX:
        raise InvalidInputError("inheritance must be an unsigned 24-bit integer")
    return f"{inheritance:06x}"


def agent_key_path(inheritance: int, independence: int) -> str:
    """The agent identity path is beneath the ROOT address, not the BASE leaf."""
    inheritance_hex(inheritance)
    if type(independence) is not int or independence not in INDEPENDENCE_CHOICES:
        raise InvalidInputError("independence must be a candidate index from 1 to 8")
    return f"{ROOT_ADDRESS_PATH}/{inheritance}'/{independence}/0"


def agent_key_paths(inheritance: int) -> tuple[str, ...]:
    return tuple(agent_key_path(inheritance, i) for i in INDEPENDENCE_CHOICES)


@dataclass(frozen=True, slots=True)
class PledgeOffer:
    """The exact signed public pledge fields plus a pulse anti-replay challenge.

    The temporary public verification key travels in SignedPledgeOffer's outer
    envelope, because a public-key hash alone cannot verify a signature.
    An independently authenticated operator pulse supplies the challenge.
    """

    temporary_public_key_hash: bytes
    target_master_public_key_hash: bytes
    timestamp_unix_seconds: int
    pulse_challenge: bytes

    def __post_init__(self) -> None:
        _fixed_bytes(self.temporary_public_key_hash, 32, "temporary public key hash")
        _fixed_bytes(self.target_master_public_key_hash, 32, "master public key hash")
        _fixed_bytes(self.pulse_challenge, 32, "pulse challenge")
        if (
            type(self.timestamp_unix_seconds) is not int
            or not 1 <= self.timestamp_unix_seconds < 2**63
        ):
            raise InvalidInputError("pledge timestamp must be positive Unix seconds")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "type_ref": PLEDGE_OFFER_TYPE,
            "temporary_public_key_hash": self.temporary_public_key_hash.hex(),
            "timestamp_unix_seconds": self.timestamp_unix_seconds,
            "target_master_public_key_hash": self.target_master_public_key_hash.hex(),
            "pulse_challenge": self.pulse_challenge.hex(),
        }

    def signed_bytes(self) -> bytes:
        """Unambiguous signed transcript, including master and pulse binding."""
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return OFFER_SIGNATURE_DOMAIN + canonical

    @classmethod
    def from_dict(cls, value: object) -> PledgeOffer:
        required = {
            "schema_version", "type_ref", "temporary_public_key_hash",
            "timestamp_unix_seconds", "target_master_public_key_hash", "pulse_challenge",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise InvalidInputError("pledge offer fields differ from the versioned schema")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["type_ref"] != PLEDGE_OFFER_TYPE:
            raise InvalidInputError("pledge offer uses an unknown SYN type/version")
        return cls(
            _hex_bytes(value["temporary_public_key_hash"], 32, "temporary public key hash"),
            _hex_bytes(value["target_master_public_key_hash"], 32, "master public key hash"),
            value["timestamp_unix_seconds"],
            _hex_bytes(value["pulse_challenge"], 32, "pulse challenge"),
        )


@dataclass(frozen=True, slots=True)
class SignedPledgeOffer:
    offer: PledgeOffer
    temporary_public_key: bytes
    signature: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.offer, PledgeOffer):
            raise InvalidInputError("signed pledge needs a typed SYN offer")
        if public_key_hash(_fixed_bytes(self.temporary_public_key, 32, "temporary public key")) != self.offer.temporary_public_key_hash:
            raise StateMismatchError("temporary public key does not match advertised hash")
        _fixed_bytes(self.signature, 64, "Ed25519 offer signature")

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer": self.offer.to_dict(),
            "temporary_public_key": self.temporary_public_key.hex(),
            "signature": self.signature.hex(),
        }

    def verify(
        self, *, expected_master_hash: bytes, expected_pulse_challenge: bytes,
        now_unix_seconds: int, max_skew_seconds: int = 120,
    ) -> None:
        """Validate the public offer; durable replay claiming belongs to SYN.

        A successful verification does not accept the servant, prove possession
        of hardware, reserve inheritance, or authorize key release.
        """
        if self.offer.target_master_public_key_hash != _fixed_bytes(
            expected_master_hash, 32, "expected master hash"
        ):
            raise ScopeDeniedError("pledge targets a different operator master")
        if self.offer.pulse_challenge != _fixed_bytes(
            expected_pulse_challenge, 32, "expected pulse challenge"
        ):
            raise ScopeDeniedError("pledge does not bind the current operator pulse")
        if (
            type(now_unix_seconds) is not int or type(max_skew_seconds) is not int
            or not 1 <= max_skew_seconds <= 3600
        ):
            raise InvalidInputError("pledge freshness policy is invalid")
        if abs(now_unix_seconds - self.offer.timestamp_unix_seconds) > max_skew_seconds:
            raise StateMismatchError("pledge timestamp is outside the accepted window")
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as exc:
            raise CapabilityUnavailableError("Ed25519 public verification requires the security extra") from exc
        try:
            Ed25519PublicKey.from_public_bytes(self.temporary_public_key).verify(
                self.signature, self.offer.signed_bytes()
            )
        except (InvalidSignature, ValueError) as exc:
            raise ScopeDeniedError("temporary SYN identity signature is invalid") from exc


@dataclass(frozen=True, slots=True)
class IdentityCandidates:
    """Eight *public* abbreviated hashes, not private keys or derivation proofs."""

    temporary_public_key_hash: bytes
    master_public_key_hash: bytes
    operator_byte: int
    inheritance: int
    short_public_key_hashes: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _fixed_bytes(self.temporary_public_key_hash, 32, "temporary public key hash")
        _fixed_bytes(self.master_public_key_hash, 32, "master public key hash")
        if self.inheritance != calculate_inheritance(
            self.temporary_public_key_hash, self.operator_byte
        ):
            raise StateMismatchError("inheritance does not match chosen operator byte")
        inheritance_hex(self.inheritance)
        if (
            not isinstance(self.short_public_key_hashes, tuple)
            or len(self.short_public_key_hashes) != len(INDEPENDENCE_CHOICES)
        ):
            raise InvalidInputError("agent must receive exactly eight short public key hashes")
        for digest in self.short_public_key_hashes:
            _fixed_bytes(digest, 3, "candidate short public key hash")
        if len(set(self.short_public_key_hashes)) != len(INDEPENDENCE_CHOICES):
            raise StateMismatchError("ambiguous candidate short hashes; select a new inheritance")

    @classmethod
    def from_public_hashes(
        cls, *, temporary_public_key_hash: bytes, master_public_key_hash: bytes,
        operator_byte: int, full_candidate_hashes: tuple[bytes, ...],
    ) -> IdentityCandidates:
        if not isinstance(full_candidate_hashes, tuple) or len(full_candidate_hashes) != 8:
            raise InvalidInputError("SYN must supply eight actual derived candidate public hashes")
        return cls(
            temporary_public_key_hash,
            master_public_key_hash,
            operator_byte,
            calculate_inheritance(temporary_public_key_hash, operator_byte),
            tuple(shorten_public_key_hash(h) for h in full_candidate_hashes),
        )

    def choose(self, independence: int) -> tuple[bytes, str]:
        """Choice index and public short hash; SYN must confirm full key/custody."""
        return (
            self.short_public_key_hashes[INDEPENDENCE_CHOICES.index(_check_independence(independence))],
            agent_key_path(self.inheritance, independence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "type_ref": CANDIDATE_TYPE,
            "temporary_public_key_hash": self.temporary_public_key_hash.hex(),
            "target_master_public_key_hash": self.master_public_key_hash.hex(),
            "operator_byte": self.operator_byte,
            "inheritance": self.inheritance,
            "inheritance_hex": inheritance_hex(self.inheritance),
            "short_public_key_hashes": [h.hex() for h in self.short_public_key_hashes],
        }


def _check_independence(value: int) -> int:
    if type(value) is not int or value not in INDEPENDENCE_CHOICES:
        raise InvalidInputError("independence must be an unsigned choice from 1 to 8")
    return value
