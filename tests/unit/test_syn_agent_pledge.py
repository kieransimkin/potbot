"""Public SYN agent inheritance, offer signatures and candidate-choice contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.syn_agent_pledge import (
    BASE_ADDRESS_PATH,
    CANDIDATE_TYPE,
    INHERITANCE_MAX,
    MAX_HARDWARE_ID_BYTES,
    ROOT_ADDRESS_PATH,
    IdentityCandidates,
    PledgeOffer,
    SignedPledgeOffer,
    agent_key_path,
    agent_key_paths,
    calculate_inheritance,
    inheritance_hex,
    public_key_hash,
    shorten_public_key_hash,
)


def _hardware_id() -> bytes:
    # A deterministic test-only 6-byte device identifier (e.g. a MAC address).
    return bytes.fromhex("021122334455")


def _offer() -> PledgeOffer:
    return PledgeOffer(_temporary_hash(), _master_hash(), 1792920000, b"\x55" * 32, _hardware_id())


def _temporary_hash() -> bytes:
    return hashlib.sha256(b"fixed test-only SYN agent ephemeral key fingerprint").digest()


def _master_hash() -> bytes:
    return hashlib.sha256(b"fixed test-only target SYN operator base public key").digest()


def _candidates(operator_byte: int = 0xA5) -> IdentityCandidates:
    return IdentityCandidates.from_public_hashes(
        offer=_offer(),
        operator_byte=operator_byte,
        full_candidate_hashes=tuple(hashlib.sha256(f"fixture-{i}".encode()).digest() for i in range(1, 9)),
    )


def test_root_and_base_paths_are_distinct_and_hardened_as_specified() -> None:
    assert ROOT_ADDRESS_PATH == "m/6969'/420'"
    assert BASE_ADDRESS_PATH == "m/6969'/420'/0'/0/0"
    assert agent_key_paths(0) == tuple(
        f"m/6969'/420'/0'/{number}/0" for number in range(1, 9)
    )
    assert agent_key_paths(INHERITANCE_MAX)[-1] == "m/6969'/420'/16777215'/8/0"
    assert "m/6969'/420'/0'/0/0/" not in agent_key_path(7, 1)


def test_inheritance_is_24_bit_big_endian_sha256_truncation() -> None:
    identifier = _hardware_id()
    salt = 0xA5
    expected = hashlib.sha256(identifier + b"\xa5").digest()[:3]
    value = calculate_inheritance(identifier, salt)
    # The temporary public-key hash MUST NOT contribute to inheritance.
    assert value != int.from_bytes(hashlib.sha256(_temporary_hash() + b"\xa5").digest()[:3], "big")
    assert value == int.from_bytes(expected, "big")
    assert inheritance_hex(value) == expected.hex()
    assert 0 <= value <= 16_777_215
    assert len({calculate_inheritance(identifier, choice) for choice in range(256)}) >= 250
    # Protocol vectors for synthetic raw hardware identifiers (NOT public keys).
    assert calculate_inheritance(bytes(range(32)), 0) == 1_775_373
    assert calculate_inheritance(bytes(range(32)), 0xA5) == 5_647_788
    assert calculate_inheritance(bytes(range(32)), 255) == 12_433_217


@pytest.mark.parametrize("bad", [-1, 256, True, 0.1, "12", b"\xff", None])
def test_invalid_operator_octets_are_rejected(bad: object) -> None:
    with pytest.raises(InvalidInputError):
        calculate_inheritance(_hardware_id(), bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, 16_777_216, True, 0.5, "2"])
def test_invalid_inheritance_values_are_rejected(bad: object) -> None:
    with pytest.raises(InvalidInputError):
        inheritance_hex(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, 9, -1, True, False, 1.0, "3"])
def test_invalid_independence_values_are_rejected(bad: object) -> None:
    with pytest.raises(InvalidInputError):
        agent_key_path(7, bad)  # type: ignore[arg-type]


def test_short_public_hash_is_only_first_three_digest_bytes() -> None:
    key = bytes(range(32))
    full_hash = hashlib.sha256(key).digest()
    assert public_key_hash(key) == full_hash
    assert shorten_public_key_hash(full_hash) == full_hash[:3]
    assert int.from_bytes(shorten_public_key_hash(full_hash), "big") <= INHERITANCE_MAX
    for invalid in (b"", b"short", b"x" * 33):
        with pytest.raises(InvalidInputError):
            public_key_hash(invalid)
        with pytest.raises(InvalidInputError):
            shorten_public_key_hash(invalid)


def test_eight_offered_public_hashes_bind_to_exact_paths_and_choice() -> None:
    candidates = _candidates()
    report = candidates.to_dict()
    assert report["type_ref"] == CANDIDATE_TYPE
    assert report["operator_byte"] == 0xA5
    assert report["inheritance_hex"] == inheritance_hex(candidates.inheritance)
    assert len(report["short_public_key_hashes"]) == 8
    assert report["schema_version"] == 2
    assert report["pledge_offer_sha256"] == hashlib.sha256(_offer().signed_bytes()).hexdigest()
    assert "temporary_public_key_hash" not in report
    assert "hardware_unique_id" not in report  # Not rebroadcast with the candidate announcement.
    assert len(set(report["short_public_key_hashes"])) == 8
    for independence in range(1, 9):
        chosen_hash, chosen_path = candidates.choose(independence)
        assert chosen_hash == candidates.short_public_key_hashes[independence - 1]
        assert chosen_path == agent_key_path(candidates.inheritance, independence)


def test_duplicate_short_candidates_and_unproven_inheritance_fail() -> None:
    candidates = _candidates()
    with pytest.raises(StateMismatchError, match="ambiguous"):
        replace(candidates, short_public_key_hashes=(b"\x00" * 3,) * 8)
    with pytest.raises(StateMismatchError, match="inheritance"):
        replace(candidates, inheritance=candidates.inheritance ^ 1)
    with pytest.raises(InvalidInputError, match="eight"):
        replace(candidates, short_public_key_hashes=(b"\x00" * 3,) * 7)
    with pytest.raises(InvalidInputError, match="eight"):
        IdentityCandidates.from_public_hashes(
            offer=_offer(), operator_byte=1, full_candidate_hashes=(),
        )


def test_offer_roundtrips_canonical_signed_fields_and_rejects_unknown_fields() -> None:
    offer = _offer()
    raw = offer.to_dict()
    assert PledgeOffer.from_dict(raw) == offer
    assert offer.signed_bytes() == PledgeOffer.from_dict(raw).signed_bytes()
    assert b"target_master_public_key_hash" in offer.signed_bytes()
    assert b"pulse_challenge" in offer.signed_bytes()
    assert b"hardware_unique_id" in offer.signed_bytes()
    assert raw["hardware_unique_id"] == "021122334455"
    assert raw["schema_version"] == 2
    assert b"syn.agent.pledge.offer.v2\x00" in offer.signed_bytes()
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "extra": "not signed"})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "timestamp_unix_seconds": True})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "schema_version": True})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "schema_version": 1})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "hardware_unique_id": "not a hex device ID"})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "hardware_unique_id": "001122"})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "temporary_public_key_hash": "A" * 64})


def test_signature_verification_binds_ephemeral_key_master_timestamp_and_pulse() -> None:
    cryptography = pytest.importorskip("cryptography")
    del cryptography
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # A test-only signing key is generated inside the test, never by Potbot runtime.
    key = Ed25519PrivateKey.generate()
    raw_public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    offer = PledgeOffer(public_key_hash(raw_public), _master_hash(), 1792920000, b"\x55" * 32, _hardware_id())
    signed = SignedPledgeOffer(offer, raw_public, key.sign(offer.signed_bytes()))
    envelope = signed.to_dict()
    assert envelope["offer"]["temporary_public_key_hash"] == hashlib.sha256(raw_public).hexdigest()
    assert envelope["temporary_public_key"] == raw_public.hex()
    signed.verify(expected_master_hash=_master_hash(), expected_pulse_challenge=b"\x55" * 32,
                  now_unix_seconds=1792920005)
    with pytest.raises(ScopeDeniedError, match="master"):
        signed.verify(expected_master_hash=b"\x33" * 32, expected_pulse_challenge=b"\x55" * 32,
                      now_unix_seconds=1792920005)
    with pytest.raises(ScopeDeniedError, match="pulse"):
        signed.verify(expected_master_hash=_master_hash(), expected_pulse_challenge=b"\x33" * 32,
                      now_unix_seconds=1792920005)
    with pytest.raises(StateMismatchError, match="timestamp"):
        signed.verify(expected_master_hash=_master_hash(), expected_pulse_challenge=b"\x55" * 32,
                      now_unix_seconds=1792920200)
    with pytest.raises(ScopeDeniedError, match="signature"):
        replace(signed, signature=b"\x00" * 64).verify(
            expected_master_hash=_master_hash(), expected_pulse_challenge=b"\x55" * 32,
            now_unix_seconds=1792920005,
        )
    with pytest.raises(StateMismatchError, match="temporary"):
        replace(signed, temporary_public_key=b"\x01" * 32)
    # Hardware metadata is part of the Ed25519-authenticated transcript.
    tampered = replace(signed, offer=replace(offer, hardware_unique_id=b"\xaa" * 6))
    with pytest.raises(ScopeDeniedError, match="signature"):
        tampered.verify(expected_master_hash=_master_hash(), expected_pulse_challenge=b"\x55" * 32,
                        now_unix_seconds=1792920005)


def test_public_verification_never_grants_pledge_acceptance_or_key_custody() -> None:
    from potbot.domain.syn_agent_pledge import SignedPledgeOffer as Signed
    assert not hasattr(Signed, "accept")
    assert not hasattr(Signed, "derive_private_key")
    assert not hasattr(IdentityCandidates, "reserve")
    assert not hasattr(IdentityCandidates, "export_private_key")


def test_hardware_id_is_source_of_inheritance_independent_of_ephemeral_key() -> None:
    first, second = _hardware_id(), b"\x02\x11\x22\x33\x44\x56"
    byte = 0xA5
    assert calculate_inheritance(first, byte) != calculate_inheritance(second, byte)
    one = _offer()
    other = replace(one, temporary_public_key_hash=hashlib.sha256(b"new CSPRNG key").digest())
    assert calculate_inheritance(one.hardware_unique_id, byte) == calculate_inheritance(
        other.hardware_unique_id, byte
    )
    assert IdentityCandidates.from_public_hashes(
        offer=one, operator_byte=byte,
        full_candidate_hashes=tuple(hashlib.sha256(f"fixture-{i}".encode()).digest() for i in range(1, 9)),
    ).inheritance == IdentityCandidates.from_public_hashes(
        offer=other, operator_byte=byte,
        full_candidate_hashes=tuple(hashlib.sha256(f"fixture-{i}".encode()).digest() for i in range(1, 9)),
    ).inheritance
    for bad in (b"", b"\x01" * 5, b"\x02" * (MAX_HARDWARE_ID_BYTES + 1), "021122334455"):
        with pytest.raises(InvalidInputError):
            calculate_inheritance(bad, byte)  # type: ignore[arg-type]


def test_exact_device_identifier_is_signed_but_not_echoed_in_candidate_message() -> None:
    offer = _offer()
    published = _candidates().to_dict()
    assert offer.hardware_unique_id.hex() in str(offer.to_dict())
    assert offer.hardware_unique_id.hex() not in str(published)
    assert offer.temporary_public_key_hash.hex() not in str(published)
    assert _candidates().inheritance == calculate_inheritance(offer.hardware_unique_id, 0xA5)


def test_versioned_public_wire_schema_and_historical_v1_rejection() -> None:
    validator = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "vocab/syn-agent-pledge.schema.json").read_text())
    old_schema = json.loads((root / "vocab/syn-agent-pledge-v1.schema.json").read_text())
    validator.Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith("/v2")
    assert old_schema["$id"].endswith("/v1")
    offer = _offer()
    packet = {"offer": offer.to_dict(), "temporary_public_key": "a" * 64,
              "signature": "b" * 128}
    validator.validate(packet, schema)
    packet["offer"]["hardware_unique_id"] = "01"
    assert not validator.Draft202012Validator(schema).is_valid(packet)
    packet["offer"] = offer.to_dict()
    packet["offer"]["schema_version"] = 1
    assert not validator.Draft202012Validator(schema).is_valid(packet)
    announcement = {"candidates": _candidates().to_dict(), "operator_public_key": "a" * 64,
                    "signature": "b" * 128}
    validator.validate(announcement, schema)
    assert "temporary_public_key_hash" not in announcement["candidates"]
    selection = {
        "selection": {
            "schema_version": 2, "type_ref": "synny://type/syn-agent-independence-selection-v2",
            "target_master_public_key_hash": _master_hash().hex(), "inheritance": _candidates().inheritance,
            "independence": 1, "chosen_short_public_key_hash": _candidates().short_public_key_hashes[0].hex(),
            "candidate_offer_digest": hashlib.sha256(b"signed operator candidates").hexdigest(),
        },
        "temporary_public_key": "a" * 64, "signature": "b" * 128,
    }
    validator.validate(selection, schema)


def test_fresh_random_temporary_keys_do_not_change_hardware_inheritance() -> None:
    cryptography = pytest.importorskip("cryptography")
    del cryptography
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # SYN is responsible for production CSPRNG generation. This checks only
    # that a pair of OS-random test keys does not affect the inheritance math.
    hashes = []
    for _ in range(2):
        key = Ed25519PrivateKey.generate()
        raw = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        hashes.append(public_key_hash(raw))
    assert hashes[0] != hashes[1]
    pledge_a = replace(_offer(), temporary_public_key_hash=hashes[0])
    pledge_b = replace(_offer(), temporary_public_key_hash=hashes[1])
    assert calculate_inheritance(pledge_a.hardware_unique_id, 17) == calculate_inheritance(
        pledge_b.hardware_unique_id, 17
    )
    assert pledge_a.signed_bytes() != pledge_b.signed_bytes()
