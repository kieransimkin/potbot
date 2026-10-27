"""Phase 43 standalone SYN node: real local socket, public custody claims, boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from syn_node.custody import NativeOperatorSigner, create_agent_temporary_key
from syn_node.local_service import SynLocalServer, create_registry_entry, publish_registry
from syn_node.protocol import SynProtocolError, canonical, receive, send
from syn_node.receipts import PublicAdoptionJournal, verify_adoption

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.syn_adoption import (
    AdoptionReceipt, SelectedKeyProof, SignedAdoptionReceipt, SignedSelectedKeyProof,
)
from potbot.tools.syn_local_daemon import (
    SynInstallationRegistry, SynLocalDaemonClient, SynLocalEndpoint,
)


def public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )


class DisposableTestSigner:
    """Test-only key. NOT an operator's derived private key or an OS keystore."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()

    @property
    def public_key(self) -> bytes:
        return public(self.key)

    def sign(self, payload: bytes) -> bytes:
        return self.key.sign(payload)


def secure(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


@contextmanager
def running_service(tmp_path: Path, *, mode: str = "operator", store: bool = True):
    area = secure(tmp_path / "server")
    signer = DisposableTestSigner()
    master = hashlib.sha256(signer.public_key).digest()
    journal = PublicAdoptionJournal(area / "public-adoptions.sqlite", expected_master_hash=master) if store else None
    server = SynLocalServer(socket_path=area / "syn.sock", mode=mode, signer=signer, receipts=journal)
    server.bind()
    stop = threading.Event()
    thread = threading.Thread(target=server.serve, args=(stop,), daemon=True)
    thread.start()
    try:
        yield signer, journal, server, SynLocalEndpoint(server.path, master, mode, os.getuid())
    finally:
        stop.set()
        thread.join(timeout=4)
        assert not thread.is_alive()
        server.close()


def sample_receipt(master: DisposableTestSigner, *, now: int, offer: bytes | None = None):
    agent = Ed25519PrivateKey.generate()
    pledge = offer or os.urandom(32)
    h = hashlib.sha256(master.public_key).digest()
    selected = public(agent)
    proof = SelectedKeyProof(pledge, h, 12_345, 3, selected, os.urandom(32), now - 1, now + 50)
    signed = SignedSelectedKeyProof(proof, agent.sign(proof.signed_bytes()))
    rec = AdoptionReceipt(pledge, h, proof.inheritance, proof.independence,
                          proof.selected_public_key_hash, signed.digest, now)
    return SignedAdoptionReceipt(rec, signed, master.public_key, master.sign(rec.signed_bytes()))


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux unix peer credentials")
def test_standalone_server_speaks_real_phase42_client_wire(tmp_path):
    with running_service(tmp_path) as (signer, journal, server, endpoint):
        assert journal is not None
        client = SynLocalDaemonClient(endpoint)
        attested = client.attest()
        assert attested.base_public_key_hash == hashlib.sha256(signer.public_key).digest()
        assert attested.capabilities == ("adoption_receipt.get.v1",)
        offer = os.urandom(32)
        assert client.adoption_receipt(offer) is None
        record = sample_receipt(signer, now=1_792_920_000, offer=offer)
        assert journal.publish(record.to_dict(), now=1_792_920_000)
        assert client.adoption_receipt(offer).digest == record.digest
        assert server._processed >= 3
    assert not endpoint.socket_path.exists()


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux")
def test_syn_registry_created_privately_and_discovered_by_phase42(tmp_path):
    with running_service(tmp_path) as (signer, _journal, _server, endpoint):
        parent = secure(tmp_path / "registry")
        manifest = parent / "syn.json"
        entry = create_registry_entry(socket_path=endpoint.socket_path, mode="operator",
                                      base_public_key_hash=endpoint.base_public_key_hash)
        publish_registry(manifest, entry)
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
        assert SynInstallationRegistry(manifest).discover_operator() == endpoint
        publish_registry(manifest, entry)  # exact duplicate: idempotent
        assert len(json.loads(manifest.read_text())["installations"]) == 1
        with pytest.raises(SynProtocolError, match="conflicting"):
            publish_registry(manifest, {**entry, "base_public_key_hash": "ab" * 32})
        assert SynLocalDaemonClient(endpoint).attest().mode == "operator"


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux")
def test_agent_mode_refuses_operator_adoption_reads(tmp_path):
    with running_service(tmp_path, mode="agent", store=False) as (_signer, journal, server, endpoint):
        assert journal is None
        assert SynLocalDaemonClient(endpoint).attest().capabilities == ()
        with pytest.raises(ScopeDeniedError, match="operator"):
            SynLocalDaemonClient(endpoint).adoption_receipt(os.urandom(32))
        assert server.capabilities == []


def test_immutable_public_receipt_store_rejects_conflicting_commitments(tmp_path):
    signer = DisposableTestSigner()
    folder = secure(tmp_path / "public")
    master = hashlib.sha256(signer.public_key).digest()
    first = sample_receipt(signer, now=1_792_920_000)
    store = PublicAdoptionJournal(folder / "store.sqlite", expected_master_hash=master)
    digest = store.publish(first.to_dict(), now=1_792_920_000)
    assert store.publish(first.to_dict(), now=1_792_920_001) == digest
    resumed = PublicAdoptionJournal(folder / "store.sqlite", expected_master_hash=master)
    assert resumed.inspect(first.receipt.offer_sha256) == first.to_dict()
    another = sample_receipt(signer, now=1_792_920_000, offer=first.receipt.offer_sha256)
    with pytest.raises(SynProtocolError, match="conflicts"):
        resumed.publish(another.to_dict(), now=1_792_920_000)
    with resumed._db() as db:
        db.execute("UPDATE public_adoptions SET digest=?", ("0" * 64,))
    with pytest.raises(SynProtocolError, match="altered"):
        resumed.inspect(first.receipt.offer_sha256)


@pytest.mark.parametrize("fault", ["operator_signature", "selected_proof", "different_operator", "wrong_child", "time", "claim"])
def test_signatures_and_selected_key_binding_fail_closed(tmp_path, fault):
    from copy import deepcopy

    signer = DisposableTestSigner()
    packet = sample_receipt(signer, now=1_792_920_000).to_dict()
    expected = hashlib.sha256(signer.public_key).digest()
    broken = deepcopy(packet)
    if fault == "operator_signature":
        broken["operator_signature"] = "0" * 128
    elif fault == "selected_proof":
        broken["key_proof"]["signature"] = "0" * 128
    elif fault == "different_operator":
        expected = os.urandom(32)
    elif fault == "wrong_child":
        broken["receipt"]["selected_public_key_hash"] = "e" * 64
    elif fault == "time":
        broken["receipt"]["adopted_at"] += 999
    else:
        broken["receipt"]["custody_claim"] = "unknown"
    with pytest.raises(SynProtocolError):
        verify_adoption(broken, expected_master_hash=expected, now=1_792_920_000)


def test_original_raw_hardware_id_and_private_keys_never_enter_receipts(tmp_path):
    signer = DisposableTestSigner()
    folder = secure(tmp_path / "private")
    store = PublicAdoptionJournal(folder / "receipts.sqlite", expected_master_hash=hashlib.sha256(signer.public_key).digest())
    sample = sample_receipt(signer, now=1_792_920_000)
    store.publish(sample.to_dict(), now=1_792_920_000)
    raw = (folder / "receipts.sqlite").read_bytes()
    for private in (b"hardware_unique_id", b"private_key", b"entropy", b"chain_code", b"root_private"):
        assert private not in raw


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux")
def test_public_service_rejects_mismatched_pinned_master_and_symlink_socket(tmp_path):
    with running_service(tmp_path) as (signer, journal, server, endpoint):
        wrong = SynLocalEndpoint(server.path, os.urandom(32), "operator", os.getuid())
        with pytest.raises((ScopeDeniedError, StateMismatchError)):
            SynLocalDaemonClient(wrong).attest()
        with pytest.raises(SynProtocolError, match="different operator"):
            SynLocalServer(socket_path=tmp_path / "unused.sock", mode="operator",
                           signer=DisposableTestSigner(), receipts=journal)
        link = server.path.parent / "link.sock"
        link.symlink_to(server.path)
        with pytest.raises(SynProtocolError):
            SynLocalServer(socket_path=link, mode="operator", signer=signer)


def test_strict_protocol_refuses_duplicate_json_and_large_frames():
    from syn_node.protocol import parse

    for bad in [b'{"x":1,"x":2}', b' {"x":1}', b'[]', b'{}\n']:
        with pytest.raises(SynProtocolError):
            parse(bad)
    with pytest.raises(SynProtocolError):
        parse(b'x' * 65_537)


def test_no_native_keystore_fallback_for_new_agent_temporary_key(monkeypatch, tmp_path):
    from syn_node import custody

    class FakeNativeStore:
        def __init__(self):
            self.storage = {}

        def get_password(self, service, account):
            return self.storage.get((service, account))

        def set_password(self, service, account, value):
            self.storage[(service, account)] = value

    fake = FakeNativeStore()
    lock_dir = secure(tmp_path / "agent-locks")
    monkeypatch.setattr(custody, "_native_backend", lambda: fake)
    pub = create_agent_temporary_key(service="syn.agent", account="fixture-1", lock_dir=lock_dir)
    assert pub["schema"] == "syn-agent-temporary-public-identity/v1"
    assert pub["custody"] == "native-os-credential-store"
    assert len(pub["temporary_public_key_hash"]) == 64
    assert hashlib.sha256(bytes.fromhex(pub["temporary_public_key"])).hexdigest() == pub["temporary_public_key_hash"]
    assert fake.storage[("syn.agent", "fixture-1")]
    with pytest.raises(SynProtocolError, match="already exists"):
        create_agent_temporary_key(service="syn.agent", account="fixture-1", lock_dir=lock_dir)
    other = create_agent_temporary_key(service="syn.agent", account="fixture-2", lock_dir=lock_dir)
    assert other["temporary_public_key_hash"] != pub["temporary_public_key_hash"]
    assert "temporary_public_key" not in str(fake.storage)


def test_missing_native_keyring_backend_is_a_hard_error(monkeypatch):
    from syn_node import custody
    import importlib

    old = importlib.import_module

    def missing(name):
        if name == "keyring":
            raise ImportError("missing native keyring")
        return old(name)

    monkeypatch.setattr(custody.importlib, "import_module", missing)
    with pytest.raises(SynProtocolError, match="not installed"):
        custody._native_backend()


def test_concurrent_operator_registry_updates_remain_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    area = secure(tmp_path / "registry-concurrent")
    target = area / "installations.json"
    peer_area = secure(tmp_path / "agent-sockets")
    entries = [create_registry_entry(socket_path=peer_area / f"agent-{i}.sock",
                                     mode="agent", base_public_key_hash=hashlib.sha256(bytes((i,))).digest())
               for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda entry: publish_registry(target, entry), entries))
    assert len(json.loads(target.read_text())["installations"]) == 8
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_agent_temporary_creation_uses_one_interprocess_lock(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from syn_node import custody

    class FakeNativeStore:
        def __init__(self):
            self.value = None

        def get_password(self, service, account):
            return self.value

        def set_password(self, service, account, value):
            self.value = value

    store = FakeNativeStore()
    monkeypatch.setattr(custody, "_native_backend", lambda: store)
    lock_dir = secure(tmp_path / "agent-private")

    def attempt(_):
        try:
            return custody.create_agent_temporary_key(service="syn.agent", account="one",
                                                     lock_dir=lock_dir)
        except SynProtocolError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert sum(value is not None for value in outcomes) == 1
    assert len(store.value) == 64  # test-only seed held by mock OS vault


def test_registry_preserves_existing_contents_on_invalid_extension(tmp_path):
    area = secure(tmp_path / "registry-invalid")
    peer_area = secure(tmp_path / "peer-dir")
    path = area / "syn.json"
    first = create_registry_entry(socket_path=peer_area / "op.sock", mode="operator",
                                  base_public_key_hash=os.urandom(32))
    publish_registry(path, first)
    original = path.read_bytes()
    bad = {**first, "owner_uid": -1}
    with pytest.raises(SynProtocolError):
        publish_registry(path, bad)
    assert path.read_bytes() == original
    bad = {**first, "base_public_key_hash": "not-hex"}
    with pytest.raises(SynProtocolError):
        publish_registry(path, bad)
    assert path.read_bytes() == original
