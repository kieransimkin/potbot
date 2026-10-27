"""SYN Phase 42: public adoption proof, signed operator receipt and durable CAS."""

from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.syn_adoption import (
    ADOPTION_TYPE, PROOF_TYPE, AdoptionReceipt, SelectedKeyProof,
    SignedAdoptionReceipt, SignedSelectedKeyProof,
)
from potbot.domain.syn_agent_pledge import (
    PledgeOffer, SignedPledgeOffer, agent_key_path, public_key_hash,
)
from potbot.domain.syn_enrollment_protocol import (
    CANDIDATE_DOMAIN, IndependenceSelection, OperatorPulse, SignedIndependenceSelection,
    SignedOperatorPulse,
)
from potbot.tools.syn_adoption_ledger import SynAdoptionLedger
from potbot.tools.syn_enrollment_ledger import SynEnrollmentLedger
from potbot.tools.syn_local_daemon import (
    ATTEST_TYPE, HELLO_DOMAIN, RESPONSE_DOMAIN, RESPONSE_TYPE, SynLocalDaemonClient,
    SynLocalEndpoint, _canonical, _receive, _send,
)

CLOCK = 1792920000
NETWORK = "phase42-local-test"


@pytest.fixture(name="tmp_path")
def _short_unix_socket_fixture():
    """Owner-private socket fixture; Linux's sockaddr_un limit is ~107 bytes.

    The monorepo uses a deep default pytest base path. Only these SYN wire
    tests need a shorter directory; they never weaken socket path validation.
    """
    with TemporaryDirectory(prefix="syn-wire-", dir="/tmp" if os.path.isdir("/tmp") else None) as name:
        yield Path(name)


def raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )


class _SyntheticSynAuthority:
    """Only a disposable test fake; production signing stays in SYN."""

    def __init__(self):
        import hmac
        import os

        self._operator = Ed25519PrivateKey.generate()
        self._hmac_key = os.urandom(32)
        self._hmac = hmac
        self._keys = [Ed25519PrivateKey.generate() for _ in range(8)]

    def operator_public_key(self):
        return raw_public(self._operator)

    def attest_and_blind_device(self, hardware_id):
        return self._hmac.digest(self._hmac_key, hardware_id, 'sha256')

    def authorize_offer(self, offer_sha256):
        return True

    def derive_candidate_public_hashes(self, inheritance):
        return tuple(public_key_hash(raw_public(k)) for k in self._keys)

    def sign_candidates(self, preimage):
        assert preimage.startswith(CANDIDATE_DOMAIN)
        return self._operator.sign(preimage)


def fixture(tmp_path: Path):
    auth = _SyntheticSynAuthority()
    agent = Ed25519PrivateKey.generate()
    master_hash = public_key_hash(auth.operator_public_key())
    pulse = OperatorPulse(NETWORK, master_hash, b'P'*32, CLOCK-5, CLOCK+50)
    signed_pulse = SignedOperatorPulse(pulse, auth.operator_public_key(),
                                        auth._operator.sign(pulse.signed_bytes()))
    offer = PledgeOffer(public_key_hash(raw_public(agent)), master_hash, CLOCK,
                        pulse.challenge, b'\x02\x11\x22\x33\x44\x55')
    signed = SignedPledgeOffer(offer, raw_public(agent), agent.sign(offer.signed_bytes()))
    directory = tmp_path/'operator'
    directory.mkdir(mode=0o700, exist_ok=True)
    ledger = SynEnrollmentLedger(directory/'pledges.sqlite3', master_hash=master_hash,
                                 network_id=NETWORK, authority=auth)
    return auth, agent, signed_pulse, signed, ledger


def choose(agent: Ed25519PrivateKey, candidates, independence=4):
    selected = IndependenceSelection(
        candidates.candidates.offer.target_master_public_key_hash,
        candidates.candidates.inheritance, independence,
        candidates.candidates.short_public_key_hashes[independence-1], candidates.digest,
    )
    return SignedIndependenceSelection(selected, raw_public(agent), agent.sign(selected.signed_bytes()))



@contextmanager
def fake_adoption_service(tmp_path: Path, authority, packet=None):
    """Public SYN protocol server simulator with disposable test signing key."""
    directory = tmp_path/'local-syn-service'
    directory.mkdir(mode=0o700)
    path = directory/'syn.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.settimeout(.1)
    done = threading.Event()
    observed = []
    pub = authority.operator_public_key()
    endpoint = SynLocalEndpoint(path, public_key_hash(pub), 'operator', os.getuid())

    def serve():
        while not done.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(.5)
                try:
                    first = _receive(conn)
                    hello = {'schema_version':1, 'type_ref':ATTEST_TYPE,
                             'request_id':first['request_id'],'challenge':first['challenge'],
                             'mode':'operator','base_public_key':pub.hex(),
                             'capabilities':['adoption_receipt.get.v1']}
                    _send(conn, {'attestation':hello, 'signature':authority._operator.sign(
                        HELLO_DOMAIN+_canonical(hello)).hex()})
                    query = _receive(conn)
                    observed.append(query)
                    reply = {'schema_version':1,'type_ref':RESPONSE_TYPE,
                             'method':'adoption_receipt.get','request_id':query['request_id'],
                             'session_challenge':query['session_challenge'],
                             'offer_sha256':query['offer_sha256'],
                             'state':'confirmed' if packet else 'pending',
                             'receipt':packet.to_dict() if packet else None}
                    _send(conn, {'response':reply,'signature':authority._operator.sign(
                        RESPONSE_DOMAIN+_canonical(reply)).hex()})
                except (ConnectionError, TimeoutError, OSError, KeyError):
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield type('LocalSynProbe', (), {'endpoint':lambda self: endpoint,
                                        'queries':observed})()
    finally:
        done.set()
        listener.close()
        thread.join(timeout=2)
        path.unlink(missing_ok=True)


def enrolled(tmp_path: Path):
    authority, agent, pulse, offer, ledger = fixture(tmp_path)
    ledger.begin(offer, pulse, now=CLOCK)
    candidates = ledger.offer_candidates(offer, operator_byte=0xAA, now=CLOCK + 1)
    selected = ledger.select(offer, candidates, choose(agent, candidates, 4), now=CLOCK + 2)
    return authority, ledger, selected, authority._keys[3]


def valid_receipt(authority, selected, signer, *, challenge=b'X' * 32, timestamp=CLOCK + 3):
    proof = SelectedKeyProof(
        bytes.fromhex(selected.offer_sha256), public_key_hash(authority.operator_public_key()),
        selected.inheritance, selected.independence, raw_public(signer), challenge,
        timestamp - 1, timestamp + 50,
    )
    signed_proof = SignedSelectedKeyProof(proof, signer.sign(proof.signed_bytes()))
    receipt = AdoptionReceipt(
        proof.offer_sha256, proof.master_public_key_hash, proof.inheritance,
        proof.independence, proof.selected_public_key_hash, signed_proof.digest, timestamp,
    )
    return SignedAdoptionReceipt(
        receipt, signed_proof, authority.operator_public_key(),
        authority._operator.sign(receipt.signed_bytes()),
    )


def test_adoption_after_selected_key_proof_is_durable(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    packet.verify(master_hash=ledger.master_hash, now=CLOCK+3)
    assert packet.key_proof.proof.to_dict()['type_ref'] == PROOF_TYPE
    assert packet.receipt.to_dict()['type_ref'] == ADOPTION_TYPE
    store = SynAdoptionLedger(ledger)
    recorded = store.record(packet, now=CLOCK+3)
    assert recorded.agent_base_public_key_hash == selected.selected_public_key_hash
    assert recorded.root_derivation_path == agent_key_path(selected.inheritance, 4)
    assert recorded.state == 'syn_adoption_confirmed'
    assert store.record(packet, now=CLOCK+4) == recorded
    # Durable restart: the existing Phase 41 pledge state stays 'selected'.
    restarted = SynEnrollmentLedger(
        tmp_path/'operator'/'pledges.sqlite3', master_hash=ledger.master_hash,
        network_id=ledger.network_id, authority=auth,
    )
    assert SynAdoptionLedger(restarted).inspect(selected.offer_sha256) == recorded
    assert ledger.inspect(selected.offer_sha256).state == 'selected'
    raw = (tmp_path/'operator'/'pledges.sqlite3').read_bytes()
    assert b'private_key' not in raw and b'X'*32 not in raw


@pytest.mark.parametrize('field', ['offer_sha256','master_public_key_hash','inheritance','independence',
                                   'selected_public_key_hash','proof_sha256','adopted_at'])
def test_receipt_binds_every_selected_value(tmp_path: Path, field: str) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    receipt = packet.receipt
    if field.endswith('_sha256') or field.endswith('_hash'):
        value = b'\x33' * 32
    elif field == 'inheritance':
        value = receipt.inheritance ^ 1
    elif field == 'independence':
        value = 5
    else:
        value = receipt.adopted_at + 75
    modified = replace(packet, receipt=replace(receipt, **{field: value}))
    with pytest.raises((ScopeDeniedError, StateMismatchError)):
        modified.verify(master_hash=ledger.master_hash, now=CLOCK+3)


def test_key_proof_is_not_equivalent_to_operator_adoption(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    store = SynAdoptionLedger(ledger)
    with pytest.raises(ScopeDeniedError, match='signature'):
        store.record(replace(packet, operator_signature=b'\0'*64), now=CLOCK+3)
    with pytest.raises(ScopeDeniedError):
        store.inspect(selected.offer_sha256)


def test_operator_claim_without_selected_key_proof_is_rejected(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    bad = replace(packet, key_proof=replace(packet.key_proof, signature=b'\0'*64))
    with pytest.raises(ScopeDeniedError, match='signature'):
        SynAdoptionLedger(ledger).record(bad, now=CLOCK+3)


def test_conflicting_receipt_and_challenge_replay_are_fenced(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    store = SynAdoptionLedger(ledger)
    first = valid_receipt(auth, selected, signer)
    second = valid_receipt(auth, selected, signer, challenge=b'Y'*32)
    first_result = store.record(first, now=CLOCK+3)
    with pytest.raises(StateMismatchError, match='different evidence'):
        store.record(second, now=CLOCK+3)
    assert store.inspect(selected.offer_sha256) == first_result


def test_expired_proof_and_stale_unconfirmed_pledge_are_rejected(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    with pytest.raises(StateMismatchError, match='expired'):
        SynAdoptionLedger(ledger).record(packet, now=CLOCK+70)
    with pytest.raises(ScopeDeniedError):
        SynAdoptionLedger(ledger).inspect(selected.offer_sha256)


def test_concurrent_same_receipt_is_idempotent(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    store = SynAdoptionLedger(ledger)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.record(packet, now=CLOCK+3), range(8)))
    assert all(x == results[0] for x in results)


def test_receipt_wire_is_strict_and_hardware_free(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    record = packet.to_dict()
    assert SignedAdoptionReceipt.from_dict(record) == packet
    text = json.dumps(record)
    assert 'hardware_unique_id' not in text and 'temporary_public_key_hash' not in text
    with pytest.raises(InvalidInputError):
        SignedAdoptionReceipt.from_dict({**record, 'private_key':'forbidden'})
    with pytest.raises(InvalidInputError):
        SignedAdoptionReceipt.from_dict({**record, 'operator_signature':'ZZ'})
    with pytest.raises(InvalidInputError):
        AdoptionReceipt.from_dict({**packet.receipt.to_dict(), 'independence':True})


def test_no_auto_agent_provisioning_from_receipt(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    receipt = valid_receipt(auth, selected, signer)
    confirmed = SynAdoptionLedger(ledger).record(receipt, now=CLOCK+3)
    assert not hasattr(confirmed, 'guest_uid')
    assert not hasattr(SynAdoptionLedger, 'provision_agent')
    assert not hasattr(SynAdoptionLedger, 'export_key')


def test_exact_proof_and_receipt_signing_domains_are_distinct(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    assert packet.key_proof.proof.signed_bytes().startswith(b'syn.agent.selected-key-proof.v1\0')
    assert packet.receipt.signed_bytes().startswith(b'syn.operator.agent-adoption.v1\0')
    assert packet.key_proof.proof.signed_bytes() != packet.receipt.signed_bytes()


@pytest.mark.skipif(__import__('os').name!='posix', reason='Unix socket and SO_PEERCRED')
def test_syn_daemon_public_receipt_reconciliation_end_to_end(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    packet = valid_receipt(auth, selected, signer)
    with fake_adoption_service(tmp_path, auth, packet) as daemon:
        bridge = SynAdoptionLedger(ledger)
        confirmed = bridge.reconcile(
            SynLocalDaemonClient(daemon.endpoint()),
            bytes.fromhex(selected.offer_sha256), now=CLOCK+3,
        )
        assert confirmed is not None
        assert confirmed.agent_base_public_key_hash == selected.selected_public_key_hash
        assert len(daemon.queries) == 1
        assert set(daemon.queries[0]) == {
            'schema_version','type_ref','method','request_id',
            'session_challenge','offer_sha256'
        }
        assert ledger.inspect(selected.offer_sha256).state == 'selected'
        assert bridge.reconcile(SynLocalDaemonClient(daemon.endpoint()),
                                bytes.fromhex(selected.offer_sha256), now=CLOCK+4) == confirmed


@pytest.mark.skipif(__import__('os').name!='posix', reason='Unix socket')
def test_pledge_reconciliation_requires_matching_pinned_operator(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    with fake_adoption_service(tmp_path, _SyntheticSynAuthority()) as daemon:
        with pytest.raises(ScopeDeniedError, match='does not match'):
            SynAdoptionLedger(ledger).reconcile(
                SynLocalDaemonClient(daemon.endpoint()), bytes.fromhex(selected.offer_sha256),
                now=CLOCK+3,
            )


def test_signed_adoption_json_schema_tracks_public_contract(tmp_path: Path) -> None:
    import jsonschema
    auth, ledger, selected, signer = enrolled(tmp_path)
    path = Path(__file__).resolve().parents[2]/'vocab'/'syn-agent-adoption.schema.json'
    schema=json.loads(path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    valid_receipt(auth, selected, signer).verify(master_hash=ledger.master_hash, now=CLOCK+3)
    jsonschema.validate(valid_receipt(auth, selected, signer).to_dict(), schema)
    bad=valid_receipt(auth, selected, signer).to_dict()
    bad['receipt']['custody_claim']='plaintext-file'
    assert not jsonschema.Draft202012Validator(schema).is_valid(bad)


def test_persisted_public_adoption_receipt_detects_sql_tampering(tmp_path: Path) -> None:
    auth, ledger, selected, signer = enrolled(tmp_path)
    store = SynAdoptionLedger(ledger)
    packet = valid_receipt(auth, selected, signer)
    store.record(packet, now=CLOCK+3)
    with ledger._connect() as db:
        db.execute('UPDATE syn_agent_adoptions SET selected_public_key_hash=? WHERE offer_sha256=?',
                   ('f'*64, selected.offer_sha256))
    with pytest.raises(StateMismatchError, match='summary'):
        store.inspect(selected.offer_sha256)
