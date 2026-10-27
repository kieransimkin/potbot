"""Operator-only SYN local installation discovery and authenticated UDS client.

SYN owns its keys, OS keystore, private P2P and agent adoption. Potbot may
read signed public state from an independently installed SYN daemon, never
launch an untrusted binary, unwrap keys, or assume that absence of an endpoint
means no SYN installation. Windows named-pipe qualification is separate.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.syn_agent_pledge import _fixed_bytes, public_key_hash
from potbot.domain.syn_enrollment_protocol import _canonical, _hex, _verify_ed25519
from potbot.domain.syn_adoption import SignedAdoptionReceipt

HELLO_TYPE = "synny://type/syn-local-challenge-v1"
ATTEST_TYPE = "synny://type/syn-local-attestation-v1"
RESPONSE_TYPE = "synny://type/syn-local-response-v1"
HELLO_DOMAIN = b"syn.local.attestation.v1\x00"
RESPONSE_DOMAIN = b"syn.local.response.v1\x00"
_MAX_MESSAGE = 65536
_MAX_REGISTRY = 32768
_READ_TIMEOUT = 3.0


def _exact_keys(value: object, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise InvalidInputError(f"{label} fields differ from the protocol")
    return value


def _strict_json(raw: bytes, limit: int) -> dict[str, Any]:
    if not raw or len(raw) > limit:
        raise InvalidInputError("SYN local payload is absent or exceeds its bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in pairs:
            if k in result:
                raise InvalidInputError("SYN local JSON contains duplicate keys")
            result[k] = v
        return result

    try:
        record = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
    except InvalidInputError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise InvalidInputError("SYN local JSON is not well-formed") from exc
    if not isinstance(record, dict) or _canonical(record) != raw:
        raise InvalidInputError("SYN local JSON must be a canonical object")
    return record


def _private_file(path: Path, *, max_size: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise InvalidInputError("SYN registry must have a canonical absolute path")
    if path.parent.is_symlink() or path.parent.resolve() != path.parent:
        raise ScopeDeniedError("SYN registry parent cannot be a symlink")
    try:
        directory = path.parent.stat()
    except FileNotFoundError as exc:
        raise CapabilityUnavailableError("no authoritative SYN installation registry") from exc
    if (directory.st_uid != os.getuid() or directory.st_mode & 0o077
            or not stat.S_ISDIR(directory.st_mode)):
        raise ScopeDeniedError("SYN registry directory must be privately owned")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise CapabilityUnavailableError("no authoritative SYN installation registry") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > max_size):
            raise ScopeDeniedError("SYN registry has insecure ownership or size")
        data = os.read(fd, max_size + 1)
        if len(data) != info.st_size or len(data) > max_size:
            raise StateMismatchError("SYN registry changed during read")
        return data
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class SynLocalEndpoint:
    """Previously pinned host registry identity; never obtained from a pulse."""

    socket_path: Path
    base_public_key_hash: bytes
    mode: str
    owner_uid: int

    def __post_init__(self) -> None:
        _fixed_bytes(self.base_public_key_hash, 32, "SYN base address hash")
        if self.mode not in {"operator", "agent"}:
            raise InvalidInputError("SYN installation must declare operator or agent mode")
        if (type(self.owner_uid) is not int or self.owner_uid < 0
                or not isinstance(self.socket_path, Path) or not self.socket_path.is_absolute()
                or self.socket_path.parent.resolve() != self.socket_path.parent
                or self.socket_path.is_symlink()):
            raise InvalidInputError("SYN local endpoint must be an exact host-owned socket")


@dataclass(frozen=True, slots=True)
class SynDaemonAttestation:
    mode: str
    base_public_key_hash: bytes
    capabilities: tuple[str, ...]


class SynInstallationRegistry:
    """Trusted, read-only manifest of known local SYN instances.

    Empty (verified) manifest requests a NEW SYN bootstrap from an external
    installer. An absent/locked/corrupted manifest NEVER triggers new entropy
    or silent creation of a competing operator identity.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def endpoints(self) -> tuple[SynLocalEndpoint, ...]:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            raise CapabilityUnavailableError("SYN local socket qualification requires Linux/WSL2")
        value = _strict_json(_private_file(self.path, max_size=_MAX_REGISTRY), _MAX_REGISTRY)
        _exact_keys(value, {"schema_version", "type_ref", "installations"}, "SYN registry")
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["type_ref"] != "synny://type/syn-local-installation-registry-v1"
                or not isinstance(value["installations"], list)
                or len(value["installations"]) > 16):
            raise InvalidInputError("SYN registry requires a bounded v1 installation list")
        endpoints: list[SynLocalEndpoint] = []
        seen: set[tuple[str, bytes]] = set()
        sockets: set[Path] = set()
        for entry in value["installations"]:
            fields = _exact_keys(entry, {"socket", "mode", "base_public_key_hash", "owner_uid"}, "SYN installation")
            endpoint = SynLocalEndpoint(
                Path(fields["socket"]) if isinstance(fields["socket"], str) else Path("."),
                _hex(fields["base_public_key_hash"], 32, "base public key hash"),
                fields["mode"], fields["owner_uid"],
            )
            key = (endpoint.mode, endpoint.base_public_key_hash)
            if key in seen or endpoint.socket_path in sockets:
                raise StateMismatchError("SYN registry has duplicate identity or socket")
            seen.add(key)
            sockets.add(endpoint.socket_path)
            endpoints.append(endpoint)
        return tuple(endpoints)

    def discover_operator(self, *, expected_master_hash: bytes | None = None) -> SynLocalEndpoint | None:
        if expected_master_hash is not None:
            _fixed_bytes(expected_master_hash, 32, "operator master hash")
        known = self.endpoints()
        candidates = [x for x in known if x.mode == "operator"]
        if expected_master_hash is not None:
            candidates = [x for x in candidates if x.base_public_key_hash == expected_master_hash]
        if expected_master_hash is not None and not candidates and any(
            x.mode == "operator" for x in known
        ):
            raise ScopeDeniedError("pinned SYN operator was not found; do not create a replacement")
        if len(candidates) > 1:
            raise StateMismatchError("multiple SYN operators; require an explicit identity pin")
        return candidates[0] if candidates else None


def _read_exact(conn: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        part = conn.recv(length - len(output))
        if not part:
            raise StateMismatchError("SYN local daemon disconnected mid-frame")
        output.extend(part)
    return bytes(output)


def _receive(conn: socket.socket) -> dict[str, Any]:
    size = struct.unpack(">I", _read_exact(conn, 4))[0]
    if size == 0 or size > _MAX_MESSAGE:
        raise ScopeDeniedError("SYN response exceeds the local message bound")
    return _strict_json(_read_exact(conn, size), _MAX_MESSAGE)


def _send(conn: socket.socket, record: dict[str, Any]) -> None:
    payload = _canonical(record)
    if not payload or len(payload) > _MAX_MESSAGE:
        raise InvalidInputError("SYN request exceeds the local message bound")
    conn.sendall(struct.pack(">I", len(payload)) + payload)


def _socket_preflight(endpoint: SynLocalEndpoint) -> None:
    directory = endpoint.socket_path.parent
    if directory.is_symlink() or directory.resolve() != directory:
        raise ScopeDeniedError("SYN socket parent may not be a symlink")
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != endpoint.owner_uid or info.st_mode & 0o077:
        raise ScopeDeniedError("SYN socket directory must be private to the pinned UID")
    if endpoint.socket_path.is_symlink():
        raise ScopeDeniedError("SYN socket cannot be a symlink")
    node = endpoint.socket_path.stat()
    if not stat.S_ISSOCK(node.st_mode) or node.st_uid != endpoint.owner_uid:
        raise ScopeDeniedError("SYN endpoint is not a pinned-owner Unix socket")


class SynLocalDaemonClient:
    """Authenticated local READ-ONLY Syn API over one protected Unix socket.

    This is a *proposed adapter protocol* awaiting a real independently shipped
    SYN daemon. Only scoped status and adopted PUBLIC receipts may be queried.
    Never send a raw hardware ID, private key, entropy, wallet secret or pledge.
    """

    def __init__(self, endpoint: SynLocalEndpoint, *, timeout: float = _READ_TIMEOUT) -> None:
        if not isinstance(endpoint, SynLocalEndpoint):
            raise InvalidInputError("SYN local client requires a pinned installation")
        if type(timeout) not in (float, int) or not 0.1 <= timeout <= 10:
            raise InvalidInputError("SYN connection timeout must be bounded")
        self.endpoint, self.timeout = endpoint, float(timeout)

    @contextmanager
    def _session(self) -> Iterator[tuple[socket.socket, bytes, bytes, tuple[str, ...]]]:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            raise CapabilityUnavailableError("SYN authenticated local socket is Linux-only")
        _socket_preflight(self.endpoint)
        before = self.endpoint.socket_path.stat()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.endpoint.socket_path))
            after = self.endpoint.socket_path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ScopeDeniedError("SYN endpoint changed during connection")
            pid, uid, gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid <= 0 or uid != self.endpoint.owner_uid or gid < 0:
                raise ScopeDeniedError("SYN local peer credential does not match its pinned owner")
            challenge = secrets.token_bytes(32)
            request_id = str(uuid4())
            _send(connection, {"schema_version": 1, "type_ref": HELLO_TYPE,
                               "request_id": request_id, "challenge": challenge.hex(),
                               "expected_base_public_key_hash": self.endpoint.base_public_key_hash.hex(),
                               "mode": self.endpoint.mode})
            response = _receive(connection)
            packet = _exact_keys(response, {"attestation", "signature"}, "SYN local hello")
            attestation = _exact_keys(packet["attestation"], {
                "schema_version", "type_ref", "request_id", "challenge", "mode",
                "base_public_key", "capabilities",
            }, "SYN local attestation")
            if (type(attestation["schema_version"]) is not int or attestation["schema_version"] != 1
                    or attestation["type_ref"] != ATTEST_TYPE
                    or attestation["request_id"] != request_id
                    or attestation["challenge"] != challenge.hex()
                    or attestation["mode"] != self.endpoint.mode):
                raise ScopeDeniedError("SYN local attestation differs from the pinned challenge")
            pub = _hex(attestation["base_public_key"], 32, "daemon public key")
            if public_key_hash(pub) != self.endpoint.base_public_key_hash:
                raise ScopeDeniedError("SYN daemon base public-key identity mismatch")
            caps = attestation["capabilities"]
            if (not isinstance(caps, list) or len(caps) > 16
                    or any(not isinstance(x, str) or len(x) > 64 for x in caps)
                    or len(set(caps)) != len(caps)):
                raise InvalidInputError("SYN capabilities must be a bounded unique list")
            _verify_ed25519(pub, _hex(packet["signature"], 64, "attestation signature"),
                            HELLO_DOMAIN + _canonical(attestation))
            yield connection, challenge, pub, tuple(caps)
        except (OSError, TimeoutError) as exc:
            raise CapabilityUnavailableError("SYN local daemon is unavailable or locked") from exc
        finally:
            connection.close()

    def attest(self) -> SynDaemonAttestation:
        with self._session() as (_conn, _challenge, pub, capabilities):
            return SynDaemonAttestation(self.endpoint.mode, public_key_hash(pub), capabilities)

    def adoption_receipt(self, offer_sha256: bytes) -> SignedAdoptionReceipt | None:
        """Fetch a signed public receipt by digest, never expose raw device data."""
        _fixed_bytes(offer_sha256, 32, "enrollment offer digest")
        if self.endpoint.mode != "operator":
            raise ScopeDeniedError("only an operator-mode SYN may confirm agent adoption")
        with self._session() as (conn, challenge, pub, capabilities):
            if "adoption_receipt.get.v1" not in capabilities:
                raise CapabilityUnavailableError("SYN daemon does not advertise adoption receipt reads")
            # Separate request_id plus session nonce bind the reply to this request.
            request_id = str(uuid4())
            _send(conn, {"schema_version": 1, "type_ref": "synny://type/syn-local-read-v1",
                         "method": "adoption_receipt.get", "request_id": request_id,
                         "session_challenge": challenge.hex(), "offer_sha256": offer_sha256.hex()})
            answer = _receive(conn)
            outer = _exact_keys(answer, {"response", "signature"}, "SYN adoption query")
            body = _exact_keys(outer["response"], {
                "schema_version", "type_ref", "method", "request_id", "session_challenge",
                "offer_sha256", "state", "receipt",
            }, "SYN adoption response")
            if (type(body["schema_version"]) is not int or body["schema_version"] != 1
                    or body["type_ref"] != RESPONSE_TYPE
                    or body["method"] != "adoption_receipt.get"
                    or body["request_id"] != request_id
                    or body["session_challenge"] != challenge.hex()
                    or body["offer_sha256"] != offer_sha256.hex()
                    or body["state"] not in {"pending", "confirmed", "denied"}):
                raise ScopeDeniedError("SYN local adoption response lacks request binding")
            _verify_ed25519(pub, _hex(outer["signature"], 64, "response signature"),
                            RESPONSE_DOMAIN + _canonical(body))
            if body["state"] == "denied":
                raise ScopeDeniedError("SYN operator denied the adoption")
            if body["state"] == "pending":
                if body["receipt"] is not None:
                    raise StateMismatchError("pending SYN adoption has an unexpected receipt")
                return None
            if body["receipt"] is None:
                raise StateMismatchError("confirmed SYN adoption is missing a signed receipt")
            receipt = SignedAdoptionReceipt.from_dict(body["receipt"])
            if receipt.receipt.offer_sha256 != offer_sha256:
                raise ScopeDeniedError("SYN adoption receipt belongs to another pledge")
            return receipt


@dataclass(frozen=True, slots=True)
class SynBootstrapDecision:
    """Plan only; SYN's signed installer/OS-keystore broker performs effects."""

    state: str
    required_mode: str
    operator_master_hash: str | None
    verified_local_identity: bool


class SynStartupPlanner:
    """Detect, then require explicit SYN-controlled pledge/install operations.

    Do not infer "no installation" from a failed connection, unreadable
    registry, ambiguous operator list, or a disconnected/locked daemon.
    """

    def __init__(self, registry: SynInstallationRegistry) -> None:
        if not isinstance(registry, SynInstallationRegistry):
            raise InvalidInputError("SYN startup requires a protected local installation registry")
        self.registry = registry

    def potbot_first_run(self, *, expected_master_hash: bytes | None = None) -> SynBootstrapDecision:
        operator = self.registry.discover_operator(expected_master_hash=expected_master_hash)
        if operator is None:
            # Only an explicitly empty verified host registry is evidence of no
            # local operator. A real SYN installer must handle entropy/keystore.
            return SynBootstrapDecision("external_syn_operator_install_required", "operator", None, False)
        SynLocalDaemonClient(operator).attest()  # Signed, live, pinned identity check.
        return SynBootstrapDecision(
            "external_syn_pledge_required", "operator", operator.base_public_key_hash.hex(), True
        )

    def new_agent(self, *, target_master_hash: bytes | None = None) -> SynBootstrapDecision:
        if target_master_hash is not None:
            _fixed_bytes(target_master_hash, 32, "agent target master hash")
        # A newly provisioned agent MUST have its own SYN installation in agent
        # mode, even if an operator SYN is already present on this host.
        # Joining P2P and key adoption are controlled by SYN after installation.
        self.registry.endpoints()  # enforce qualified, not missing, inventory
        return SynBootstrapDecision(
            "external_syn_agent_install_required", "agent",
            target_master_hash.hex() if target_master_hash is not None else None,
            False,
        )
