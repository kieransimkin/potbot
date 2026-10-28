"""SYN P2P v1: signed Phase-41 operator pulses and encrypted-offer envelopes.

The signed broadcast is public. The hardware identifier is authenticated by the
agent's temporary Ed25519 key and ONLY transmitted inside an authenticated
X25519/AES-GCM session. No SYN key material is exported over this interface.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .protocol import SynProtocolError, canonical, exact, hex_bytes, parse

PULSE_TYPE = "synny://type/syn-operator-pulse-v1"
PULSE_DOMAIN = b"syn.operator.pulse.v1\x00"
OFFER_TYPE = "synny://type/syn-agent-pledge-offer-v2"
OFFER_DOMAIN = b"syn.agent.pledge.offer.v2\x00"
ADVERT_TYPE = "synny://type/syn-p2p-advertisement-v1"
ADVERT_DOMAIN = b"syn.p2p.advertisement.v1\x00"
CLIENT_HELLO = "synny://type/syn-p2p-client-hello-v1"
SERVER_HELLO = "synny://type/syn-p2p-server-hello-v1"
SERVER_DOMAIN = b"syn.p2p.server-hello.v1\x00"
ENCRYPTED_TYPE = "synny://type/syn-p2p-encrypted-frame-v1"
CHANNEL_DOMAIN = b"syn.p2p.channel.v1\x00"
MAX_PULSE_AGE = 120
MAX_ADVERT_BYTES = 3500
MAX_WIRE_BYTES = 12288
_NETWORK = re.compile(r"[a-z][a-z0-9-]{0,63}\Z", re.ASCII)


class Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...
    def sign(self, payload: bytes) -> bytes: ...


def _bytes_public(value: bytes, size: int, label: str) -> bytes:
    if type(value) is not bytes or len(value) != size:
        raise SynProtocolError(f"SYN {label} must have exactly {size} bytes")
    return value


def _sha(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _verify(public: bytes, signature: bytes, content: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, content)
    except (ValueError, InvalidSignature) as exc:
        raise SynProtocolError("SYN P2P signature is invalid") from exc


def _time(now: int | None) -> int:
    value = int(time.time()) if now is None else now
    if type(value) is not int or value <= 0:
        raise SynProtocolError("SYN P2P requires an integer Unix clock")
    return value


def _endpoint(host: object, port: object, *, loopback: bool) -> str:
    if not isinstance(host, str):
        raise SynProtocolError("SYN P2P endpoint must be a literal IPv4 address")
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise SynProtocolError("SYN P2P endpoint is not IPv4") from exc
    # v1 is intentionally LAN-only, never arbitrary remote URLs or redirects.
    private = any(address in network for network in (
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    ))
    if not (private or (loopback and address.is_loopback)) or str(address) != host:
        raise SynProtocolError("SYN P2P endpoint must be an allowed private LAN address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SynProtocolError("SYN P2P endpoint port is invalid")
    return host


def _network_id(value: object) -> str:
    if not isinstance(value, str) or _NETWORK.fullmatch(value) is None:
        raise SynProtocolError("SYN P2P network identifier is invalid")
    return value


def signed_pulse(signer: Signer, *, network_id: str, issued_at: int,
                 challenge: bytes | None = None, lifetime: int = 45) -> dict[str, Any]:
    _network_id(network_id)
    _time(issued_at)
    if type(lifetime) is not int or not 1 <= lifetime <= MAX_PULSE_AGE:
        raise SynProtocolError("SYN P2P pulse lifetime invalid")
    import os
    token = os.urandom(32) if challenge is None else _bytes_public(challenge, 32, "pulse challenge")
    if not any(token):
        raise SynProtocolError("SYN P2P pulse challenge must not be zero")
    pub = _bytes_public(signer.public_key, 32, "operator public key")
    body = {"schema_version": 1, "type_ref": PULSE_TYPE, "network_id": network_id,
            "master_public_key_hash": _sha(pub).hex(), "challenge": token.hex(),
            "issued_at": issued_at, "expires_at": issued_at + lifetime}
    signature = _bytes_public(signer.sign(PULSE_DOMAIN + canonical(body)), 64, "pulse signature")
    _verify(pub, signature, PULSE_DOMAIN + canonical(body))
    return {"pulse": body, "operator_public_key": pub.hex(), "signature": signature.hex()}


def verify_pulse(value: object, *, master: bytes, network_id: str, now: int) -> dict[str, Any]:
    outer = exact(value, {"pulse", "operator_public_key", "signature"}, "operator pulse")
    pulse = exact(outer["pulse"], {"schema_version", "type_ref", "network_id",
                    "master_public_key_hash", "challenge", "issued_at", "expires_at"}, "pulse body")
    if type(pulse["schema_version"]) is not int or pulse["schema_version"] != 1 or pulse["type_ref"] != PULSE_TYPE:
        raise SynProtocolError("SYN P2P pulse version differs")
    if pulse["network_id"] != _network_id(network_id) or pulse["master_public_key_hash"] != master.hex():
        raise SynProtocolError("SYN P2P pulse targets another master/network")
    if hex_bytes(pulse["challenge"], 32, "pulse challenge") == bytes(32):
        raise SynProtocolError("SYN P2P pulse challenge is invalid")
    start, end = pulse["issued_at"], pulse["expires_at"]
    if (type(start) is not int or type(end) is not int or not 0 < start <= now <= end
            or not 1 <= end - start <= MAX_PULSE_AGE):
        raise SynProtocolError("SYN P2P pulse expired or has invalid bounds")
    pub = hex_bytes(outer["operator_public_key"], 32, "operator public key")
    if _sha(pub) != _bytes_public(master, 32, "pinned master"):
        raise SynProtocolError("SYN P2P operator key differs from pinned identity")
    _verify(pub, hex_bytes(outer["signature"], 64, "pulse signature"), PULSE_DOMAIN + canonical(pulse))
    return outer


def make_advert(signer: Signer, *, network_id: str, host: str, port: int,
                now: int, loopback: bool = False, lifetime: int = 45) -> dict[str, Any]:
    _endpoint(host, port, loopback=loopback)
    pulse = signed_pulse(signer, network_id=network_id, issued_at=now, lifetime=lifetime)
    body = {"schema_version": 1, "type_ref": ADVERT_TYPE, "host": host,
            "port": port, "signed_pulse": pulse}
    pub = _bytes_public(signer.public_key, 32, "operator key")
    sig = _bytes_public(signer.sign(ADVERT_DOMAIN + canonical(body)), 64, "advert signature")
    advert = {**body, "signature": sig.hex()}
    if len(canonical(advert)) > MAX_ADVERT_BYTES:
        raise SynProtocolError("SYN P2P advert exceeds broadcast datagram limit")
    return advert


def verify_advert(value: object, *, master: bytes, network_id: str,
                  now: int | None = None, source_host: str | None = None,
                  loopback: bool = False) -> dict[str, Any]:
    advert = exact(value, {"schema_version", "type_ref", "host", "port", "signed_pulse",
                           "signature"}, "P2P advertisement")
    if type(advert["schema_version"]) is not int or advert["schema_version"] != 1 or advert["type_ref"] != ADVERT_TYPE:
        raise SynProtocolError("SYN P2P advertisement version differs")
    host = _endpoint(advert["host"], advert["port"], loopback=loopback)
    if source_host is not None and host != source_host:
        raise SynProtocolError("SYN P2P advert endpoint differs from UDP source")
    signed = verify_pulse(advert["signed_pulse"], master=master, network_id=network_id,
                          now=_time(now))
    body = {key: advert[key] for key in advert if key != "signature"}
    _verify(hex_bytes(signed["operator_public_key"], 32, "operator key"),
            hex_bytes(advert["signature"], 64, "advert signature"),
            ADVERT_DOMAIN + canonical(body))
    if len(canonical(advert)) > MAX_ADVERT_BYTES:
        raise SynProtocolError("SYN P2P advert is too large")
    return advert


def build_signed_offer(signer: Signer, *, advert: dict[str, Any],
                       hardware_id: bytes, now: int) -> dict[str, Any]:
    if (type(hardware_id) is not bytes or not 6 <= len(hardware_id) <= 256):
        raise SynProtocolError("SYN hardware ID must be 6..256 raw bytes")
    pub = _bytes_public(signer.public_key, 32, "temporary public key")
    pulse = advert["signed_pulse"]["pulse"]
    body = {"schema_version": 2, "type_ref": OFFER_TYPE,
            "temporary_public_key_hash": _sha(pub).hex(),
            "hardware_unique_id": hardware_id.hex(),
            "timestamp_unix_seconds": now,
            "target_master_public_key_hash": pulse["master_public_key_hash"],
            "pulse_challenge": pulse["challenge"]}
    sig = _bytes_public(signer.sign(OFFER_DOMAIN + canonical(body)), 64, "offer signature")
    return {"offer": body, "temporary_public_key": pub.hex(), "signature": sig.hex()}


def verify_signed_offer(value: object, *, advert: dict[str, Any], now: int) -> dict[str, Any]:
    outer = exact(value, {"offer", "temporary_public_key", "signature"}, "agent signed offer")
    body = exact(outer["offer"], {"schema_version", "type_ref", "temporary_public_key_hash",
                  "hardware_unique_id", "timestamp_unix_seconds", "target_master_public_key_hash",
                  "pulse_challenge"}, "signed hardware pledge")
    if type(body["schema_version"]) is not int or body["schema_version"] != 2 or body["type_ref"] != OFFER_TYPE:
        raise SynProtocolError("SYN agent pledge version invalid")
    signed_pulse_body = advert["signed_pulse"]["pulse"]
    if (body["target_master_public_key_hash"] != signed_pulse_body["master_public_key_hash"]
            or body["pulse_challenge"] != signed_pulse_body["challenge"]):
        raise SynProtocolError("SYN P2P offer targets a different signed pulse")
    signed_at = body["timestamp_unix_seconds"]
    if type(signed_at) is not int or not 0 < signed_at <= now or now - signed_at > MAX_PULSE_AGE:
        raise SynProtocolError("SYN P2P offer is stale or future-dated")
    hid = hex_bytes(body["hardware_unique_id"], len(body["hardware_unique_id"]) // 2,
                    "hardware identifier") if isinstance(body["hardware_unique_id"], str) else b""
    if not 6 <= len(hid) <= 256:
        raise SynProtocolError("SYN hardware identifier has invalid length")
    pub = hex_bytes(outer["temporary_public_key"], 32, "temporary public key")
    if _sha(pub).hex() != body["temporary_public_key_hash"]:
        raise SynProtocolError("SYN temporary public-key hash differs from signer")
    _verify(pub, hex_bytes(outer["signature"], 64, "offer signature"),
            OFFER_DOMAIN + canonical(body))
    return outer


def x25519_public(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass(frozen=True, slots=True, repr=False)
class SessionKeys:
    client_key: bytes
    server_key: bytes
    client_nonce: bytes
    server_nonce: bytes
    transcript: bytes

    def encrypt(self, value: dict[str, Any], *, client: bool) -> dict[str, Any]:
        plaintext = canonical(value)
        if not 0 < len(plaintext) <= 4096:
            raise SynProtocolError("SYN P2P encrypted message exceeds bounds")
        key = self.client_key if client else self.server_key
        nonce = self.client_nonce if client else self.server_nonce
        purpose = b"request" if client else b"reply"
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, CHANNEL_DOMAIN + purpose + self.transcript)
        return {"schema_version": 1, "type_ref": ENCRYPTED_TYPE,
                "ciphertext": ciphertext.hex()}

    def decrypt(self, frame: object, *, client: bool) -> dict[str, Any]:
        value = exact(frame, {"schema_version", "type_ref", "ciphertext"}, "encrypted SYN P2P frame")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["type_ref"] != ENCRYPTED_TYPE:
            raise SynProtocolError("SYN P2P encrypted frame revision differs")
        raw = value["ciphertext"]
        if not isinstance(raw, str) or len(raw) % 2 or not 32 <= len(raw) <= 8300:
            raise SynProtocolError("SYN P2P encrypted ciphertext exceeds bound")
        try:
            data = bytes.fromhex(raw)
        except ValueError as exc:
            raise SynProtocolError("SYN P2P encrypted ciphertext is malformed") from exc
        key = self.client_key if client else self.server_key
        nonce = self.client_nonce if client else self.server_nonce
        purpose = b"request" if client else b"reply"
        try:
            return parse(AESGCM(key).decrypt(nonce, data, CHANNEL_DOMAIN + purpose + self.transcript))
        except (InvalidTag, ValueError) as exc:
            raise SynProtocolError("SYN P2P encrypted channel authentication failed") from exc


def derive_session(private: X25519PrivateKey, peer_public: bytes,
                   client_hello: dict[str, Any], server_hello: dict[str, Any]) -> SessionKeys:
    peer = hex_bytes(peer_public.hex(), 32, "peer X25519")
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
    except ValueError as exc:
        raise SynProtocolError("SYN P2P key agreement rejected a peer") from exc
    if shared == bytes(32):
        raise SynProtocolError("SYN P2P X25519 produced a low-order shared secret")
    transcript = _sha(canonical({"client_hello": client_hello, "server_hello": server_hello}))
    salt = _sha(hex_bytes(client_hello["nonce"], 32, "client nonce")
                + hex_bytes(server_hello["nonce"], 32, "server nonce"))
    material = HKDF(algorithm=hashes.SHA256(), length=88, salt=salt,
                    info=CHANNEL_DOMAIN + transcript).derive(shared)
    return SessionKeys(material[:32], material[32:64], material[64:76], material[76:88], transcript)


def verify_server_hello(value: object, *, client_hello: dict[str, Any],
                        advert: dict[str, Any]) -> dict[str, Any]:
    hello = exact(value, {"schema_version", "type_ref", "advert_sha256", "client_public",
                         "client_nonce", "server_public", "nonce", "signature"}, "server hello")
    if type(hello["schema_version"]) is not int or hello["schema_version"] != 1 or hello["type_ref"] != SERVER_HELLO:
        raise SynProtocolError("SYN server hello version differs")
    if (hello["advert_sha256"] != _sha(canonical(advert)).hex()
            or hello["client_public"] != client_hello["public"]
            or hello["client_nonce"] != client_hello["nonce"]):
        raise SynProtocolError("SYN server hello not bound to current client")
    hex_bytes(hello["server_public"], 32, "server X25519")
    if not any(hex_bytes(hello["nonce"], 32, "server nonce")):
        raise SynProtocolError("SYN server nonce is zero")
    preimage = SERVER_DOMAIN + canonical({k: hello[k] for k in hello if k != "signature"})
    _verify(hex_bytes(advert["signed_pulse"]["operator_public_key"], 32, "master public key"),
            hex_bytes(hello["signature"], 64, "server proof"), preimage)
    return hello


def new_client_hello(advert: dict[str, Any], private: X25519PrivateKey,
                     nonce: bytes | None = None) -> dict[str, Any]:
    import os
    token = os.urandom(32) if nonce is None else _bytes_public(nonce, 32, "client nonce")
    if not any(token):
        raise SynProtocolError("SYN client nonce cannot be zero")
    return {"schema_version": 1, "type_ref": CLIENT_HELLO,
            "advert_sha256": _sha(canonical(advert)).hex(),
            "public": x25519_public(private).hex(), "nonce": token.hex()}


def new_server_hello(signer: Signer, advert: dict[str, Any],
                     client_hello: dict[str, Any], private: X25519PrivateKey) -> dict[str, Any]:
    import os
    body = {"schema_version": 1, "type_ref": SERVER_HELLO,
            "advert_sha256": _sha(canonical(advert)).hex(),
            "client_public": client_hello["public"], "client_nonce": client_hello["nonce"],
            "server_public": x25519_public(private).hex(), "nonce": os.urandom(32).hex()}
    signature = _bytes_public(signer.sign(SERVER_DOMAIN + canonical(body)), 64, "server proof")
    return {**body, "signature": signature.hex()}
