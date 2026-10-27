"""SYN Phase 42: authenticated Unix peer plus Ed25519 challenge-response."""
from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.syn_agent_pledge import public_key_hash
from potbot.tools.syn_local_daemon import (
    ATTEST_TYPE, HELLO_DOMAIN, HELLO_TYPE, RESPONSE_DOMAIN, RESPONSE_TYPE,
    SynInstallationRegistry, SynLocalDaemonClient, SynLocalEndpoint,
    _canonical, _receive, _send, _strict_json,
)


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
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


class LocalSynFake:
    """DISPOSABLE TEST SERVER. Not a SYN runtime and not OS keystore custody."""

    def __init__(self, directory: Path) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public = raw_public(self.private)
        self.directory = directory
        self.directory.mkdir(mode=0o700)
        self.path = directory / 'syn-operator.sock'
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(10)
        self.sock.settimeout(.1)
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._serve, daemon=True)
        self.mode = 'operator'
        self.tamper: str | None = None
        self.adoption = None
        self.queries = []
        self.worker.start()

    def endpoint(self) -> SynLocalEndpoint:
        return SynLocalEndpoint(self.path, public_key_hash(self.public), 'operator', os.getuid())

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(.7)
                try:
                    hello = _receive(conn)
                    assert hello['type_ref'] == HELLO_TYPE
                    response = {
                        'schema_version':1, 'type_ref':ATTEST_TYPE,
                        'request_id':hello['request_id'],
                        'challenge':hello['challenge'],
                        'mode':self.mode,
                        'base_public_key':self.public.hex(),
                        'capabilities':['adoption_receipt.get.v1']
                    }
                    if self.tamper == 'hello_nonce': response['challenge'] = 'f'*64
                    signature = self.private.sign(HELLO_DOMAIN + _canonical(response))
                    if self.tamper == 'hello_signature': signature = bytes(64)
                    _send(conn, {'attestation':response, 'signature':signature.hex()})
                    try: q = _receive(conn)
                    except (OSError, StateMismatchError): continue
                    self.queries.append(q)
                    body = {
                        'schema_version':1, 'type_ref':RESPONSE_TYPE,
                        'method':'adoption_receipt.get', 'request_id':q['request_id'],
                        'session_challenge':q['session_challenge'],
                        'offer_sha256':q['offer_sha256'],
                        'state':'confirmed' if self.adoption else 'pending',
                        'receipt':self.adoption.to_dict() if self.adoption else None,
                    }
                    if self.tamper == 'reply_nonce': body['session_challenge'] = 'b'*64
                    if self.tamper == 'reply_offer': body['offer_sha256'] = 'a'*64
                    if self.tamper == 'reply_missing': body['receipt'] = None; body['state'] = 'confirmed'
                    if self.tamper == 'reply_denied': body['state'] = 'denied'; body['receipt'] = None
                    sig = self.private.sign(RESPONSE_DOMAIN + _canonical(body))
                    if self.tamper == 'reply_signature': sig = bytes(64)
                    _send(conn, {'response':body,'signature':sig.hex()})
                except (BrokenPipeError, ConnectionError, OSError, AssertionError, KeyError):
                    pass

    def close(self):
        self.stop.set()
        self.sock.close()
        self.worker.join(timeout=2)
        self.path.unlink(missing_ok=True)


@contextmanager
def fake_syn(tmp_path: Path):
    fake = LocalSynFake(tmp_path/'operator-private')
    try: yield fake
    finally: fake.close()


def registry_file(tmp_path, *endpoints: SynLocalEndpoint):
    directory = tmp_path/'registry'
    directory.mkdir(mode=0o700,exist_ok=True)
    path = directory/'syn-installations.json'
    value = {
        'schema_version':1,
        'type_ref':'synny://type/syn-local-installation-registry-v1',
        'installations':[{'socket':str(e.socket_path),'mode':e.mode,
                          'base_public_key_hash':e.base_public_key_hash.hex(),
                          'owner_uid':e.owner_uid} for e in endpoints],
    }
    path.write_bytes(_canonical(value))
    path.chmod(0o600)
    return path


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'),reason='Linux socket credentials')
def test_pinned_local_identity_discovery_and_challenge_authentication(tmp_path: Path):
    with fake_syn(tmp_path) as fake:
        config = registry_file(tmp_path, fake.endpoint())
        chosen = SynInstallationRegistry(config).discover_operator()
        assert chosen == fake.endpoint()
        status = SynLocalDaemonClient(chosen).attest()
        assert status.mode == 'operator'
        assert status.base_public_key_hash == public_key_hash(fake.public)
        assert status.capabilities == ('adoption_receipt.get.v1',)
        assert fake.queries == []


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'), reason='Linux')
def test_existing_registry_without_syn_is_not_silently_replaced(tmp_path: Path):
    config=registry_file(tmp_path)
    assert SynInstallationRegistry(config).discover_operator() is None
    with pytest.raises(CapabilityUnavailableError,match='registry'):
        SynInstallationRegistry(tmp_path/'not-present'/'syn.json').discover_operator()
    with fake_syn(tmp_path) as fake:
        config=registry_file(tmp_path,fake.endpoint())
        with pytest.raises(ScopeDeniedError,match='pinned'):
            SynInstallationRegistry(config).discover_operator(expected_master_hash=b'Z'*32)
        bad=replace(fake.endpoint(),base_public_key_hash=b'Z'*32)
        with pytest.raises(ScopeDeniedError,match='identity'):
            SynLocalDaemonClient(bad).attest()


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'),reason='Linux')
@pytest.mark.parametrize('fault', ['hello_nonce','hello_signature'])
def test_hello_rejects_wrong_nonce_or_signature(tmp_path: Path, fault):
    with fake_syn(tmp_path) as fake:
        fake.tamper=fault
        with pytest.raises(ScopeDeniedError):
            SynLocalDaemonClient(fake.endpoint()).attest()


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'),reason='Linux')
@pytest.mark.parametrize('fault', ['reply_nonce','reply_offer','reply_signature','reply_missing','reply_denied'])
def test_operator_reply_rejects_replay_tampering_and_absent_receipts(tmp_path: Path, fault):
    with fake_syn(tmp_path) as fake:
        fake.tamper=fault
        fake.adoption=type('FakePacket',(),{'to_dict':lambda self: {'irrelevant':'data'}})()
        with pytest.raises((ScopeDeniedError, StateMismatchError)):
            SynLocalDaemonClient(fake.endpoint()).adoption_receipt(b'D'*32)


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'), reason='Linux')
def test_pending_reply_is_safe_and_contains_no_hardware(tmp_path: Path):
    with fake_syn(tmp_path) as fake:
        assert SynLocalDaemonClient(fake.endpoint()).adoption_receipt(b'D'*32) is None
        assert len(fake.queries)==1
        sent=json.dumps(fake.queries[0])
        assert 'hardware_unique_id' not in sent and 'private_key' not in sent
        assert fake.queries[0]['offer_sha256']=='44'*32


def test_json_framing_rejects_duplicate_keys_and_uncanonical_bytes():
    with pytest.raises(InvalidInputError,match='duplicate'):
        _strict_json(b'{"x":1,"x":2}',1024)
    with pytest.raises(InvalidInputError,match='canonical'):
        _strict_json(b'{"z":1, "a":2}',1024)
    with pytest.raises(InvalidInputError):
        _strict_json(b'[]',1024)
    with pytest.raises(InvalidInputError):
        _strict_json(b'{}\n',1024)


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'), reason='Linux')
def test_registry_file_perms_symlink_and_duplications_fail(tmp_path: Path):
    with fake_syn(tmp_path) as fake:
        config=registry_file(tmp_path,fake.endpoint())
        config.chmod(0o644)
        with pytest.raises(ScopeDeniedError,match='ownership'):
            SynInstallationRegistry(config).discover_operator()
        config.chmod(0o600)
        link=config.parent/'symlink.json'
        link.symlink_to(config)
        with pytest.raises(OSError):
            SynInstallationRegistry(link).discover_operator()
        config=registry_file(tmp_path,fake.endpoint(),fake.endpoint())
        with pytest.raises(StateMismatchError,match='duplicate'):
            SynInstallationRegistry(config).discover_operator()


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'), reason='Linux')
def test_socket_symlink_and_bad_mode_fail_closed(tmp_path: Path):
    with fake_syn(tmp_path) as fake:
        link=fake.path.parent/'alias.sock'
        link.symlink_to(fake.path)
        with pytest.raises(InvalidInputError):
            SynLocalEndpoint(link, public_key_hash(fake.public), 'operator', os.getuid())
        fake.path.parent.chmod(0o777)
        with pytest.raises(ScopeDeniedError):
            SynLocalDaemonClient(fake.endpoint()).attest()
        fake.path.parent.chmod(0o700)


def test_registry_never_generates_new_keys_or_calls_an_installer(tmp_path: Path):
    # It reports absence only from an independently protected inventory.
    config=registry_file(tmp_path)
    assert not hasattr(SynInstallationRegistry(config),'init')
    assert not hasattr(SynInstallationRegistry(config),'generate_keypair')
    assert not hasattr(SynLocalDaemonClient,'export_private_key')


@pytest.mark.skipif(os.name!='posix' or not hasattr(socket,'SO_PEERCRED'),reason='Linux')
def test_startup_planner_requires_real_syn_external_pledge_and_keystore(tmp_path: Path):
    from potbot.tools.syn_local_daemon import SynStartupPlanner
    with fake_syn(tmp_path) as fake:
        registry=SynInstallationRegistry(registry_file(tmp_path, fake.endpoint()))
        planner=SynStartupPlanner(registry)
        first=planner.potbot_first_run()
        assert first.state=='external_syn_pledge_required'
        assert first.verified_local_identity
        assert first.operator_master_hash==fake.endpoint().base_public_key_hash.hex()
        new_agent=planner.new_agent(target_master_hash=fake.endpoint().base_public_key_hash)
        assert new_agent.state=='external_syn_agent_install_required'
        assert new_agent.required_mode=='agent'
        assert not new_agent.verified_local_identity
        assert not hasattr(planner,'create_private_key')
        registry_file(tmp_path)
        assert planner.potbot_first_run().state=='external_syn_operator_install_required'


def test_registry_and_signed_daemon_wire_schema(tmp_path: Path):
    import jsonschema
    schema=json.loads((Path(__file__).resolve().parents[2]/'vocab'/'syn-local-daemon.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    config=registry_file(tmp_path)
    jsonschema.validate(json.loads(config.read_text()),schema)
    fake=Ed25519PrivateKey.generate()
    public=raw_public(fake)
    hello={"schema_version":1,"type_ref":HELLO_TYPE,"request_id":str(uuid4()),
           "challenge":'3'*64,"expected_base_public_key_hash":public_key_hash(public).hex(),"mode":"operator"}
    jsonschema.validate(hello,schema)
    attest={"schema_version":1,"type_ref":ATTEST_TYPE,"request_id":hello['request_id'],
            "challenge":hello['challenge'],"mode":"operator", "base_public_key":public.hex(),"capabilities":[]}
    jsonschema.validate({"attestation":attest,"signature":fake.sign(HELLO_DOMAIN+_canonical(attest)).hex()},schema)
    assert not jsonschema.Draft202012Validator(schema).is_valid({**hello,'seed':'forbidden'})
