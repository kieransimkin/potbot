"""Verify published identity keys and hourly delegated Ed25519 signatures.

This module handles public data only. A verifier must pin the expected base-key
fingerprint through an independently trusted channel and supply its own scope
policy. A certificate or self-signature cannot establish that initial trust.

Protocol objects contain only bounded ASCII strings, integer counters, and
objects. Sorted compact JSON therefore implements the applicable RFC 8785 JCS
subset without ambiguous floating-point or Unicode canonicalization. Received
fields are never rewritten before checking a signature, including SYN aliases.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from potbot.domain.errors import InvalidInputError
from potbot.domain.syn import type_identity
from potbot.semantics.catalog import builtin_types, extension_types

IDENTITY_PROFILE = "potbot-identity-v1"
SIGNING_PROFILE = "potbot-hourly-signing-v1"
BASE_PATH = "m/6969'/420'/0'/0/0"
MAX_SIGNING_EPOCH = 2_147_483_647
EPOCH_SECONDS = 3600
MAX_VALIDITY_SECONDS = 300
MAX_CLOCK_SKEW_SECONDS = 30
MAX_PUBLIC_DOCUMENT_BYTES = 65_536
SIGNATURE_PREFIX = b"potbot-signature-v2\x00"
DELEGATION_PREFIX = b"potbot-hourly-delegation-v1\x00"
SIGNATURE_DOMAINS = frozenset(("potbot.artifact.v2", "potbot.event.v2", "potbot.challenge.v2"))

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_AUDIENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_IDENTITY_FIELDS = frozenset(
    (
        "type_ref",
        "schema_version",
        "profile_id",
        "hash_algorithm",
        "signer_hash",
        "public_key_algorithm",
        "public_key_encoding",
        "public_key",
    )
)
_DELEGATION_FIELDS = frozenset(
    (
        "type_ref",
        "schema_version",
        "profile_id",
        "signing_profile_id",
        "signer_hash",
        "signing_epoch",
        "valid_from",
        "valid_until",
        "public_key_algorithm",
        "public_key_encoding",
        "public_key",
        "signature_algorithm",
        "signature_encoding",
        "signature",
    )
)
_ENVELOPE_FIELDS = frozenset(
    (
        "type_ref",
        "schema_version",
        "profile_id",
        "signer_hash",
        "domain",
        "workspace_id",
        "payload_type_ref",
        "payload_sha256",
        "audience",
        "request_id",
        "issued_at",
        "expires_at",
        "signature_algorithm",
        "signature_encoding",
        "signature",
        "signing_profile_id",
        "signing_epoch",
    )
)
_BUNDLE_FIELDS = frozenset(("type_ref", "schema_version", "identity", "delegation", "envelope"))


class PublicVerificationError(InvalidInputError):
    """Malformed, untrusted, out-of-scope, expired, or invalid public evidence."""


class ReplayDetectedError(PublicVerificationError):
    """An authenticated request identifier was already consumed."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise PublicVerificationError("duplicate JSON object key")
        result[key] = value
    return result


def _no_constant(_: str) -> Any:
    raise PublicVerificationError("non-JSON numeric constant")


def _no_float(_: str) -> Any:
    raise PublicVerificationError("protocol counters must be JSON integers")


def _check_canonical_subset(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise PublicVerificationError("public document nesting exceeds limit")
    if isinstance(value, int) and not isinstance(value, bool):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise PublicVerificationError("protocol integer exceeds exact JSON range")
        return
    if isinstance(value, str):
        if len(value) > MAX_PUBLIC_DOCUMENT_BYTES:
            raise PublicVerificationError("public string exceeds size limit")
        if any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise PublicVerificationError("protocol strings must be printable ASCII")
        return
    if isinstance(value, dict):
        if len(value) > 32:
            raise PublicVerificationError("public object exceeds field limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise PublicVerificationError("JSON object keys must be strings")
            _check_canonical_subset(key, depth=depth + 1)
            _check_canonical_subset(item, depth=depth + 1)
        return
    raise PublicVerificationError("unsupported value in public protocol object")


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Encode the protocol's strict ASCII/integer/object JCS subset."""
    if not isinstance(value, dict):
        value = dict(value)
    _check_canonical_subset(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    if len(encoded) > MAX_PUBLIC_DOCUMENT_BYTES:
        raise PublicVerificationError("public document exceeds size limit")
    return encoded


def load_public_document(raw: str | bytes | Mapping[str, object]) -> dict[str, Any]:
    """Decode strict bounded JSON, rejecting duplicate keys at every depth."""
    if isinstance(raw, Mapping):
        # Detach from callers' mutable mappings and enforce the same public subset.
        raw = canonical_json_bytes(dict(raw))
    if not isinstance(raw, (str, bytes)):
        raise PublicVerificationError("public document must be JSON bytes or an object")
    try:
        if isinstance(raw, bytes):
            if len(raw) > MAX_PUBLIC_DOCUMENT_BYTES:
                raise PublicVerificationError("public document exceeds size limit")
            raw = raw.decode("utf-8", errors="strict")
        if len(raw.encode("utf-8")) > MAX_PUBLIC_DOCUMENT_BYTES:
            raise PublicVerificationError("public document exceeds size limit")
        value = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=_no_constant, parse_float=_no_float
        )
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, PublicVerificationError):
            raise
        raise PublicVerificationError("invalid strict UTF-8 JSON document") from error
    if not isinstance(value, dict):
        raise PublicVerificationError("public document must be a JSON object")
    canonical_json_bytes(value)
    return value


def _object(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise PublicVerificationError(f"{label} requires exactly its versioned fields")
    return value


def _constant(value: object, expected: str | int, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise PublicVerificationError(f"unsupported {label}")


def _string(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PublicVerificationError(f"invalid {label}")
    return value


def _integer(value: object, minimum: int, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise PublicVerificationError(f"invalid {label}")
    return value


def _type_slug(value: object, label: str) -> str:
    try:
        return type_identity(value)
    except InvalidInputError as error:
        raise PublicVerificationError(f"invalid {label}") from error


def _type_constant(value: object, expected: str) -> None:
    if _type_slug(value, "type reference") != expected:
        raise PublicVerificationError("unexpected public document type")


def _base64(value: object, length: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) != ((length + 2) // 3) * 4:
        raise PublicVerificationError(f"invalid {label} encoding")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise PublicVerificationError(f"invalid {label} encoding") from error
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise PublicVerificationError(f"noncanonical {label} encoding")
    return decoded


def _timestamp(value: object, label: str) -> int:
    text = _string(value, _TIMESTAMP, label)
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        result = int((moment - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds())
    except (ValueError, OverflowError) as error:
        raise PublicVerificationError(f"invalid {label} calendar time") from error
    if result < 0:
        raise PublicVerificationError(f"{label} must be nonnegative POSIX time")
    return result


def _verify_ed25519(public_key: bytes, signature: bytes, message: bytes, label: str) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise PublicVerificationError(
            "public signature verification requires the potbot security dependency"
        ) from error
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError) as error:
        raise PublicVerificationError(f"invalid {label} signature") from error


def delegation_signing_bytes(value: Mapping[str, object]) -> bytes:
    """Return the fixed, domain-separated certificate message, excluding its signature."""
    return DELEGATION_PREFIX + canonical_json_bytes(
        {key: item for key, item in value.items() if key != "signature"}
    )


def envelope_signing_bytes(value: Mapping[str, object]) -> bytes:
    """Return the original v2 signing message; never normalize received signed fields."""
    return SIGNATURE_PREFIX + canonical_json_bytes(
        {key: item for key, item in value.items() if key != "signature"}
    )


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    signer_hash: str
    public_key: bytes

    def to_dict(self) -> dict[str, object]:
        return {"verified": True, **public_identity_descriptor(self.public_key)}


@dataclass(frozen=True, slots=True)
class VerifiedDelegation:
    signer_hash: str
    signing_epoch: int
    valid_from: int
    valid_until: int
    public_key: bytes


def public_identity_descriptor(public_key: bytes) -> dict[str, object]:
    """Publish a raw 32-byte verification key and its stable base fingerprint."""
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise PublicVerificationError("base verification key must contain 32 public bytes")
    return {
        "type_ref": "synny://type/public-identity-descriptor",
        "schema_version": 2,
        "profile_id": IDENTITY_PROFILE,
        "hash_algorithm": "SHA-256",
        "signer_hash": hashlib.sha256(public_key).hexdigest(),
        "public_key_algorithm": "Ed25519",
        "public_key_encoding": "base64",
        "public_key": base64.b64encode(public_key).decode("ascii"),
    }


def verify_public_identity(
    raw: str | bytes | Mapping[str, object], *, expected_signer_hash: str
) -> VerifiedIdentity:
    """Bind published base-key bytes to the verifier's independently pinned identity."""
    expected = _string(expected_signer_hash, _HASH, "expected identity fingerprint")
    value = _object(load_public_document(raw), _IDENTITY_FIELDS, "public identity")
    _type_constant(value["type_ref"], "public-identity-descriptor")
    _constant(value["schema_version"], 2, "identity descriptor version")
    _constant(value["profile_id"], IDENTITY_PROFILE, "identity profile")
    _constant(value["hash_algorithm"], "SHA-256", "identity hash algorithm")
    _constant(value["public_key_algorithm"], "Ed25519", "public-key algorithm")
    _constant(value["public_key_encoding"], "base64", "public-key encoding")
    supplied = _string(value["signer_hash"], _HASH, "identity fingerprint")
    public_key = _base64(value["public_key"], 32, "base public key")
    actual = hashlib.sha256(public_key).hexdigest()
    if not hmac.compare_digest(actual, supplied) or not hmac.compare_digest(actual, expected):
        raise PublicVerificationError("published key does not match the pinned identity")
    return VerifiedIdentity(actual, public_key)


def verify_hourly_delegation(
    raw: str | bytes | Mapping[str, object], *, identity: VerifiedIdentity
) -> VerifiedDelegation:
    """Authenticate exactly one hourly public key using the pinned base public key."""
    if (
        not isinstance(identity, VerifiedIdentity)
        or not isinstance(identity.public_key, bytes)
        or len(identity.public_key) != 32
        or hashlib.sha256(identity.public_key).hexdigest() != identity.signer_hash
    ):
        raise PublicVerificationError("delegation requires a checked base identity")
    value = _object(load_public_document(raw), _DELEGATION_FIELDS, "hourly delegation")
    _type_constant(value["type_ref"], "hourly-key-delegation")
    _constant(value["schema_version"], 1, "delegation version")
    _constant(value["profile_id"], IDENTITY_PROFILE, "identity profile")
    _constant(value["signing_profile_id"], SIGNING_PROFILE, "hourly signing profile")
    _constant(value["public_key_algorithm"], "Ed25519", "public-key algorithm")
    _constant(value["public_key_encoding"], "base64", "public-key encoding")
    _constant(value["signature_algorithm"], "Ed25519", "signature algorithm")
    _constant(value["signature_encoding"], "base64", "signature encoding")
    signer_hash = _string(value["signer_hash"], _HASH, "delegation identity fingerprint")
    if not hmac.compare_digest(identity.signer_hash, signer_hash):
        raise PublicVerificationError("delegation is for another base identity")
    epoch = _integer(value["signing_epoch"], 0, MAX_SIGNING_EPOCH, "delegation epoch")
    start = _integer(value["valid_from"], 0, (MAX_SIGNING_EPOCH + 1) * EPOCH_SECONDS, "valid_from")
    end = _integer(value["valid_until"], 0, (MAX_SIGNING_EPOCH + 1) * EPOCH_SECONDS, "valid_until")
    if start != epoch * EPOCH_SECONDS or end != (epoch + 1) * EPOCH_SECONDS:
        raise PublicVerificationError("delegation interval must equal its exact POSIX hour")
    public_key = _base64(value["public_key"], 32, "hourly public key")
    signature = _base64(value["signature"], 64, "delegation signature")
    _verify_ed25519(identity.public_key, signature, delegation_signing_bytes(value), "delegation")
    return VerifiedDelegation(signer_hash, epoch, start, end, public_key)


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """Independently authenticated verifier context, never taken from the bundle.

    ``known_type_refs`` may supply a trusted effective SYN catalog. When omitted,
    the shipped native and Potbot catalogs are used. Aliases identify one Type;
    the signed spelling is preserved during cryptographic verification.
    """

    expected_signer_hash: str
    audience: str
    workspace_id: str
    domain: str
    allowed_payload_type_refs: frozenset[str]
    known_type_refs: frozenset[str] | None = None
    clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS

    def __post_init__(self) -> None:
        _string(self.expected_signer_hash, _HASH, "expected identity fingerprint")
        _string(self.audience, _AUDIENCE, "expected audience")
        _string(self.workspace_id, _UUID, "expected workspace UUID")
        if not isinstance(self.domain, str) or self.domain not in SIGNATURE_DOMAINS:
            raise PublicVerificationError("unsupported expected signature domain")
        _integer(self.clock_skew_seconds, 0, MAX_CLOCK_SKEW_SECONDS, "clock skew allowance")
        if not isinstance(self.allowed_payload_type_refs, frozenset):
            raise PublicVerificationError("allowed payload types must be an immutable set")
        if not self.allowed_payload_type_refs or len(self.allowed_payload_type_refs) > 4096:
            raise PublicVerificationError("allowed payload type set is empty or too large")
        allowed = {
            _type_slug(ref, "allowed payload type") for ref in self.allowed_payload_type_refs
        }
        known_refs = self.known_type_refs
        if known_refs is None:
            known_refs = frozenset(
                str(record.ref) for record in (*builtin_types(), *extension_types())
            )
        if not isinstance(known_refs, frozenset) or len(known_refs) > 100_000:
            raise PublicVerificationError("known types must be a bounded immutable catalog")
        known = {_type_slug(ref, "known payload type") for ref in known_refs}
        if not allowed.issubset(known):
            raise PublicVerificationError("allowed payload type is absent from the trusted catalog")
        # Freeze the catalog actually used even when it came from the bundled data.
        object.__setattr__(self, "known_type_refs", known_refs)


@dataclass(frozen=True, slots=True)
class VerifiedSignature:
    signer_hash: str
    signing_epoch: int
    request_id: str
    payload_type_ref: str
    payload_sha256: str
    domain: str
    audience: str
    workspace_id: str
    issued_at: int
    expires_at: int
    replay_protected: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"verified": True, **asdict(self)}


def _current_time(now: int | None) -> int:
    if now is None:
        now = int(time.time())
    return _integer(now, 0, (MAX_SIGNING_EPOCH + 1) * EPOCH_SECONDS, "verification time")


def verify_signature_bundle(
    raw_bundle: str | bytes | Mapping[str, object],
    payload: bytes,
    *,
    policy: VerificationPolicy,
    now: int | None = None,
) -> VerifiedSignature:
    """Check identity, delegation, exact payload, signature, scope, and freshness.

    This repeatable verification does not consume a request identifier or perform
    an external action. Use ``accept_signature_bundle`` for durable one-time
    acceptance before an authorization consumer acts on the result.
    """
    if not isinstance(payload, bytes):
        raise PublicVerificationError("payload must be its exact original bytes")
    return verify_signature_bundle_digest(
        raw_bundle, hashlib.sha256(payload).hexdigest(), policy=policy, now=now
    )


def verify_signature_bundle_digest(
    raw_bundle: str | bytes | Mapping[str, object],
    expected_payload_sha256: str,
    *,
    policy: VerificationPolicy,
    now: int | None = None,
) -> VerifiedSignature:
    """Verify against a digest independently computed from the original payload.

    This streaming-friendly entry point has the same checks as the bytes API.
    The expected digest must come from the caller's payload, never from the
    unverified bundle. It does not change the Ed25519 signing algorithm or enable
    the Ed25519ph variant; the signature still covers the full v2 envelope.
    """
    expected_digest = _string(expected_payload_sha256, _HASH, "expected payload digest")
    if not isinstance(policy, VerificationPolicy):
        raise PublicVerificationError("verification requires an explicit checked scope policy")
    current = _current_time(now)
    bundle = _object(load_public_document(raw_bundle), _BUNDLE_FIELDS, "signature bundle")
    _type_constant(bundle["type_ref"], "public-signature-bundle")
    _constant(bundle["schema_version"], 1, "signature bundle version")
    identity = verify_public_identity(
        _object(bundle["identity"], _IDENTITY_FIELDS, "public identity"),
        expected_signer_hash=policy.expected_signer_hash,
    )
    delegation = verify_hourly_delegation(
        _object(bundle["delegation"], _DELEGATION_FIELDS, "hourly delegation"), identity=identity
    )
    value = _object(bundle["envelope"], _ENVELOPE_FIELDS, "signature envelope")
    _type_constant(value["type_ref"], "signature-envelope")
    _constant(value["schema_version"], 2, "signature envelope version")
    _constant(value["profile_id"], IDENTITY_PROFILE, "identity profile")
    _constant(value["signing_profile_id"], SIGNING_PROFILE, "hourly signing profile")
    _constant(value["signature_algorithm"], "Ed25519", "signature algorithm")
    _constant(value["signature_encoding"], "base64", "signature encoding")
    signer_hash = _string(value["signer_hash"], _HASH, "envelope identity fingerprint")
    epoch = _integer(value["signing_epoch"], 0, MAX_SIGNING_EPOCH, "signing epoch")
    if (
        not hmac.compare_digest(signer_hash, identity.signer_hash)
        or epoch != delegation.signing_epoch
    ):
        raise PublicVerificationError("envelope does not match its identity and hourly delegation")
    domain = value["domain"]
    if not isinstance(domain, str) or domain not in SIGNATURE_DOMAINS or domain != policy.domain:
        raise PublicVerificationError("signature domain is not authorized by verifier policy")
    audience = _string(value["audience"], _AUDIENCE, "audience")
    workspace = _string(value["workspace_id"], _UUID, "workspace UUID")
    if audience != policy.audience or workspace != policy.workspace_id:
        raise PublicVerificationError("signature audience or workspace is outside verifier scope")
    request = _string(value["request_id"], _UUID, "request UUID")
    payload_ref = value["payload_type_ref"]
    payload_slug = _type_slug(payload_ref, "payload type reference")
    allowed = {_type_slug(ref, "allowed payload type") for ref in policy.allowed_payload_type_refs}
    if payload_slug not in allowed:
        raise PublicVerificationError("payload type is not authorized by verifier policy")
    payload_hash = _string(value["payload_sha256"], _HASH, "payload digest")
    if not hmac.compare_digest(expected_digest, payload_hash):
        raise PublicVerificationError("payload bytes do not match the signed digest")
    issued = _timestamp(value["issued_at"], "issued_at")
    expires = _timestamp(value["expires_at"], "expires_at")
    if (
        issued // EPOCH_SECONDS != epoch
        or not delegation.valid_from <= issued < delegation.valid_until
    ):
        raise PublicVerificationError("issued_at does not belong to the delegated hour")
    if not 0 < expires - issued <= MAX_VALIDITY_SECONDS:
        raise PublicVerificationError("signature validity must be between 1 and 300 seconds")
    if issued > current + policy.clock_skew_seconds:
        raise PublicVerificationError("signature was issued too far in the future")
    if current >= expires + policy.clock_skew_seconds:
        raise PublicVerificationError("signature has expired")
    signature = _base64(value["signature"], 64, "message signature")
    _verify_ed25519(delegation.public_key, signature, envelope_signing_bytes(value), "message")
    return VerifiedSignature(
        signer_hash,
        epoch,
        request,
        payload_ref,
        payload_hash,
        domain,
        audience,
        workspace,
        issued,
        expires,
    )


class ReplayStore(Protocol):
    """Consume a signer/request pair atomically, retaining it through acceptance expiry."""

    def consume(
        self, signer_hash: str, request_id: str, *, retain_until: int, now: int
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SQLiteReplayStore:
    """A durable, concurrent replay ledger containing only public identifiers.

    Put this database in the verifier's protected state directory. A caller that
    removes or rolls back the database also removes its replay protection.
    """

    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or str(self.path) == ":memory:":
            raise PublicVerificationError("replay ledger requires a persistent filesystem path")

    def consume(self, signer_hash: str, request_id: str, *, retain_until: int, now: int) -> None:
        import sqlite3

        _string(signer_hash, _HASH, "replay identity")
        _string(request_id, _UUID, "replay request UUID")
        _integer(now, 0, 2**53 - 1, "replay current time")
        _integer(retain_until, now + 1, 2**53 - 1, "replay retention time")
        try:
            with sqlite3.connect(self.path, timeout=10, isolation_level=None) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS accepted_public_signatures ("
                        "signer_hash TEXT NOT NULL, request_id TEXT NOT NULL, "
                        "retain_until INTEGER NOT NULL, PRIMARY KEY(signer_hash, request_id))"
                    )
                    connection.execute(
                        "DELETE FROM accepted_public_signatures WHERE retain_until <= ?", (now,)
                    )
                    connection.execute(
                        "INSERT INTO accepted_public_signatures VALUES (?, ?, ?)",
                        (signer_hash, request_id, retain_until),
                    )
                    connection.execute("COMMIT")
                except sqlite3.IntegrityError as error:
                    connection.execute("ROLLBACK")
                    raise ReplayDetectedError(
                        "authenticated request was already consumed"
                    ) from error
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.Error as error:
            raise PublicVerificationError("durable replay ledger is unavailable") from error


def accept_signature_bundle(
    raw_bundle: str | bytes | Mapping[str, object],
    payload: bytes,
    *,
    policy: VerificationPolicy,
    replay_store: ReplayStore,
    now: int | None = None,
) -> VerifiedSignature:
    """Verify and atomically reserve a fresh request before a consumer authorizes an action."""
    if not isinstance(payload, bytes):
        raise PublicVerificationError("payload must be its exact original bytes")
    return accept_signature_bundle_digest(
        raw_bundle,
        hashlib.sha256(payload).hexdigest(),
        policy=policy,
        replay_store=replay_store,
        now=now,
    )


def accept_signature_bundle_digest(
    raw_bundle: str | bytes | Mapping[str, object],
    expected_payload_sha256: str,
    *,
    policy: VerificationPolicy,
    replay_store: ReplayStore,
    now: int | None = None,
) -> VerifiedSignature:
    """Atomically accept a bundle against an independently computed payload digest."""
    current = _current_time(now)
    verified = verify_signature_bundle_digest(
        raw_bundle, expected_payload_sha256, policy=policy, now=current
    )
    replay_store.consume(
        verified.signer_hash,
        verified.request_id,
        retain_until=verified.expires_at + policy.clock_skew_seconds,
        now=current,
    )
    return replace(verified, replay_protected=True)
