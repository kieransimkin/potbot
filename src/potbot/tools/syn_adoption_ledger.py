"""Durably reconcile SYN-signed adoption proof with Phase 41 selected pledges.

This records a *public* signed adoption receipt. It does not create agent homes,
activate privileges, store selected private keys or assert OS keystore attestation.
A real SYN daemon must perform sealed adoption and its own durable custody steps.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.syn_adoption import SignedAdoptionReceipt
from potbot.domain.syn_agent_pledge import _fixed_bytes
from potbot.tools.syn_enrollment_ledger import SynEnrollmentLedger, _now

if TYPE_CHECKING:
    from potbot.tools.syn_local_daemon import SynLocalDaemonClient


@dataclass(frozen=True, slots=True)
class VerifiedSynAdoption:
    """External SYN claims adoption; Potbot has not independently audited custody."""

    offer_sha256: str
    agent_base_public_key_hash: str
    root_derivation_path: str
    receipt_sha256: str
    state: str = "syn_adoption_confirmed"


class SynAdoptionLedger:
    def __init__(self, enrollment: SynEnrollmentLedger) -> None:
        if not isinstance(enrollment, SynEnrollmentLedger):
            raise ScopeDeniedError("SYN adoption must use an existing private enrollment ledger")
        self.enrollment = enrollment
        with enrollment._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS syn_agent_adoptions (
                offer_sha256 TEXT PRIMARY KEY,
                master_hash TEXT NOT NULL,
                selected_public_key_hash TEXT NOT NULL,
                inheritance INTEGER NOT NULL CHECK(inheritance BETWEEN 0 AND 16777215),
                independence INTEGER NOT NULL CHECK(independence BETWEEN 1 AND 8),
                receipt_sha256 TEXT NOT NULL UNIQUE,
                adopted_at INTEGER NOT NULL,
                signed_receipt_json TEXT NOT NULL,
                FOREIGN KEY (offer_sha256) REFERENCES syn_agent_pledges(offer_sha256)
            );
            """)

    def record(self, adoption: SignedAdoptionReceipt, *, now: int | None = None) -> VerifiedSynAdoption:
        current = _now(now)
        if not isinstance(adoption, SignedAdoptionReceipt):
            raise ScopeDeniedError("SYN adoption requires a verified signed public receipt")
        adoption.verify(master_hash=self.enrollment.master_hash, now=current)
        identity = adoption.receipt
        offer_hash = identity.offer_sha256.hex()
        signature_digest = adoption.digest.hex()
        payload = json.dumps(adoption.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.enrollment._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                pledge = self.enrollment._get_locked(db, offer_hash)
                if (pledge["state"] != "selected"
                        or pledge["master_hash"] != self.enrollment.master_hash.hex()
                        or pledge["inheritance"] != identity.inheritance
                        or pledge["independence"] != identity.independence
                        or pledge["selected_public_key_hash"] != identity.selected_public_key_hash.hex()
                        or identity.adopted_at > pledge["expires_at"]):
                    raise ScopeDeniedError("SYN adoption does not match a selected, retained pledge")
                stored = db.execute(
                    "SELECT * FROM syn_agent_adoptions WHERE offer_sha256=?", (offer_hash,)
                ).fetchone()
                if stored is not None:
                    if stored["receipt_sha256"] != signature_digest or stored["signed_receipt_json"] != payload:
                        raise StateMismatchError("SYN adoption was already recorded with different evidence")
                    # Safe idempotent readback; no second provisioning side effect.
                else:
                    if current > pledge["expires_at"]:
                        raise StateMismatchError("SYN adoption window expired without a durable receipt")
                    db.execute(
                        "INSERT INTO syn_agent_adoptions "
                        "(offer_sha256,master_hash,selected_public_key_hash,inheritance,"
                        "independence,receipt_sha256,adopted_at,signed_receipt_json) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (offer_hash, self.enrollment.master_hash.hex(),
                         identity.selected_public_key_hash.hex(), identity.inheritance,
                         identity.independence, signature_digest, identity.adopted_at, payload),
                    )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise StateMismatchError("SYN adoption conflicts with a previously committed identity") from exc
            except BaseException:
                db.rollback()
                raise
        return self.inspect(offer_hash)

    def inspect(self, offer_sha256: str) -> VerifiedSynAdoption:
        if not isinstance(offer_sha256, str) or len(offer_sha256) != 64:
            raise ScopeDeniedError("SYN adoption inspection requires a full pledge digest")
        try:
            _fixed_bytes(bytes.fromhex(offer_sha256), 32, "pledge digest")
        except ValueError as exc:
            raise ScopeDeniedError("SYN adoption pledge digest is not hex") from exc
        if offer_sha256.lower() != offer_sha256:
            raise ScopeDeniedError("SYN adoption pledge must be canonical lowercase hex")
        with self.enrollment._connect() as db:
            row = db.execute(
                "SELECT * FROM syn_agent_adoptions WHERE offer_sha256=? AND master_hash=?",
                (offer_sha256, self.enrollment.master_hash.hex()),
            ).fetchone()
        if row is None:
            raise ScopeDeniedError("SYN agent has no confirmed adoption receipt")
        # An operator-local database can be restored or altered independently.
        # Never trust its projection without revalidating both public signatures
        # and exact digest/binding against the authoritative Phase 41 selection.
        try:
            receipt = SignedAdoptionReceipt.from_dict(json.loads(row["signed_receipt_json"]))
            receipt.verify(master_hash=self.enrollment.master_hash, now=receipt.receipt.adopted_at)
        except (ValueError, TypeError, KeyError) as exc:
            raise StateMismatchError("persisted SYN adoption record is malformed") from exc
        if (receipt.digest.hex() != row["receipt_sha256"]
                or receipt.receipt.offer_sha256.hex() != row["offer_sha256"]
                or receipt.receipt.selected_public_key_hash.hex() != row["selected_public_key_hash"]
                or receipt.receipt.inheritance != row["inheritance"]
                or receipt.receipt.independence != row["independence"]
                or receipt.receipt.adopted_at != row["adopted_at"]):
            raise StateMismatchError("persisted SYN adoption summary differs from its signed receipt")
        with self.enrollment._connect() as db:
            pledge = self.enrollment._get_locked(db, offer_sha256)
            if (pledge["state"] != "selected"
                    or pledge["selected_public_key_hash"] != row["selected_public_key_hash"]
                    or pledge["inheritance"] != row["inheritance"]
                    or pledge["independence"] != row["independence"]):
                raise StateMismatchError("SYN adoption no longer matches the selected pledge")
        from potbot.domain.syn_agent_pledge import agent_key_path

        return VerifiedSynAdoption(
            row["offer_sha256"], row["selected_public_key_hash"],
            agent_key_path(row["inheritance"], row["independence"]), row["receipt_sha256"],
        )

    def reconcile(self, daemon: SynLocalDaemonClient, offer_sha256: bytes,
                  *, now: int | None = None) -> VerifiedSynAdoption | None:
        """Read only a signed public receipt; never trigger private adoption here."""
        _fixed_bytes(offer_sha256, 32, "pledge digest")
        if daemon.endpoint.base_public_key_hash != self.enrollment.master_hash:
            raise ScopeDeniedError("SYN daemon does not match the enrollment operator")
        with self.enrollment._connect() as db:
            pledge = self.enrollment._get_locked(db, offer_sha256.hex())
            if pledge["state"] != "selected":
                raise StateMismatchError("only a selected pledge can be reconciled")
        receipt = daemon.adoption_receipt(offer_sha256)
        if receipt is None:
            return None
        return self.record(receipt, now=now)
