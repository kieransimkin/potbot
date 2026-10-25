"""Public SYN agent inheritance, offer signatures and candidate-choice contracts."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.syn_agent_pledge import (
    BASE_ADDRESS_PATH,
    CANDIDATE_TYPE,
    INHERITANCE_MAX,
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


def _temporary_hash() -> bytes:
    return hashlib.sha256(b"fixed test-only SYN agent ephemeral key fingerprint").digest()


def _master_hash() -> bytes:
    return hashlib.sha256(b"fixed test-only target SYN operator base public key").digest()


def _candidates(operator_byte: int = 0xA5) -> IdentityCandidates:
    return IdentityCandidates.from_public_hashes(
        temporary_public_key_hash=_temporary_hash(),
        master_public_key_hash=_master_hash(),
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
    fingerprint = _temporary_hash()
    salt = 0xA5
    expected = hashlib.sha256(fingerprint + b"\xa5").digest()[:3]
    value = calculate_inheritance(fingerprint, salt)
    assert value == int.from_bytes(expected, "big")
    assert inheritance_hex(value) == expected.hex()
    assert 0 <= value <= 16_777_215
    assert len({calculate_inheritance(fingerprint, choice) for choice in range(256)}) >= 250
    # Protocol vectors used independently by native SYN implementations.
    assert calculate_inheritance(bytes(range(32)), 0) == 1_775_373
    assert calculate_inheritance(bytes(range(32)), 0xA5) == 5_647_788
    assert calculate_inheritance(bytes(range(32)), 255) == 12_433_217


@pytest.mark.parametrize("bad", [-1, 256, True, 0.1, "12", b"\xff", None])
def test_invalid_operator_octets_are_rejected(bad: object) -> None:
    with pytest.raises(InvalidInputError):
        calculate_inheritance(_temporary_hash(), bad)  # type: ignore[arg-type]


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
            temporary_public_key_hash=_temporary_hash(), master_public_key_hash=_master_hash(),
            operator_byte=1, full_candidate_hashes=(),
        )


def test_offer_roundtrips_canonical_signed_fields_and_rejects_unknown_fields() -> None:
    offer = PledgeOffer(_temporary_hash(), _master_hash(), 1792920000, b"\x55" * 32)
    raw = offer.to_dict()
    assert PledgeOffer.from_dict(raw) == offer
    assert offer.signed_bytes() == PledgeOffer.from_dict(raw).signed_bytes()
    assert b"target_master_public_key_hash" in offer.signed_bytes()
    assert b"pulse_challenge" in offer.signed_bytes()
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "extra": "not signed"})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "timestamp_unix_seconds": True})
    with pytest.raises(InvalidInputError):
        PledgeOffer.from_dict({**raw, "schema_version": True})
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
    offer = PledgeOffer(public_key_hash(raw_public), _master_hash(), 1792920000, b"\x55" * 32)
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


def test_public_verification_never_grants_pledge_acceptance_or_key_custody() -> None:
    from potbot.domain.syn_agent_pledge import SignedPledgeOffer as Signed
    assert not hasattr(Signed, "accept")
    assert not hasattr(Signed, "derive_private_key")
    assert not hasattr(IdentityCandidates, "reserve")
    assert not hasattr(IdentityCandidates, "export_private_key")
