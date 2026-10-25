"""SYN v2 public handshake and operator-private, replay-fenced ledger acceptance.

Tests use disposable SYN signing keys and a MOCK backend. They do not attest
hardware, exercise a P2P transport, or qualify OS-keystore private-key custody.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.syn_agent_pledge import PledgeOffer, SignedPledgeOffer, public_key_hash
from potbot.domain.syn_enrollment_protocol import (
    CANDIDATE_DOMAIN, PULSE_DOMAIN, SELECTION_DOMAIN,
    IndependenceSelection, OperatorPulse, SignedIdentityCandidates,
    SignedIndependenceSelection, SignedOperatorPulse, signed_pledge_from_dict,
)
from potbot.tools.syn_enrollment_ledger import SynEnrollmentLedger

CLOCK = 1_792_920_000
NETWORK = "syn-local-qualification"


def raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


class SyntheticSynAuthority:
    """NOT a secure runtime. No private keys leave this explicit test fake."""

    def __init__(self) -> None:
        self._operator = Ed25519PrivateKey.generate()
        self._blinding_secret = os.urandom(32)
        self.approved = True
        self.hardware_ok = True
        self.break_signature = False
        self.collide_short = False
        self.child_requests: list[int] = []
        self.device_handles_broken = False
        self._handles = 0

    def operator_public_key(self) -> bytes:
        return raw_public(self._operator)

    def attest_and_blind_device(self, hardware_id: bytes) -> bytes:
        if not self.hardware_ok:
            raise ScopeDeniedError("SYN device attestation unavailable")
        if self.device_handles_broken:
            self._handles += 1
            return hashlib.sha256(hardware_id + self._handles.to_bytes(4, "big")).digest()
        return hmac.digest(self._blinding_secret, hardware_id, "sha256")

    def authorize_offer(self, offer_sha256: bytes) -> bool:
        assert len(offer_sha256) == 32
        return self.approved

    def derive_candidate_public_hashes(self, inheritance: int) -> tuple[bytes, ...]:
        self.child_requests.append(inheritance)
        if self.collide_short:
            return (b"\x00" * 32,) * 8
        # Public hash vectors only. No private derivation in this fake.
        return tuple(hashlib.sha256(f"test:{inheritance}:{i}".encode()).digest()
                     for i in range(1, 9))

    def sign_candidates(self, signing_preimage: bytes) -> bytes:
        assert signing_preimage.startswith(CANDIDATE_DOMAIN)
        return (bytes(64) if self.break_signature
                else self._operator.sign(signing_preimage))

    def sign_pulse(self, pulse: OperatorPulse) -> SignedOperatorPulse:
        return SignedOperatorPulse(
            pulse, self.operator_public_key(), self._operator.sign(pulse.signed_bytes())
        )


def fixture(tmp_path: Path, *, hardware: bytes = b"\x02\x11\x22\x33\x44\x55",
            challenge: bytes = b"P" * 32, authority: SyntheticSynAuthority | None = None):
    authority = authority or SyntheticSynAuthority()
    temp = Ed25519PrivateKey.generate()
    pulse = OperatorPulse(
        NETWORK, public_key_hash(authority.operator_public_key()), challenge, CLOCK - 5, CLOCK + 50
    )
    offer = PledgeOffer(
        public_key_hash(raw_public(temp)), pulse.master_public_key_hash,
        CLOCK, pulse.challenge, hardware,
    )
    signed = SignedPledgeOffer(offer, raw_public(temp), temp.sign(offer.signed_bytes()))
    root = tmp_path / "operator"
    root.mkdir(mode=0o700, exist_ok=True)
    store = SynEnrollmentLedger(
        root / "pledges.sqlite3", master_hash=pulse.master_public_key_hash,
        network_id=NETWORK, authority=authority,
        sandbox_home=tmp_path / "sandbox-home",
    )
    return authority, temp, authority.sign_pulse(pulse), signed, store


def choose(temp: Ed25519PrivateKey, candidates: SignedIdentityCandidates,
           independence: int = 4) -> SignedIndependenceSelection:
    selection = IndependenceSelection(
        candidates.candidates.offer.target_master_public_key_hash,
        candidates.candidates.inheritance,
        independence,
        candidates.candidates.short_public_key_hashes[independence - 1],
        candidates.digest,
    )
    return SignedIndependenceSelection(
        selection, raw_public(temp), temp.sign(selection.signed_bytes())
    )


def test_end_to_end_public_pledge_and_durable_restart(tmp_path: Path) -> None:
    authority, agent, pulse, offer, store = fixture(tmp_path)
    admission = store.begin(offer, pulse, now=CLOCK)
    assert admission.state == "pending"
    assert store.inspect(admission.offer_sha256) == admission
    candidates = store.offer_candidates(offer, operator_byte=0xA5, now=CLOCK + 1)
    assert candidates.candidates.inheritance == authority.child_requests[0]
    assert len(candidates.candidates.short_public_key_hashes) == 8
    assert len(candidates.signature) == 64
    assert candidates.digest == hashlib.sha256(candidates.signed_bytes()).digest()
    result = store.select(offer, candidates, choose(agent, candidates), now=CLOCK + 2)
    assert result.state == "selected"  # NOT 'adopted' or 'active'
    assert result.independence == 4
    assert len(result.selected_public_key_hash or "") == 64
    assert store.inspect(admission.offer_sha256) == result
    restarted = SynEnrollmentLedger(
        tmp_path / "operator" / "pledges.sqlite3",
        master_hash=offer.offer.target_master_public_key_hash,
        network_id=NETWORK, authority=authority,
    )
    assert restarted.inspect(admission.offer_sha256) == result
    with pytest.raises(StateMismatchError, match="replayed"):
        restarted.select(offer, candidates, choose(agent, candidates), now=CLOCK + 3)
    with pytest.raises(ScopeDeniedError, match="replay"):
        restarted.begin(offer, pulse, now=CLOCK + 3)


def test_device_hardware_never_persisted_or_rebroadcast(tmp_path: Path) -> None:
    authority, agent, pulse, offer, store = fixture(tmp_path)
    store.begin(offer, pulse, now=CLOCK)
    packet = store.offer_candidates(offer, operator_byte=2, now=CLOCK)
    assert offer.offer.hardware_unique_id.hex() not in json.dumps(packet.to_dict())
    data = (tmp_path / "operator" / "pledges.sqlite3").read_bytes()
    assert offer.offer.hardware_unique_id not in data
    assert offer.offer.hardware_unique_id.hex().encode() not in data
    assert offer.offer.temporary_public_key_hash.hex().encode() not in data
    assert "hardware_unique_id" not in json.dumps(asdict(store.inspect(store._digest(offer))))
    del agent, authority


def test_signed_pulse_is_scoped_to_network_master_and_freshness(tmp_path: Path) -> None:
    auth, _, pulse, offer, store = fixture(tmp_path)
    with pytest.raises(ScopeDeniedError, match="network"):
        pulse.verify(master_hash=offer.offer.target_master_public_key_hash,
                     now=CLOCK, network_id="other-network")
    with pytest.raises(ScopeDeniedError, match="network"):
        pulse.verify(master_hash=b"z" * 32, now=CLOCK, network_id=NETWORK)
    with pytest.raises(StateMismatchError, match="expired"):
        store.begin(offer, pulse, now=CLOCK + 51)
    with pytest.raises(ScopeDeniedError, match="signature"):
        replace(pulse, signature=bytes(64)).verify(
            master_hash=offer.offer.target_master_public_key_hash, now=CLOCK, network_id=NETWORK
        )
    assert pulse.to_dict()["pulse"]["challenge"] == offer.offer.pulse_challenge.hex()
    del auth


def test_broadcast_pulse_can_admit_multiple_distinct_devices(tmp_path: Path) -> None:
    authority, _, pulse, first, store = fixture(tmp_path)
    first_record = store.begin(first, pulse, now=CLOCK)
    _, _, reused, second, _ = fixture(
        tmp_path, hardware=b"\x03\x11\x22\x33\x44\x66", authority=authority,
    )
    assert pulse.pulse.challenge == reused.pulse.challenge
    second_record = store.begin(second, pulse, now=CLOCK)
    assert first_record.offer_sha256 != second_record.offer_sha256
    assert store.inspect(first_record.offer_sha256).state == "pending"
    assert store.inspect(second_record.offer_sha256).state == "pending"
    # A signed broadcast pulse is NOT single-use: the entire signed offer is.
    with pytest.raises(ScopeDeniedError, match="replay"):
        store.begin(first, pulse, now=CLOCK)


def test_one_device_cannot_pledge_with_rotated_temporary_key(tmp_path: Path) -> None:
    auth, _, pulse, first, store = fixture(tmp_path)
    store.begin(first, pulse, now=CLOCK)
    _, _, other_pulse, again, _ = fixture(
        tmp_path, authority=auth, challenge=b"Q" * 32,
    )
    with pytest.raises(ScopeDeniedError, match="duplicate device"):
        store.begin(again, other_pulse, now=CLOCK)


def test_hardware_attestation_rejection_does_not_reserve_state(tmp_path: Path) -> None:
    auth, _, pulse, offer, store = fixture(tmp_path)
    auth.hardware_ok = False
    with pytest.raises(ScopeDeniedError, match="attestation"):
        store.begin(offer, pulse, now=CLOCK)
    with pytest.raises(ScopeDeniedError, match="not enrolled"):
        store.inspect(store._digest(offer))


def test_operator_approval_is_external_syn_control(tmp_path: Path) -> None:
    auth, _, pulse, offer, store = fixture(tmp_path)
    pending = store.begin(offer, pulse, now=CLOCK)
    auth.approved = False
    with pytest.raises(ScopeDeniedError, match="authorized"):
        store.offer_candidates(offer, operator_byte=0, now=CLOCK)
    assert store.inspect(pending.offer_sha256).state == "pending"
    auth.approved = True
    store.offer_candidates(offer, operator_byte=0, now=CLOCK)
    assert store.inspect(pending.offer_sha256).state == "offered"


def test_operator_signature_failure_leaves_pending(tmp_path: Path) -> None:
    auth, _, pulse, offer, store = fixture(tmp_path)
    pending = store.begin(offer, pulse, now=CLOCK)
    auth.break_signature = True
    with pytest.raises(ScopeDeniedError, match="signature"):
        store.offer_candidates(offer, operator_byte=10, now=CLOCK)
    assert store.inspect(pending.offer_sha256).state == "pending"


def test_collision_of_short_public_candidate_hashes_rejected(tmp_path: Path) -> None:
    auth, _, pulse, offer, store = fixture(tmp_path)
    pending = store.begin(offer, pulse, now=CLOCK)
    auth.collide_short = True
    with pytest.raises(StateMismatchError, match="ambiguous"):
        store.offer_candidates(offer, operator_byte=11, now=CLOCK)
    assert store.inspect(pending.offer_sha256).state == "pending"


def test_24_bit_inheritance_collision_is_reserved(tmp_path: Path) -> None:
    auth, _, pulse, first, store = fixture(tmp_path)
    store.begin(first, pulse, now=CLOCK)
    store.offer_candidates(first, operator_byte=11, now=CLOCK)
    # Synthetic deliberately broken attestation allows another device handle
    # for identical raw hardware. The 24-bit reservation still catches it.
    auth.device_handles_broken = True
    _, _, another_pulse, second, _ = fixture(
        tmp_path, authority=auth, challenge=b"Q" * 32,
    )
    store.begin(second, another_pulse, now=CLOCK)
    with pytest.raises(ScopeDeniedError, match="already reserved"):
        store.offer_candidates(second, operator_byte=11, now=CLOCK)
    assert store.inspect(store._digest(second)).state == "pending"
    store.offer_candidates(second, operator_byte=12, now=CLOCK)


def test_signed_selection_rejects_wrong_index_hash_and_digest(tmp_path: Path) -> None:
    _, agent, pulse, offer, store = fixture(tmp_path)
    store.begin(offer, pulse, now=CLOCK)
    packet = store.offer_candidates(offer, operator_byte=22, now=CLOCK)
    valid = choose(agent, packet)
    with pytest.raises(ScopeDeniedError, match="selection"):
        replace(valid, selection=replace(valid.selection, chosen_short_public_key_hash=b"\x00" * 3)).verify(
            candidates=packet, expected_agent_public_key=offer.temporary_public_key)
    with pytest.raises(ScopeDeniedError, match="selection"):
        replace(valid, selection=replace(valid.selection, candidate_offer_digest=b"\xff" * 32)).verify(
            candidates=packet, expected_agent_public_key=offer.temporary_public_key)
    with pytest.raises(ScopeDeniedError, match="signer"):
        valid.verify(candidates=packet, expected_agent_public_key=b"\xff" * 32)
    with pytest.raises(ScopeDeniedError, match="signature"):
        replace(valid, signature=bytes(64)).verify(
            candidates=packet, expected_agent_public_key=offer.temporary_public_key)
    assert store.select(offer, packet, valid, now=CLOCK).independence == 4


def test_original_pledge_hardware_claim_is_bound_to_signature(tmp_path: Path) -> None:
    _, _, pulse, offer, store = fixture(tmp_path)
    tampered = replace(offer, offer=replace(offer.offer, hardware_unique_id=b"\xff" * 6))
    with pytest.raises(ScopeDeniedError, match="signature"):
        store.begin(tampered, pulse, now=CLOCK)


def test_pledge_expires_without_releasing_replay_reservation(tmp_path: Path) -> None:
    _, agent, pulse, offer, store = fixture(tmp_path)
    entry = store.begin(offer, pulse, now=CLOCK)
    candidates = store.offer_candidates(offer, operator_byte=7, now=CLOCK)
    expired = store.expire(entry.offer_sha256, now=CLOCK + 301)
    assert expired.state == "expired"
    with pytest.raises(StateMismatchError, match="expired"):
        store.select(offer, candidates, choose(agent, candidates), now=CLOCK + 301)
    with pytest.raises(ScopeDeniedError, match="replay"):
        store.begin(offer, pulse, now=CLOCK)


def test_private_journal_permissions_and_symlink(tmp_path: Path) -> None:
    auth = SyntheticSynAuthority()
    master = public_key_hash(auth.operator_public_key())
    directory = tmp_path / "operator"
    directory.mkdir(mode=0o700)
    other = tmp_path / "elsewhere.sqlite3"
    (directory / "pledges.sqlite3").symlink_to(other)
    with pytest.raises(ScopeDeniedError, match="symlink"):
        SynEnrollmentLedger(directory / "pledges.sqlite3", master_hash=master,
                            network_id=NETWORK, authority=auth)
    assert not other.exists()
    (directory / "pledges.sqlite3").unlink()
    directory.chmod(0o755)
    with pytest.raises(ScopeDeniedError, match="ownership or mode"):
        SynEnrollmentLedger(directory / "pledges.sqlite3", master_hash=master,
                            network_id=NETWORK, authority=auth)


def test_private_journal_cannot_overlap_sandbox(tmp_path: Path) -> None:
    auth = SyntheticSynAuthority()
    sandbox = tmp_path / "home"
    private = sandbox / "operator"
    private.mkdir(parents=True, mode=0o700)
    with pytest.raises(ScopeDeniedError, match="overlaps"):
        SynEnrollmentLedger(private / "pledges.sqlite3",
                            master_hash=public_key_hash(auth.operator_public_key()),
                            network_id=NETWORK, authority=auth, sandbox_home=sandbox)


def test_concurrent_same_offer_has_one_winner(tmp_path: Path) -> None:
    _, _, pulse, offer, store = fixture(tmp_path)
    def admit():
        try:
            return store.begin(offer, pulse, now=CLOCK).state
        except ScopeDeniedError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(lambda _: admit(), range(8)))
    assert observed.count("pending") == 1
    assert observed.count("rejected") == 7


def test_pulse_input_validation_and_parsing(tmp_path: Path) -> None:
    _, _, pulse, _, _ = fixture(tmp_path)
    assert OperatorPulse.from_dict(pulse.pulse.to_dict()) == pulse.pulse
    for override in ({"challenge": bytes(32)}, {"issued_at": CLOCK + 300}):
        with pytest.raises(InvalidInputError):
            replace(pulse.pulse, **override)
    for field, replacement in (("network_id", "UPPER"), ("issued_at", True),
                               ("challenge", "01"), ("extra", "x")):
        val = {**pulse.pulse.to_dict(), field: replacement}
        with pytest.raises(InvalidInputError):
            OperatorPulse.from_dict(val)


def test_selection_input_validation_and_parsing(tmp_path: Path) -> None:
    _, agent, pulse, offer, store = fixture(tmp_path)
    store.begin(offer, pulse, now=CLOCK)
    candidates = store.offer_candidates(offer, operator_byte=5, now=CLOCK)
    selected = choose(agent, candidates)
    assert IndependenceSelection.from_dict(selected.selection.to_dict()) == selected.selection
    for key, value in (("independence", 0), ("inheritance", True),
                       ("chosen_short_public_key_hash", "x"), ("garbage", "x")):
        data = {**selected.selection.to_dict(), key: value}
        with pytest.raises(InvalidInputError):
            IndependenceSelection.from_dict(data)


def test_pulse_and_selection_use_distinct_signature_domains() -> None:
    auth = SyntheticSynAuthority()
    operator = raw_public(auth._operator)
    challenge = b"J" * 32
    pulse = OperatorPulse(NETWORK, public_key_hash(operator), challenge, CLOCK, CLOCK + 60)
    assert pulse.signed_bytes().startswith(PULSE_DOMAIN)
    selection = IndependenceSelection(public_key_hash(operator), 7, 2, b"\x01" * 3, b"\x02" * 32)
    assert selection.signed_bytes().startswith(SELECTION_DOMAIN)
    assert pulse.signed_bytes() != selection.signed_bytes()


def test_unavailable_synthetic_provider_fails_closed(tmp_path: Path) -> None:
    fake = SyntheticSynAuthority()
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with pytest.raises(StateMismatchError, match="master"):
        SynEnrollmentLedger(root / "db.sqlite3", master_hash=b"x" * 32,
                            network_id=NETWORK, authority=fake)
    with pytest.raises(CapabilityUnavailableError):
        SynEnrollmentLedger(root / "db.sqlite3", master_hash=b"x" * 32,
                            network_id=NETWORK, authority=object())


def test_public_wire_json_schema_roundtrip(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[2] / "vocab"
                         / "syn-agent-operator-enrollment.schema.json").read_text())
    validator.check_schema(schema)
    auth, agent, pulse, offer, store = fixture(tmp_path)
    store.begin(offer, pulse, now=CLOCK)
    candidates = store.offer_candidates(offer, operator_byte=0xA7, now=CLOCK)
    selection = choose(agent, candidates)
    for packet in (pulse, candidates, selection):
        wire = packet.to_dict()
        validator(schema).validate(wire)
        assert "hardware_unique_id" not in json.dumps(wire)
    assert SignedOperatorPulse.from_dict(pulse.to_dict()) == pulse
    decoded_candidates = SignedIdentityCandidates.from_dict(
        candidates.to_dict(), verified_pledge=offer.offer,
    )
    decoded_selection = SignedIndependenceSelection.from_dict(selection.to_dict())
    assert decoded_candidates == candidates
    assert decoded_selection == selection
    assert decoded_selection.verify(candidates=decoded_candidates,
                                    expected_agent_public_key=offer.temporary_public_key)
    assert store.select(offer, decoded_candidates, decoded_selection, now=CLOCK).state == "selected"
    del auth


def test_public_wire_unknown_fields_and_cross_pledge_candidates_rejected(tmp_path: Path) -> None:
    auth, _, pulse, first, store = fixture(tmp_path)
    store.begin(first, pulse, now=CLOCK)
    packet = store.offer_candidates(first, operator_byte=18, now=CLOCK)
    mutated = {**pulse.to_dict(), "foreign": "not allowed"}
    with pytest.raises(InvalidInputError, match="unsupported"):
        SignedOperatorPulse.from_dict(mutated)
    mutated = {**packet.to_dict(), "foreign": "not allowed"}
    with pytest.raises(InvalidInputError, match="unsupported"):
        SignedIdentityCandidates.from_dict(mutated, verified_pledge=first.offer)
    wrong = replace(first.offer, hardware_unique_id=b"\x02\x11\x22\x33\x44\x99")
    with pytest.raises(ScopeDeniedError, match="inheritance"):
        SignedIdentityCandidates.from_dict(packet.to_dict(), verified_pledge=wrong)
    mutated = {**packet.to_dict()}
    mutated["candidates"] = {**mutated["candidates"], "unexpected": 1}
    with pytest.raises(InvalidInputError, match="versioned"):
        SignedIdentityCandidates.from_dict(mutated, verified_pledge=first.offer)
    del auth


def test_concurrent_conflicting_selections_exactly_one_winner(tmp_path: Path) -> None:
    _, agent, pulse, pledge, store = fixture(tmp_path)
    store.begin(pledge, pulse, now=CLOCK)
    candidate = store.offer_candidates(pledge, operator_byte=81, now=CLOCK)
    first = choose(agent, candidate, 2)
    second = choose(agent, candidate, 7)

    def attempt(choice: SignedIndependenceSelection) -> int | None:
        try:
            return store.select(pledge, candidate, choice, now=CLOCK).independence
        except StateMismatchError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, (first, second)))
    assert (result.count(None), len([x for x in result if x is not None])) == (1, 1)
    assert store.inspect(store._digest(pledge)).independence in {2, 7}


def test_expired_unregistered_offer_never_triggers_private_derivation(tmp_path: Path) -> None:
    auth, _, pulse, pledge, store = fixture(tmp_path)
    with pytest.raises(ScopeDeniedError, match="not enrolled"):
        store.offer_candidates(pledge, operator_byte=1, now=CLOCK)
    assert not auth.child_requests
    pending = store.begin(pledge, pulse, now=CLOCK)
    with pytest.raises(StateMismatchError, match="expired"):
        store.offer_candidates(pledge, operator_byte=1, now=CLOCK + 301)
    assert not auth.child_requests
    assert store.inspect(pending.offer_sha256).state == "pending"


def test_journal_keeps_expired_inheritance_exclusive(tmp_path: Path) -> None:
    auth, _, pulse, pledge, store = fixture(tmp_path)
    first = store.begin(pledge, pulse, now=CLOCK)
    packet = store.offer_candidates(pledge, operator_byte=41, now=CLOCK)
    store.expire(first.offer_sha256, now=CLOCK + 301)
    assert store.inspect(first.offer_sha256).state == "expired"
    auth.device_handles_broken = True
    _, _, other_pulse, second, _ = fixture(tmp_path, authority=auth, challenge=b"M" * 32)
    store.begin(second, other_pulse, now=CLOCK)
    with pytest.raises(ScopeDeniedError, match="already reserved"):
        store.offer_candidates(second, operator_byte=41, now=CLOCK)
    assert store.inspect(store._digest(second)).state == "pending"
    assert packet.candidates.operator_byte == 41


def test_signed_v2_pledge_wire_decoder_requires_exact_version_and_key(tmp_path: Path) -> None:
    _, _, pulse, offer, ledger = fixture(tmp_path)
    assert signed_pledge_from_dict(offer.to_dict()) == offer
    with pytest.raises(InvalidInputError, match="unsupported"):
        signed_pledge_from_dict({**offer.to_dict(), "extra": True})
    altered = offer.to_dict()
    altered["offer"]["schema_version"] = 1
    with pytest.raises(InvalidInputError):
        signed_pledge_from_dict(altered)
    altered = offer.to_dict()
    altered["temporary_public_key"] = "00" * 32
    with pytest.raises(StateMismatchError, match="temporary"):
        signed_pledge_from_dict(altered)
    assert ledger.begin(signed_pledge_from_dict(offer.to_dict()), pulse, now=CLOCK).state == "pending"
