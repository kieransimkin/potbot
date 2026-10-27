"""SYN-only OS credential-store custody for local signing identities.

The service requires the platform's explicit native backend. OS keyring software
backends are NOT necessarily non-exportable hardware keys; this module makes no
hardware-keystore or attestation claim. Potbot never imports this package.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .protocol import SynProtocolError, hex_bytes


class Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    def sign(self, payload: bytes) -> bytes: ...


def _native_backend() -> object:
    try:
        keyring = importlib.import_module("keyring")
    except ImportError as exc:
        raise SynProtocolError("SYN OS credential backend is not installed") from exc
    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    accepted = {
        "linux": "keyring.backends.SecretService.Keyring",
        "darwin": "keyring.backends.macOS.Keyring",
        "win32": "keyring.backends.Windows.WinVaultKeyring",
    }
    if name != accepted.get(sys.platform):
        raise SynProtocolError("SYN refused a non-native or fallback credential backend")
    return backend


def _entry(service: str, account: str) -> None:
    for name, value in (("service", service), ("account", account)):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) is None:
            raise SynProtocolError(f"invalid SYN keystore {name}")


def _raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )


@dataclass(frozen=True, slots=True)
class AgentTemporaryOsSigner:
    """Existing Ed25519 private seed held by the platform credential store.

    This seed-based helper is ONLY suitable for temporary agent keys. It does
    NOT reconstruct an Ed25519-BIP32 V2 extended private key. The operator's
    base key at m/6969'/420'/0'/0/0 must use the qualified native HD signer.
    """

    service: str
    account: str
    expected_public_hash: bytes

    def __post_init__(self) -> None:
        _entry(self.service, self.account)
        if type(self.expected_public_hash) is not bytes or len(self.expected_public_hash) != 32:
            raise SynProtocolError("SYN signer must be pinned to a 32-byte public-key hash")
        _native_backend()
        if hashlib.sha256(self.public_key).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN keystore key differs from pinned installation identity")

    def _private(self) -> Ed25519PrivateKey:
        backend = _native_backend()
        try:
            encoded = backend.get_password(self.service, self.account)
        except Exception as exc:
            raise SynProtocolError("SYN OS credential store is locked or unavailable") from exc
        if encoded is None:
            raise SynProtocolError("SYN OS credential is absent; no fallback key")
        private = Ed25519PrivateKey.from_private_bytes(hex_bytes(encoded, 32, "keystore key"))
        return private

    @property
    def public_key(self) -> bytes:
        return _raw_public(self._private())

    def sign(self, payload: bytes) -> bytes:
        if type(payload) is not bytes or not 0 < len(payload) <= 65_536:
            raise SynProtocolError("invalid bounded SYN signing request")
        private = self._private()
        if hashlib.sha256(_raw_public(private)).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN operator signing identity rotated unexpectedly")
        return private.sign(payload)


@contextmanager
def _agent_key_lock(directory: Path, service: str, account: str):
    """OS-enforced singleton around non-CAS credential manager APIs."""
    try:
        import fcntl
    except ImportError as exc:
        raise SynProtocolError("SYN agent provisioning requires a qualified OS lock") from exc
    if (not isinstance(directory, Path) or not directory.is_absolute() or directory.is_symlink()
            or directory.resolve() != directory):
        raise SynProtocolError("SYN agent installation lock directory must be absolute")
    st = directory.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SynProtocolError("SYN agent installation lock must be owner-only")
    name = hashlib.sha256((service + "\x00" + account).encode()).hexdigest()
    path = directory / ("syn-agent-" + name + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        details = os.fstat(fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or details.st_uid != os.getuid() or details.st_mode & 0o077):
            raise SynProtocolError("SYN agent singleton lock changed")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def create_agent_temporary_key(*, service: str, account: str, lock_dir: Path) -> dict[str, str]:
    """One explicit agent-mode SYN temporary key from OS CSPRNG.

    This is NOT a base/root CIP-3 key, and its hash is never treated as the
    installation's durable post-adoption address. Only SYN calls this function.
    """
    _entry(service, account)
    # OS credential stores generally do not implement compare-and-swap. The
    # installation lock serializes same-account initialization across SYN
    # processes, never exposing key material to Potbot.
    with _agent_key_lock(lock_dir, service, account):
        backend = _native_backend()
        try:
            existing = backend.get_password(service, account)
        except Exception as exc:
            raise SynProtocolError("SYN keystore cannot confirm whether key already exists") from exc
        if existing is not None:
            raise SynProtocolError("SYN temporary key already exists; refuse replacement")
        private = Ed25519PrivateKey.generate()  # OS CSPRNG, not a hardware identifier
        secret = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex()
        try:
            backend.set_password(service, account, secret)
            verified = backend.get_password(service, account)
        except Exception as exc:
            raise SynProtocolError("SYN OS keystore did not confirm temporary-key custody") from exc
        if verified != secret:
            raise SynProtocolError("SYN OS keystore readback differed; enrollment is blocked")
        pub = _raw_public(private)
        return {
            "schema": "syn-agent-temporary-public-identity/v1",
            "temporary_public_key": pub.hex(),
            "temporary_public_key_hash": hashlib.sha256(pub).hexdigest(),
            "custody": "native-os-credential-store",
            "agent_adopted": "false",
        }


@dataclass(frozen=True, slots=True)
class NativeOperatorSigner:
    """Pinned, external SYN native signing broker; no key bytes cross the IPC.

    Proposed `syn-native-keystore-sign-v1` contract. The signer executable itself
    MUST be separately qualified for CIP-3 Ed25519-BIP32 and OS-keystore custody.
    Potbot and the Python SYN daemon have no API to import/export operator keys.
    """

    executable: Path
    executable_sha256: str
    expected_public_hash: bytes

    def __post_init__(self) -> None:
        import os
        import stat
        from pathlib import Path

        if not isinstance(self.executable, Path) or not self.executable.is_absolute() or self.executable.is_symlink():
            raise SynProtocolError("SYN signer must have an absolute pinned native executable")
        st = self.executable.stat(follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022 or not os.access(self.executable, os.X_OK):
            raise SynProtocolError("SYN native signing executable is writable or unavailable")
        if (not isinstance(self.executable_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256) is None
                or type(self.expected_public_hash) is not bytes or len(self.expected_public_hash) != 32):
            raise SynProtocolError("SYN native signer pin is invalid")
        self._pin()
        if hashlib.sha256(self.public_key).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN native signer does not match the installation identity")

    def _pin(self) -> None:
        with self.executable.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != self.executable_sha256:
                raise SynProtocolError("SYN native signer executable differs from its pinned source")

    def _call(self, method: str, payload: bytes = b"") -> dict[str, str]:
        import subprocess
        from .protocol import canonical, exact, parse

        if method not in {"public", "sign"} or type(payload) is not bytes or len(payload) > 65_536:
            raise SynProtocolError("SYN native signer request is not admitted")
        # Verify the exact *open inode* before execution. Executing via an
        # inherited /proc/self/fd reference avoids a path-swap race after pinning.
        if sys.platform != "linux":
            raise SynProtocolError("pinned SYN native signer requires qualified Linux fd exec")
        request = canonical({"schema_version": 1, "type_ref": "synny://type/syn-native-keystore-sign-v1",
                             "method": method, "payload_hex": payload.hex()})
        try:
            fd = os.open(self.executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise SynProtocolError("SYN native signer is not an existing ordinary executable") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                    or not info.st_mode & 0o111 or info.st_nlink != 1):
                raise SynProtocolError("SYN native signer executable inode is insecure")
            digest = hashlib.sha256()
            while True:
                data = os.read(fd, 131072)
                if not data:
                    break
                digest.update(data)
            if digest.hexdigest() != self.executable_sha256:
                raise SynProtocolError("SYN native signer inode differs from its pinned source")
            try:
                child = subprocess.run([f"/proc/self/fd/{fd}"], input=request,
                                       pass_fds=(fd,), capture_output=True,
                                       timeout=3, cwd="/",
                                       env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                                       check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SynProtocolError("SYN native signing broker unavailable") from exc
        finally:
            os.close(fd)
        if child.returncode != 0 or not 0 < len(child.stdout) <= 4096 or child.stderr:
            raise SynProtocolError("SYN native signing broker failed closed")
        result = exact(parse(child.stdout), {"schema_version", "type_ref", "method",
                                              "public_key", "signature"}, "native signer response")
        if (type(result["schema_version"]) is not int or result["schema_version"] != 1
                or result["type_ref"] != "synny://type/syn-native-keystore-sign-result-v1"
                or result["method"] != method):
            raise SynProtocolError("SYN native signing broker returned a mismatched reply")
        pub = hex_bytes(result["public_key"], 32, "signer public key")
        if hashlib.sha256(pub).digest() != self.expected_public_hash:
            raise SynProtocolError("SYN native signer returned the wrong public identity")
        if method == "public" and result["signature"] is not None:
            raise SynProtocolError("SYN native public operation returned an unexpected signature")
        if method == "sign":
            sig = hex_bytes(result["signature"], 64, "native signature")
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            try:
                Ed25519PublicKey.from_public_bytes(pub).verify(sig, payload)
            except (InvalidSignature, ValueError) as exc:
                raise SynProtocolError("SYN native signing broker produced an invalid signature") from exc
        return result

    @property
    def public_key(self) -> bytes:
        return hex_bytes(self._call("public")["public_key"], 32, "signer key")

    def sign(self, payload: bytes) -> bytes:
        return hex_bytes(self._call("sign", payload)["signature"], 64, "signature")
