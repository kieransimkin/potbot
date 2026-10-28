"""MANDATORY real PyCardano key transfer gate in SYN Python HD CI.

The OS credential backend here is deliberately simulated. The private transfer
uses real Ed25519-BIP32 V2 nodes at the exact owner-specified paths, real X25519
key exchange, HKDF, AES-256-GCM, and both real signatures. Hardware/OS keystore
qualification remains separate and cannot be inferred from this test.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from syn_node import custody, hd_keys, inherited_adoption as adoption
from syn_node.receipts import PublicAdoptionJournal, verify_adoption


class DisposableCredentials:
    def __init__(self):
        self.records = {}

    def get_password(self, service, account):
        return self.records.get((service, account))

    def set_password(self, service, account, value):
        self.records[(service, account)] = value


class AlreadySelectedFixture:
    """Test-only: actual deployment uses SelectedPledgeAuthorizer."""
    def __init__(self, selection):
        self.selection = selection

    def require_selected(self, candidate):
        assert candidate == self.selection


@pytest.fixture
def real_operator(tmp_path: Path, monkeypatch):
    pytest.importorskip("pycardano")
    hd_keys._pycardano()  # Pins precise provider + CBOR versions.
    lock = tmp_path / "locks"
    lock.mkdir(mode=0o700)
    store = DisposableCredentials()
    monkeypatch.setattr(hd_keys, "_native_backend", lambda: store)
    monkeypatch.setattr(custody, "_native_backend", lambda: store)
    monkeypatch.setattr(adoption, "_native_backend", lambda: store)
    public = hd_keys.create_operator_identity(service="test.real.operator", account="operator",
                                               lock_dir=lock)
    temp = custody.create_agent_temporary_key(service="test.real.agent", account="agent",
                                               lock_dir=lock)
    signer = hd_keys.PythonHdOperatorSigner("test.real.operator", "operator",
                                             bytes.fromhex(public["base_public_key_hash"]))
    return lock, signer, temp, store


@pytest.mark.parametrize("inheritance,independence", [(0, 1), (172941, 4), (2**24-1, 8)])
def test_real_hd_selected_key_keystore_proof_and_operator_receipt(
    real_operator, tmp_path: Path, inheritance: int, independence: int
):
    lock, signer, agent, store = real_operator
    hashes = signer.derive_candidate_public_hashes(inheritance)
    index = independence - 1
    selected = {
        "offer_sha256": hashlib.sha256(f"qualified-offer-{inheritance}-{independence}".encode()).hexdigest(),
        "master_public_key_hash": signer.expected_public_hash.hex(),
        "temporary_public_key": agent["temporary_public_key"],
        "inheritance": inheritance,
        "independence": independence,
        "selected_public_key_hash": hashes[index].hex(),
        "selection_sha256": hashlib.sha256(b"authorised, test-only selection transcript").hexdigest(),
    }
    authorizer = AlreadySelectedFixture(selected)
    ledger = adoption.SynHandoffLedger(tmp_path / "transfer.sqlite",
                                        operator_hash=signer.expected_public_hash)
    receipts = PublicAdoptionJournal(tmp_path / "receipts.sqlite",
                                     expected_master_hash=signer.expected_public_hash)
    now = 1_792_900_000
    grant = adoption.grant_selected_key(selection=selected, authorizer=authorizer,
                                         signer=signer, ledger=ledger, now=now)
    request = adoption.prepare_agent_request(grant_packet=grant, service="test.real.agent",
                                              account="agent", lock_dir=lock,
                                              expected_master_hash=signer.expected_public_hash,
                                              now=now+1)
    sealed = adoption.seal_selected_key(grant_packet=grant, request_packet=request,
                                        signer=signer, authorizer=authorizer, ledger=ledger,
                                        now=now+2)
    assert set(sealed) == {"header", "ciphertext", "operator_signature"}
    result = adoption.adopt_selected_key(grant_packet=grant, request_packet=request,
                                          envelope=sealed, service="test.real.agent",
                                          account="agent", lock_dir=lock,
                                          expected_master_hash=signer.expected_public_hash,
                                          now=now+3)
    receipt = adoption.confirm_selected_key(grant_packet=grant, key_proof=result["key_proof"],
                                             signer=signer, authorizer=authorizer, ledger=ledger,
                                             journal=receipts, now=now+4)
    assert verify_adoption(receipt, expected_master_hash=signer.expected_public_hash,
                           now=now+4)
    assert result["selected_public_key_hash"] == hashes[index].hex()
    # Independently verify the final private-key proof with its derived public key.
    proof = result["key_proof"]
    from syn_node.protocol import canonical
    from syn_node.receipts import PROOF_DOMAIN
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(proof["proof"]["selected_public_key"])).verify(
        bytes.fromhex(proof["signature"]), PROOF_DOMAIN + canonical(proof["proof"]))
    # Neither the sealed wire frame nor the public return values contain private material.
    assert not any(k.endswith(".adoption.manifest.v1") for k in sealed)
    assert result["activation_authorized"] is False
    assert receipts.inspect(bytes.fromhex(selected["offer_sha256"])) == receipt
    assert len(store.records) >= 15  # 8 operator slots + temporary + 2 transport + 4 adopted slots.
