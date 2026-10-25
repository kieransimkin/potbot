"""Operator-local, durable SYN pledge transaction ledger (no private keys).

The authoritative SYN daemon supplies hardware attestation, private blinding,
operator approval, public child-key derivation and signing. Potbot's integration
never receives seed/chain code/private keys and cannot activate an adopted agent.
This is a host-only bridge, NOT a remotely callable pledge API or P2P transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.syn_agent_pledge import (
    IdentityCandidates,
    SignedPledgeOffer,
    _fixed_bytes,
)
from potbot.domain.syn_enrollment_protocol import (
    SignedIdentityCandidates,
    SignedIndependenceSelection,
    SignedOperatorPulse,
)

_ENROLLMENT_TTL = 300


class SynOperatorAuthority(Protocol):
    """Authenticated, host-configured SYN operator API; NOT a model-visible tool.

    Implementations must be supplied by SYN, use its OS keystore, and verify
    device attestation and owner approval. The opaque device handle MUST be a
    keyed HMAC/PRF output, never SHA256(MAC) or raw hardware metadata.
    """

    def attest_and_blind_device(self, hardware_id: bytes) -> bytes: ...

    def authorize_offer(self, offer_sha256: bytes) -> bool: ...

    def derive_candidate_public_hashes(self, inheritance: int) -> tuple[bytes, ...]: ...

    def operator_public_key(self) -> bytes: ...

    def sign_candidates(self, signing_preimage: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PledgeRecord:
    offer_sha256: str
    state: str
    expires_at: int
    inheritance: int | None = None
    independence: int | None = None
    selected_public_key_hash: str | None = None


def _now(value: int | None) -> int:
    timestamp = int(time.time()) if value is None else value
    if type(timestamp) is not int or not 1 <= timestamp < 2**63:
        raise InvalidInputError("operator clock must be Unix seconds")
    return timestamp


def _operator_db_file(path: Path, sandbox_home: Path | None) -> Path:
    """Require a private local operator directory, never a sandbox mount.

    This POSIX-only local journal is not an OS keystore and holds NO key or
    hardware ID. Windows native installations must use SYN's qualified host
    storage; no guessed NTFS ACL or unsafe SQLite fallback is attempted.
    """
    if os.name != "posix":
        raise CapabilityUnavailableError("SYN enrollment journal needs a qualified POSIX host")
    if not isinstance(path, Path) or not path.is_absolute() or path.suffix != ".sqlite3":
        raise InvalidInputError("SYN enrollment requires an absolute operator SQLite path")
    directory = path.parent
    if directory.resolve() != directory or directory.is_symlink() or not directory.is_dir():
        raise ScopeDeniedError("SYN enrollment needs an existing private operator directory")
    dstat = directory.stat()
    if dstat.st_uid != os.getuid() or dstat.st_mode & 0o077:
        raise ScopeDeniedError("SYN operator directory ownership or mode is unsafe")
    if sandbox_home is not None:
        if not sandbox_home.is_absolute():
            raise InvalidInputError("sandbox home must be absolute")
        home = sandbox_home.resolve()
        resolved_dir = directory.resolve()
        if (resolved_dir == home or resolved_dir.is_relative_to(home)
                or home.is_relative_to(resolved_dir)):
            raise ScopeDeniedError("SYN operator journal overlaps a sandbox")
    if path.is_symlink():
        raise ScopeDeniedError("SYN operator journal may not be a symlink")
    if path.exists():
        fs = path.stat()
        if not stat.S_ISREG(fs.st_mode) or fs.st_uid != os.getuid() or fs.st_nlink != 1 or fs.st_mode & 0o077:
            raise ScopeDeniedError("SYN operator journal has unsafe inode or permissions")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        os.close(fd)
    return path


class SynEnrollmentLedger:
    """Serialize first-use pledges and 24-bit inheritance allocation.

    A signed operator pulse is a *broadcast*: several unrelated agents may
    answer the same pulse during its validity window. Unique signed-offer and
    SYN-blinded device handles, not globally single-use pulse challenges, are
    the anti-replay and anti-duplication boundaries.

    Every method fails closed on an inconsistent state. The journal retains
    only an opaque SYN-blinded hardware handle; clear hardware identifiers are
    never written here. An expired/rejected transaction remains reserved so a
    replay cannot turn into a fresh pledge without explicit SYN reconciliation.
    """

    def __init__(
        self, path: Path, *, master_hash: bytes, network_id: str,
        authority: SynOperatorAuthority, sandbox_home: Path | None = None,
    ) -> None:
        self.path = _operator_db_file(path, sandbox_home)
        self.master_hash = _fixed_bytes(master_hash, 32, "SYN master hash")
        if not isinstance(network_id, str) or not network_id:
            raise InvalidInputError("SYN operator network is required")
        self.network_id = network_id
        self.authority = authority
        if not all(callable(getattr(authority, item, None)) for item in (
            "attest_and_blind_device", "authorize_offer", "derive_candidate_public_hashes",
            "operator_public_key", "sign_candidates",
        )):
            raise CapabilityUnavailableError("SYN operator identity authority is unavailable")
        from potbot.domain.syn_agent_pledge import public_key_hash

        if public_key_hash(authority.operator_public_key()) != self.master_hash:
            raise StateMismatchError("SYN authority is not the configured operator master")
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS syn_agent_pledges (
                offer_sha256 TEXT PRIMARY KEY,
                master_hash TEXT NOT NULL,
                network_id TEXT NOT NULL,
                device_handle TEXT NOT NULL,
                pulse_challenge TEXT NOT NULL,
                temporary_public_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','offered','selected','expired','denied')),
                operator_byte INTEGER,
                inheritance INTEGER,
                candidate_json TEXT,
                full_candidate_hashes_json TEXT,
                independence INTEGER,
                selected_public_key_hash TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                UNIQUE(master_hash, device_handle),
                UNIQUE(master_hash, inheritance)
            );
            CREATE INDEX IF NOT EXISTS syn_agent_pledges_state ON syn_agent_pledges(state);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            # Rollback journaling keeps one atomic writer even across operator
            # processes; directory mode 0700 protects its transient sidecar files.
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _digest(signed: SignedPledgeOffer) -> str:
        return hashlib.sha256(signed.offer.signed_bytes()).hexdigest()

    def begin(
        self, signed: SignedPledgeOffer, pulse: SignedOperatorPulse,
        *, now: int | None = None,
    ) -> PledgeRecord:
        current = _now(now)
        pulse.verify(master_hash=self.master_hash, now=current, network_id=self.network_id)
        signed.verify(
            expected_master_hash=self.master_hash,
            expected_pulse_challenge=pulse.pulse.challenge,
            now_unix_seconds=current,
        )
        if not pulse.pulse.issued_at <= signed.offer.timestamp_unix_seconds <= pulse.pulse.expires_at:
            raise ScopeDeniedError("pledge signature is not tied to pulse validity interval")
        # SYN attests the device and returns a *secret-key-blinded* opaque handle.
        # This call must not return a raw/hardcoded/stable public MAC hash.
        device_handle = _fixed_bytes(
            self.authority.attest_and_blind_device(signed.offer.hardware_unique_id),
            32, "SYN private device handle",
        )
        digest = self._digest(signed)
        with self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO syn_agent_pledges (offer_sha256,master_hash,network_id,"
                    "device_handle,pulse_challenge,temporary_public_key,created_at,expires_at,state) "
                    "VALUES(?,?,?,?,?,?,?,?, 'pending')",
                    (
                        digest, self.master_hash.hex(), self.network_id,
                        device_handle.hex(), pulse.pulse.challenge.hex(),
                        signed.temporary_public_key.hex(), current, current + _ENROLLMENT_TTL,
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ScopeDeniedError(
                    "pledge replay or duplicate device enrollment"
                ) from exc
        return PledgeRecord(digest, "pending", current + _ENROLLMENT_TTL)

    def _get_locked(self, db: sqlite3.Connection, offer_sha256: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM syn_agent_pledges WHERE offer_sha256=? AND master_hash=?",
            (offer_sha256, self.master_hash.hex()),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("pledge is not enrolled with this operator")
        return row

    def inspect(self, offer_sha256: str) -> PledgeRecord:
        with self._connect() as db:
            row = self._get_locked(db, offer_sha256)
            return PledgeRecord(
                row["offer_sha256"], row["state"], row["expires_at"],
                row["inheritance"], row["independence"], row["selected_public_key_hash"],
            )

    def _verify_original(self, row: sqlite3.Row, signed: SignedPledgeOffer) -> None:
        if (
            self._digest(signed) != row["offer_sha256"]
            or signed.temporary_public_key.hex() != row["temporary_public_key"]
            or signed.offer.pulse_challenge.hex() != row["pulse_challenge"]
            or signed.offer.target_master_public_key_hash != self.master_hash
        ):
            raise ScopeDeniedError("pledge transcript differs from the accepted offer")
        # Re-verify original signing key without assuming the old pulse is still
        # live. Session expiry is checked separately against the database clock.
        signed.verify(
            expected_master_hash=self.master_hash,
            expected_pulse_challenge=signed.offer.pulse_challenge,
            now_unix_seconds=signed.offer.timestamp_unix_seconds,
        )

    def offer_candidates(
        self, signed: SignedPledgeOffer, *, operator_byte: int,
        now: int | None = None,
    ) -> SignedIdentityCandidates:
        current = _now(now)
        digest = self._digest(signed)
        # Reject unregistered, expired or changed pledges *before* asking SYN
        # to authorize, derive public children or sign an announcement.
        with self._connect() as db:
            row = self._get_locked(db, digest)
            self._verify_original(row, signed)
            if row["state"] != "pending" or row["expires_at"] < current:
                raise StateMismatchError("pledge is no longer pending or has expired")
        if self.authority.authorize_offer(bytes.fromhex(digest)) is not True:
            raise ScopeDeniedError("SYN operator has not authorized this pledge")
        # The private derivation exists ONLY inside SYN. This API exposes full
        # public key hashes, never private keys, child chain codes or seed.
        from potbot.domain.syn_agent_pledge import calculate_inheritance

        inheritance = calculate_inheritance(signed.offer.hardware_unique_id, operator_byte)
        full_hashes = self.authority.derive_candidate_public_hashes(inheritance)
        candidates = IdentityCandidates.from_public_hashes(
            offer=signed.offer, operator_byte=operator_byte,
            full_candidate_hashes=full_hashes,
        )
        operator_pub = _fixed_bytes(self.authority.operator_public_key(), 32, "operator key")
        preimage = SignedIdentityCandidates(
            candidates, operator_pub, bytes(64),
        ).signed_bytes()
        operator_signature = _fixed_bytes(
            self.authority.sign_candidates(preimage), 64, "operator signature",
        )
        signed_candidates = SignedIdentityCandidates(candidates, operator_pub, operator_signature)
        signed_candidates.verify(offer=signed.offer, master_hash=self.master_hash)
        public_response = json.dumps(signed_candidates.to_dict(), sort_keys=True, separators=(",", ":"))
        encoded_hashes = json.dumps([h.hex() for h in full_hashes], separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_locked(db, digest)
                self._verify_original(row, signed)
                if row["state"] != "pending" or row["expires_at"] < current:
                    raise StateMismatchError("pledge is no longer pending or has expired")
                db.execute(
                    "UPDATE syn_agent_pledges SET state='offered',operator_byte=?,inheritance=?,"
                    "candidate_json=?,full_candidate_hashes_json=?,revision=revision+1 "
                    "WHERE offer_sha256=? AND state='pending'",
                    (operator_byte, inheritance, public_response, encoded_hashes, digest),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ScopeDeniedError(
                    "24-bit inheritance already reserved; SYN must choose another operator byte"
                ) from exc
            except BaseException:
                db.rollback()
                raise
        return signed_candidates

    def select(
        self, signed_offer: SignedPledgeOffer,
        signed_candidates: SignedIdentityCandidates,
        signed_selection: SignedIndependenceSelection,
        *, now: int | None = None,
    ) -> PledgeRecord:
        current = _now(now)
        digest = self._digest(signed_offer)
        signed_candidates.verify(offer=signed_offer.offer, master_hash=self.master_hash)
        signed_selection.verify(
            candidates=signed_candidates,
            expected_agent_public_key=signed_offer.temporary_public_key,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_locked(db, digest)
                self._verify_original(row, signed_offer)
                if row["state"] != "offered" or row["expires_at"] < current:
                    raise StateMismatchError("pledge selection is expired, replayed or unoffered")
                expected_candidates = json.loads(row["candidate_json"])
                if expected_candidates != signed_candidates.to_dict():
                    raise ScopeDeniedError("selection used a different operator candidate offer")
                independence = signed_selection.selection.independence
                full_hashes = json.loads(row["full_candidate_hashes_json"])
                full_hash = full_hashes[independence - 1]
                if bytes.fromhex(full_hash)[:3] != signed_selection.selection.chosen_short_public_key_hash:
                    raise StateMismatchError("SYN candidate full/short public hash differs")
                db.execute(
                    "UPDATE syn_agent_pledges SET state='selected',independence=?,"
                    "selected_public_key_hash=?,revision=revision+1 "
                    "WHERE offer_sha256=? AND state='offered'",
                    (independence, full_hash, digest),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        # 'selected' is NOT 'adopted'. SYN still must demonstrate selected-key
        # custody/activation under a separately agreed adoption protocol.
        return PledgeRecord(
            digest, "selected", row["expires_at"], row["inheritance"],
            independence, full_hash,
        )

    def expire(self, offer_sha256: str, *, now: int | None = None) -> PledgeRecord:
        current = _now(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_locked(db, offer_sha256)
                if row["expires_at"] < current and row["state"] in {"pending", "offered"}:
                    db.execute(
                        "UPDATE syn_agent_pledges SET state='expired',revision=revision+1 "
                        "WHERE offer_sha256=?", (offer_sha256,),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.inspect(offer_sha256)
