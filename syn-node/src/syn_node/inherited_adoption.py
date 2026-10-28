"""SYN-owned confidential adoption of ONE selected Ed25519-BIP32 V2 key.

This is an operator-private/agent-private handoff implementation, NOT a public
SYN socket method. The operator MUST supply an independently qualified,
transactional Phase-41 selection authorizer; no caller-controlled ``approved``
boolean is sufficient. SYN OS credential custody and encrypted delivery stay
outside Potbot. Ordinary Python memory is not HSM / hardware non-exportability.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization

from .custody import AgentTemporaryOsSigner, _agent_key_lock, _entry, _native_backend
from .hd_keys import PythonHdOperatorSigner, ROOT_PATH, _load_operator, _pycardano
from .protocol import SynProtocolError, canonical, exact, hex_bytes, parse
from .receipts import (PROOF_DOMAIN, PROOF_TYPE, RECEIPT_DOMAIN, RECEIPT_TYPE,
                       PublicAdoptionJournal, verify_adoption)

GRANT_TYPE = "synny://type/syn-inherited-key-grant-v1"
REQUEST_TYPE = "synny://type/syn-inherited-key-request-v1"
SEALED_TYPE = "synny://type/syn-inherited-key-envelope-v1"
GRANT_DOMAIN = b"syn.agent.inheritance.grant.v1\x00"
REQUEST_DOMAIN = b"syn.agent.inheritance.request.v1\x00"
SEAL_DOMAIN = b"syn.agent.inheritance.seal.v1\x00"
KDF_DOMAIN = b"syn.agent.inheritance.aes256gcm.v1\x00"
MAX_LIFETIME = 300
ADOPTED_SLOTS = ("xprivate", "public", "chain", "manifest")
TRANSFER_SLOTS = ("transfer_private", "request_digest")


class SelectionAuthorizer(Protocol):
    """Trusted SYN operator adapter over an *admitted* durable selection ledger.

    Real implementations must perform a fresh, fenced selected-state lookup and
    validate the original signed pledge, device policy and independent owner
    acceptance. Potbot's public offer parser cannot grant this authority.
    """

    def require_selected(self, selection: dict[str, object]) -> None: ...


def _sha(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _check_time(now: int, start: int, end: int) -> None:
    if (type(now) is not int or type(start) is not int or type(end) is not int
            or not 1 <= start <= now <= end < 2**63 or not 1 <= end - start <= MAX_LIFETIME):
        raise SynProtocolError("SYN inherited-key authorization is stale or invalid")


def _int(value: object, start: int, end: int, label: str) -> int:
    if type(value) is not int or not start <= value <= end:
        raise SynProtocolError(f"SYN {label} is outside its authorized range")
    return value


def _verify(public: bytes, signature: bytes, preimage: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, preimage)
    except (InvalidSignature, ValueError) as exc:
        raise SynProtocolError("SYN inherited-key signature verification failed") from exc


def _verify_grant(packet: object, expected_master: bytes, now: int) -> tuple[dict[str, Any], bytes]:
    outer = exact(packet, {"grant", "operator_public_key", "signature"}, "operator grant")
    body = exact(outer["grant"], {
        "schema_version", "type_ref", "offer_sha256", "master_public_key_hash",
        "temporary_public_key", "inheritance", "independence", "selected_public_key_hash",
        "selection_sha256", "challenge", "issued_at", "expires_at",
    }, "operator grant fields")
    if type(body["schema_version"]) is not int or body["schema_version"] != 1 or body["type_ref"] != GRANT_TYPE:
        raise SynProtocolError("SYN inherited-key grant revision differs")
    if type(expected_master) is not bytes or len(expected_master) != 32:
        raise SynProtocolError("SYN expected operator base hash required")
    pub = hex_bytes(outer["operator_public_key"], 32, "operator public key")
    if (_sha(pub) != expected_master or
            hex_bytes(body["master_public_key_hash"], 32, "master hash") != expected_master):
        raise SynProtocolError("SYN grant targets the wrong operator")
    for name in ("offer_sha256", "temporary_public_key", "selected_public_key_hash",
                 "selection_sha256", "challenge"):
        if hex_bytes(body[name], 32, name) == bytes(32):
            raise SynProtocolError("SYN inherited-key grant contains a zero identity")
    _int(body["inheritance"], 0, 0xFFFFFF, "inheritance")
    _int(body["independence"], 1, 8, "independence")
    _check_time(now, body["issued_at"], body["expires_at"])
    sig = hex_bytes(outer["signature"], 64, "operator grant signature")
    _verify(pub, sig, GRANT_DOMAIN + canonical(body))
    return body, _sha(GRANT_DOMAIN + canonical(body) + sig)


def _selection(body: dict[str, Any]) -> dict[str, object]:
    """No raw hardware identifier; selection is bound to operator-signed evidence."""
    return {field: body[field] for field in (
        "offer_sha256", "master_public_key_hash", "temporary_public_key",
        "inheritance", "independence", "selected_public_key_hash", "selection_sha256",
    )}


def _verify_request(packet: object, grant: dict[str, Any], grant_hash: bytes, now: int) -> tuple[dict[str, Any], bytes]:
    outer = exact(packet, {"request", "temporary_signature"}, "agent adoption request")
    body = exact(outer["request"], {
        "schema_version", "type_ref", "grant_sha256", "transfer_public_key",
        "request_nonce", "requested_at",
    }, "agent request fields")
    if type(body["schema_version"]) is not int or body["schema_version"] != 1 or body["type_ref"] != REQUEST_TYPE:
        raise SynProtocolError("SYN adoption request has a wrong revision")
    if hex_bytes(body["grant_sha256"], 32, "grant digest") != grant_hash:
        raise SynProtocolError("SYN adoption request changes the signed grant")
    if (hex_bytes(body["request_nonce"], 32, "request challenge") == bytes(32)
            or hex_bytes(body["transfer_public_key"], 32, "transfer public key") == bytes(32)):
        raise SynProtocolError("SYN adoption request contains an empty challenge")
    _check_time(now, grant["issued_at"], grant["expires_at"])
    if type(body["requested_at"]) is not int or not grant["issued_at"] <= body["requested_at"] <= now:
        raise SynProtocolError("SYN transfer request is stale or from the future")
    sig = hex_bytes(outer["temporary_signature"], 64, "agent temporary signature")
    _verify(hex_bytes(grant["temporary_public_key"], 32, "temporary public key"),
            sig, REQUEST_DOMAIN + canonical(body))
    return body, _sha(REQUEST_DOMAIN + canonical(body) + sig)


def _credential_slot(account: str, name: str) -> str:
    if name not in (*ADOPTED_SLOTS, *TRANSFER_SLOTS):
        raise SynProtocolError("unknown SYN adoption credential slot")
    value = f"{account}.adoption.{name}.v1"
    if len(value) > 128:
        raise SynProtocolError("SYN adoption account exceeds credential limits")
    return value


def _read_keyring(store: object, service: str, account: str, name: str) -> str | None:
    try:
        return store.get_password(service, _credential_slot(account, name))  # type: ignore[attr-defined]
    except Exception as exc:
        raise SynProtocolError("SYN OS credential read unavailable") from exc


def _write_keyring(store: object, service: str, account: str, name: str, value: str) -> None:
    try:
        store.set_password(service, _credential_slot(account, name), value)  # type: ignore[attr-defined]
    except Exception as exc:
        raise SynProtocolError("SYN OS credential write failed; identity must be reconciled") from exc
    if _read_keyring(store, service, account, name) != value:
        raise SynProtocolError("SYN OS credential readback failed; identity blocked")


def _x25519_public(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)


def _x25519_secret(private: X25519PrivateKey) -> bytes:
    return private.private_bytes(serialization.Encoding.Raw,
                                 serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())


def _derive_sealing_key(private: X25519PrivateKey, peer: bytes, salt: bytes,
                        grant_hash: bytes, request_hash: bytes) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
    except ValueError as exc:
        raise SynProtocolError("SYN key agreement rejected the peer public key") from exc
    if shared == bytes(32):
        raise SynProtocolError("SYN key agreement returned an invalid shared secret")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=KDF_DOMAIN + grant_hash + request_hash).derive(shared)


def _seal_node(signer: PythonHdOperatorSigner, inheritance: int, independence: int) -> tuple[bytes, bytes, bytes]:
    """Only SYN's operator process may invoke private HD derivation."""
    values, identity = _load_operator(signer.service, signer.account)
    if bytes.fromhex(str(identity["base_public_key_hash"])) != signer.expected_public_hash:
        raise SynProtocolError("SYN operator keystore identity changed")
    wallet_cls, _ = _pycardano()
    try:
        wallet = wallet_cls.from_entropy(values["entropy"].hex(), passphrase="")
        node = wallet.derive_from_path(f"{ROOT_PATH}/{inheritance}'/{independence}/0")
        priv, pub, chain = bytes(node.xprivate_key), bytes(node.public_key), bytes(node.chain_code)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SynProtocolError("SYN selected HD derivation failed") from exc
    if len(priv) != 64 or len(pub) != 32 or len(chain) != 32:
        raise SynProtocolError("SYN selected HD node has invalid lengths")
    return priv, pub, chain


class SynHandoffLedger:
    """Private operator transaction journal; ambiguous seal failures stay fenced."""

    def __init__(self, path: Path, *, operator_hash: bytes):
        if type(operator_hash) is not bytes or len(operator_hash) != 32:
            raise SynProtocolError("SYN operator ledger requires a pinned master")
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise SynProtocolError("SYN handoff ledger path must be absolute and non-symlink")
        from .receipts import _private_directory
        _private_directory(path.parent)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        st = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
                or st.st_mode & 0o077):
            raise SynProtocolError("SYN handoff ledger must be a private ordinary file")
        self.path, self.master_hash = path, operator_hash
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS handoffs ("
                       "offer_sha256 TEXT PRIMARY KEY, master_sha256 TEXT NOT NULL, "
                       "grant_sha256 TEXT NOT NULL, request_sha256 TEXT, receipt_sha256 TEXT, "
                       "state TEXT NOT NULL CHECK(state IN ('granted','sealed','confirmed'))) ")

    def _connect(self) -> sqlite3.Connection:
        st = self.path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o077):
            raise SynProtocolError("SYN handoff ledger permissions changed")
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def reserve_grant(self, *, offer: str, grant_hash: str) -> None:
        hex_bytes(offer, 32, "offer")
        hex_bytes(grant_hash, 32, "grant")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO handoffs(offer_sha256,master_sha256,grant_sha256,state) "
                           "VALUES(?,?,?,'granted')", (offer, self.master_hash.hex(), grant_hash))
            except sqlite3.IntegrityError as exc:
                raise SynProtocolError("SYN offer has already reserved an identity handoff") from exc

    def reserve_seal(self, *, offer: str, grant_hash: str, request_hash: str) -> None:
        hex_bytes(request_hash, 32, "request")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,grant_sha256,master_sha256 FROM handoffs "
                             "WHERE offer_sha256=?", (offer,)).fetchone()
            if (row is None or row[0] != "granted" or row[1] != grant_hash
                    or row[2] != self.master_hash.hex()):
                raise SynProtocolError("SYN selected offer is not available for sealing")
            db.execute("UPDATE handoffs SET state='sealed', request_sha256=? "
                       "WHERE offer_sha256=? AND state='granted'", (request_hash, offer))

    def confirm(self, *, offer: str, grant_hash: str, receipt_hash: str) -> None:
        hex_bytes(receipt_hash, 32, "receipt digest")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,grant_sha256,receipt_sha256,master_sha256 "
                             "FROM handoffs WHERE offer_sha256=?", (offer,)).fetchone()
            if (row is None or row[1] != grant_hash or row[3] != self.master_hash.hex()
                    or (row[0] == "confirmed" and row[2] != receipt_hash)
                    or row[0] not in {"sealed", "confirmed"}):
                raise SynProtocolError("SYN adoption confirmation conflicts with original handoff")
            if row[0] == "sealed":
                db.execute("UPDATE handoffs SET state='confirmed',receipt_sha256=? "
                           "WHERE offer_sha256=?", (receipt_hash, offer))


def grant_selected_key(*, selection: dict[str, object], authorizer: SelectionAuthorizer,
                       signer: PythonHdOperatorSigner, ledger: SynHandoffLedger,
                       now: int, lifetime: int = 240) -> dict[str, Any]:
    """Mint a one-use operator grant ONLY after independently checking selected.

    This is not a socket endpoint; production authorizer wiring must verify the
    existing Phase-41 selected transaction and original signed acceptance.
    """
    if type(now) is not int or not 1 <= now < 2**63 or type(lifetime) is not int or not 1 <= lifetime <= 300:
        raise SynProtocolError("SYN grant lifetime is not admitted")
    approved = exact(selection, {"offer_sha256", "master_public_key_hash", "temporary_public_key",
                                 "inheritance", "independence", "selected_public_key_hash",
                                 "selection_sha256"}, "SYN selected enrollment")
    for key in ("offer_sha256", "master_public_key_hash", "temporary_public_key",
                "selected_public_key_hash", "selection_sha256"):
        hex_bytes(approved[key], 32, key)
    _int(approved["inheritance"], 0, 0xFFFFFF, "inheritance")
    _int(approved["independence"], 1, 8, "independence")
    if (hex_bytes(approved["master_public_key_hash"], 32, "master") != signer.expected_public_hash
            or ledger.master_hash != signer.expected_public_hash):
        raise SynProtocolError("SYN selection and operator identity differ")
    authorizer.require_selected(approved)
    actual_hashes = signer.derive_candidate_public_hashes(approved["inheritance"])
    if actual_hashes[approved["independence"] - 1] != bytes.fromhex(str(approved["selected_public_key_hash"])):
        raise SynProtocolError("SYN selected public key differs from HD derivation")
    body: dict[str, Any] = {"schema_version": 1, "type_ref": GRANT_TYPE,
                            **approved, "challenge": os.urandom(32).hex(),
                            "issued_at": now, "expires_at": now + lifetime}
    signature = signer.sign(GRANT_DOMAIN + canonical(body))
    packet = {"grant": body, "operator_public_key": signer.public_key.hex(),
              "signature": signature.hex()}
    _, grant_hash = _verify_grant(packet, ledger.master_hash, now)
    ledger.reserve_grant(offer=str(body["offer_sha256"]), grant_hash=grant_hash.hex())
    return packet


def prepare_agent_request(*, grant_packet: dict[str, Any], service: str, account: str,
                          lock_dir: Path, expected_master_hash: bytes, now: int) -> dict[str, Any]:
    """Generate independent OS-random X25519 receiver, retained in agent keyring."""
    grant, grant_hash = _verify_grant(grant_packet, expected_master_hash, now)
    _entry(service, account)
    with _agent_key_lock(lock_dir, service, account + ".adoption"):
        store = _native_backend()
        if any(_read_keyring(store, service, account, label) is not None
               for label in (*TRANSFER_SLOTS, *ADOPTED_SLOTS)):
            raise SynProtocolError("SYN agent adoption exists or is incomplete; no silent replay")
        signer = AgentTemporaryOsSigner(service, account,
                                        _sha(bytes.fromhex(grant["temporary_public_key"])))
        receiver = X25519PrivateKey.generate()
        request: dict[str, Any] = {"schema_version": 1, "type_ref": REQUEST_TYPE,
                                   "grant_sha256": grant_hash.hex(),
                                   "transfer_public_key": _x25519_public(receiver).hex(),
                                   "request_nonce": os.urandom(32).hex(),
                                   "requested_at": now}
        signature = signer.sign(REQUEST_DOMAIN + canonical(request))
        packet = {"request": request, "temporary_signature": signature.hex()}
        _, request_hash = _verify_request(packet, grant, grant_hash, now)
        # Store secret first; any interrupted second write is a permanent
        # tombstone until an authorized operator explicitly reconciles it.
        _write_keyring(store, service, account, "transfer_private", _x25519_secret(receiver).hex())
        _write_keyring(store, service, account, "request_digest", request_hash.hex())
        return packet


def seal_selected_key(*, grant_packet: dict[str, Any], request_packet: dict[str, Any],
                      signer: PythonHdOperatorSigner, authorizer: SelectionAuthorizer,
                      ledger: SynHandoffLedger, now: int) -> dict[str, Any]:
    """Operator-only, one-time encrypted key release. No plaintext wire output."""
    grant, grant_hash = _verify_grant(grant_packet, signer.expected_public_hash, now)
    request, request_hash = _verify_request(request_packet, grant, grant_hash, now)
    authorizer.require_selected(_selection(grant))
    if ledger.master_hash != signer.expected_public_hash:
        raise SynProtocolError("SYN handoff ledger targets another master")
    # Durable fence PRECEDES HD derivation and release of any encrypted key.
    ledger.reserve_seal(offer=grant["offer_sha256"], grant_hash=grant_hash.hex(),
                        request_hash=request_hash.hex())
    priv, pub, chain = _seal_node(signer, grant["inheritance"], grant["independence"])
    if _sha(pub) != bytes.fromhex(grant["selected_public_key_hash"]):
        raise SynProtocolError("SYN selected private key does not match approved public identity")
    sender = X25519PrivateKey.generate()
    salt, nonce = os.urandom(32), os.urandom(12)
    header = {"schema_version": 1, "type_ref": SEALED_TYPE,
              "grant_sha256": grant_hash.hex(), "request_sha256": request_hash.hex(),
              "operator_transfer_public_key": _x25519_public(sender).hex(),
              "salt": salt.hex(), "nonce": nonce.hex(), "expires_at": grant["expires_at"]}
    secret = canonical({"schema_version": 1, "xprivate": priv.hex(), "public": pub.hex(),
                        "chain": chain.hex(), "grant_sha256": grant_hash.hex()})
    key = _derive_sealing_key(sender, bytes.fromhex(request["transfer_public_key"]),
                              salt, grant_hash, request_hash)
    ciphertext = AESGCM(key).encrypt(nonce, secret, canonical(header))
    signature = signer.sign(SEAL_DOMAIN + canonical(header) + ciphertext)
    return {"header": header, "ciphertext": ciphertext.hex(),
            "operator_signature": signature.hex()}


def _unseal(grant: dict[str, Any], grant_hash: bytes, request: dict[str, Any],
            request_hash: bytes, envelope: object, secret: X25519PrivateKey,
            operator_public: bytes, now: int) -> dict[str, bytes]:
    packet = exact(envelope, {"header", "ciphertext", "operator_signature"}, "sealed SYN handoff")
    header = exact(packet["header"], {
        "schema_version", "type_ref", "grant_sha256", "request_sha256",
        "operator_transfer_public_key", "salt", "nonce", "expires_at",
    }, "sealed SYN header")
    if (type(header["schema_version"]) is not int or header["schema_version"] != 1
            or header["type_ref"] != SEALED_TYPE or header["expires_at"] != grant["expires_at"]
            or header["grant_sha256"] != grant_hash.hex()
            or header["request_sha256"] != request_hash.hex()):
        raise SynProtocolError("SYN sealed adoption changes the approved identities")
    _check_time(now, grant["issued_at"], grant["expires_at"])
    if not isinstance(packet["ciphertext"], str) or (
            len(packet["ciphertext"]) > 2048 or len(packet["ciphertext"]) % 2 != 0):
        raise SynProtocolError("SYN encrypted identity envelope is invalid")
    try:
        ciphertext = bytes.fromhex(packet["ciphertext"])
    except ValueError as exc:
        raise SynProtocolError("SYN encrypted identity envelope has invalid encoding") from exc
    if not 0 < len(ciphertext) <= 1024:
        raise SynProtocolError("SYN encrypted identity envelope is invalid")
    sig = hex_bytes(packet["operator_signature"], 64, "seal signature")
    _verify(operator_public, sig, SEAL_DOMAIN + canonical(header) + ciphertext)
    sender_pub = hex_bytes(header["operator_transfer_public_key"], 32, "sender X25519")
    salt = hex_bytes(header["salt"], 32, "salt")
    nonce = hex_bytes(header["nonce"], 12, "nonce")
    key = _derive_sealing_key(secret, sender_pub, salt, grant_hash, request_hash)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, canonical(header))
        values = exact(parse(plaintext), {"schema_version", "xprivate", "public", "chain",
                                          "grant_sha256"}, "selected key plaintext")
    except (InvalidTag, ValueError, UnicodeError) as exc:
        raise SynProtocolError("SYN encrypted adoption could not be authenticated") from exc
    if type(values["schema_version"]) is not int or values["schema_version"] != 1 or values["grant_sha256"] != grant_hash.hex():
        raise SynProtocolError("SYN encrypted adoption is not bound to the correct grant")
    decoded = {label: hex_bytes(values[label], 64 if label == "xprivate" else 32,
                                "derived selected key") for label in ("xprivate", "public", "chain")}
    if _sha(decoded["public"]) != bytes.fromhex(grant["selected_public_key_hash"]):
        raise SynProtocolError("SYN encrypted key differs from granted public address")
    return decoded


def _proof_from_key(grant: dict[str, Any], key: dict[str, bytes], now: int) -> dict[str, Any]:
    _, extended_cls = _pycardano()
    signer = extended_cls(key["xprivate"] + key["public"] + key["chain"])
    challenge = GRANT_DOMAIN + hex_bytes(grant["challenge"], 32, "grant challenge")
    try:
        signature = signer.sign(challenge)
        _verify(key["public"], signature, challenge)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SynProtocolError("SYN selected private key cannot prove possession") from exc
    proof = {"schema_version": 1, "type_ref": PROOF_TYPE,
             "offer_sha256": grant["offer_sha256"],
             "master_public_key_hash": grant["master_public_key_hash"],
             "inheritance": grant["inheritance"], "independence": grant["independence"],
             "selected_public_key": key["public"].hex(), "challenge": grant["challenge"],
             "issued_at": now, "expires_at": min(grant["expires_at"], now + 300)}
    proof_sig = signer.sign(PROOF_DOMAIN + canonical(proof))
    _verify(key["public"], proof_sig, PROOF_DOMAIN + canonical(proof))
    return {"proof": proof, "signature": proof_sig.hex()}


def adopt_selected_key(*, grant_packet: dict[str, Any], request_packet: dict[str, Any],
                       envelope: dict[str, Any], service: str, account: str,
                       lock_dir: Path, expected_master_hash: bytes, now: int) -> dict[str, Any]:
    """Agent-owned OS-keystore installation, followed by signed key proof.

    A crash after the first credential write leaves a tombstone; there is no
    destructive cleanup, silent overwrite or second choice. No adopted private
    key escapes the SYN process or appears in the return object.
    """
    grant, grant_hash = _verify_grant(grant_packet, expected_master_hash, now)
    request, request_hash = _verify_request(request_packet, grant, grant_hash, now)
    _entry(service, account)
    with _agent_key_lock(lock_dir, service, account + ".adoption"):
        store = _native_backend()
        if any(_read_keyring(store, service, account, name) is not None for name in ADOPTED_SLOTS):
            raise SynProtocolError("SYN agent adopted key exists or keystore is incomplete")
        encoded = _read_keyring(store, service, account, "transfer_private")
        digest = _read_keyring(store, service, account, "request_digest")
        if encoded is None or digest != request_hash.hex():
            raise SynProtocolError("SYN agent transfer identity was lost or changed")
        private = X25519PrivateKey.from_private_bytes(hex_bytes(encoded, 32, "transfer secret"))
        if _x25519_public(private).hex() != request["transfer_public_key"]:
            raise SynProtocolError("SYN agent recipient private key differs from signed request")
        pub = hex_bytes(grant_packet["operator_public_key"], 32, "operator public")
        key = _unseal(grant, grant_hash, request, request_hash, envelope, private, pub, now)
        # Prove key consistency *before* making any persistent mutation.
        _proof_from_key(grant, key, now)
        manifest = canonical({"schema_version": 1,
                              "type_ref": "synny://type/syn-agent-adopted-keystore-v1",
                              "grant_sha256": grant_hash.hex(),
                              "selected_public_key_hash": grant["selected_public_key_hash"],
                              "master_public_key_hash": grant["master_public_key_hash"],
                              "offer_sha256": grant["offer_sha256"],
                              "inheritance": grant["inheritance"],
                              "independence": grant["independence"]}).decode("ascii")
        for name in ADOPTED_SLOTS:
            if name != "manifest":
                _write_keyring(store, service, account, name, key[name].hex())
        _write_keyring(store, service, account, "manifest", manifest)
        readback = {name: _read_keyring(store, service, account, name) for name in ADOPTED_SLOTS}
        if (readback["manifest"] != manifest or any(
                readback[name] != key[name].hex() for name in ("xprivate", "public", "chain"))):
            raise SynProtocolError("SYN agent keyring failed post-adoption readback")
        proof = _proof_from_key(grant, key, now)
        return {"key_proof": proof, "selected_public_key_hash": grant["selected_public_key_hash"],
                "custody_claim": "syn-os-keystore", "activation_authorized": False}


def confirm_selected_key(*, grant_packet: dict[str, Any], key_proof: dict[str, Any],
                         signer: PythonHdOperatorSigner, authorizer: SelectionAuthorizer,
                         ledger: SynHandoffLedger, journal: PublicAdoptionJournal,
                         now: int) -> dict[str, Any]:
    """Operator signs final receipt only after fresh selection and key proof."""
    grant, grant_hash = _verify_grant(grant_packet, signer.expected_public_hash, now)
    authorizer.require_selected(_selection(grant))
    if journal.master_hash != signer.expected_public_hash or ledger.master_hash != signer.expected_public_hash:
        raise SynProtocolError("SYN adoption journal belongs to a different operator")
    data = exact(key_proof, {"proof", "signature"}, "selected private key proof")
    proof = exact(data["proof"], {
        "schema_version", "type_ref", "offer_sha256", "master_public_key_hash",
        "inheritance", "independence", "selected_public_key", "challenge",
        "issued_at", "expires_at",
    }, "selected key proof")
    if (type(proof["schema_version"]) is not int or proof["schema_version"] != 1
            or proof["type_ref"] != PROOF_TYPE
            or proof["offer_sha256"] != grant["offer_sha256"]
            or proof["master_public_key_hash"] != grant["master_public_key_hash"]
            or proof["inheritance"] != grant["inheritance"]
            or proof["independence"] != grant["independence"]
            or proof["challenge"] != grant["challenge"]
            or _sha(hex_bytes(proof["selected_public_key"], 32, "selected public key")).hex()
               != grant["selected_public_key_hash"]):
        raise SynProtocolError("SYN selected key proof differs from the approved grant")
    _check_time(now, proof["issued_at"], proof["expires_at"])
    proof_sig = hex_bytes(data["signature"], 64, "key proof")
    _verify(bytes.fromhex(proof["selected_public_key"]), proof_sig,
            PROOF_DOMAIN + canonical(proof))
    existing = journal.inspect(bytes.fromhex(grant["offer_sha256"]))
    if existing is not None:
        if existing["key_proof"] != key_proof:
            raise SynProtocolError("SYN adoption receipt conflicts with its original signed proof")
        receipt = existing
    else:
        receipt_body = {"schema_version": 1, "type_ref": RECEIPT_TYPE,
                        "offer_sha256": grant["offer_sha256"],
                        "master_public_key_hash": grant["master_public_key_hash"],
                        "inheritance": grant["inheritance"],
                        "independence": grant["independence"],
                        "selected_public_key_hash": grant["selected_public_key_hash"],
                        "proof_sha256": _sha(PROOF_DOMAIN + canonical(proof) + proof_sig).hex(),
                        "adopted_at": now, "agent_installation_mode": "agent",
                        "custody_claim": "syn-os-keystore"}
        receipt = {"receipt": receipt_body, "key_proof": key_proof,
                   "operator_public_key": signer.public_key.hex(),
                   "operator_signature": signer.sign(RECEIPT_DOMAIN + canonical(receipt_body)).hex()}
        verify_adoption(receipt, expected_master_hash=signer.expected_public_hash, now=now)
        journal.publish(receipt, now=now)
    # Crash after journal publication but before this transaction can be
    # reconciled with the exact same verified signed receipt on retry.
    receipt_hash = _sha(canonical(receipt)).hex()
    ledger.confirm(offer=grant["offer_sha256"], grant_hash=grant_hash.hex(),
                   receipt_hash=receipt_hash)
    return receipt
