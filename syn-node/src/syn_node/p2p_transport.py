"""SYN authenticated LAN discovery + encrypted single-pledge transport.

This transport never accepts identity adoption, exposes an operator signing
endpoint, or sends selected extended private keys. Successful delivery means
only that the pledge is durably encrypted in the private SYN inbox, waiting for
independent device attestation and explicit operator approval.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .p2p_contracts import (
    CLIENT_HELLO, MAX_ADVERT_BYTES, MAX_WIRE_BYTES,
    _endpoint, _network_id, _sha, _time, build_signed_offer, derive_session,
    make_advert, new_client_hello, new_server_hello, verify_advert,
    verify_server_hello, verify_signed_offer,
)
from .p2p_inbox import PledgeInbox
from .protocol import SynProtocolError, canonical, exact, hex_bytes, parse

OUTCOME_TYPE = "synny://type/syn-p2p-pledge-outcome-v1"
TIMEOUT_SECONDS = 4.0
MAX_ACTIVE = 16
MAX_PER_PEER_PER_MINUTE = 32
MAX_GLOBAL_PER_MINUTE = 256
MAX_TRACKED_PEERS = 1024


def _read(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        fragment = sock.recv(size - len(data))
        if not fragment:
            raise SynProtocolError("SYN P2P frame ended before completion")
        data.extend(fragment)
    return bytes(data)


def _recv(sock: socket.socket) -> dict[str, Any]:
    size = struct.unpack(">I", _read(sock, 4))[0]
    if not 0 < size <= MAX_WIRE_BYTES:
        raise SynProtocolError("SYN P2P frame exceeds protocol bound")
    return parse(_read(sock, size))


def _send(sock: socket.socket, value: dict[str, Any]) -> None:
    payload = canonical(value)
    if not 0 < len(payload) <= MAX_WIRE_BYTES:
        raise SynProtocolError("SYN P2P message is too large")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


class SynP2PServer:
    """One-use encrypted pledge ingress, without any enrollment acceptance API."""

    def __init__(self, *, signer: object, inbox: PledgeInbox, network_id: str,
                 bind_host: str, advertised_host: str, tcp_port: int = 0,
                 allow_loopback: bool = False):
        if not callable(getattr(signer, "sign", None)):
            raise SynProtocolError("SYN P2P operator signer is unavailable")
        pub = getattr(signer, "public_key", None)
        if type(pub) is not bytes or _sha(pub) != inbox.master_hash:
            raise SynProtocolError("SYN P2P signer does not match pinned inbox")
        if _network_id(network_id) != inbox.network_id:
            raise SynProtocolError("SYN P2P operator network differs from the inbox")
        _endpoint(bind_host, tcp_port if tcp_port else 1, loopback=allow_loopback)
        _endpoint(advertised_host, tcp_port if tcp_port else 1, loopback=allow_loopback)
        if type(tcp_port) is not int or not 0 <= tcp_port <= 65535:
            raise SynProtocolError("SYN P2P operator TCP port is invalid")
        self.signer = signer
        self.inbox = inbox
        self.network_id = network_id
        self.bind_host = bind_host
        self.advertised_host = advertised_host
        self.allow_loopback = allow_loopback
        self.master_hash = inbox.master_hash
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((bind_host, tcp_port))
            self.listener.listen(MAX_ACTIVE)
            self.listener.settimeout(0.25)
        except OSError:
            self.listener.close()
            raise
        self.port = self.listener.getsockname()[1]
        self._guard = threading.Lock()
        self._adverts: dict[str, dict[str, Any]] = {}
        self._active = threading.BoundedSemaphore(MAX_ACTIVE)
        self._threads: set[threading.Thread] = set()
        self._peer_budget: dict[str, tuple[float, int]] = {}
        self._global_budget: tuple[float, int] = (time.monotonic(), 0)

    def pulse(self, *, now: int | None = None) -> dict[str, Any]:
        current = _time(now)
        packet = make_advert(self.signer, network_id=self.network_id,
                             host=self.advertised_host, port=self.port,
                             now=current, loopback=self.allow_loopback)
        digest = _sha(canonical(packet)).hex()
        with self._guard:
            self._adverts = {k: v for k, v in self._adverts.items()
                             if v["signed_pulse"]["pulse"]["expires_at"] >= current}
            # Fixed upper bound; multiple different agents may use one pulse.
            if len(self._adverts) >= 8:
                oldest = min(self._adverts, key=lambda k: self._adverts[k]["signed_pulse"]["pulse"]["issued_at"])
                del self._adverts[oldest]
            self._adverts[digest] = packet
        return packet

    def broadcast_once(self, target_host: str, target_port: int, *, now: int | None = None) -> dict[str, Any]:
        if not isinstance(target_host, str) or type(target_port) is not int or not 1 <= target_port <= 65535:
            raise SynProtocolError("SYN broadcast target is invalid")
        if target_host != "255.255.255.255":
            _endpoint(target_host, target_port, loopback=self.allow_loopback)
        packet = self.pulse(now=now)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.bind((self.bind_host, 0))
            udp.sendto(canonical(packet), (target_host, target_port))
        return packet

    def _admit_peer(self, address: str) -> bool:
        """Bound CPU cost of signing untrusted handshakes; not DDoS proof."""
        instant = time.monotonic()
        with self._guard:
            start, total = self._global_budget
            if instant - start >= 60.0:
                start, total = instant, 0
                self._peer_budget = {}
            self._global_budget = (start, total + 1)
            if total >= MAX_GLOBAL_PER_MINUTE:
                return False
            if len(self._peer_budget) >= MAX_TRACKED_PEERS and address not in self._peer_budget:
                return False
            peer_start, count = self._peer_budget.get(address, (instant, 0))
            if instant - peer_start >= 60.0:
                peer_start, count = instant, 0
            self._peer_budget[address] = (peer_start, count + 1)
            return count < MAX_PER_PEER_PER_MINUTE

    def _serve_one(self, conn: socket.socket, peer_ip: str) -> None:
        with conn:
            conn.settimeout(TIMEOUT_SECONDS)
            _endpoint(peer_ip, 1, loopback=self.allow_loopback)
            hello = exact(_recv(conn), {"schema_version", "type_ref", "advert_sha256",
                                         "public", "nonce"}, "SYN agent ephemeral hello")
            if type(hello["schema_version"]) is not int or hello["schema_version"] != 1 or hello["type_ref"] != CLIENT_HELLO:
                raise SynProtocolError("SYN P2P agent hello revision differs")
            token = hex_bytes(hello["advert_sha256"], 32, "advert digest").hex()
            peer_pub = hex_bytes(hello["public"], 32, "agent X25519")
            if not any(hex_bytes(hello["nonce"], 32, "agent nonce")):
                raise SynProtocolError("SYN P2P agent nonce cannot be zero")
            with self._guard:
                advert = self._adverts.get(token)
            if advert is None:
                raise SynProtocolError("SYN P2P pulse is unknown, stale or unadvertised")
            verify_advert(advert, master=self.master_hash, network_id=self.network_id,
                          loopback=self.allow_loopback)
            private = X25519PrivateKey.generate()
            response = new_server_hello(self.signer, advert, hello, private)
            _send(conn, response)
            keys = derive_session(private, peer_pub, hello, response)
            signed = keys.decrypt(_recv(conn), client=True)
            verify_signed_offer(signed, advert=advert, now=int(time.time()))
            digest = self.inbox.admit(signed, advert["signed_pulse"],
                                      advert=advert, now=int(time.time()))
            # This is a delivery acknowledgment, NOT an adoption or pledge grant.
            result = {"schema_version": 1, "type_ref": OUTCOME_TYPE,
                      "state": "pending-operator-review", "offer_sha256": digest,
                      "activation_authorized": False}
            _send(conn, keys.encrypt(result, client=False))

    def _worker(self, conn: socket.socket, peer_ip: str) -> None:
        try:
            self._serve_one(conn, peer_ip)
        except Exception:
            # Never log untrusted input or leak an error oracle to a peer.
            # A monitoring implementation must record only a redacted counter.
            conn.close()
        finally:
            self._active.release()
            with self._guard:
                self._threads.discard(threading.current_thread())

    def serve(self, stop: threading.Event, *, broadcast_host: str | None = None,
              broadcast_port: int = 0, period_seconds: float = 10.0) -> None:
        if type(period_seconds) not in (int, float) or not 2 <= period_seconds <= 60:
            raise SynProtocolError("SYN broadcast interval must be 2..60 seconds")
        if broadcast_host is not None and not 1 <= broadcast_port <= 65535:
            raise SynProtocolError("SYN broadcast port is invalid")
        next_advert = 0.0
        try:
            while not stop.is_set():
                if broadcast_host is not None and time.monotonic() >= next_advert:
                    self.broadcast_once(broadcast_host, broadcast_port)
                    next_advert = time.monotonic() + period_seconds
                try:
                    conn, address = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        break
                    raise
                if not self._admit_peer(address[0]) or not self._active.acquire(blocking=False):
                    conn.close()
                    continue
                thread = threading.Thread(target=self._worker, args=(conn, address[0]), daemon=True)
                with self._guard:
                    self._threads.add(thread)
                thread.start()
        finally:
            self.listener.close()
            with self._guard:
                threads = list(self._threads)
            for thread in threads:
                thread.join(timeout=TIMEOUT_SECONDS + 1)

    def close(self) -> None:
        self.listener.close()


class SynP2PClient:
    """Agent caller. Never reads operator private keys or publishes hardware IDs."""

    def __init__(self, *, master_hash: bytes, network_id: str,
                 allow_loopback: bool = False):
        if type(master_hash) is not bytes or len(master_hash) != 32:
            raise SynProtocolError("SYN agent must pin the operator master before discovery")
        self.master_hash = master_hash
        self.network_id = _network_id(network_id)
        self.allow_loopback = allow_loopback

    def discover(self, *, udp_port: int, timeout_seconds: float = 3.0,
                 bind_host: str = "0.0.0.0") -> dict[str, Any]:
        if type(udp_port) is not int or not 1 <= udp_port <= 65535:
            raise SynProtocolError("SYN discovery UDP port invalid")
        if not isinstance(timeout_seconds, (float, int)) or not 0 < timeout_seconds <= 30:
            raise SynProtocolError("SYN discovery timeout outside bound")
        deadline = time.monotonic() + timeout_seconds
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.bind((bind_host, udp_port))
            checked = 0
            while checked < 64 and time.monotonic() < deadline:
                udp.settimeout(max(0.001, deadline - time.monotonic()))
                try:
                    data, address = udp.recvfrom(MAX_ADVERT_BYTES + 1)
                except socket.timeout:
                    break
                checked += 1
                if not 0 < len(data) <= MAX_ADVERT_BYTES:
                    continue
                try:
                    advert = parse(data)
                    return verify_advert(advert, master=self.master_hash,
                                         network_id=self.network_id,
                                         source_host=address[0], loopback=self.allow_loopback)
                except SynProtocolError:
                    continue
        raise SynProtocolError("SYN found no currently valid pulse from the pinned operator")

    def pledge(self, *, advert: dict[str, Any], temporary_signer: object,
               hardware_id: bytes, now: int | None = None) -> dict[str, Any]:
        current = _time(now)
        verify_advert(advert, master=self.master_hash, network_id=self.network_id,
                      now=current, loopback=self.allow_loopback)
        private = X25519PrivateKey.generate()
        hello = new_client_hello(advert, private)
        host = advert["host"]
        port = advert["port"]
        with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS) as conn:
            conn.settimeout(TIMEOUT_SECONDS)
            _send(conn, hello)
            response = verify_server_hello(_recv(conn), client_hello=hello, advert=advert)
            keys = derive_session(private, hex_bytes(response["server_public"], 32, "server X25519"),
                                  hello, response)
            offer = build_signed_offer(temporary_signer, advert=advert,
                                       hardware_id=hardware_id, now=current)
            _send(conn, keys.encrypt(offer, client=True))
            outcome = exact(keys.decrypt(_recv(conn), client=False),
                            {"schema_version", "type_ref", "state", "offer_sha256",
                             "activation_authorized"}, "private operator pledge receipt")
            if (type(outcome["schema_version"]) is not int or outcome["schema_version"] != 1
                    or outcome["type_ref"] != OUTCOME_TYPE
                    or outcome["state"] != "pending-operator-review"
                    or outcome["activation_authorized"] is not False
                    or outcome["offer_sha256"] != _sha(b"syn.agent.pledge.offer.v2\x00" +
                                                      canonical(offer["offer"])).hex()):
                raise SynProtocolError("SYN private operator acknowledgment is inconsistent")
            return outcome
