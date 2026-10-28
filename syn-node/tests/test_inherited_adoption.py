"""SYN v47 confidential adopted key: disposable TEST-ONLY HD substitute.

The fake signs with ordinary Ed25519 solely to test encryption/protocol/state.
Real production HD derivation and extended signing run in the separate CI gate.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from syn_node import hd_keys, custody, inherited_adoption as ia
from syn_node.protocol import SynProtocolError
from syn_node.receipts import PublicAdoptionJournal, verify_adoption


class MemoryNativeCredentialStore:
    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_after: int | None = None
        self.write_count = 0

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, value):
        self.write_count += 1
        if self.fail_after is not None and self.write_count >= self.fail_after:
            raise OSError("synthetic disconnected OS backend")
        self.entries[service, account] = value


class FakeWallet:
    def __init__(self, entropy: bytes, path: str="m"):
        self.entropy = entropy
        self.path = path
        seed = hashlib.sha256(b"test-only-hd-node\0" + entropy + path.encode()).digest()
        self.xprivate_key = seed + hashlib.sha256(seed + b"extra").digest()
        self.chain_code = hashlib.sha256(seed + b"chain").digest()
        private = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_key = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @classmethod
    def from_entropy(cls, entropy: str, passphrase: str=""):
        assert passphrase == ""
        return cls(bytes.fromhex(entropy))

    def derive_from_path(self, path: str):
        return type(self)(self.entropy, path)


class FakeExtendedSigner:
    def __init__(self, payload: bytes):
        assert len(payload) == 128
        self.private = Ed25519PrivateKey.from_private_bytes(payload[:32])

    def sign(self, message):
        return self.private.sign(message)


class ApprovedSelection:
    def __init__(self, approved):
        self.approved = approved
        self.called = 0
        self.enabled = True

    def require_selected(self, selection):
        self.called += 1
        if not self.enabled or selection != self.approved:
            raise SynProtocolError("SYN durable ledger has no approved selected offer")


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch):
    backend = MemoryNativeCredentialStore()
    monkeypatch.setattr(hd_keys, "_native_backend", lambda: backend)
    monkeypatch.setattr(custody, "_native_backend", lambda: backend)
    monkeypatch.setattr(ia, "_native_backend", lambda: backend)
    fake_provider = lambda: (FakeWallet, FakeExtendedSigner)
    monkeypatch.setattr(hd_keys, "_pycardano", fake_provider)
    monkeypatch.setattr(ia, "_pycardano", fake_provider)
    monkeypatch.setattr(ia, "_load_operator", hd_keys._load_operator)
    tmp_path.chmod(0o700)
    lock = tmp_path / "lock"
    lock.mkdir(mode=0o700)
    operator = hd_keys.create_operator_identity(service="syn.test.operator", account="op", lock_dir=lock)
    temp = custody.create_agent_temporary_key(service="syn.test.agent", account="agent", lock_dir=lock)
    signer = hd_keys.PythonHdOperatorSigner("syn.test.operator", "op",
                                             bytes.fromhex(operator["base_public_key_hash"]))
    hashes = signer.derive_candidate_public_hashes(42)
    selected = {"offer_sha256": hashlib.sha256(b"signed pledge").hexdigest(),
                "master_public_key_hash": operator["base_public_key_hash"],
                "temporary_public_key": temp["temporary_public_key"],
                "inheritance": 42, "independence": 3,
                "selected_public_key_hash": hashes[2].hex(),
                "selection_sha256": hashlib.sha256(b"signed selected independence").hexdigest()}
    authority = ApprovedSelection(selected)
    handoffs = ia.SynHandoffLedger(tmp_path / "handoffs.sqlite", operator_hash=signer.expected_public_hash)
    journal = PublicAdoptionJournal(tmp_path / "adoptions.sqlite", expected_master_hash=signer.expected_public_hash)
    now = 1_792_900_000
    return (backend, lock, signer, selected, authority, handoffs, journal, now)


def flow(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant = ia.grant_selected_key(selection=selected, authorizer=authority, signer=signer,
                                   ledger=handoffs, now=now)
    request = ia.prepare_agent_request(grant_packet=grant, service="syn.test.agent",
                                        account="agent", lock_dir=lock,
                                        expected_master_hash=signer.expected_public_hash, now=now+1)
    envelope = ia.seal_selected_key(grant_packet=grant, request_packet=request, signer=signer,
                                    authorizer=authority, ledger=handoffs, now=now+2)
    return grant, request, envelope


def test_end_to_end_confidential_adoption_and_phase42_receipt(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, encrypted = flow(scenario)
    assert "xprivate" not in str(grant) + str(request) + str(encrypted)
    assert "hardware_unique_id" not in str(grant) + str(request) + str(encrypted)
    assert "temporary_public_key" not in str(encrypted)
    result = ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=encrypted,
                                    service="syn.test.agent", account="agent", lock_dir=lock,
                                    expected_master_hash=signer.expected_public_hash, now=now+3)
    assert result["selected_public_key_hash"] == selected["selected_public_key_hash"]
    assert result["activation_authorized"] is False
    assert "xprivate" not in json.dumps(result)
    packet = ia.confirm_selected_key(grant_packet=grant, key_proof=result["key_proof"],
                                      signer=signer, authorizer=authority, ledger=handoffs,
                                      journal=journal, now=now+4)
    assert verify_adoption(packet, expected_master_hash=signer.expected_public_hash, now=now+4)
    assert journal.inspect(bytes.fromhex(selected["offer_sha256"])) == packet
    assert ia.confirm_selected_key(grant_packet=grant, key_proof=result["key_proof"],
                                   signer=signer, authorizer=authority, ledger=handoffs,
                                   journal=journal, now=now+4) == packet
    assert authority.called >= 3
    assert len(backend.entries) > 8


def test_restart_replay_and_grant_reservation(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, sealed = flow(scenario)
    with pytest.raises(SynProtocolError, match="already reserved"):
        ia.grant_selected_key(selection=selected, authorizer=authority, signer=signer,
                              ledger=handoffs, now=now)
    with pytest.raises(SynProtocolError, match="not available"):
        ia.seal_selected_key(grant_packet=grant, request_packet=request, signer=signer,
                             authorizer=authority, ledger=handoffs, now=now+3)
    new_ledger = ia.SynHandoffLedger(handoffs.path, operator_hash=signer.expected_public_hash)
    with pytest.raises(SynProtocolError, match="not available"):
        ia.seal_selected_key(grant_packet=grant, request_packet=request, signer=signer,
                             authorizer=authority, ledger=new_ledger, now=now+3)
    ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                          service="syn.test.agent", account="agent", lock_dir=lock,
                          expected_master_hash=signer.expected_public_hash, now=now+3)
    with pytest.raises(SynProtocolError, match="exists or keystore is incomplete"):
        ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+4)


def test_envelope_tampering_and_wrong_operator_fail_before_custody(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, sealed = flow(scenario)
    swapped = dict(sealed, ciphertext="00" + sealed["ciphertext"][2:])
    with pytest.raises(SynProtocolError):
        ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=swapped,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+3)
    with pytest.raises(SynProtocolError, match="wrong operator"):
        ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=b"z" * 32, now=now+3)
    assert not any(k[1].endswith("adoption.manifest.v1") for k in backend.entries)


def test_request_expiry_signature_and_wrong_agent(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, sealed = flow(scenario)
    tampered = {"request": dict(request["request"], request_nonce=os.urandom(32).hex()),
                "temporary_signature": request["temporary_signature"]}
    with pytest.raises(SynProtocolError, match="signature"):
        ia.adopt_selected_key(grant_packet=grant, request_packet=tampered, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+3)
    with pytest.raises(SynProtocolError, match="stale"):
        ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+310)


def test_claimed_approval_cannot_bypass_denied_authorizer(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    authority.enabled = False
    with pytest.raises(SynProtocolError, match="no approved"):
        ia.grant_selected_key(selection=selected, authorizer=authority, signer=signer,
                              ledger=handoffs, now=now)
    authority.enabled = True
    grant = ia.grant_selected_key(selection=selected, authorizer=authority, signer=signer,
                                   ledger=handoffs, now=now)
    request = ia.prepare_agent_request(grant_packet=grant, service="syn.test.agent",
                                        account="agent", lock_dir=lock,
                                        expected_master_hash=signer.expected_public_hash, now=now+1)
    authority.enabled = False
    with pytest.raises(SynProtocolError, match="no approved"):
        ia.seal_selected_key(grant_packet=grant, request_packet=request, signer=signer,
                             authorizer=authority, ledger=handoffs, now=now+2)


def test_partial_keystore_write_blocks_reattempt(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, sealed = flow(scenario)
    backend.fail_after = backend.write_count + 2
    with pytest.raises(SynProtocolError, match="write failed"):
        ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+3)
    backend.fail_after = None
    with pytest.raises(SynProtocolError, match="incomplete"):
        ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+3)


def test_proof_replay_conflicting_signature_is_rejected(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, sealed = flow(scenario)
    result = ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                                    service="syn.test.agent", account="agent", lock_dir=lock,
                                    expected_master_hash=signer.expected_public_hash, now=now+3)
    proof = result["key_proof"]
    changed = {"proof": dict(proof["proof"], challenge=os.urandom(32).hex()),
               "signature": proof["signature"]}
    with pytest.raises(SynProtocolError, match="differs"):
        ia.confirm_selected_key(grant_packet=grant, key_proof=changed, signer=signer,
                                 authorizer=authority, ledger=handoffs, journal=journal,
                                 now=now+4)


def test_wrong_adopted_public_and_identity_pinning(scenario, monkeypatch):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant, request, sealed = flow(scenario)
    grant_mod = {"grant": dict(grant["grant"], selected_public_key_hash="11" * 32),
                 "operator_public_key": grant["operator_public_key"],
                 "signature": grant["signature"]}
    with pytest.raises(SynProtocolError, match="signature"):
        ia.adopt_selected_key(grant_packet=grant_mod, request_packet=request, envelope=sealed,
                              service="syn.test.agent", account="agent", lock_dir=lock,
                              expected_master_hash=signer.expected_public_hash, now=now+3)


def test_private_candidate_bytes_not_leaked_in_json(scenario):
    grant, request, sealed = flow(scenario)
    assert sealed["header"]["grant_sha256"]
    for frame in (grant, request, sealed):
        assert not any(k in frame for k in ("xprivate", "private", "chain", "entropy"))
    assert len(sealed["ciphertext"]) > 250


def make_selected_pledge_evidence(scenario):
    """Private Phase-41 schema populated with real disposable operator/agent sigs."""
    import sqlite3
    from syn_node.selection_authority import (SelectedPledgeAuthorizer, CANDIDATE_DOMAIN,
                                               SELECTION_DOMAIN)
    from syn_node.protocol import canonical
    backend, lock, signer, selected, _, handoffs, journal, now = scenario
    agent = custody.AgentTemporaryOsSigner(
        "syn.test.agent", "agent", hashlib.sha256(bytes.fromhex(selected["temporary_public_key"])).digest())
    candidates = signer.derive_candidate_public_hashes(selected["inheritance"])
    proposal = {
        "schema_version": 2, "type_ref": "synny://type/syn-agent-identity-candidates-v2",
        "pledge_offer_sha256": selected["offer_sha256"],
        "target_master_public_key_hash": selected["master_public_key_hash"],
        "operator_byte": 19, "inheritance": selected["inheritance"],
        "inheritance_hex": f"{selected['inheritance']:06x}",
        "short_public_key_hashes": [v[:3].hex() for v in candidates],
    }
    signed_candidates = {"candidates": proposal, "operator_public_key": signer.public_key.hex(),
                         "signature": signer.sign(CANDIDATE_DOMAIN + canonical(proposal)).hex()}
    choice = {"schema_version": 2,
              "type_ref": "synny://type/syn-agent-independence-selection-v2",
              "target_master_public_key_hash": selected["master_public_key_hash"],
              "inheritance": selected["inheritance"], "independence": selected["independence"],
              "chosen_short_public_key_hash": candidates[selected["independence"] - 1][:3].hex(),
              "candidate_offer_digest": hashlib.sha256(CANDIDATE_DOMAIN + canonical(proposal)).hexdigest()}
    selected_packet = {"selection": choice, "temporary_public_key": selected["temporary_public_key"],
                       "signature": agent.sign(SELECTION_DOMAIN + canonical(choice)).hex()}
    selected = dict(selected, selection_sha256=hashlib.sha256(
        SELECTION_DOMAIN + canonical(choice) + bytes.fromhex(selected_packet["signature"])).hexdigest())
    db_file = lock.parent / "phase41.sqlite"
    db_file.touch(mode=0o600)
    with sqlite3.connect(db_file) as db:
        db.executescript("""CREATE TABLE syn_agent_pledges(
         offer_sha256 TEXT PRIMARY KEY, master_hash TEXT NOT NULL, network_id TEXT NOT NULL,
         device_handle TEXT NOT NULL, pulse_challenge TEXT NOT NULL,
         temporary_public_key TEXT NOT NULL, created_at INTEGER NOT NULL,
         expires_at INTEGER NOT NULL, state TEXT NOT NULL, operator_byte INTEGER,
         inheritance INTEGER, candidate_json TEXT, full_candidate_hashes_json TEXT,
         independence INTEGER, selected_public_key_hash TEXT, revision INTEGER NOT NULL);
        """)
        db.execute("INSERT INTO syn_agent_pledges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            selected["offer_sha256"], selected["master_public_key_hash"], "syn.local", "a" * 64,
            "55" * 32, selected["temporary_public_key"], now, now + 300, "selected", 19,
            selected["inheritance"], json.dumps(signed_candidates),
            json.dumps([x.hex() for x in candidates]), selected["independence"],
            selected["selected_public_key_hash"], 3))
    authorizer = SelectedPledgeAuthorizer(ledger_path=db_file,
                                         expected_master_hash=signer.expected_public_hash,
                                         network_id="syn.local", signed_selection=selected_packet,
                                         now=now+1)
    return authorizer, selected, db_file, selected_packet


def test_real_signed_selected_state_authorizes_only_matching_adoption(scenario):
    authorizer, approved, db, packet = make_selected_pledge_evidence(scenario)
    _, lock, signer, _, _, handoffs, journal, now = scenario
    authorizer.require_selected(approved)
    grant = ia.grant_selected_key(selection=approved, authorizer=authorizer, signer=signer,
                                   ledger=handoffs, now=now+1)
    assert grant["grant"]["selection_sha256"] == approved["selection_sha256"]
    request = ia.prepare_agent_request(grant_packet=grant, service="syn.test.agent",
                                        account="agent", lock_dir=lock,
                                        expected_master_hash=signer.expected_public_hash, now=now+2)
    sealed = ia.seal_selected_key(grant_packet=grant, request_packet=request, signer=signer,
                                  authorizer=authorizer, ledger=handoffs, now=now+3)
    proof = ia.adopt_selected_key(grant_packet=grant, request_packet=request, envelope=sealed,
                                  service="syn.test.agent", account="agent", lock_dir=lock,
                                  expected_master_hash=signer.expected_public_hash, now=now+4)
    receipt = ia.confirm_selected_key(grant_packet=grant, key_proof=proof["key_proof"],
                                       signer=signer, authorizer=authorizer, ledger=handoffs,
                                       journal=journal, now=now+5)
    assert verify_adoption(receipt, expected_master_hash=signer.expected_public_hash,
                           now=now+5)


def test_private_authorization_rejects_forged_or_changed_evidence(scenario):
    import sqlite3
    from syn_node.selection_authority import SelectedPledgeAuthorizer
    authorizer, selected, file, signed = make_selected_pledge_evidence(scenario)
    with pytest.raises(SynProtocolError, match="stale or contradictory"):
        authorizer.require_selected(dict(selected, independence=7))
    with sqlite3.connect(file) as db:
        db.execute("UPDATE syn_agent_pledges SET state='offered'")
    with pytest.raises(SynProtocolError, match="not reached"):
        authorizer.require_selected(selected)
    with sqlite3.connect(file) as db:
        db.execute("UPDATE syn_agent_pledges SET state='selected', selected_public_key_hash=?",
                   ("33" * 32,))
    with pytest.raises(SynProtocolError, match="contradictory"):
        authorizer.require_selected(selected)


def test_selection_evidence_signature_and_master_tampering_fails(scenario):
    from syn_node.selection_authority import SelectedPledgeAuthorizer
    authorizer, selected, file, packet = make_selected_pledge_evidence(scenario)
    bad = dict(packet, signature="00" * 64)
    broken = SelectedPledgeAuthorizer(ledger_path=file,
                                       expected_master_hash=authorizer.master_hash,
                                       network_id="syn.local", signed_selection=bad,
                                       now=authorizer._at)
    with pytest.raises(SynProtocolError, match="signature"):
        broken.require_selected(selected)
    with pytest.raises(SynProtocolError, match="another operator"):
        authorizer.require_selected(dict(selected, master_public_key_hash="00" * 32))


def test_host_local_cli_full_selected_key_adoption(scenario, monkeypatch, tmp_path, capsys):
    from syn_node import adoption_cli
    from syn_node.protocol import canonical
    _, lock, signer, _, _, handoffs, journal, now = scenario
    authorizer, selection, phase41, packet = make_selected_pledge_evidence(scenario)
    approved_dir = tmp_path / "operator-approved"
    approved_dir.mkdir(mode=0o700)
    config = approved_dir / "operator.json"
    config.write_bytes(canonical({
        "schema_version": 2, "mode": "operator", "keyring_service": "syn.test.operator",
        "keyring_account": "op", "expected_base_public_key_hash": signer.expected_public_hash.hex(),
    }))
    config.chmod(0o600)
    chosen = approved_dir / "signed-selection.json"
    chosen.write_bytes(canonical(packet))
    chosen.chmod(0o600)
    monkeypatch.setattr(adoption_cli, "_approve_on_tty", lambda _: None)
    clock = [now+1]
    monkeypatch.setattr(adoption_cli.time, "time", lambda: clock[0])
    common = ["--config", str(config), "--phase41-ledger", str(phase41),
              "--signed-selection", str(chosen), "--network-id", "syn.local",
              "--handoff-ledger", str(handoffs.path)]
    grant = approved_dir / "grant.json"
    assert adoption_cli.main(["operator-grant", *common,
                              "--offer-sha256", selection["offer_sha256"],
                              "--output", str(grant)]) == 0
    clock[0] += 1
    request = approved_dir / "request.json"
    agent = ["--keyring-service", "syn.test.agent", "--keyring-account", "agent",
             "--lock-dir", str(lock), "--expected-master-hash", signer.expected_public_hash.hex()]
    assert adoption_cli.main(["agent-request", *agent, "--grant", str(grant),
                              "--output", str(request)]) == 0
    clock[0] += 1
    sealed = approved_dir / "sealed.json"
    assert adoption_cli.main(["operator-seal", *common, "--grant", str(grant),
                              "--request", str(request), "--output", str(sealed)]) == 0
    clock[0] += 1
    proof = approved_dir / "proof.json"
    assert adoption_cli.main(["agent-adopt", *agent, "--grant", str(grant),
                              "--request", str(request), "--envelope", str(sealed),
                              "--output", str(proof)]) == 0
    clock[0] += 1
    receipt = approved_dir / "receipt.json"
    assert adoption_cli.main(["operator-confirm", *common, "--grant", str(grant),
                              "--proof", str(proof), "--receipt-journal", str(journal.path),
                              "--output", str(receipt)]) == 0
    assert verify_adoption(json.loads(receipt.read_text()),
                           expected_master_hash=signer.expected_public_hash, now=clock[0])
    stdout = capsys.readouterr().out
    assert "xprivate" not in stdout
    assert "agent_activation_authorized" in stdout
    # Both public output and native secret material are fenced against replay.
    assert adoption_cli.main(["agent-adopt", *agent, "--grant", str(grant),
                              "--request", str(request), "--envelope", str(sealed),
                              "--output", str(approved_dir / "proof-replay.json")]) == 2
    assert not (approved_dir / "proof-replay.json").exists()


def test_cli_requires_private_files_and_cannot_issue_grant_without_owner(scenario, monkeypatch, tmp_path):
    from syn_node import adoption_cli
    from syn_node.protocol import canonical
    _, _, signer, _, _, handoffs, _, now = scenario
    authority, selected, file, packet = make_selected_pledge_evidence(scenario)
    base = tmp_path / "operator-files"
    base.mkdir(mode=0o700)
    conf = base / "operator.json"
    conf.write_bytes(canonical({
        "schema_version":2, "mode":"operator", "keyring_service":"syn.test.operator",
        "keyring_account":"op", "expected_base_public_key_hash":signer.expected_public_hash.hex(),
    }))
    conf.chmod(0o600)
    signed = base / "signed.json"
    signed.write_bytes(canonical(packet));signed.chmod(0o600)
    now_value = [now+1]
    monkeypatch.setattr(adoption_cli.time, "time", lambda: now_value[0])
    def deny(_):
        raise SynProtocolError("operator did not consent")
    monkeypatch.setattr(adoption_cli, "_approve_on_tty", deny)
    args = ["operator-grant", "--config", str(conf), "--phase41-ledger", str(file),
            "--signed-selection", str(signed), "--network-id", "syn.local",
            "--handoff-ledger", str(handoffs.path), "--offer-sha256", selected["offer_sha256"],
            "--output", str(base / "grant.json")]
    assert adoption_cli.main(args) == 2
    assert not (base / "grant.json").exists()
    signed.chmod(0o644)
    assert adoption_cli.main(args) == 2


def test_public_wire_schema_covers_full_handoff_and_rejects_secrets(scenario):
    validator = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[1] / "schema" /
                         "inherited-key-handoff-v1.schema.json").read_text())
    validator.Draft202012Validator.check_schema(schema)
    grant, request, sealed = flow(scenario)
    for packet in (grant, request, sealed):
        validator.validate(packet, schema)
    assert not validator.Draft202012Validator(schema).is_valid(
        dict(sealed, xprivate="00" * 64))
    assert not validator.Draft202012Validator(schema).is_valid(
        dict(request, private_seed="00" * 32))
    assert not validator.Draft202012Validator(schema).is_valid(
        dict(sealed, ciphertext="not hexadecimal"))


def test_transfer_request_uses_os_random_x25519_not_hardware_identifier(scenario):
    backend, lock, signer, selected, authority, handoffs, journal, now = scenario
    grant = ia.grant_selected_key(selection=selected, authorizer=authority, signer=signer,
                                   ledger=handoffs, now=now)
    request = ia.prepare_agent_request(grant_packet=grant, service="syn.test.agent",
                                        account="agent", lock_dir=lock,
                                        expected_master_hash=signer.expected_public_hash, now=now+1)
    assert request["request"]["transfer_public_key"] != selected["temporary_public_key"]
    assert len(bytes.fromhex(request["request"]["transfer_public_key"])) == 32
    # The temporary signing key and the one-time X25519 transfer secret occupy
    # completely separate OS-credential slots; no deterministic hardware derivation.
    assert backend.get_password("syn.test.agent", "agent") != backend.get_password(
        "syn.test.agent", "agent.adoption.transfer_private.v1")
    with pytest.raises(SynProtocolError, match="exists or is incomplete"):
        ia.prepare_agent_request(grant_packet=grant, service="syn.test.agent",
                                 account="agent", lock_dir=lock,
                                 expected_master_hash=signer.expected_public_hash, now=now+2)
