"""SYN signed LAN pulses, encrypted on-wire offers, and private/replay-safe inbox."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from syn_node.p2p_contracts import (
    build_signed_offer, derive_session, new_client_hello,
    new_server_hello, verify_advert, verify_server_hello,
    verify_signed_offer,
)
from syn_node.p2p_inbox import PledgeInbox
from syn_node.p2p_transport import SynP2PClient, SynP2PServer
from syn_node.protocol import SynProtocolError, canonical

NOW = int(time.time())
NETWORK = "syn-fixture-lan"
HARDWARE = bytes.fromhex("021122334455")


class Signer:
    def __init__(self):
        self.private = Ed25519PrivateKey.generate()

    @property
    def public_key(self):
        return self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def sign(self, payload):
        return self.private.sign(payload)


class Backend:
    def __init__(self):
        self.data = {}
        self.fail = False

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, value):
        self.data[service, account] = value
        if self.fail:
            raise OSError("simulated credential persistence uncertainty")


@pytest.fixture
def setup(tmp_path):
    backend = Backend()
    master = Signer()
    identity = hashlib.sha256(master.public_key).digest()
    db_path = tmp_path / "private-inbox.sqlite"
    inbox = PledgeInbox.create(path=db_path, service="syn.p2p", account="operator",
                               master_hash=identity, network_id=NETWORK,
                               lock_dir=tmp_path, backend=backend)
    client = SynP2PClient(master_hash=identity, network_id=NETWORK,
                          allow_loopback=True)
    return tmp_path, backend, master, inbox, client


def make_server(setup):
    _, _, master, inbox, _ = setup
    return SynP2PServer(signer=master, inbox=inbox, network_id=NETWORK,
                        bind_host="127.0.0.1", advertised_host="127.0.0.1",
                        allow_loopback=True)


class Running:
    def __init__(self, server):
        self.server = server
        self.stop = threading.Event()
        self.thread = threading.Thread(target=server.serve, args=(self.stop,), daemon=True)
        self.thread.start()

    def __enter__(self):
        return self.server

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def test_private_signed_pulse_and_exact_phase41_schema(setup):
    _, _, master, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse(now=NOW)
        assert set(advert["signed_pulse"]["pulse"]) == {
            "schema_version", "type_ref", "network_id", "master_public_key_hash",
            "challenge", "issued_at", "expires_at"
        }
        assert verify_advert(advert, master=inbox.master_hash,
                             network_id=NETWORK, now=NOW, loopback=True) == advert
        assert HARDWARE.hex() not in str(advert)
        assert master.public_key.hex() == advert["signed_pulse"]["operator_public_key"]


def test_end_to_end_two_agents_same_pulse_with_private_encrypted_inbox(setup):
    path, backend, master, inbox, client = setup
    agent_one, agent_two = Signer(), Signer()
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result_one = client.pledge(advert=advert, temporary_signer=agent_one, hardware_id=HARDWARE)
        result_two = client.pledge(advert=advert, temporary_signer=agent_two,
                                   hardware_id=bytes.fromhex("021122334466"))
        assert result_one["state"] == result_two["state"] == "pending-operator-review"
        assert result_one["activation_authorized"] is False
        assert len(inbox.pending_digests()) == 2
        value = inbox.read_private(result_one["offer_sha256"], now=int(time.time()))
        assert bytes.fromhex(value["signed_offer"]["offer"]["hardware_unique_id"]) == HARDWARE
        assert value["signed_pulse"] == advert["signed_pulse"]
        assert HARDWARE.hex().encode() not in path.joinpath("private-inbox.sqlite").read_bytes()
        assert agent_one.public_key.hex().encode() not in path.joinpath("private-inbox.sqlite").read_bytes()
        reopened = PledgeInbox(path=path / "private-inbox.sqlite", service="syn.p2p",
                               account="operator", master_hash=inbox.master_hash,
                               network_id=NETWORK, backend=backend)
        assert reopened.pending_digests() == inbox.pending_digests()
        assert reopened.read_private(result_two["offer_sha256"], now=int(time.time()))["signed_pulse"] == advert["signed_pulse"]


def test_replay_same_offer_and_rotated_temp_key_same_hardware_blocked(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        assert result["state"] == "pending-operator-review"
        with pytest.raises(SynProtocolError):
            client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        assert len(inbox.pending_digests()) == 1


def test_tampered_operator_advert_host_endpoint_master_and_signature(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        packet = server.pulse()
        for changed in (
            {**packet, "host": "192.168.1.99"},
            {**packet, "port": packet["port"] + 1},
            {**packet, "signature": "00" * 64},
            {**packet, "unknown": 3},
        ):
            with pytest.raises(SynProtocolError):
                verify_advert(changed, master=inbox.master_hash, network_id=NETWORK,
                              now=int(time.time()), loopback=True)
        with pytest.raises(SynProtocolError):
            verify_advert(packet, master=bytes(32), network_id=NETWORK,
                          now=int(time.time()), loopback=True)
        with pytest.raises(SynProtocolError):
            verify_advert(packet, master=inbox.master_hash, network_id="other-network",
                          now=int(time.time()), loopback=True)
        with pytest.raises(SynProtocolError):
            verify_advert(packet, master=inbox.master_hash, network_id=NETWORK,
                          now=int(time.time()), source_host="192.168.0.5", loopback=True)


def test_pulse_has_strict_expiry_and_rejects_unpinned_operator(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        old = server.pulse(now=NOW - 200)
        with pytest.raises(SynProtocolError):
            verify_advert(old, master=inbox.master_hash, network_id=NETWORK,
                          now=NOW, loopback=True)
        packet = server.pulse(now=NOW)
        with pytest.raises(SynProtocolError):
            verify_advert(packet, master=inbox.master_hash, network_id=NETWORK,
                          now=NOW - 1, loopback=True)


def test_hardware_id_is_signature_bound_and_never_in_public_pulse(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        packet = server.pulse(now=NOW)
        signer = Signer()
        signed = build_signed_offer(signer, advert=packet, hardware_id=HARDWARE, now=NOW)
        assert verify_signed_offer(signed, advert=packet, now=NOW) == signed
        assert HARDWARE.hex() not in canonical(packet).decode()
        clone = json.loads(json.dumps(signed))
        clone["offer"]["hardware_unique_id"] = "021122334466"
        with pytest.raises(SynProtocolError):
            verify_signed_offer(clone, advert=packet, now=NOW)
        clone = json.loads(json.dumps(signed))
        clone["temporary_public_key"] = bytes(32).hex()
        with pytest.raises(SynProtocolError):
            verify_signed_offer(clone, advert=packet, now=NOW)


def test_wrong_key_on_encrypted_frame_and_modified_transcript(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        priv_c, priv_s = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        hello = new_client_hello(advert, priv_c)
        response = new_server_hello(server.signer, advert, hello, priv_s)
        verify_server_hello(response, advert=advert, client_hello=hello)
        sender = derive_session(priv_c, priv_s.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw), hello, response)
        recipient = derive_session(priv_s, priv_c.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw), hello, response)
        frame = sender.encrypt({"secret": "signed-only"}, client=True)
        assert recipient.decrypt(frame, client=True) == {"secret": "signed-only"}
        altered = {**frame, "ciphertext": "00" * 32}
        with pytest.raises(SynProtocolError):
            recipient.decrypt(altered, client=True)
        other = {**response, "nonce": os.urandom(32).hex()}
        with pytest.raises(SynProtocolError):
            verify_server_hello(other, advert=advert, client_hello=hello)


def test_udp_discovery_ignores_spoof_and_accepts_pinned_pulse(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        response = {}
        def scan():
            try:
                response["advert"] = client.discover(udp_port=port, timeout_seconds=2)
            except Exception as exc:
                response["error"] = exc
        worker = threading.Thread(target=scan)
        worker.start()
        time.sleep(0.07)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b'{"invalid":true}', ("127.0.0.1", port))
        packet = server.broadcast_once("127.0.0.1", port)
        worker.join(3)
        assert "error" not in response, response
        assert response["advert"] == packet


def test_missing_pinned_master_or_external_ip_rejected(setup):
    _, _, _, inbox, _ = setup
    with pytest.raises(SynProtocolError):
        SynP2PClient(master_hash=b"", network_id=NETWORK)
    with pytest.raises(SynProtocolError):
        SynP2PServer(signer=Signer(), inbox=inbox, network_id=NETWORK,
                     bind_host="8.8.8.8", advertised_host="8.8.8.8")
    with pytest.raises(SynProtocolError):
        SynP2PClient(master_hash=inbox.master_hash, network_id="BAD")


def test_expiration_destroys_ciphertext_but_retains_replay_tombstone(setup):
    _, backend, master, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        assert inbox.expire(now=int(time.time()) + 400) == 1
        with pytest.raises(SynProtocolError):
            inbox.read_private(result["offer_sha256"], now=int(time.time()) + 400)
        assert inbox.pending_digests() == ()
        with pytest.raises(SynProtocolError):
            client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)


def test_keystore_failure_partial_store_symlink_and_metadata_tamper(setup):
    path, backend, master, inbox, client = setup
    with pytest.raises(SynProtocolError):
        PledgeInbox.create(path=path / "private-inbox.sqlite", service="syn.p2p",
                           account="operator", master_hash=inbox.master_hash,
                           network_id=NETWORK, lock_dir=path, backend=backend)
    wrong = Backend()
    with pytest.raises(SynProtocolError):
        PledgeInbox(path=path / "private-inbox.sqlite", service="syn.p2p", account="operator",
                    master_hash=inbox.master_hash, network_id=NETWORK, backend=wrong)
    with sqlite3.connect(inbox.path) as db:
        db.execute("UPDATE inbox_metadata SET master=? WHERE id=1", ("00" * 32,))
    with pytest.raises(SynProtocolError):
        PledgeInbox(path=inbox.path, service="syn.p2p", account="operator",
                    master_hash=inbox.master_hash, network_id=NETWORK, backend=backend)
    fake = path / "redirect.db"
    fake.symlink_to(inbox.path)
    with pytest.raises(SynProtocolError):
        PledgeInbox(path=fake, service="syn.p2p", account="operator",
                    master_hash=inbox.master_hash, network_id=NETWORK, backend=backend)


def test_wrong_packet_length_rejected_before_payload_read(setup):
    _, _, _, inbox, _ = setup
    with Running(make_server(setup)) as server:
        server.pulse()
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as s:
            s.sendall(struct.pack(">I", 65000))
            assert s.recv(4096) == b""
        assert inbox.pending_digests() == ()


def test_empty_broadcast_cannot_establish_p2p_trust(setup):
    _, _, _, inbox, client = setup
    with pytest.raises(SynProtocolError):
        client.discover(udp_port=1234, timeout_seconds=0.05)


def test_p2p_client_never_releases_raw_hardware_in_result(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        assert HARDWARE.hex() not in str(result)
        assert result["activation_authorized"] is False


def test_hardware_bytes_never_in_sqlite_metadata(setup):
    path, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        with sqlite3.connect(inbox.path) as db:
            rows = db.execute("SELECT device_handle,nonce,encrypted FROM pledge_inbox").fetchall()
        assert len(rows) == 1
        assert rows[0][0] != hashlib.sha256(HARDWARE).hexdigest()
        assert len(rows[0][0]) == 64
        assert rows[0][2] and HARDWARE not in rows[0][2]
        assert HARDWARE.hex() not in str(rows)


def test_private_inbox_conflicts_with_wrong_network_and_master(setup):
    path, backend, master, inbox, client = setup
    with pytest.raises(SynProtocolError):
        PledgeInbox(path=inbox.path, service="syn.p2p", account="operator",
                    master_hash=inbox.master_hash, network_id="other-network", backend=backend)
    with pytest.raises(SynProtocolError):
        PledgeInbox(path=inbox.path, service="syn.p2p", account="operator",
                    master_hash=b"w" * 32, network_id=NETWORK, backend=backend)


def test_durable_private_record_tampering_rejected(setup):
    _, backend, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        with sqlite3.connect(inbox.path) as db:
            db.execute("UPDATE pledge_inbox SET encrypted=? WHERE offer_sha=?", (b"deadbeef", result["offer_sha256"]))
        with pytest.raises(SynProtocolError):
            inbox.read_private(result["offer_sha256"], now=int(time.time()))


def test_p2p_wire_schema_structural_contract(setup):
    validator = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[1] / "schema" /
                         "syn-p2p-lan-v1.schema.json").read_text())
    validator.Draft202012Validator.check_schema(schema)
    _, _, master, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        assert validator.Draft202012Validator(schema).is_valid(advert)
        signed = build_signed_offer(Signer(), advert=advert, hardware_id=HARDWARE,
                                    now=int(time.time()))
        assert validator.Draft202012Validator(schema).is_valid(signed)
        private = X25519PrivateKey.generate()
        hello = new_client_hello(advert, private)
        assert validator.Draft202012Validator(schema).is_valid(hello)
        server_hello = new_server_hello(server.signer, advert, hello, X25519PrivateKey.generate())
        assert validator.Draft202012Validator(schema).is_valid(server_hello)
        key = derive_session(private, bytes.fromhex(server_hello["server_public"]),
                             hello, server_hello)
        encrypted = key.encrypt(signed, client=True)
        assert validator.Draft202012Validator(schema).is_valid(encrypted)
        assert not validator.Draft202012Validator(schema).is_valid(
            {**advert, "hardware_unique_id": HARDWARE.hex()}
        )


def test_created_and_expiry_metadata_are_authenticated(setup):
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
    with sqlite3.connect(inbox.path) as db:
        db.execute("UPDATE pledge_inbox SET created=created-1,expires=expires+1"
                   " WHERE offer_sha=?", (result["offer_sha256"],))
    with pytest.raises(SynProtocolError):
        inbox.read_private(result["offer_sha256"], now=int(time.time()))


def test_partial_keystore_write_stays_tombstoned(tmp_path):
    backend = Backend()
    backend.fail = True
    identity = hashlib.sha256(b"operator fixture").digest()
    location = tmp_path / "interrupted.sqlite"
    with pytest.raises(SynProtocolError):
        PledgeInbox.create(path=location, service="syn.operator", account="broken",
                           master_hash=identity, network_id=NETWORK, lock_dir=tmp_path,
                           backend=backend)
    backend.fail = False
    with pytest.raises(SynProtocolError):
        PledgeInbox.create(path=location, service="syn.operator", account="broken",
                           master_hash=identity, network_id=NETWORK, lock_dir=tmp_path,
                           backend=backend)
    assert not location.exists()  # No plaintext fallback or regenerated key.


def test_quota_fail_closed_without_overwriting_older_pledge(setup, monkeypatch):
    from syn_node import p2p_inbox
    _, _, _, inbox, client = setup
    with Running(make_server(setup)) as server:
        advert = server.pulse()
        result = client.pledge(advert=advert, temporary_signer=Signer(), hardware_id=HARDWARE)
        monkeypatch.setattr(p2p_inbox, "MAX_PENDING", 1)
        with pytest.raises(SynProtocolError):
            client.pledge(advert=advert, temporary_signer=Signer(),
                          hardware_id=bytes.fromhex("021122334466"))
        assert inbox.pending_digests() == (result["offer_sha256"],)


def test_cli_hardware_id_never_accepted_from_symlink_or_public_file(tmp_path):
    from syn_node.p2p_cli import _private_hardware_file
    data = tmp_path / "hardware.bin"
    data.write_bytes(HARDWARE)
    data.chmod(0o600)
    assert _private_hardware_file(data) == HARDWARE
    data.chmod(0o644)
    with pytest.raises(SynProtocolError):
        _private_hardware_file(data)
    data.chmod(0o600)
    alias = tmp_path / "symlink"
    alias.symlink_to(data)
    with pytest.raises(SynProtocolError):
        _private_hardware_file(alias)


def test_cli_does_not_echo_secrets_or_allow_activation(capsys):
    from syn_node.p2p_cli import main
    assert main(["agent-pledge", "--unknown-secret", HARDWARE.hex()]) == 2
    result = capsys.readouterr()
    assert HARDWARE.hex() not in result.out + result.err
    assert "blocked" in result.err
    assert "activation_authorized" in result.err


def test_rate_budget_limits_unsigned_handshake_cpu_cost(setup, monkeypatch):
    from syn_node import p2p_transport
    with Running(make_server(setup)) as server:
        monkeypatch.setattr(p2p_transport, "MAX_PER_PEER_PER_MINUTE", 2)
        assert server._admit_peer("127.0.0.1") is True
        assert server._admit_peer("127.0.0.1") is True
        assert server._admit_peer("127.0.0.1") is False
        assert server._admit_peer("127.0.0.2") is True
