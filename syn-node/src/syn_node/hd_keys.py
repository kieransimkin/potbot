"""SYN-owned Python Icarus/Ed25519-BIP32 V2 operator keystore.

No Potbot imports, private-key IPC, custom curve arithmetic, or raw Ed25519
substitution.  This is a *software OS-credential-store* profile; it does not
claim TPM/HSM non-exportability or constant-time Python secret handling.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .custody import _agent_key_lock, _entry, _native_backend
from .protocol import SynProtocolError, canonical, exact, hex_bytes

ROOT_PATH = "m/6969'/420'"
BASE_PATH = "m/6969'/420'/0'/0/0"
PROFILE = "syn-icarus-ed25519-bip32-v2"
MANIFEST_TYPE = "synny://type/syn-hd-keystore-manifest-v1"
PUBLIC_TYPE = "synny://type/syn-operator-public-identity-v1"
SLOTS = ("entropy", "root_xprivate", "root_public", "root_chain",
         "base_xprivate", "base_public", "base_chain", "manifest")


def _pycardano() -> tuple[type[Any], type[Any]]:
    """Pinned Python HD implementation, never an unqualified algorithm fallback."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        from pycardano.crypto.bip32 import HDWallet
        from pycardano.key import ExtendedSigningKey
        try:
            installed = version("pycardano")
        except PackageNotFoundError as exc:
            raise SynProtocolError("SYN Python HD provider metadata is unavailable") from exc
    except ImportError as exc:
        raise SynProtocolError("SYN requires the pinned pycardano HD extra") from exc
    if installed != "0.19.2":
        raise SynProtocolError("SYN Python HD provider revision is not pinned")
    return HDWallet, ExtendedSigningKey


def _derive(entropy: bytes) -> dict[str, bytes]:
    """Use the audited external implementation; never reinterpret an xprv as a seed."""
    if type(entropy) is not bytes or len(entropy) != 32:
        raise SynProtocolError("SYN installation entropy must be exactly 256 bits")
    wallet_cls, _ = _pycardano()
    try:
        master = wallet_cls.from_entropy(entropy.hex(), passphrase="")
        root = master.derive_from_path(ROOT_PATH)
        base = master.derive_from_path(BASE_PATH)
        values = {
            "entropy": entropy,
            "root_xprivate": bytes(root.xprivate_key),
            "root_public": bytes(root.public_key),
            "root_chain": bytes(root.chain_code),
            "base_xprivate": bytes(base.xprivate_key),
            "base_public": bytes(base.public_key),
            "base_chain": bytes(base.chain_code),
        }
    except (TypeError, ValueError, AttributeError) as exc:
        raise SynProtocolError("SYN HD provider failed exact derivation") from exc
    if any(len(values[k]) != (64 if k.endswith("_xprivate") else 32) for k in values):
        raise SynProtocolError("SYN HD provider returned invalid extended keys")
    if values["base_public"] == values["root_public"]:
        raise SynProtocolError("SYN root and base public identities must be different")
    return values


def _slot(account: str, name: str) -> str:
    if name not in SLOTS:
        raise SynProtocolError("unknown SYN HD keystore slot")
    value = f"{account}.operator.{name}.v1"
    if len(value) > 128:
        raise SynProtocolError("SYN HD account would exceed keystore limits")
    return value


def _read(backend: object, service: str, account: str, name: str) -> str | None:
    try:
        return backend.get_password(service, _slot(account, name))  # type: ignore[attr-defined]
    except Exception as exc:
        raise SynProtocolError("SYN OS credential read failed or was locked") from exc


def _write(backend: object, service: str, account: str, name: str, value: str) -> None:
    try:
        backend.set_password(service, _slot(account, name), value)  # type: ignore[attr-defined]
    except Exception as exc:
        raise SynProtocolError("SYN OS credential write failed; installation requires recovery") from exc
    if _read(backend, service, account, name) != value:
        raise SynProtocolError("SYN OS credential readback differed; installation blocked")


def _public(values: dict[str, bytes]) -> dict[str, object]:
    root = values["root_public"]
    base = values["base_public"]
    return {"schema_version": 1, "type_ref": PUBLIC_TYPE, "profile": PROFILE,
            "root_path": ROOT_PATH, "base_path": BASE_PATH,
            "root_public_key": root.hex(), "base_public_key": base.hex(),
            "root_public_key_hash": hashlib.sha256(root).hexdigest(),
            "base_public_key_hash": hashlib.sha256(base).hexdigest()}


def _manifest(public: dict[str, object]) -> str:
    return canonical({"schema_version": 1, "type_ref": MANIFEST_TYPE,
                      "root_public_key_hash": public["root_public_key_hash"],
                      "base_public_key_hash": public["base_public_key_hash"],
                      "profile": PROFILE}).decode("ascii")


def create_operator_identity(*, service: str, account: str, lock_dir: Path) -> dict[str, object]:
    """Fresh OS entropy; root/base xprv+chain+public installed in OS credential store.

    No rollback-by-deletion: incomplete stores deliberately block reinitialising
    with new entropy, avoiding accidental stable identity loss after a crash.
    The manifest is written last as the completion marker.
    """
    _entry(service, account)
    # Bound the largest suffix before side effects.
    for name in SLOTS:
        _slot(account, name)
    with _agent_key_lock(lock_dir, service, account + ".operator"):
        backend = _native_backend()
        if any(_read(backend, service, account, name) is not None for name in SLOTS):
            raise SynProtocolError("SYN operator identity exists or installation is incomplete")
        values = _derive(os.urandom(32))
        public = _public(values)
        for name in SLOTS:
            if name == "manifest":
                continue
            _write(backend, service, account, name, values[name].hex())
        _write(backend, service, account, "manifest", _manifest(public))
        return public


def _load_operator(service: str, account: str) -> tuple[dict[str, bytes], dict[str, object]]:
    _entry(service, account)
    backend = _native_backend()
    marker = _read(backend, service, account, "manifest")
    if marker is None:
        raise SynProtocolError("SYN operator identity has not completed initialization")
    try:
        header = exact(json.loads(marker), {"schema_version", "type_ref", "profile",
                                            "root_public_key_hash", "base_public_key_hash"},
                       "SYN operator keystore marker")
    except (ValueError, TypeError) as exc:
        raise SynProtocolError("SYN operator keystore marker is corrupt") from exc
    if (type(header["schema_version"]) is not int or header["schema_version"] != 1
            or header["type_ref"] != MANIFEST_TYPE or header["profile"] != PROFILE):
        raise SynProtocolError("SYN operator keystore marker revision is unsupported")
    for label in ("root_public_key_hash", "base_public_key_hash"):
        hex_bytes(header[label], 32, label)
    records: dict[str, bytes] = {}
    for name in SLOTS:
        if name == "manifest":
            continue
        stored = _read(backend, service, account, name)
        if stored is None:
            raise SynProtocolError("SYN operator keystore has missing identity material")
        records[name] = hex_bytes(stored, 64 if name.endswith("_xprivate") else 32, "operator key")
    verified = _derive(records["entropy"])
    if verified != records or _manifest(_public(verified)) != marker:
        raise SynProtocolError("SYN operator key records differ from deterministic identity")
    return verified, _public(verified)


@dataclass(frozen=True, slots=True)
class PythonHdOperatorSigner:
    """SYN daemon-only signing interface. Never exports private bytes via IPC."""

    service: str
    account: str
    expected_public_hash: bytes

    def __post_init__(self) -> None:
        _entry(self.service, self.account)
        if type(self.expected_public_hash) is not bytes or len(self.expected_public_hash) != 32:
            raise SynProtocolError("SYN expected operator base identity must be 32 bytes")
        if hashlib.sha256(self.public_key).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN operator identity does not match pinned base hash")

    @property
    def public_key(self) -> bytes:
        _, identity = _load_operator(self.service, self.account)
        return hex_bytes(identity["base_public_key"], 32, "operator base public key")

    def sign(self, payload: bytes) -> bytes:
        if type(payload) is not bytes or not 0 < len(payload) <= 65_536:
            raise SynProtocolError("SYN operator signing input must be bounded bytes")
        values, identity = _load_operator(self.service, self.account)
        if hex_bytes(identity["base_public_key_hash"], 32, "operator base key hash") != self.expected_public_hash:
            raise SynProtocolError("SYN operator base identity unexpectedly changed")
        _, extended_cls = _pycardano()
        try:
            # PyCardano extended payload is xprivate (64) || public (32) || chain (32).
            extended = extended_cls(values["base_xprivate"] + values["base_public"] +
                                    values["base_chain"])
            signature = extended.sign(payload)
        except (TypeError, ValueError, AttributeError) as exc:
            raise SynProtocolError("SYN extended signing provider failed") from exc
        if type(signature) is not bytes or len(signature) != 64:
            raise SynProtocolError("SYN extended signer returned an invalid signature")
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(values["base_public"]).verify(signature, payload)
        except (InvalidSignature, ValueError) as exc:
            raise SynProtocolError("SYN operator signing self-verification failed") from exc
        return signature

    def derive_candidate_public_hashes(self, inheritance: int) -> tuple[bytes, ...]:
        """Public commitments only; private descendants never returned."""
        if type(inheritance) is not int or not 0 <= inheritance <= 0xFFFFFF:
            raise SynProtocolError("SYN inheritance must be an unsigned 24-bit integer")
        values, identity = _load_operator(self.service, self.account)
        if hex_bytes(identity["base_public_key_hash"], 32, "base key") != self.expected_public_hash:
            raise SynProtocolError("SYN installation identity changed")
        wallet_cls, _ = _pycardano()
        try:
            wallet = wallet_cls.from_entropy(values["entropy"].hex(), passphrase="")
            keys = [bytes(wallet.derive_from_path(f"{ROOT_PATH}/{inheritance}'/{n}/0").public_key)
                    for n in range(1, 9)]
        except (TypeError, ValueError, AttributeError) as exc:
            raise SynProtocolError("SYN failed to derive eight agent candidates") from exc
        if len(keys) != 8 or any(len(k) != 32 for k in keys):
            raise SynProtocolError("SYN HD provider returned invalid candidate public keys")
        hashes = tuple(hashlib.sha256(k).digest() for k in keys)
        if len({h[:3] for h in hashes}) != 8:
            raise SynProtocolError("SYN candidate short-hash collision; choose new inheritance")
        return hashes
