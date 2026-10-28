"""SYN operator-private encrypted P2P pledge inbox.

No hardware identifier or plaintext pledge is written to SQLite. A random AES
key lives only in the OS credential backend. The durable offer digest remains
reserved after expiry, preventing replay after an operator restart. Only a
trusted local SYN operator may explicitly read a pending pledge for attestation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .custody import _agent_key_lock, _entry, _native_backend
from .p2p_contracts import _sha, verify_signed_offer, verify_pulse
from .protocol import SynProtocolError, canonical, hex_bytes, parse
from .receipts import _private_directory

KEY_SUFFIX = ".p2p-inbox-key.v1"
MAX_PENDING = 4096
MAX_TOMBSTONES = 65536
AAD_DOMAIN = b"syn.p2p.encrypted-inbox.v1\x00"


def _safe_path(path: Path, *, exists: bool) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise SynProtocolError("SYN inbox must be a real absolute file")
    _private_directory(path.parent)
    if exists:
        try:
            st = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise SynProtocolError("SYN inbox is unavailable") from exc
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SynProtocolError("SYN inbox must be owner-private and unlinked")
    elif path.exists() or path.is_symlink():
        raise SynProtocolError("SYN inbox cannot be replaced")
    return path


class PledgeInbox:
    """Privileged SYN-only journal. This object is never exposed to agents."""

    def __init__(self, *, path: Path, service: str, account: str, master_hash: bytes,
                 network_id: str, backend: object | None = None):
        _entry(service, account)
        if len(account + KEY_SUFFIX) > 128:
            raise SynProtocolError("SYN inbox credential name is too long")
        if type(master_hash) is not bytes or len(master_hash) != 32:
            raise SynProtocolError("SYN inbox requires a pinned master hash")
        if not isinstance(network_id, str) or not 1 <= len(network_id) <= 64:
            raise SynProtocolError("SYN inbox network identifier is missing")
        self.path = _safe_path(path, exists=True)
        self.master_hash = master_hash
        self.network_id = network_id
        self.backend = _native_backend() if backend is None else backend
        try:
            secret = self.backend.get_password(service, account + KEY_SUFFIX)
        except Exception as exc:
            raise SynProtocolError("SYN inbox OS keystore is unavailable") from exc
        self.key = hex_bytes(secret, 32, "SYN inbox key")
        with self._db() as db:
            row = db.execute("SELECT master,network,key_commitment FROM inbox_metadata WHERE id=1").fetchone()
            if (row is None or row[0] != master_hash.hex() or row[1] != network_id
                    or row[2] != _sha(AAD_DOMAIN + self.key + master_hash).hex()):
                raise SynProtocolError("SYN inbox metadata or OS key changed")

    @classmethod
    def create(cls, *, path: Path, service: str, account: str, master_hash: bytes,
               network_id: str, lock_dir: Path, backend: object | None = None) -> PledgeInbox:
        _entry(service, account)
        store = _native_backend() if backend is None else backend
        with _agent_key_lock(lock_dir, service, account + ".p2p-inbox"):
            _safe_path(path, exists=False)
            try:
                old = store.get_password(service, account + KEY_SUFFIX)
            except Exception as exc:
                raise SynProtocolError("SYN inbox OS keystore is unavailable") from exc
            if old is not None:
                raise SynProtocolError("SYN inbox key already exists or installation is incomplete")
            if type(master_hash) is not bytes or len(master_hash) != 32:
                raise SynProtocolError("SYN inbox requires an exact master hash")
            if not isinstance(network_id, str) or not 1 <= len(network_id) <= 64:
                raise SynProtocolError("SYN inbox network is invalid")
            key = os.urandom(32)
            # Write keystore BEFORE journal. A crash leaves an explicit tombstone;
            # subsequent create must refuse, never replace the encryption key.
            try:
                store.set_password(service, account + KEY_SUFFIX, key.hex())
                if store.get_password(service, account + KEY_SUFFIX) != key.hex():
                    raise SynProtocolError("SYN inbox key custody could not be confirmed")
            except SynProtocolError:
                raise
            except Exception as exc:
                raise SynProtocolError("SYN inbox OS key creation failed") from exc
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            os.close(fd)
            db = sqlite3.connect(path, timeout=5, isolation_level=None)
            try:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("CREATE TABLE inbox_metadata (id INTEGER PRIMARY KEY CHECK(id=1),"
                           " master TEXT NOT NULL,network TEXT NOT NULL,key_commitment TEXT NOT NULL)")
                db.execute("CREATE TABLE pledge_inbox (offer_sha TEXT PRIMARY KEY,"
                           " device_handle TEXT NOT NULL UNIQUE,nonce BLOB UNIQUE,encrypted BLOB, created INTEGER NOT NULL,"
                           " expires INTEGER NOT NULL,state TEXT NOT NULL"
                           " CHECK(state IN ('pending','expired')))")
                db.execute("INSERT INTO inbox_metadata VALUES(1,?,?,?)",
                           (master_hash.hex(), network_id,
                            _sha(AAD_DOMAIN + key + master_hash).hex()))
                db.commit()
            finally:
                db.close()
            # No automatic deletion of a failed installation or partial journal.
            with open(path, "rb") as stream:
                os.fsync(stream.fileno())
            dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        return cls(path=path, service=service, account=account,
                   master_hash=master_hash, network_id=network_id, backend=store)

    @contextmanager
    def _db(self):
        _safe_path(self.path, exists=True)
        db = sqlite3.connect("file:" + quote(str(self.path), safe="/") + "?mode=rw",
                             uri=True, isolation_level=None, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA secure_delete=ON")
            yield db
        finally:
            db.close()

    def admit(self, signed_offer: dict[str, Any], signed_pulse: dict[str, Any],
              *, advert: dict[str, Any], now: int) -> str:
        """Durable first use. Repeated offers never receive a fresh admission."""
        verify_signed_offer(signed_offer, advert=advert, now=now)
        verify_pulse(signed_pulse, master=self.master_hash, network_id=self.network_id, now=now)
        if signed_pulse != advert["signed_pulse"]:
            raise SynProtocolError("SYN P2P inbox pulse differs from signed broadcast")
        if advert["signed_pulse"]["pulse"]["master_public_key_hash"] != self.master_hash.hex():
            raise SynProtocolError("SYN inbox belongs to another master")
        if advert["signed_pulse"]["pulse"]["network_id"] != self.network_id:
            raise SynProtocolError("SYN P2P inbox network mismatch")
        offer_digest = _sha(b"syn.agent.pledge.offer.v2\x00" + canonical(signed_offer["offer"])).hex()
        record = canonical({"signed_offer": signed_offer, "signed_pulse": signed_pulse})
        if len(record) > 4096:
            raise SynProtocolError("SYN P2P inbox record exceeds the private limit")
        device = hex_bytes(signed_offer["offer"]["hardware_unique_id"],
                           len(signed_offer["offer"]["hardware_unique_id"]) // 2,
                           "signed hardware identifier")
        handle = hmac.new(self.key, b"syn.p2p.hardware-handle.v1\x00" + device,
                          hashlib.sha256).hexdigest()
        nonce = os.urandom(12)
        if type(now) is not int or not 1 <= now < 2**63 - 301:
            raise SynProtocolError("SYN inbox clock must be positive and bounded")
        expiry = now + 300
        aad = (AAD_DOMAIN + self.master_hash + bytes.fromhex(offer_digest)
               + now.to_bytes(8, "big") + expiry.to_bytes(8, "big"))
        sealed = AESGCM(self.key).encrypt(nonce, record, aad)
        with self._db() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                total, pending = db.execute("SELECT count(*), count(*) FILTER (WHERE state='pending') FROM pledge_inbox").fetchone()
                if total >= MAX_TOMBSTONES or pending >= MAX_PENDING:
                    raise SynProtocolError("SYN inbox reached operator-managed storage quota")
                db.execute("INSERT INTO pledge_inbox (offer_sha,device_handle,nonce,encrypted,created,expires,state)"
                           " VALUES(?,?,?,?,?,?,'pending')",
                           (offer_digest, handle, nonce, sealed, now, expiry))
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise SynProtocolError("SYN P2P pledge replay has already been recorded") from exc
            except BaseException:
                db.rollback()
                raise
        return offer_digest

    def pending_digests(self) -> tuple[str, ...]:
        with self._db() as db:
            return tuple(row[0] for row in db.execute(
                "SELECT offer_sha FROM pledge_inbox WHERE state='pending' ORDER BY created,offer_sha"
            ).fetchall())

    def read_private(self, offer_sha: str, *, now: int) -> dict[str, Any]:
        """Only call inside SYN operator processes; never a public socket method."""
        digest = hex_bytes(offer_sha, 32, "operator-only offer")
        with self._db() as db:
            row = db.execute("SELECT nonce,encrypted,created,expires,state FROM pledge_inbox WHERE offer_sha=?",
                             (offer_sha,)).fetchone()
        if (row is None or row[4] != "pending" or type(now) is not int
                or not 1 <= row[2] <= now <= row[3] < 2**63
                or row[3] - row[2] != 300):
            raise SynProtocolError("SYN P2P pending pledge is missing or expired")
        try:
            plaintext = AESGCM(self.key).decrypt(
                row[0], row[1], AAD_DOMAIN + self.master_hash + digest
                + row[2].to_bytes(8, "big") + row[3].to_bytes(8, "big")
            )
        except (InvalidTag, ValueError) as exc:
            raise SynProtocolError("SYN P2P inbox record authentication failed") from exc
        record = parse(plaintext)
        if set(record) != {"signed_offer", "signed_pulse"}:
            raise SynProtocolError("SYN P2P inbox record is malformed")
        return record

    def expire(self, *, now: int | None = None) -> int:
        """Purge sensitive ciphertext but retain one-way replay tombstones."""
        current = int(time.time()) if now is None else now
        if type(current) is not int or current <= 0:
            raise SynProtocolError("SYN P2P expiry clock is invalid")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute(
                    "UPDATE pledge_inbox SET state='expired',nonce=NULL,encrypted=NULL"
                    " WHERE state='pending' AND expires<?", (current,)
                ).rowcount
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return changed
