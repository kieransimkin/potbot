"""Real PyCardano + OS-keystore API fixture for SYN's signed P2P pledge.

The CI provider MUST be installed, and every test must pass. The credential
backend is disposable: this is not a hardware/OS-keyring certification.
"""

from __future__ import annotations

import hashlib
import threading
import time

import pytest

from syn_node import custody, hd_keys
from syn_node.p2p_inbox import PledgeInbox
from syn_node.p2p_transport import SynP2PClient, SynP2PServer


class DisposableCredentialBackend:
    def __init__(self):
        self.data = {}

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, value):
        self.data[service, account] = value


@pytest.fixture
def real_hd(tmp_path, monkeypatch):
    pytest.importorskip("pycardano")
    hd_keys._pycardano()  # Exact pinned PyCardano and CBOR library versions.
    backend = DisposableCredentialBackend()
    monkeypatch.setattr(hd_keys, "_native_backend", lambda: backend)
    monkeypatch.setattr(custody, "_native_backend", lambda: backend)
    lock = tmp_path / "locks"
    lock.mkdir(mode=0o700)
    public = hd_keys.create_operator_identity(service="syn.test.operator", account="one",
                                               lock_dir=lock)
    temporary = custody.create_agent_temporary_key(service="syn.test.agent", account="one",
                                                   lock_dir=lock)
    operator = hd_keys.PythonHdOperatorSigner(
        "syn.test.operator", "one", bytes.fromhex(public["base_public_key_hash"])
    )
    agent = custody.AgentTemporaryOsSigner(
        "syn.test.agent", "one", bytes.fromhex(temporary["temporary_public_key_hash"])
    )
    inbox = PledgeInbox.create(path=tmp_path / "syn-inbox.sqlite", service="syn.test.inbox",
                               account="one", master_hash=operator.expected_public_hash,
                               network_id="syn-real-qualification", lock_dir=lock,
                               backend=backend)
    return operator, agent, inbox


def test_real_extended_signer_and_true_keystore_agent_on_encrypted_tcp(real_hd):
    operator, temporary, inbox = real_hd
    server = SynP2PServer(signer=operator, inbox=inbox, network_id="syn-real-qualification",
                          bind_host="127.0.0.1", advertised_host="127.0.0.1",
                          allow_loopback=True)
    stop = threading.Event()
    thread = threading.Thread(target=server.serve, args=(stop,), daemon=True)
    thread.start()
    try:
        client = SynP2PClient(master_hash=operator.expected_public_hash,
                              network_id="syn-real-qualification", allow_loopback=True)
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=temporary,
                               hardware_id=bytes.fromhex("021122334455"))
        assert result["activation_authorized"] is False
        assert result["state"] == "pending-operator-review"
        pending = inbox.read_private(result["offer_sha256"], now=int(time.time()))
        assert pending["signed_offer"]["offer"]["temporary_public_key_hash"] == hashlib.sha256(
            temporary.public_key
        ).hexdigest()
        # A real operator can derive all eight children from a reserved public
        # inheritance. Nothing authorizes issuance of an adopted key here.
        hashes = operator.derive_candidate_public_hashes(39721)
        assert len(hashes) == len(set(hashes)) == 8
        assert all(len(item) == 32 for item in hashes)
    finally:
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
