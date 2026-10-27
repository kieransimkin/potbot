"""SYN Python operator custody: offline state/security fixtures, not CIP-3 certification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from syn_node import custody, hd_keys
from syn_node.cli import _configure
from syn_node.protocol import SynProtocolError


class FakeKeyring:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_after: int | None = None
        self.writes = 0

    def get_password(self, service: str, account: str) -> str | None:
        return self.entries.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.writes += 1
        if self.fail_after == self.writes:
            raise OSError("simulated OS keystore disconnect")
        self.entries[service, account] = value


class FakeExtendedKey:
    """Fixtures use *ordinary* Ed25519, NEVER claims to be BIP32."""

    def __init__(self, payload: bytes):
        if len(payload) != 128:
            raise ValueError("wrong synthetic extended payload")
        self.seed = payload[:32]
        self.public_key = payload[64:96]
        self.chain_code = payload[96:]
        if (self._private().public_key().public_bytes(serialization.Encoding.Raw,
                serialization.PublicFormat.Raw) != self.public_key):
            raise ValueError("mismatched synthetic child")

    def _private(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.seed)

    def sign(self, payload: bytes) -> bytes:
        return self._private().sign(payload)


class FakeWallet:
    """Only simulates HD-wallet *API shape*; no actual CIP-3 algorithm."""

    def __init__(self, entropy: bytes, path: str = "m"):
        self.entropy = entropy
        self.path = path
        key = hashlib.sha256(entropy + path.encode() + b"syn-fixture-seed").digest()
        private = Ed25519PrivateKey.from_private_bytes(key)
        self.xprivate_key = key + hashlib.sha256(key).digest()
        self.public_key = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.chain_code = hashlib.sha256(path.encode() + entropy + b"chain").digest()

    @classmethod
    def from_entropy(cls, raw: str, passphrase: str = "") -> FakeWallet:
        if passphrase != "" or len(bytes.fromhex(raw)) != 32:
            raise ValueError("Icarus entropy contract violated")
        return cls(bytes.fromhex(raw))

    def derive_from_path(self, path: str) -> FakeWallet:
        assert path.startswith("m/6969'/420'")
        return FakeWallet(self.entropy, path)


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    keyring = FakeKeyring()
    monkeypatch.setattr(custody, "_native_backend", lambda: keyring)
    monkeypatch.setattr(hd_keys, "_native_backend", lambda: keyring)
    monkeypatch.setattr(hd_keys, "_pycardano", lambda: (FakeWallet, FakeExtendedKey))
    lock = tmp_path / "locks"
    lock.mkdir(mode=0o700)
    return keyring, lock


def create(installed):
    keyring, lock = installed
    result = hd_keys.create_operator_identity(
        service="syn.operator", account="fixture", lock_dir=lock)
    return result


def test_paths_and_public_root_vs_base(installed):
    public = create(installed)
    assert public["root_path"] == "m/6969'/420'"
    assert public["base_path"] == "m/6969'/420'/0'/0/0"
    assert public["root_public_key_hash"] != public["base_public_key_hash"]
    assert public["base_public_key_hash"] == hashlib.sha256(
        bytes.fromhex(public["base_public_key"])).hexdigest()
    for forbidden in ("entropy", "private", "xprivate", "seed", "chain", "mnemonic"):
        assert forbidden not in str(public)
    assert len(installed[0].entries) == 8
    assert all(isinstance(v, str) for v in installed[0].entries.values())
    assert all(account.startswith("fixture.operator.") for _, account in installed[0].entries)


def test_unique_entropy_and_fail_closed_duplicate(installed):
    first = create(installed)
    with pytest.raises(SynProtocolError, match="exists"):
        create(installed)
    assert first == hd_keys._load_operator("syn.operator", "fixture")[1]


def test_os_keystore_mid_write_failure_retains_tombstone(installed):
    keyring, lock = installed
    keyring.fail_after = 4
    with pytest.raises(SynProtocolError, match="write failed"):
        create(installed)
    assert keyring.entries
    assert ("syn.operator", "fixture.operator.manifest.v1") not in keyring.entries
    with pytest.raises(SynProtocolError, match="incomplete"):
        create(installed)
    with pytest.raises(SynProtocolError, match="not completed"):
        hd_keys._load_operator("syn.operator", "fixture")


def test_tampered_root_private_data_refuses_every_signature(installed):
    public = create(installed)
    signer = hd_keys.PythonHdOperatorSigner(
        "syn.operator", "fixture", bytes.fromhex(public["base_public_key_hash"]))
    payload = b"syn.local.attestation.v1\0fixture"
    signature = signer.sign(payload)
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(public["base_public_key"])).verify(
        signature, payload)
    installed[0].entries["syn.operator", "fixture.operator.root_xprivate.v1"] = "ff" * 64
    with pytest.raises(SynProtocolError, match="differ"):
        signer.sign(payload)


def test_corrupted_manifest_cannot_override_key_bytes(installed):
    create(installed)
    installed[0].entries["syn.operator", "fixture.operator.manifest.v1"] = "{}"
    with pytest.raises(SynProtocolError, match="fields"):
        hd_keys._load_operator("syn.operator", "fixture")


def test_wrong_expected_operator_identity_fails(installed):
    create(installed)
    with pytest.raises(SynProtocolError, match="pinned"):
        hd_keys.PythonHdOperatorSigner("syn.operator", "fixture", b"z" * 32)


@pytest.mark.parametrize("bad", [-1, 16_777_216, True, None, "10", 0.5])
def test_invalid_inheritance_rejected(installed, bad):
    public = create(installed)
    signer = hd_keys.PythonHdOperatorSigner("syn.operator", "fixture",
                                           bytes.fromhex(public["base_public_key_hash"]))
    with pytest.raises(SynProtocolError, match="24-bit"):
        signer.derive_candidate_public_hashes(bad)


def test_operator_eight_candidate_full_hashes_and_distinct(installed):
    public = create(installed)
    signer = hd_keys.PythonHdOperatorSigner("syn.operator", "fixture",
                                           bytes.fromhex(public["base_public_key_hash"]))
    one = signer.derive_candidate_public_hashes(9214)
    assert len(one) == len(set(one)) == 8
    assert all(type(item) is bytes and len(item) == 32 for item in one)
    assert signer.derive_candidate_public_hashes(9214) == one
    assert signer.derive_candidate_public_hashes(9215) != one
    assert signer.derive_candidate_public_hashes(9214)[0] != one[1]


def test_operator_signing_requires_bounded_input(installed):
    public = create(installed)
    signer = hd_keys.PythonHdOperatorSigner("syn.operator", "fixture",
                                           bytes.fromhex(public["base_public_key_hash"]))
    for bad in (b"", b"x" * 65537, "a string", None):
        with pytest.raises(SynProtocolError, match="bounded"):
            signer.sign(bad)


def test_missing_real_pycardano_fails_closed(monkeypatch, installed):
    monkeypatch.undo()
    if hd_keys._pycardano.__module__ == "syn_node.hd_keys":
        import importlib.util
        if importlib.util.find_spec("pycardano") is None:
            with pytest.raises(SynProtocolError, match="pinned pycardano"):
                hd_keys._pycardano()


def test_cli_version2_operator_signer_from_os_keystore(installed, tmp_path):
    public = create(installed)
    private = tmp_path / "settings"
    private.mkdir(mode=0o700)
    config = private / "node.json"
    config.write_text(json.dumps({
        "schema_version": 2, "type_ref": "synny://type/syn-local-service-config-v2",
        "mode": "operator", "socket": str(private / "sock"),
        "registry": str(private / "registry.json"), "receipt_db": None,
        "expected_base_public_key_hash": public["base_public_key_hash"],
        "keyring_service": "syn.operator", "keyring_account": "fixture",
    }, sort_keys=True, separators=(",", ":")))
    config.chmod(0o600)
    node, registry = _configure(config, read_only=True)
    assert node.master_hash.hex() == public["base_public_key_hash"]
    assert registry == private / "registry.json"
    assert not (private / "sock").exists()
    assert not (private / "registry.json").exists()


def test_cli_version2_rejects_mixed_native_keys(installed, tmp_path):
    public = create(installed)
    private = tmp_path / "settings"
    private.mkdir(mode=0o700)
    config = private / "bad.json"
    config.write_text(json.dumps({
        "schema_version": 2, "type_ref": "synny://type/syn-local-service-config-v2",
        "mode": "operator", "socket": str(private / "sock"),
        "registry": str(private / "registry.json"), "receipt_db": None,
        "expected_base_public_key_hash": public["base_public_key_hash"],
        "keyring_service": "syn.operator", "keyring_account": "fixture",
        "native_signer_executable": "/tmp/unsafe",
    }, sort_keys=True, separators=(",", ":")))
    config.chmod(0o600)
    with pytest.raises(SynProtocolError, match="fields"):
        _configure(config, read_only=True)


def test_explicit_python_library_absent_or_wrong_version_is_blocking(monkeypatch):
    # Real-library qualification happens separately; never test with a substitute silently.
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "0.0.0")
    with pytest.raises(SynProtocolError, match="pinned"):
        hd_keys._pycardano()  # import may fail earlier in an offline environment


def test_fixed_entropy_real_pycardano_cross_language_vector_if_installed():
    pytest.importorskip("pycardano")
    # Reference identity vector is published by the independent native Potbot
    # Ed25519-BIP32 implementation. This test is a real-crypto gate, not a fake.
    value = hd_keys._derive(bytes(range(32)))
    assert len(value["root_public"]) == len(value["base_public"]) == 32
    assert value["root_public"] != value["base_public"]
    _, ExtendedSigningKey = hd_keys._pycardano()
    signer = ExtendedSigningKey(value["base_xprivate"] + value["base_public"] + value["base_chain"])
    msg = b"Syn Python Icarus V2 independent signature"
    Ed25519PublicKey.from_public_bytes(value["base_public"]).verify(signer.sign(msg), msg)


def test_multiple_operator_creations_are_serialized(installed):
    from concurrent.futures import ThreadPoolExecutor

    def attempt() -> bool:
        try:
            create(installed)
            return True
        except SynProtocolError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(lambda _: attempt(), range(8)))
    assert result.count(True) == 1
    assert result.count(False) == 7
    assert hd_keys._load_operator("syn.operator", "fixture")[1]["profile"] == hd_keys.PROFILE


def test_no_os_backend_refuses_entropy_creation(monkeypatch, tmp_path):
    def locked():
        raise SynProtocolError("credential backend unavailable")

    monkeypatch.setattr(hd_keys, "_native_backend", locked)
    lock = tmp_path / "locks"
    lock.mkdir(mode=0o700)
    with pytest.raises(SynProtocolError, match="unavailable"):
        hd_keys.create_operator_identity(service="syn.operator", account="device", lock_dir=lock)


def test_eight_candidate_abbreviation_collision_blocks_enrollment(installed, monkeypatch):
    public = create(installed)
    signer = hd_keys.PythonHdOperatorSigner("syn.operator", "fixture",
                                           bytes.fromhex(public["base_public_key_hash"]))

    class CollidingWallet(FakeWallet):
        def derive_from_path(self, path):
            if path.startswith("m/6969'/420'/42'/"):
                return FakeWallet(self.entropy, "m/same-agent")
            return super().derive_from_path(path)

    monkeypatch.setattr(hd_keys, "_pycardano", lambda: (CollidingWallet, FakeExtendedKey))
    with pytest.raises(SynProtocolError, match="collision"):
        signer.derive_candidate_public_hashes(42)


def test_cli_new_operator_never_prints_private_material(installed, capsys):
    from syn_node.cli import main

    result = main(["new-operator", "--keyring-service", "syn.operator",
                   "--keyring-account", "fixture", "--lock-dir", str(installed[1])])
    assert result == 0
    output = capsys.readouterr().out
    body = json.loads(output)
    assert body["base_path"] == hd_keys.BASE_PATH
    assert body["root_path"] == hd_keys.ROOT_PATH
    for secret in ("private", "entropy", "chain", "mnemonic", "seed"):
        assert secret not in output
    assert main(["new-operator", "--keyring-service", "syn.operator",
                 "--keyring-account", "fixture", "--lock-dir", str(installed[1])]) == 2


def test_versioned_public_identity_and_configuration_schema(installed):
    schema_lib = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "operator-keystore-v2.schema.json"
    schema = json.loads(schema_path.read_text())
    schema_lib.Draft202012Validator.check_schema(schema)
    public = create(installed)
    schema_lib.validate(public, schema)
    bad = {**public, "root_path": "m/6969/420'"}
    assert not schema_lib.Draft202012Validator(schema).is_valid(bad)
    config = {
        "schema_version": 2, "type_ref": "synny://type/syn-local-service-config-v2",
        "mode": "operator", "socket": "/syn/ops.sock", "registry": "/syn/registry.json",
        "receipt_db": "/syn/receipts.db",
        "expected_base_public_key_hash": public["base_public_key_hash"],
        "keyring_service": "syn.operator", "keyring_account": "fixture",
    }
    schema_lib.validate(config, schema)
    config["native_signer_sha256"] = "00" * 32
    assert not schema_lib.Draft202012Validator(schema).is_valid(config)
