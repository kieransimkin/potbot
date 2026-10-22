"""Public-only identity verification, adversarial binding, and durable replay tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, FormatChecker

from potbot.security.public import (
    DELEGATION_PREFIX,
    EPOCH_SECONDS,
    MAX_PUBLIC_DOCUMENT_BYTES,
    MAX_SIGNING_EPOCH,
    SIGNATURE_PREFIX,
    PublicVerificationError,
    ReplayDetectedError,
    SQLiteReplayStore,
    VerificationPolicy,
    VerifiedIdentity,
    _verify_ed25519,
    accept_signature_bundle,
    canonical_json_bytes,
    delegation_signing_bytes,
    envelope_signing_bytes,
    load_public_document,
    public_identity_descriptor,
    verify_hourly_delegation,
    verify_public_identity,
    verify_signature_bundle,
    verify_signature_bundle_digest,
)

_VOCAB = Path(__file__).resolve().parents[2] / "vocab" / "security-public"
_PAYLOAD = b"An exact payload.\x00\xff\n"
_WORKSPACE = "88e26d3a-f40c-43af-9085-4a87415b7fd9"
_REQUEST = "5cf4914d-67f6-4e3f-9e7c-a02b9060d428"
_NOW = 1_792_584_100
# Synthetic test-only keys. The production module never imports private-key APIs.
_BASE = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_HOURLY = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _utc(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resign(bundle: dict[str, Any], *, base: bool = True, hourly: bool = True) -> None:
    if base:
        bundle["delegation"]["signature"] = _b64(
            _BASE.sign(delegation_signing_bytes(bundle["delegation"]))
        )
    if hourly:
        bundle["envelope"]["signature"] = _b64(
            _HOURLY.sign(envelope_signing_bytes(bundle["envelope"]))
        )


def _bundle(*, issued: int = _NOW, validity: int = 120) -> dict[str, Any]:
    identity = public_identity_descriptor(_public(_BASE))
    epoch = issued // EPOCH_SECONDS
    result = {
        "type_ref": "synny://type/public-signature-bundle",
        "schema_version": 1,
        "identity": identity,
        "delegation": {
            "type_ref": "synny://type/hourly-key-delegation",
            "schema_version": 1,
            "profile_id": "potbot-identity-v1",
            "signing_profile_id": "potbot-hourly-signing-v1",
            "signer_hash": identity["signer_hash"],
            "signing_epoch": epoch,
            "valid_from": epoch * EPOCH_SECONDS,
            "valid_until": (epoch + 1) * EPOCH_SECONDS,
            "public_key_algorithm": "Ed25519",
            "public_key_encoding": "base64",
            "public_key": _b64(_public(_HOURLY)),
            "signature_algorithm": "Ed25519",
            "signature_encoding": "base64",
            "signature": "",
        },
        "envelope": {
            "type_ref": "synny://type/signature-envelope",
            "schema_version": 2,
            "profile_id": "potbot-identity-v1",
            "signer_hash": identity["signer_hash"],
            "domain": "potbot.artifact.v2",
            "workspace_id": _WORKSPACE,
            "payload_type_ref": "synny://type/document",
            "payload_sha256": hashlib.sha256(_PAYLOAD).hexdigest(),
            "audience": "test-verifier",
            "request_id": _REQUEST,
            "issued_at": _utc(issued),
            "expires_at": _utc(issued + validity),
            "signature_algorithm": "Ed25519",
            "signature_encoding": "base64",
            "signature": "",
            "signing_profile_id": "potbot-hourly-signing-v1",
            "signing_epoch": epoch,
        },
    }
    _resign(result)
    return result


def _policy(**changes: Any) -> VerificationPolicy:
    values = {
        "expected_signer_hash": hashlib.sha256(_public(_BASE)).hexdigest(),
        "audience": "test-verifier",
        "workspace_id": _WORKSPACE,
        "domain": "potbot.artifact.v2",
        "allowed_payload_type_refs": frozenset(("syn://type/document",)),
    }
    values.update(changes)
    return VerificationPolicy(**values)


def test_rfc8032_known_answer_is_verified_independently_of_fixture_signer() -> None:
    # RFC 8032 section 7.1, TEST 1: https://www.rfc-editor.org/rfc/rfc8032#section-7.1
    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    _verify_ed25519(public, signature, b"", "known answer")
    with pytest.raises(PublicVerificationError, match="signature"):
        _verify_ed25519(public, signature, b"changed", "known answer")


def test_complete_bundle_verifies_only_with_pinned_identity_and_scope() -> None:
    value = _bundle()
    result = verify_signature_bundle(
        canonical_json_bytes(value), _PAYLOAD, policy=_policy(), now=_NOW
    )
    assert result.signing_epoch == _NOW // 3600
    assert result.payload_type_ref == "synny://type/document"
    assert result.to_dict()["verified"] is True
    assert result.to_dict()["replay_protected"] is False
    assert verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW) == result
    assert verify_signature_bundle(
        json.dumps(value, indent=2), _PAYLOAD, policy=_policy(), now=_NOW
    )


def test_public_identity_fingerprint_uses_raw_base_key_bytes() -> None:
    raw = _public(_BASE)
    descriptor = public_identity_descriptor(raw)
    assert descriptor["signer_hash"] == hashlib.sha256(raw).hexdigest()
    identity = verify_public_identity(
        descriptor, expected_signer_hash=str(descriptor["signer_hash"])
    )
    assert identity.public_key == raw
    assert identity.to_dict()["public_key"] == _b64(raw)
    assert "signature" not in descriptor
    with pytest.raises(PublicVerificationError, match="pinned"):
        verify_public_identity(
            descriptor, expected_signer_hash=hashlib.sha256(_public(_HOURLY)).hexdigest()
        )


@pytest.mark.parametrize("layer", ["identity", "delegation", "envelope"])
def test_base_fingerprint_cannot_be_replaced_with_hourly_fingerprint(layer: str) -> None:
    value = _bundle()
    value[layer]["signer_hash"] = hashlib.sha256(_public(_HOURLY)).hexdigest()
    _resign(value)
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


def test_self_signed_other_identity_does_not_acquire_pinned_trust() -> None:
    value = _bundle()
    other = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    value["identity"] = public_identity_descriptor(_public(other))
    value["delegation"]["signer_hash"] = value["identity"]["signer_hash"]
    value["envelope"]["signer_hash"] = value["identity"]["signer_hash"]
    value["delegation"]["signature"] = _b64(
        other.sign(delegation_signing_bytes(value["delegation"]))
    )
    _resign(value, base=False)
    with pytest.raises(PublicVerificationError, match="pinned"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


def test_identity_and_hourly_key_substitution_are_rejected() -> None:
    value = _bundle()
    value["identity"]["public_key"] = _b64(_public(_HOURLY))
    with pytest.raises(PublicVerificationError, match="pinned"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    value = _bundle()
    value["delegation"]["public_key"] = _b64(_public(_BASE))
    with pytest.raises(PublicVerificationError, match="delegation signature"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    _resign(value)
    with pytest.raises(PublicVerificationError, match="message signature"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


def test_certificate_cannot_be_self_signed_by_its_hourly_key() -> None:
    value = _bundle()
    value["delegation"]["signature"] = _b64(
        _HOURLY.sign(delegation_signing_bytes(value["delegation"]))
    )
    with pytest.raises(PublicVerificationError, match="delegation signature"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize("part", ["delegation", "envelope"])
def test_signing_domains_cannot_be_crossed_or_omitted(part: str) -> None:
    value = _bundle()
    key = _BASE if part == "delegation" else _HOURLY
    unsigned = {k: v for k, v in value[part].items() if k != "signature"}
    incorrect = SIGNATURE_PREFIX if part == "delegation" else DELEGATION_PREFIX
    value[part]["signature"] = _b64(key.sign(incorrect + canonical_json_bytes(unsigned)))
    with pytest.raises(PublicVerificationError, match="signature"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    value[part]["signature"] = _b64(key.sign(canonical_json_bytes(unsigned)))
    with pytest.raises(PublicVerificationError, match="signature"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("audience", "other-verifier"),
        ("workspace_id", "a88d61a4-9849-48e3-802f-b4e97522e50d"),
        ("domain", "potbot.event.v2"),
        ("payload_type_ref", "synny://type/integer"),
    ],
)
def test_valid_signatures_cannot_escape_independent_scope(field: str, replacement: str) -> None:
    value = _bundle()
    value["envelope"][field] = replacement
    _resign(value)
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


def test_exact_payload_bytes_are_covered_including_binary_data() -> None:
    with pytest.raises(PublicVerificationError, match="payload bytes"):
        verify_signature_bundle(_bundle(), _PAYLOAD[:-1], policy=_policy(), now=_NOW)
    with pytest.raises(PublicVerificationError, match="original bytes"):
        verify_signature_bundle(_bundle(), "text", policy=_policy(), now=_NOW)  # type: ignore[arg-type]


def test_alias_resolution_never_rewrites_signed_fields() -> None:
    value = _bundle()
    value["envelope"]["payload_type_ref"] = "syn://type/document"
    with pytest.raises(PublicVerificationError, match="message signature"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    for part in ("identity", "delegation", "envelope"):
        value[part]["type_ref"] = value[part]["type_ref"].replace("synny://", "syn://")
    value["type_ref"] = value["type_ref"].replace("synny://", "syn://")
    _resign(value)
    result = verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    assert result.payload_type_ref == "syn://type/document"


def test_released_signature_remains_valid_across_hour_boundary() -> None:
    boundary = (_NOW // 3600 + 1) * 3600
    value = _bundle(issued=boundary - 1, validity=300)
    verified = verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=boundary + 1)
    assert verified.signing_epoch == (boundary - 1) // 3600
    assert verified.expires_at > boundary
    with pytest.raises(PublicVerificationError, match="expired"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=boundary + 329)


@pytest.mark.parametrize("delta", [-1, 1])
def test_epoch_is_not_selected_independently_of_issued_at(delta: int) -> None:
    value = _bundle()
    value["envelope"]["signing_epoch"] += delta
    value["delegation"]["signing_epoch"] += delta
    value["delegation"]["valid_from"] += delta * 3600
    value["delegation"]["valid_until"] += delta * 3600
    _resign(value)
    with pytest.raises(PublicVerificationError, match="issued_at"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize("field", ["valid_from", "valid_until"])
@pytest.mark.parametrize("delta", [-1, 1, 3600])
def test_certificate_cannot_expand_its_fixed_hour_interval(field: str, delta: int) -> None:
    value = _bundle()
    value["delegation"][field] += delta
    _resign(value)
    with pytest.raises(PublicVerificationError, match="exact POSIX hour"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize("validity", [-1, 0, 301, 3600])
def test_invalid_lifetimes_rejected_even_when_signed(validity: int) -> None:
    with pytest.raises(PublicVerificationError, match="validity"):
        verify_signature_bundle(_bundle(validity=validity), _PAYLOAD, policy=_policy(), now=_NOW)


def test_skew_is_bounded_and_expiry_is_exclusive() -> None:
    value = _bundle()
    assert verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW - 30)
    with pytest.raises(PublicVerificationError, match="future"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW - 31)
    assert verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW + 149)
    with pytest.raises(PublicVerificationError, match="expired"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW + 150)
    with pytest.raises(PublicVerificationError, match="expired"):
        verify_signature_bundle(
            value, _PAYLOAD, policy=_policy(clock_skew_seconds=0), now=_NOW + 120
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-02-30T12:00:00Z",
        "2026-10-21T12:00:60Z",
        "2026-10-21T24:00:00Z",
        "2026-10-21T12:00:00+00:00",
        "2026-10-21T12:00:00.000Z",
        "1969-12-31T23:59:59Z",
        "0000-01-01T00:00:00Z",
        "2026-1-21T12:00:00Z",
    ],
)
def test_noncanonical_or_impossible_times_rejected(timestamp: str) -> None:
    value = _bundle()
    value["envelope"]["issued_at"] = timestamp
    _resign(value)
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize("bad", [True, -1, MAX_SIGNING_EPOCH + 1, 1.0, "1", None])
def test_epoch_counter_cannot_coerce_wrap_or_overflow(bad: object) -> None:
    value = _bundle()
    value["delegation"]["signing_epoch"] = bad
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle(json.dumps(value), _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize("part", [None, "identity", "delegation", "envelope"])
def test_closed_shapes_reject_secret_fields_unknown_fields_and_missing_fields(
    part: str | None,
) -> None:
    for field in ("private_key", "chain_code", "mnemonic", "arbitrary_path"):
        value = _bundle()
        target = value if part is None else value[part]
        target[field] = "forbidden extra field"
        with pytest.raises(PublicVerificationError, match="exactly"):
            verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    value = _bundle()
    target = value if part is None else value[part]
    del target["type_ref"]
    with pytest.raises(PublicVerificationError, match="exactly"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize("part", ["identity", "delegation", "envelope"])
def test_versions_profiles_and_algorithms_are_closed(part: str) -> None:
    for field, bad in (
        ("schema_version", True),
        ("schema_version", 99),
        ("profile_id", "other-profile"),
    ):
        value = _bundle()
        value[part][field] = bad
        with pytest.raises(PublicVerificationError):
            verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    algorithm_field = "public_key_algorithm" if part != "envelope" else "signature_algorithm"
    value = _bundle()
    value[part][algorithm_field] = "Ed25519ph"
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


@pytest.mark.parametrize(
    "part,field,size",
    [
        ("identity", "public_key", 32),
        ("delegation", "public_key", 32),
        ("delegation", "signature", 64),
        ("envelope", "signature", 64),
    ],
)
def test_key_and_signature_encodings_are_exact(part: str, field: str, size: int) -> None:
    original = _bundle()[part][field]
    for bad in (original.rstrip("="), " " + original, _b64(b"x" * (size - 1)), "!" * len(original)):
        value = _bundle()
        value[part][field] = bad
        with pytest.raises(PublicVerificationError):
            verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)
    # Change unused base64 pad bits without changing decoded bytes.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    position = -2 if size == 32 else -3
    mutated = list(original)
    mutated[position] = alphabet[alphabet.index(mutated[position]) + 1]
    value = _bundle()
    value[part][field] = "".join(mutated)
    assert base64.b64decode(value[part][field]) == base64.b64decode(original)
    with pytest.raises(PublicVerificationError, match="noncanonical"):
        verify_signature_bundle(value, _PAYLOAD, policy=_policy(), now=_NOW)


def test_duplicate_json_keys_are_rejected_at_any_depth() -> None:
    raw = canonical_json_bytes(_bundle()).decode("ascii")
    for original, duplicate in (
        ('"schema_version":1', '"schema_version":1,"schema_version":1'),
        ('"audience":"test-verifier"', '"audience":"test-verifier","audience":"test-verifier"'),
        (
            '"public_key_algorithm":"Ed25519"',
            '"public_key_algorithm":"Ed25519","public_key_algorithm":"Ed25519"',
        ),
    ):
        with pytest.raises(PublicVerificationError, match="duplicate"):
            verify_signature_bundle(
                raw.replace(original, duplicate, 1), _PAYLOAD, policy=_policy(), now=_NOW
            )


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"\xef\xbb\xbf{}",
        "{} {}",
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1.0}',
        '{"a":1e0}',
        "[]",
        "null",
        '{"a":"\\ud800"}',
        '{"a":true}',
        '{"a":null}',
        '{"a":[]}',
        '{"a":9007199254740992}',
    ],
)
def test_strict_input_rejects_invalid_json_or_unsupported_canonical_values(
    raw: str | bytes,
) -> None:
    with pytest.raises(PublicVerificationError):
        load_public_document(raw)


def test_size_and_nesting_limits_are_enforced_before_crypto() -> None:
    with pytest.raises(PublicVerificationError, match="size"):
        load_public_document(b" " * (MAX_PUBLIC_DOCUMENT_BYTES + 1))
    with pytest.raises(PublicVerificationError, match="nesting"):
        load_public_document('{"x":' * 20 + "0" + "}" * 20)


@pytest.mark.parametrize("bad", [True, -1, 1.5, "1792584100"])
def test_verification_clock_requires_nonnegative_integer_seconds(bad: object) -> None:
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle(_bundle(), _PAYLOAD, policy=_policy(), now=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_signer_hash": "0" * 63},
        {"expected_signer_hash": "F" * 64},
        {"audience": "*"},
        {"audience": "verifier\n"},
        {"workspace_id": "unknown"},
        {"domain": "potbot.event.v1"},
        {"domain": "*"},
        {"clock_skew_seconds": 31},
        {"clock_skew_seconds": True},
        {"allowed_payload_type_refs": frozenset()},
        {"allowed_payload_type_refs": {"syn://type/document"}},
        {"allowed_payload_type_refs": frozenset(("syn://type/nonexistent-security-payload",))},
    ],
)
def test_scope_policy_must_be_explicit_bounded_and_registered(changes: dict[str, Any]) -> None:
    with pytest.raises(PublicVerificationError):
        _policy(**changes)


def test_custom_effective_catalog_is_explicit_and_alias_aware() -> None:
    policy = _policy(
        allowed_payload_type_refs=frozenset(("synny://type/custom-payload",)),
        known_type_refs=frozenset(("syn://type/custom-payload",)),
    )
    value = _bundle()
    value["envelope"]["payload_type_ref"] = "synny://type/custom-payload"
    _resign(value)
    assert verify_signature_bundle(value, _PAYLOAD, policy=policy, now=_NOW)
    with pytest.raises(PublicVerificationError, match="absent"):
        replace(policy, known_type_refs=frozenset(("syn://type/document",)))


def test_delegation_api_rejects_forged_verified_identity_container() -> None:
    identity = VerifiedIdentity("0" * 64, _public(_BASE))
    with pytest.raises(PublicVerificationError, match="checked base"):
        verify_hourly_delegation(_bundle()["delegation"], identity=identity)


def test_replay_acceptance_is_durable_and_verification_remains_repeatable(tmp_path: Path) -> None:
    value = _bundle()
    store = SQLiteReplayStore(tmp_path / "replay.sqlite")
    accepted = accept_signature_bundle(
        value, _PAYLOAD, policy=_policy(), replay_store=store, now=_NOW
    )
    assert accepted.replay_protected
    reopened = SQLiteReplayStore(store.path)
    with pytest.raises(ReplayDetectedError):
        accept_signature_bundle(
            value, _PAYLOAD, policy=_policy(), replay_store=reopened, now=_NOW + 1
        )
    assert not verify_signature_bundle(
        value, _PAYLOAD, policy=_policy(), now=_NOW + 1
    ).replay_protected


def test_replay_claim_is_atomic_under_concurrent_acceptance(tmp_path: Path) -> None:
    store = SQLiteReplayStore(tmp_path / "concurrent.sqlite")
    value = _bundle()

    def accept(_: int) -> str:
        try:
            accept_signature_bundle(value, _PAYLOAD, policy=_policy(), replay_store=store, now=_NOW)
            return "accepted"
        except ReplayDetectedError:
            return "replayed"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(accept, range(8)))
    assert outcomes.count("accepted") == 1
    assert outcomes.count("replayed") == 7


def test_invalid_signature_cannot_poison_a_replay_reservation(tmp_path: Path) -> None:
    store = SQLiteReplayStore(tmp_path / "replay.sqlite")
    value = _bundle()
    invalid = copy.deepcopy(value)
    invalid["envelope"]["signature"] = _b64(bytes(64))
    with pytest.raises(PublicVerificationError, match="message signature"):
        accept_signature_bundle(invalid, _PAYLOAD, policy=_policy(), replay_store=store, now=_NOW)
    assert accept_signature_bundle(value, _PAYLOAD, policy=_policy(), replay_store=store, now=_NOW)


def test_replay_store_failure_is_closed_and_memory_store_is_disallowed(tmp_path: Path) -> None:
    with pytest.raises(PublicVerificationError, match="persistent"):
        SQLiteReplayStore(Path(":memory:"))
    unavailable = SQLiteReplayStore(tmp_path / "absent" / "replay.sqlite")
    with pytest.raises(PublicVerificationError, match="unavailable"):
        accept_signature_bundle(
            _bundle(), _PAYLOAD, policy=_policy(), replay_store=unavailable, now=_NOW
        )


def test_schemas_accept_the_complete_wire_and_close_each_object() -> None:
    bundle = _bundle()
    selected = {
        "public-identity.schema.json": bundle["identity"],
        "hourly-key-delegation.schema.json": bundle["delegation"],
        "hourly-signature-envelope.schema.json": bundle["envelope"],
        "public-signature-bundle.schema.json": bundle,
    }
    for filename, value in selected.items():
        schema = json.loads((_VOCAB / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(value)
        with_extra = {**value, "private_key": "not allowed"}
        assert list(validator.iter_errors(with_extra))
        bad = {**value, "schema_version": True}
        assert list(validator.iter_errors(bad))


def test_canonical_signing_contract_has_no_private_material_or_ambiguous_json() -> None:
    value = {"z": 1, "a": "ascii/string"}
    assert canonical_json_bytes(value) == b'{"a":"ascii/string","z":1}'
    assert envelope_signing_bytes(
        {**value, "signature": "ignored"}
    ) == SIGNATURE_PREFIX + canonical_json_bytes(value)
    assert delegation_signing_bytes(value) == DELEGATION_PREFIX + canonical_json_bytes(value)
    source = Path(__file__).resolve().parents[2] / "src" / "potbot" / "security" / "public.py"
    text = source.read_text(encoding="utf-8")
    assert "Ed25519PrivateKey" not in text
    assert ".sign(" not in text


def test_native_rust_bundle_verifies_with_an_independent_fixed_fingerprint() -> None:
    # Produced by the pinned Rust Ed25519-BIP32 implementation. No private
    # material is stored here and the trust pin is not read from the bundle.
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/security/native-public-signature-bundle.json"
    )
    policy = VerificationPolicy(
        expected_signer_hash="28996c77aba50985b85b6c81f0e60d5b405b81a45efdafb3fda5d9f8581720e8",
        audience="test",
        workspace_id="00000000-0000-4000-8000-000000000001",
        domain="potbot.event.v2",
        allowed_payload_type_refs=frozenset(("syn://type/ember",)),
    )
    result = verify_signature_bundle(
        fixture.read_bytes(), b"fixture", policy=policy, now=1792584000
    )
    assert result.signing_epoch == 497940
    assert result.expires_at == 1792584300
    assert result.request_id == "00000000-0000-4000-8000-000000000002"
    assert result.payload_type_ref == "synny://type/ember"
    assert result == verify_signature_bundle_digest(
        fixture.read_bytes(), hashlib.sha256(b"fixture").hexdigest(), policy=policy, now=1792584000
    )


@pytest.mark.parametrize("digest", ["0" * 64, "A" * 64, "00", "é" * 64, True, None])
def test_streaming_digest_api_rejects_wrong_or_noncanonical_digest(digest: Any) -> None:
    with pytest.raises(PublicVerificationError):
        verify_signature_bundle_digest(_bundle(), digest, policy=_policy(), now=_NOW)
