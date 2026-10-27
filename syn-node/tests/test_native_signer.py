"""Versioned, executable-pinned external SYN signer boundary (test-only binary)."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from syn_node.custody import NativeOperatorSigner
from syn_node.protocol import SynProtocolError


def make_test_signer(tmp_path: Path) -> tuple[Path, bytes, str]:
    """Single-use fixture signer with literal secret in TEMPORARY TEST CODE ONLY."""
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption()).hex()
    pub = private.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    code = f'''#!{sys.executable}
import json, sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex({seed!r}))
public = private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
request = json.loads(sys.stdin.buffer.read())
method = request["method"]
signature = private.sign(bytes.fromhex(request["payload_hex"])).hex() if method == "sign" else None
reply = {{"schema_version":1,"type_ref":"synny://type/syn-native-keystore-sign-result-v1", "method":method, "public_key":public.hex(), "signature":signature}}
sys.stdout.buffer.write(json.dumps(reply, sort_keys=True, separators=(',',':')).encode())
'''
    file = tmp_path / "syn-native-test-helper"
    file.write_text(code)
    file.chmod(0o700)
    return file, hashlib.sha256(pub).digest(), hashlib.sha256(file.read_bytes()).hexdigest()


def test_pinned_native_signer_public_and_actual_signatures(tmp_path):
    binary, expected, pin = make_test_signer(tmp_path)
    signer = NativeOperatorSigner(binary, pin, expected)
    signature = signer.sign(b"syn.local.attestation.v1\0example")
    assert len(signature) == 64 and hashlib.sha256(signer.public_key).digest() == expected
    binary.write_text(binary.read_text() + "\n# tampered\n")
    with pytest.raises(SynProtocolError, match="differs"):
        signer.sign(b"challenge")


def test_pinned_native_signer_rejects_incorrect_identity_or_writable_file(tmp_path):
    binary, expected, pin = make_test_signer(tmp_path)
    with pytest.raises(SynProtocolError, match="identity"):
        NativeOperatorSigner(binary, pin, b'Z' * 32)
    binary.chmod(0o777)
    with pytest.raises(SynProtocolError, match="writable"):
        NativeOperatorSigner(binary, pin, expected)


def test_pinned_native_signer_cannot_process_unbounded_or_unknown_signing_requests(tmp_path):
    binary, expected, pin = make_test_signer(tmp_path)
    signer = NativeOperatorSigner(binary, pin, expected)
    with pytest.raises(SynProtocolError, match="not admitted"):
        signer._call("export", b"secret")
    with pytest.raises(SynProtocolError, match="not admitted"):
        signer.sign(b"a" * 65_537)


def test_sandboxed_native_signer_preflight_does_not_create_receipt_db(tmp_path):
    import json
    import os

    from syn_node.cli import _configure

    binary, expected, pin = make_test_signer(tmp_path)
    private = tmp_path / "settings"
    private.mkdir(mode=0o700)
    sock = private / "syn.sock"
    receipts = private / "new-receipts.sqlite"
    config = private / "operator.json"
    config.write_bytes(json.dumps({
        "schema_version": 1, "type_ref": "synny://type/syn-local-service-config-v1",
        "mode": "operator", "socket": str(sock), "registry": str(private / "registry.json"),
        "receipt_db": str(receipts), "native_signer_executable": str(binary),
        "native_signer_sha256": pin, "expected_base_public_key_hash": expected.hex(),
    }, sort_keys=True, separators=(",", ":")).encode())
    config.chmod(0o600)
    service, registry = _configure(config, read_only=True)
    assert service.master_hash == expected
    assert not sock.exists() and not receipts.exists() and not registry.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="qualified Unix process integration only")
def test_separate_syn_daemon_process_attests_and_reattaches_after_restart(tmp_path):
    """Real process, real socket and real Phase 42 client; signer remains fixture-only."""
    import json
    import subprocess
    import time

    from potbot.tools.syn_local_daemon import SynInstallationRegistry, SynLocalDaemonClient

    binary, expected, pin = make_test_signer(tmp_path)
    directory = tmp_path / "external-syn"
    directory.mkdir(mode=0o700)
    sock = directory / "syn.sock"
    registry = directory / "installations.json"
    cfg = directory / "operator.json"
    cfg.write_text(json.dumps({
        "schema_version": 1, "type_ref": "synny://type/syn-local-service-config-v1",
        "mode": "operator", "socket": str(sock), "registry": str(registry),
        "receipt_db": str(directory / "receipts.sqlite"),
        "native_signer_executable": str(binary), "native_signer_sha256": pin,
        "expected_base_public_key_hash": expected.hex(),
    }, sort_keys=True, separators=(",", ":")))
    cfg.chmod(0o600)
    for _ in range(2):
        daemon = subprocess.Popen([sys.executable, "-m", "syn_node", "serve", "--config", str(cfg)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(150):
                if sock.exists() and registry.exists():
                    break
                if daemon.poll() is not None:
                    raise AssertionError("SYN daemon stopped before installing local socket")
                time.sleep(0.02)
            endpoint = SynInstallationRegistry(registry).discover_operator()
            assert endpoint is not None
            attestation = SynLocalDaemonClient(endpoint).attest()
            assert attestation.base_public_key_hash == expected
            assert attestation.capabilities == ("adoption_receipt.get.v1",)
            assert SynLocalDaemonClient(endpoint).adoption_receipt(os.urandom(32)) is None
        finally:
            daemon.terminate()
            _stdout, err = daemon.communicate(timeout=5)
            assert daemon.returncode == 0, err.decode(errors="replace")
            assert not sock.exists()  # no stale-socket replacement on restart
