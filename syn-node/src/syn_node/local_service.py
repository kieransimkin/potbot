"""SYN-owned Unix daemon implementation for Potbot Phase 42 read-only API.

Keystore signing is supplied ONLY by an independently qualified SYN signer.
No private key, seed, hardware ID or arbitrary signing method is on this API.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import struct
import threading
import time
from pathlib import Path
from typing import Any

from .custody import Signer
from .protocol import SynProtocolError, canonical, exact, hex_bytes, receive, send, uuid
from .receipts import PublicAdoptionJournal

HELLO_TYPE = "synny://type/syn-local-challenge-v1"
ATTEST_TYPE = "synny://type/syn-local-attestation-v1"
QUERY_TYPE = "synny://type/syn-local-read-v1"
RESPONSE_TYPE = "synny://type/syn-local-response-v1"
REGISTRY_TYPE = "synny://type/syn-local-installation-registry-v1"
HELLO_DOMAIN = b"syn.local.attestation.v1\x00"
RESPONSE_DOMAIN = b"syn.local.response.v1\x00"
CAPABILITY_RECEIPT = "adoption_receipt.get.v1"


def private_directory(path: Path) -> None:
    if (not isinstance(path, Path) or not path.is_absolute() or path.is_symlink()
            or path.resolve() != path):
        raise SynProtocolError("SYN service requires a canonical private directory")
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SynProtocolError("SYN service directory must be owner-only")


class SynLocalServer:
    """Real local framed socket transport for a SYN-provided signing authority.

    This class does not provide the *HD operator signer* or attest its OS custody.
    Production provisioning must independently qualify those dependencies.
    """

    def __init__(self, *, socket_path: Path, mode: str, signer: Signer,
                 receipts: PublicAdoptionJournal | None = None, timeout: float = 2.0) -> None:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            raise SynProtocolError("SYN local service currently requires Linux/WSL2")
        if mode not in {"operator", "agent"} or not isinstance(socket_path, Path) or not socket_path.is_absolute():
            raise SynProtocolError("SYN local service requires an explicit mode and socket")
        if type(timeout) not in (int, float) or not 0.1 <= timeout <= 10:
            raise SynProtocolError("SYN local service deadline is invalid")
        private_directory(socket_path.parent)
        if socket_path.exists() or socket_path.is_symlink():
            raise SynProtocolError("SYN refuses to replace an existing socket or symlink")
        if mode == "agent" and receipts is not None:
            raise SynProtocolError("agent-mode SYN cannot serve operator adoption records")
        pub = signer.public_key
        if type(pub) is not bytes or len(pub) != 32:
            raise SynProtocolError("SYN local signer requires a raw Ed25519 public key")
        master = hashlib.sha256(pub).digest()
        if receipts is not None and receipts.master_hash != master:
            raise SynProtocolError("SYN adoption journal belongs to a different operator")
        self.path, self.mode, self.signer, self.receipts = socket_path, mode, signer, receipts
        self.master_hash = master
        self.timeout = float(timeout)
        self._listener: socket.socket | None = None
        self._socket_inode: tuple[int, int] | None = None
        self._processed = 0
        self._rejected = 0
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> list[str]:
        return [CAPABILITY_RECEIPT] if self.mode == "operator" and self.receipts is not None else []

    def bind(self) -> None:
        if self._listener is not None:
            raise SynProtocolError("SYN local listener is already active")
        private_directory(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            raise SynProtocolError("SYN local socket exists; recovery requires operator review")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(16)
            listener.settimeout(0.15)
            st = self.path.stat(follow_symlinks=False)
            self._socket_inode = (st.st_dev, st.st_ino)
            self._listener = listener
        except BaseException:
            listener.close()
            raise
        finally:
            os.umask(old_umask)

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        # A replaced socket is NOT ours and must never be unlinked blindly.
        if self._socket_inode is not None:
            try:
                st = self.path.stat(follow_symlinks=False)
                if (st.st_dev, st.st_ino) == self._socket_inode and stat.S_ISSOCK(st.st_mode):
                    self.path.unlink()
            except FileNotFoundError:
                pass
        self._socket_inode = None

    def _hello(self, conn: socket.socket) -> tuple[str, str]:
        hello = exact(receive(conn), {
            "schema_version", "type_ref", "request_id", "challenge",
            "expected_base_public_key_hash", "mode",
        }, "SYN local hello")
        if (type(hello["schema_version"]) is not int or hello["schema_version"] != 1
                or hello["type_ref"] != HELLO_TYPE or hello["mode"] != self.mode):
            raise SynProtocolError("SYN local hello uses the wrong version or mode")
        request_id = uuid(hello["request_id"])
        nonce = hex_bytes(hello["challenge"], 32, "client challenge")
        if nonce == bytes(32) or hex_bytes(hello["expected_base_public_key_hash"], 32, "pin") != self.master_hash:
            raise SynProtocolError("SYN local hello has zero challenge or wrong pinned identity")
        public = self.signer.public_key
        if hashlib.sha256(public).digest() != self.master_hash:
            raise SynProtocolError("SYN signer changed its pinned base public key")
        response = {
            "schema_version": 1, "type_ref": ATTEST_TYPE,
            "request_id": request_id, "challenge": nonce.hex(), "mode": self.mode,
            "base_public_key": public.hex(), "capabilities": self.capabilities,
        }
        sig = self.signer.sign(HELLO_DOMAIN + canonical(response))
        if type(sig) is not bytes or len(sig) != 64:
            raise SynProtocolError("SYN signer did not return an Ed25519 signature")
        send(conn, {"attestation": response, "signature": sig.hex()})
        return request_id, nonce.hex()

    def _query(self, conn: socket.socket, nonce: str) -> None:
        payload = exact(receive(conn), {
            "schema_version", "type_ref", "method", "request_id",
            "session_challenge", "offer_sha256",
        }, "SYN local request")
        if (type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or payload["type_ref"] != QUERY_TYPE
                or payload["method"] != "adoption_receipt.get"
                or payload["session_challenge"] != nonce
                or self.mode != "operator" or self.receipts is None):
            raise SynProtocolError("unsupported, unbound or unauthorized SYN local method")
        request_id = uuid(payload["request_id"])
        offer = hex_bytes(payload["offer_sha256"], 32, "offer hash")
        receipt = self.receipts.inspect(offer)
        state = "confirmed" if receipt is not None else "pending"
        body: dict[str, Any] = {
            "schema_version": 1, "type_ref": RESPONSE_TYPE,
            "method": "adoption_receipt.get", "request_id": request_id,
            "session_challenge": nonce, "offer_sha256": offer.hex(),
            "state": state, "receipt": receipt,
        }
        sig = self.signer.sign(RESPONSE_DOMAIN + canonical(body))
        if type(sig) is not bytes or len(sig) != 64:
            raise SynProtocolError("SYN signer did not return a response signature")
        send(conn, {"response": body, "signature": sig.hex()})

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(self.timeout)
        try:
            # Only the same host UID may inspect SYN public receipts; a signed
            # response does not replace this local transport access check.
            peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            pid, uid, gid = struct.unpack("3i", peer)
            if pid <= 0 or gid < 0 or uid != os.getuid():
                raise SynProtocolError("SYN local client has no approved peer UID")
            _, nonce = self._hello(conn)
            try:
                self._query(conn, nonce)
            except SynProtocolError as exc:
                # Closing after hello is valid for an attestation-only client.
                if "truncated SYN local wire frame" not in str(exc):
                    raise
            with self._lock:
                self._processed += 1
        except (SynProtocolError, OSError, TimeoutError):
            with self._lock:
                self._rejected += 1

    def serve(self, stop: threading.Event) -> None:
        if not isinstance(stop, threading.Event):
            raise SynProtocolError("SYN server requires an operator-controlled stop event")
        if self._listener is None:
            self.bind()
        listener = self._listener
        assert listener is not None
        try:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        break
                    raise
                with conn:
                    self._handle(conn)
        finally:
            self.close()


def create_registry_entry(*, socket_path: Path, mode: str, base_public_key_hash: bytes) -> dict[str, object]:
    private_directory(socket_path.parent)
    if mode not in {"operator", "agent"} or type(base_public_key_hash) is not bytes or len(base_public_key_hash) != 32:
        raise SynProtocolError("invalid SYN registry identity")
    return {"socket": str(socket_path), "mode": mode,
            "base_public_key_hash": base_public_key_hash.hex(), "owner_uid": os.getuid()}


def publish_registry(path: Path, entry: dict[str, object]) -> None:
    """Atomically update the exact private SYN inventory under an OS file lock.

    The caller is an independently authorized SYN installer/service. A known
    operator pin is never replaced silently, and two installers cannot drop
    each other's simultaneous insertions.
    """
    import fcntl

    from .protocol import parse

    if not isinstance(path, Path) or not path.is_absolute():
        raise SynProtocolError("SYN registry needs an absolute path")
    private_directory(path.parent)
    if (not isinstance(entry, dict)
            or set(entry) != {"socket", "mode", "base_public_key_hash", "owner_uid"}
            or entry["mode"] not in {"operator", "agent"}
            or type(entry["owner_uid"]) is not int or entry["owner_uid"] != os.getuid()
            or not isinstance(entry["socket"], str)
            or not Path(entry["socket"]).is_absolute()):
        raise SynProtocolError("SYN registry entry is not a trusted local installation")
    hex_bytes(entry["base_public_key_hash"], 32, "SYN base identity")
    lock_path = path.parent / ".syn-installations.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        lock_stat = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid()
                or lock_stat.st_nlink != 1 or lock_stat.st_mode & 0o077):
            raise SynProtocolError("SYN registry lock is not owner-private")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        entries: list[dict[str, object]] = []
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise SynProtocolError("SYN registry cannot follow symbolic links")
            handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                st = os.fstat(handle)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                        or st.st_mode & 0o077 or st.st_nlink != 1 or st.st_size > 32_768):
                    raise SynProtocolError("SYN registry is not a bounded private file")
                raw = os.read(handle, 32_769)
                if len(raw) != st.st_size:
                    raise SynProtocolError("SYN registry changed during verification")
            finally:
                os.close(handle)
            registry = exact(parse(raw), {"schema_version", "type_ref", "installations"}, "registry")
            if (type(registry["schema_version"]) is not int or registry["schema_version"] != 1
                    or registry["type_ref"] != REGISTRY_TYPE):
                raise SynProtocolError("SYN registry has an unsupported revision")
            entries = registry["installations"]
            if not isinstance(entries, list) or len(entries) > 16:
                raise SynProtocolError("SYN registry inventory exceeds limits")
            for known in entries:
                if (not isinstance(known, dict) or set(known) != set(entry)
                        or known["mode"] not in {"agent", "operator"}
                        or not isinstance(known["socket"], str)
                        or not Path(known["socket"]).is_absolute()
                        or type(known["owner_uid"]) is not int):
                    raise SynProtocolError("SYN registry contains an invalid installation")
                hex_bytes(known["base_public_key_hash"], 32, "known SYN base identity")
                if known == entry:
                    return
                if (known["socket"] == entry["socket"]
                        or (known["mode"] == "operator" and entry["mode"] == "operator")
                        or (known["base_public_key_hash"] == entry["base_public_key_hash"]
                            and known["mode"] == entry["mode"])):
                    raise SynProtocolError("SYN registry contains conflicting installation")
        if len(entries) >= 16:
            raise SynProtocolError("SYN installation registry is full")
        payload = canonical({"schema_version": 1, "type_ref": REGISTRY_TYPE,
                             "installations": [*entries, entry]})
        if len(payload) > 32_768:
            raise SynProtocolError("SYN installation inventory exceeded 32 KiB")
        new_path = path.parent / f".{path.name}.{os.urandom(12).hex()}.tmp"
        fd = os.open(new_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise SynProtocolError("SYN registry write failed")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(new_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            new_path.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
