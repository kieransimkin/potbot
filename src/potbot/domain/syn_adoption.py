"""Public proof of SYN-selected agent key adoption (no private key custody).

The agent signs with the *selected* Ed25519 key after its own SYN instance
imports that key into an OS keystore. The operator SYN signs the adoption receipt.
This module verifies their public claims; it cannot attest keystore hardware or
perform a private derivation/import/pledge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.syn_agent_pledge import (
    _fixed_bytes,
    agent_key_path,
    public_key_hash,
)
from potbot.domain.syn_enrollment_protocol import _canonical, _hex, _verify_ed25519

PROOF_TYPE = "synny://type/syn-agent-selected-key-proof-v1"
ADOPTION_TYPE = "synny://type/syn-agent-adoption-receipt-v1"
_PROOF_DOMAIN = b"syn.agent.selected-key-proof.v1\x00"
_RECEIPT_DOMAIN = b"syn.operator.agent-adoption.v1\x00"
_MAX_PROOF_AGE_SECONDS = 300


def _seconds(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value < 2**63:
        raise InvalidInputError(f"{label} must be positive Unix seconds")
    return value


@dataclass(frozen=True, slots=True)
class SelectedKeyProof:
    """Challenge signed by the new agent's selected, imported private key."""

    offer_sha256: bytes
    master_public_key_hash: bytes
    inheritance: int
    independence: int
    selected_public_key: bytes
    challenge: bytes
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _fixed_bytes(self.offer_sha256, 32, "pledge hash")
        _fixed_bytes(self.master_public_key_hash, 32, "master hash")
        _fixed_bytes(self.selected_public_key, 32, "selected agent public key")
        _fixed_bytes(self.challenge, 32, "adoption challenge")
        if not any(self.challenge):
            raise InvalidInputError("adoption challenge cannot be all zeros")
        agent_key_path(self.inheritance, self.independence)
        _seconds(self.issued_at, "proof issued_at")
        _seconds(self.expires_at, "proof expires_at")
        if not 1 <= self.expires_at - self.issued_at <= _MAX_PROOF_AGE_SECONDS:
            raise InvalidInputError("adoption proof lifetime must be 1..300 seconds")

    @property
    def selected_public_key_hash(self) -> bytes:
        return public_key_hash(self.selected_public_key)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1, "type_ref": PROOF_TYPE,
            "offer_sha256": self.offer_sha256.hex(),
            "master_public_key_hash": self.master_public_key_hash.hex(),
            "inheritance": self.inheritance, "independence": self.independence,
            "selected_public_key": self.selected_public_key.hex(),
            "challenge": self.challenge.hex(),
            "issued_at": self.issued_at, "expires_at": self.expires_at,
        }

    def signed_bytes(self) -> bytes:
        return _PROOF_DOMAIN + _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> SelectedKeyProof:
        required = {"schema_version", "type_ref", "offer_sha256", "master_public_key_hash",
                    "inheritance", "independence", "selected_public_key", "challenge",
                    "issued_at", "expires_at"}
        if (not isinstance(value, dict) or set(value) != required
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["type_ref"] != PROOF_TYPE):
            raise InvalidInputError("SYN selected-key proof has unsupported fields")
        return cls(
            _hex(value["offer_sha256"], 32, "pledge hash"),
            _hex(value["master_public_key_hash"], 32, "master hash"),
            value["inheritance"], value["independence"],
            _hex(value["selected_public_key"], 32, "selected public key"),
            _hex(value["challenge"], 32, "adoption challenge"),
            value["issued_at"], value["expires_at"],
        )


@dataclass(frozen=True, slots=True)
class SignedSelectedKeyProof:
    proof: SelectedKeyProof
    signature: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.proof, SelectedKeyProof):
            raise InvalidInputError("adoption key proof must be typed")
        _fixed_bytes(self.signature, 64, "selected-key signature")

    def verify(self, *, now: int) -> None:
        if type(now) is not int or not self.proof.issued_at <= now <= self.proof.expires_at:
            raise StateMismatchError("selected-key proof expired or not yet valid")
        _verify_ed25519(self.proof.selected_public_key, self.signature, self.proof.signed_bytes())

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(self.proof.signed_bytes() + self.signature).digest()

    def to_dict(self) -> dict[str, Any]:
        return {"proof": self.proof.to_dict(), "signature": self.signature.hex()}

    @classmethod
    def from_dict(cls, value: object) -> SignedSelectedKeyProof:
        if not isinstance(value, dict) or set(value) != {"proof", "signature"}:
            raise InvalidInputError("signed selected-key proof has unsupported fields")
        return cls(SelectedKeyProof.from_dict(value["proof"]), _hex(value["signature"], 64, "proof signature"))


@dataclass(frozen=True, slots=True)
class AdoptionReceipt:
    """Operator SYN's signed *claim* of OS-key custody, not OS attestation."""

    offer_sha256: bytes
    master_public_key_hash: bytes
    inheritance: int
    independence: int
    selected_public_key_hash: bytes
    proof_sha256: bytes
    adopted_at: int
    agent_installation_mode: str = "agent"
    custody_claim: str = "syn-os-keystore"

    def __post_init__(self) -> None:
        for name in ("offer_sha256", "master_public_key_hash", "selected_public_key_hash", "proof_sha256"):
            _fixed_bytes(getattr(self, name), 32, name)
        agent_key_path(self.inheritance, self.independence)
        _seconds(self.adopted_at, "adopted_at")
        if self.agent_installation_mode != "agent" or self.custody_claim != "syn-os-keystore":
            raise InvalidInputError("receipt must claim agent-mode SYN OS-keystore custody")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1, "type_ref": ADOPTION_TYPE,
            "offer_sha256": self.offer_sha256.hex(),
            "master_public_key_hash": self.master_public_key_hash.hex(),
            "inheritance": self.inheritance, "independence": self.independence,
            "selected_public_key_hash": self.selected_public_key_hash.hex(),
            "proof_sha256": self.proof_sha256.hex(), "adopted_at": self.adopted_at,
            "agent_installation_mode": self.agent_installation_mode,
            "custody_claim": self.custody_claim,
        }

    def signed_bytes(self) -> bytes:
        return _RECEIPT_DOMAIN + _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> AdoptionReceipt:
        required = {"schema_version", "type_ref", "offer_sha256", "master_public_key_hash",
                    "inheritance", "independence", "selected_public_key_hash", "proof_sha256",
                    "adopted_at", "agent_installation_mode", "custody_claim"}
        if (not isinstance(value, dict) or set(value) != required
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["type_ref"] != ADOPTION_TYPE):
            raise InvalidInputError("SYN adoption receipt has unsupported fields")
        return cls(
            _hex(value["offer_sha256"], 32, "offer hash"),
            _hex(value["master_public_key_hash"], 32, "master hash"),
            value["inheritance"], value["independence"],
            _hex(value["selected_public_key_hash"], 32, "selected hash"),
            _hex(value["proof_sha256"], 32, "key proof hash"),
            value["adopted_at"], value["agent_installation_mode"], value["custody_claim"],
        )


@dataclass(frozen=True, slots=True)
class SignedAdoptionReceipt:
    receipt: AdoptionReceipt
    key_proof: SignedSelectedKeyProof
    operator_public_key: bytes
    operator_signature: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, AdoptionReceipt) or not isinstance(self.key_proof, SignedSelectedKeyProof):
            raise InvalidInputError("SYN adoption requires operator receipt and agent proof")
        _fixed_bytes(self.operator_public_key, 32, "operator base public key")
        _fixed_bytes(self.operator_signature, 64, "operator signature")

    def verify(self, *, master_hash: bytes, now: int) -> None:
        if self.receipt.master_public_key_hash != _fixed_bytes(master_hash, 32, "master hash"):
            raise ScopeDeniedError("adoption targets a different SYN operator")
        if public_key_hash(self.operator_public_key) != master_hash:
            raise ScopeDeniedError("adoption operator signing key is not pinned")
        proof = self.key_proof
        proof.verify(now=now)
        p, r = proof.proof, self.receipt
        if (
            p.offer_sha256 != r.offer_sha256 or p.master_public_key_hash != r.master_public_key_hash
            or p.inheritance != r.inheritance or p.independence != r.independence
            or p.selected_public_key_hash != r.selected_public_key_hash
            or proof.digest != r.proof_sha256
            or not p.issued_at <= r.adopted_at <= p.expires_at
        ):
            raise ScopeDeniedError("adoption receipt does not match selected-key proof")
        _verify_ed25519(self.operator_public_key, self.operator_signature, r.signed_bytes())

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(self.receipt.signed_bytes() + self.operator_signature).digest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt.to_dict(), "key_proof": self.key_proof.to_dict(),
            "operator_public_key": self.operator_public_key.hex(),
            "operator_signature": self.operator_signature.hex(),
        }

    @classmethod
    def from_dict(cls, value: object) -> SignedAdoptionReceipt:
        if not isinstance(value, dict) or set(value) != {"receipt", "key_proof", "operator_public_key", "operator_signature"}:
            raise InvalidInputError("signed SYN adoption receipt has unsupported fields")
        return cls(
            AdoptionReceipt.from_dict(value["receipt"]),
            SignedSelectedKeyProof.from_dict(value["key_proof"]),
            _hex(value["operator_public_key"], 32, "operator public key"),
            _hex(value["operator_signature"], 64, "operator signature"),
        )
