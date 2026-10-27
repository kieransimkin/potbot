"""SYN Phase 45: fail-closed BIP-39 recovery and private console boundaries.

The fake HD provider and fake BIP-39 codec below verify orchestration ONLY.
`test_real_bip39_checksum_vector` exercises the installed genuine library and
is a mandatory, unskipped job in the Python HD CI workflow.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from syn_node import cli, custody, hd_keys
from syn_node.protocol import SynProtocolError


class MemoryNativeKeyring:
    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}
        self.fail_after: int | None = None
        self.writes = 0

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, password):
        self.writes += 1
        if self.fail_after == self.writes:
            raise OSError("fixture keyring disconnected")
        self.data[service, account] = password


class StubWallet:
    """Disposable, ordinary seed-key fixture, NOT an Ed25519-BIP32 oracle."""

    def __init__(self, entropy: bytes, path: str = "m"):
        self._entropy = entropy
        seed = hashlib.sha256(entropy + path.encode("ascii") + b"fixture-only").digest()
        key = Ed25519PrivateKey.from_private_bytes(seed)
        self.xprivate_key = seed + hashlib.sha256(seed).digest()
        self.public_key = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        self.chain_code = hashlib.sha256(entropy + path.encode("ascii") + b"chain").digest()

    @classmethod
    def from_entropy(cls, entropy_hex: str, passphrase: str = ""):
        assert passphrase == ""
        return cls(bytes.fromhex(entropy_hex))

    def derive_from_path(self, path: str):
        assert path.startswith(hd_keys.ROOT_PATH)
        return type(self)(self._entropy, path)


class StubExtendedSigner:
    def __init__(self, payload):
        self.private = Ed25519PrivateKey.from_private_bytes(payload[:32])

    def sign(self, message):
        return self.private.sign(message)


class StubPhraseCodec:
    """Phrase-shaped fake that is explicitly NOT a BIP-39 implementation."""

    def __init__(self):
        self.pairs: dict[str, bytes] = {}

    def to_mnemonic(self, entropy):
        # Strictly lowercase ASCII, 24 words, synthetic per-entropy mapping.
        letters = "abcdefghijklmnop"
        digest = hashlib.sha256(entropy).hexdigest()
        phrase = " ".join("word" + "".join(letters[int(x, 16)] for x in digest[i:i+2])
                          for i in range(0, 48, 2))
        self.pairs[phrase] = entropy
        return phrase

    def check(self, phrase):
        return phrase in self.pairs

    def to_entropy(self, phrase):
        return self.pairs[phrase]


@pytest.fixture
def backend(monkeypatch, tmp_path: Path):
    native = MemoryNativeKeyring()
    phrase = StubPhraseCodec()
    monkeypatch.setattr(custody, "_native_backend", lambda: native)
    monkeypatch.setattr(hd_keys, "_native_backend", lambda: native)
    monkeypatch.setattr(hd_keys, "_pycardano", lambda: (StubWallet, StubExtendedSigner))
    monkeypatch.setattr(hd_keys, "_mnemonic", lambda: phrase)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(mode=0o700)
    return native, phrase, lock_dir


def _new(backend):
    return hd_keys.create_operator_identity(
        service="syn.operator", account="operator-A", lock_dir=backend[2]
    )


def _export(backend, public):
    return hd_keys.operator_recovery_words(
        service="syn.operator", account="operator-A",
        expected_base_public_key_hash=public["base_public_key_hash"],
    )


def _restore(backend, public, phrase, account="operator-B"):
    return hd_keys.recover_operator_identity(
        service="syn.operator", account=account, lock_dir=backend[2],
        words=phrase, expected_base_public_key_hash=public["base_public_key_hash"],
    )


def test_phrase_restores_original_root_and_base_not_new_identity(backend):
    public = _new(backend)
    phrase = _export(backend, public)
    assert len(phrase.split(" ")) == 24
    assert _restore(backend, public, phrase) == public
    assert len(backend[0].data) == 16
    assert all(phrase not in field for field in backend[0].data.values())
    assert set(public) == {
        "schema_version", "type_ref", "profile", "root_path", "base_path",
        "root_public_key", "base_public_key", "root_public_key_hash", "base_public_key_hash",
    }


def test_restoration_requires_existing_independent_base_hash_before_any_write(backend):
    public = _new(backend)
    phrase = _export(backend, public)
    with pytest.raises(SynProtocolError, match="independently pinned"):
        _restore(backend, {**public, "base_public_key_hash": "ff" * 32}, phrase)
    assert all(not account.startswith("operator-B.") for _, account in backend[0].data)


@pytest.mark.parametrize("wrong", [
    "", "hello", "abandon " * 23, "abandon " * 24, "abandon " * 23 + "art\n",
    "ABANDON " * 23 + "art", "abandon " * 23 + "🍎",
    "abandon " * 23 + "art " + "leak",
])
def test_recovery_rejects_noncanonical_or_invalid_phrase_without_writes(backend, wrong):
    public = _new(backend)
    with pytest.raises(SynProtocolError):
        _restore(backend, public, wrong)
    assert len(backend[0].data) == 8


def test_recovery_rejects_invalid_checksum_with_valid_word_count(backend):
    public = _new(backend)
    phrase = " ".join(["abandon"] * 23 + ["art"])
    with pytest.raises(SynProtocolError, match="checksum"):
        _restore(backend, public, phrase)
    assert len(backend[0].data) == 8


def test_existing_or_incomplete_keystore_never_gets_replaced(backend):
    public = _new(backend)
    phrase = _export(backend, public)
    with pytest.raises(SynProtocolError, match="exists or installation is incomplete"):
        _restore(backend, public, phrase, account="operator-A")
    backend[0].data["syn.operator", "operator-B.operator.entropy.v1"] = "00" * 32
    with pytest.raises(SynProtocolError, match="exists or installation is incomplete"):
        _restore(backend, public, phrase)
    assert len(backend[0].data) == 9


def test_partial_recovery_keeps_tombstone_and_does_not_silently_retry(backend):
    public = _new(backend)
    phrase = _export(backend, public)
    backend[0].fail_after = backend[0].writes + 4
    with pytest.raises(SynProtocolError, match="write failed"):
        _restore(backend, public, phrase)
    writes = backend[0].writes
    with pytest.raises(SynProtocolError, match="exists or installation is incomplete"):
        _restore(backend, public, phrase)
    assert backend[0].writes == writes
    with pytest.raises(SynProtocolError, match="not completed"):
        hd_keys._load_operator("syn.operator", "operator-B")


def test_export_refuses_mismatched_identity_and_tampered_entropy(backend):
    public = _new(backend)
    with pytest.raises(SynProtocolError, match="pinned"):
        hd_keys.operator_recovery_words(
            service="syn.operator", account="operator-A",
            expected_base_public_key_hash="00" * 32,
        )
    backend[0].data["syn.operator", "operator-A.operator.entropy.v1"] = "12" * 32
    with pytest.raises(SynProtocolError, match="differ"):
        _export(backend, public)


def test_concurrent_recover_only_one_wins(backend):
    public = _new(backend)
    phrase = _export(backend, public)

    def attempt(_):
        try:
            return _restore(backend, public, phrase)
        except SynProtocolError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(x is not None for x in results) == 1
    assert hd_keys._load_operator("syn.operator", "operator-B")[1] == public


class StubTerminal:
    def __init__(self, confirmation: str):
        self.confirmation = confirmation
        self.written = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def write(self, value):
        self.written.append(value)

    def readline(self):
        return self.confirmation + "\n"

    def flush(self):
        return None


def _args(command, base, lock_dir):
    args = [command, "--keyring-service", "syn.operator", "--keyring-account",
            "operator-A" if command == "show-recovery" else "operator-B",
            "--expected-base-public-key-hash", base]
    if command == "recover-operator":
        args += ["--lock-dir", str(lock_dir)]
    return args


def test_cli_recovery_shows_words_only_to_explicit_terminal(backend, monkeypatch, capsys):
    public = _new(backend)
    phrase = _export(backend, public)
    terminal = StubTerminal("SHOW RECOVERY")
    monkeypatch.setattr(cli, "_operator_tty", lambda: terminal)
    assert cli.main(_args("show-recovery", public["base_public_key_hash"], backend[2])) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert phrase in "".join(terminal.written)
    assert "private" not in out.out


def test_cli_no_terminal_confirmation_no_recovery(backend, monkeypatch, capsys):
    public = _new(backend)
    terminal = StubTerminal("WRONG PHRASE")
    monkeypatch.setattr(cli, "_operator_tty", lambda: terminal)
    assert cli.main(_args("show-recovery", public["base_public_key_hash"], backend[2])) == 2
    assert "blocked" in capsys.readouterr().err
    assert all("RECOVERY WORDS" not in part for part in terminal.written)


def test_cli_restore_never_uses_argv_or_stdout_for_phrase(backend, monkeypatch, capsys):
    public = _new(backend)
    phrase = _export(backend, public)
    terminal = StubTerminal("RESTORE IDENTITY")
    monkeypatch.setattr(cli, "_operator_tty", lambda: terminal)
    monkeypatch.setattr(cli, "_read_recovery_phrase", lambda _: phrase)
    assert cli.main(_args("recover-operator", public["base_public_key_hash"], backend[2])) == 0
    output = capsys.readouterr().out
    assert phrase not in output
    assert json.loads(output) == public


def test_cli_unrecognized_secret_argument_is_redacted(backend, capsys):
    secret = "SYN-FAKE-SECRET-ARGUMENT"
    with pytest.raises(SystemExit) as out:
        cli.main(["show-recovery", "--phrase", secret])
    assert out.value.code == 2
    assert secret not in capsys.readouterr().err


def test_real_bip39_checksum_vector():
    """Mandatory in live-provider CI; may skip in isolated offline fixture."""
    pytest.importorskip("mnemonic")
    codec = hd_keys._mnemonic()
    zero = bytes(32)
    phrase = " ".join(["abandon"] * 23 + ["art"])
    assert codec.to_mnemonic(zero) == phrase
    assert hd_keys._entropy_from_phrase(phrase) == zero
    invalid = " ".join(["abandon"] * 23 + ["abandon"])
    with pytest.raises(SynProtocolError, match="checksum"):
        hd_keys._entropy_from_phrase(invalid)


def test_local_operator_functional_smoke_is_public_only(backend, tmp_path, capsys):
    public = _new(backend)
    folder = tmp_path / "config"
    folder.mkdir(mode=0o700)
    cfg = folder / "syn.json"
    cfg.write_text(json.dumps({
        "schema_version": 2, "type_ref": "synny://type/syn-local-service-config-v2",
        "mode": "operator", "socket": str(folder / "operator.sock"),
        "registry": str(folder / "registry.json"), "receipt_db": None,
        "expected_base_public_key_hash": public["base_public_key_hash"],
        "keyring_service": "syn.operator", "keyring_account": "operator-A",
    }, sort_keys=True, separators=(",", ":")))
    cfg.chmod(0o600)
    assert cli.main(["qualify-operator", "--config", str(cfg)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["base_public_key_hash"] == public["base_public_key_hash"]
    assert data["functional_local_signature"] == "verified"
    assert data["qualification"] == "local-software-smoke-only"
    assert data["agent_adoption_authorized"] is False
    assert "secret" not in str(data)
    assert len(backend[0].data) == 8
    assert not (folder / "operator.sock").exists()
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "operator-functional-qualification-v1.schema.json"
    schema = json.loads(schema_path.read_text())
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(data, schema)


def test_operator_functional_smoke_refuses_agent_profile(backend, tmp_path, capsys):
    _new(backend)
    private = tmp_path / "config"
    private.mkdir(mode=0o700)
    cfg = private / "agent.json"
    cfg.write_text(json.dumps({
        "schema_version": 2, "type_ref": "synny://type/syn-local-service-config-v2",
        "mode": "agent", "socket": str(private / "agent.sock"),
        "registry": str(private / "registry.json"), "receipt_db": None,
        "expected_base_public_key_hash": "00" * 32,
        "keyring_service": "syn.operator", "keyring_account": "operator-A",
    }, sort_keys=True, separators=(",", ":")))
    cfg.chmod(0o600)
    # This config cannot qualify as an HD operator, regardless of key material.
    assert cli.main(["qualify-operator", "--config", str(cfg)]) == 2
    assert "blocked" in capsys.readouterr().err
