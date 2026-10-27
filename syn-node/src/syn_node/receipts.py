"""SYN-owned immutable PUBLIC adoption receipts; no hardware IDs or key material.

A signed receipt alone does NOT attest OS custody or exclusive agent ownership.
Only independently qualified SYN adoption can authorize agent activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol import SynProtocolError, canonical, exact, hex_bytes

PROOF_DOMAIN = b"syn.agent.selected-key-proof.v1\x00"
RECEIPT_DOMAIN = b"syn.operator.agent-adoption.v1\x00"
PROOF_TYPE = "synny://type/syn-agent-selected-key-proof-v1"
RECEIPT_TYPE = "synny://type/syn-agent-adoption-receipt-v1"


def _int(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SynProtocolError(f"invalid SYN {label}")
    return value


def _verify(public_key: bytes, signature: bytes, signed: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signed)
    except (InvalidSignature, ValueError) as exc:
        raise SynProtocolError("SYN adoption signature invalid") from exc


def verify_adoption(record: object, *, expected_master_hash: bytes, now: int) -> str:
    """Independently check Phase 42 public custody CLAIM against two signatures.

    This authenticates an operator's statement and a chosen-key signature, not
    third-party hardware attestation or independent control of the child key.
    """
    if type(expected_master_hash) is not bytes or len(expected_master_hash) != 32:
        raise SynProtocolError("SYN receipt verifier needs a pinned operator hash")
    outer = exact(record, {"receipt", "key_proof", "operator_public_key", "operator_signature"}, "adoption")
    signed_proof = exact(outer["key_proof"], {"proof", "signature"}, "key proof")
    proof = exact(signed_proof["proof"], {
        "schema_version", "type_ref", "offer_sha256", "master_public_key_hash",
        "inheritance", "independence", "selected_public_key", "challenge",
        "issued_at", "expires_at",
    }, "selected-key proof")
    receipt = exact(outer["receipt"], {
        "schema_version", "type_ref", "offer_sha256", "master_public_key_hash",
        "inheritance", "independence", "selected_public_key_hash", "proof_sha256",
        "adopted_at", "agent_installation_mode", "custody_claim",
    }, "adoption receipt")
    if (type(proof["schema_version"]) is not int or proof["schema_version"] != 1
            or proof["type_ref"] != PROOF_TYPE or type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != 1 or receipt["type_ref"] != RECEIPT_TYPE):
        raise SynProtocolError("SYN adoption type revision not supported")
    offer = hex_bytes(receipt["offer_sha256"], 32, "offer hash")
    if not offer or not any(offer):
        raise SynProtocolError("SYN adoption offer digest cannot be zero")
    hash_ = hex_bytes(receipt["master_public_key_hash"], 32, "master hash")
    pub = hex_bytes(outer["operator_public_key"], 32, "operator public key")
    if hash_ != expected_master_hash or hashlib.sha256(pub).digest() != expected_master_hash:
        raise SynProtocolError("SYN adoption receipt belongs to an unpinned operator")
    if (proof["offer_sha256"] != receipt["offer_sha256"]
            or proof["master_public_key_hash"] != receipt["master_public_key_hash"]
            or proof["inheritance"] != receipt["inheritance"]
            or proof["independence"] != receipt["independence"]):
        raise SynProtocolError("SYN receipt and signed agent proof disagree")
    _int(receipt["inheritance"], 0, 16_777_215, "inheritance")
    _int(receipt["independence"], 1, 8, "independence")
    chosen = hex_bytes(proof["selected_public_key"], 32, "chosen key")
    if hashlib.sha256(chosen).hexdigest() != receipt["selected_public_key_hash"]:
        raise SynProtocolError("SYN selected public key does not match adopted identity")
    sig = hex_bytes(signed_proof["signature"], 64, "proof signature")
    issued = _int(proof["issued_at"], 1, 2**63 - 1, "issued time")
    expires = _int(proof["expires_at"], 1, 2**63 - 1, "expiry")
    adopted = _int(receipt["adopted_at"], 1, 2**63 - 1, "adoption time")
    if (not 1 <= expires - issued <= 300 or type(now) is not int
            or not issued <= now <= expires or not issued <= adopted <= expires):
        raise SynProtocolError("SYN selected-key proof outside its accepted validity window")
    if hex_bytes(proof["challenge"], 32, "adoption challenge") == bytes(32):
        raise SynProtocolError("SYN adoption challenge cannot be zero")
    preimage = PROOF_DOMAIN + canonical(proof)
    _verify(chosen, sig, preimage)
    if hashlib.sha256(preimage + sig).hexdigest() != receipt["proof_sha256"]:
        raise SynProtocolError("SYN operator receipt does not bind the signed key proof")
    if receipt["agent_installation_mode"] != "agent" or receipt["custody_claim"] != "syn-os-keystore":
        raise SynProtocolError("SYN receipt does not declare separate agent custody")
    _verify(pub, hex_bytes(outer["operator_signature"], 64, "operator signature"),
            RECEIPT_DOMAIN + canonical(receipt))
    return hashlib.sha256(RECEIPT_DOMAIN + canonical(receipt)
                          + hex_bytes(outer["operator_signature"], 64, "signature")).hexdigest()


def _private_directory(path: Path) -> Path:
    if (not isinstance(path, Path) or not path.is_absolute() or path.is_symlink()
            or path.resolve() != path):
        raise SynProtocolError("SYN journal directory must be an exact absolute path")
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise SynProtocolError("SYN journal directory must be owned privately")
    return path


class PublicAdoptionJournal:
    """Immutable signed public receipts with conservative, replay-safe readback.

    A receipt may be deposited only through a trusted SYN operator workflow,
    never through the unauthenticated socket API or model-produced tool input.
    """

    def __init__(self, path: Path, *, expected_master_hash: bytes) -> None:
        self.path = path
        self.master_hash = expected_master_hash
        if type(expected_master_hash) is not bytes or len(expected_master_hash) != 32:
            raise SynProtocolError("SYN journal needs a pinned master identity")
        if not isinstance(path, Path) or not path.is_absolute():
            raise SynProtocolError("SYN journal path must be absolute")
        _private_directory(path.parent)
        if path.is_symlink():
            raise SynProtocolError("SYN journal file may not be a symlink")
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            handle = None
        if handle is not None:
            os.close(handle)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise SynProtocolError("SYN receipt store is not an owner-private ordinary file")
        with self._db() as db:
            db.execute("PRAGMA busy_timeout=3000")
            db.execute("CREATE TABLE IF NOT EXISTS public_adoptions ("
                       "offer_sha256 TEXT PRIMARY KEY, master_sha256 TEXT NOT NULL, "
                       "digest TEXT NOT NULL UNIQUE, signed_json TEXT NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        _private_directory(self.path.parent)
        info = self.path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise SynProtocolError("SYN journal permissions or location changed")
        db = sqlite3.connect(self.path, timeout=3.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=3000")
        return db

    def publish(self, record: dict[str, Any], *, now: int) -> str:
        digest = verify_adoption(record, expected_master_hash=self.master_hash, now=now)
        body = canonical(record).decode("ascii")
        pledge = record["receipt"]["offer_sha256"]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,signed_json FROM public_adoptions WHERE offer_sha256=?", (pledge,)).fetchone()
            if row is not None:
                if row["digest"] != digest or row["signed_json"] != body:
                    raise SynProtocolError("SYN public receipt conflicts with existing commitment")
            else:
                db.execute("INSERT INTO public_adoptions(offer_sha256,master_sha256,digest,signed_json) "
                           "VALUES(?,?,?,?)", (pledge, self.master_hash.hex(), digest, body))
        return digest

    def inspect(self, offer_sha256: bytes) -> dict[str, Any] | None:
        if type(offer_sha256) is not bytes or len(offer_sha256) != 32:
            raise SynProtocolError("SYN receipt lookup requires 32-byte offer digest")
        with self._db() as db:
            row = db.execute("SELECT * FROM public_adoptions WHERE offer_sha256=?", (offer_sha256.hex(),)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row["signed_json"])
            verified = verify_adoption(record, expected_master_hash=self.master_hash,
                                       now=record["receipt"]["adopted_at"])
        except (KeyError, ValueError, TypeError) as exc:
            raise SynProtocolError("SYN stored signed receipt is corrupt") from exc
        if (verified != row["digest"] or row["master_sha256"] != self.master_hash.hex()
                or canonical(record).decode("ascii") != row["signed_json"]
                or record["receipt"]["offer_sha256"] != offer_sha256.hex()):
            raise SynProtocolError("SYN stored receipt summary was altered")
        return record
