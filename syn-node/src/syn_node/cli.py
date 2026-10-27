"""Standalone SYN local service CLI; deliberately independent from Potbot."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .custody import AgentTemporaryOsSigner, NativeOperatorSigner, create_agent_temporary_key
from .hd_keys import (
    PythonHdOperatorSigner, create_operator_identity, operator_recovery_words,
    recover_operator_identity,
)
from .local_service import SynLocalServer, create_registry_entry, private_directory, publish_registry
from .protocol import SynProtocolError, exact, hex_bytes, parse
from .receipts import PublicAdoptionJournal

class _RedactedArgumentParser(argparse.ArgumentParser):
    """Never echo unrecognised CLI argument values (possibly recovery words)."""

    def error(self, _message: str) -> None:
        print("SYN command-line arguments were rejected", file=sys.stderr)
        raise SystemExit(2)


CONFIG_TYPE = "synny://type/syn-local-service-config-v1"
CONFIG_V2 = "synny://type/syn-local-service-config-v2"


def _private_json(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise SynProtocolError("SYN operator configuration path must be absolute and regular")
    private_directory(path.parent)
    st = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_nlink != 1:
        raise SynProtocolError("SYN operator input is not an owner-private file")
    if not 0 < st.st_size <= 65_536:
        raise SynProtocolError("SYN operator file has invalid length")
    return parse(path.read_bytes())


def _configure(path: Path, *, read_only: bool = False) -> tuple[SynLocalServer, Path]:
    raw = _private_json(path)
    revision = raw.get("schema_version")
    if type(revision) is not int:
        raise SynProtocolError("SYN configuration revision must be an exact integer")
    if revision == 1:
        fields = exact(raw, {
            "schema_version", "type_ref", "mode", "socket", "registry", "receipt_db",
            "expected_base_public_key_hash", "native_signer_executable", "native_signer_sha256",
        }, "SYN v1 local daemon configuration")
        if fields["type_ref"] != CONFIG_TYPE:
            raise SynProtocolError("SYN local daemon config does not match v1")
    elif revision == 2:
        fields = exact(raw, {
            "schema_version", "type_ref", "mode", "socket", "registry", "receipt_db",
            "expected_base_public_key_hash", "keyring_service", "keyring_account",
        }, "SYN Python local daemon configuration")
        if fields["type_ref"] != CONFIG_V2:
            raise SynProtocolError("SYN local daemon config does not match v2")
    else:
        raise SynProtocolError("SYN local daemon config revision is unknown")
    if fields["mode"] not in {"operator", "agent"}:
        raise SynProtocolError("SYN daemon mode must be operator or agent")
    for name in ("socket", "registry"):
        if not isinstance(fields[name], str) or not Path(fields[name]).is_absolute():
            raise SynProtocolError(f"SYN {name} must be an exact absolute path")
    if fields["receipt_db"] is not None and (
        not isinstance(fields["receipt_db"], str)
        or not Path(fields["receipt_db"]).is_absolute()
    ):
        raise SynProtocolError("SYN receipt journal path must be absolute or disabled")
    expected = hex_bytes(fields["expected_base_public_key_hash"], 32, "base hash")
    if revision == 1:
        if (not isinstance(fields["native_signer_executable"], str)
                or not Path(fields["native_signer_executable"]).is_absolute()):
            raise SynProtocolError("SYN native v1 signer must be an absolute path")
        signer = NativeOperatorSigner(Path(fields["native_signer_executable"]),
                                      fields["native_signer_sha256"], expected)
    elif fields["mode"] == "operator":
        signer = PythonHdOperatorSigner(fields["keyring_service"],
                                        fields["keyring_account"], expected)
    else:
        signer = AgentTemporaryOsSigner(fields["keyring_service"],
                                        fields["keyring_account"], expected)
    journal = None
    if fields["mode"] == "operator" and fields["receipt_db"] is not None and not read_only:
        journal = PublicAdoptionJournal(Path(fields["receipt_db"]), expected_master_hash=expected)
    if fields["mode"] == "agent" and fields["receipt_db"] is not None:
        raise SynProtocolError("agent installation cannot serve the operator receipt journal")
    service = SynLocalServer(socket_path=Path(fields["socket"]), mode=fields["mode"],
                             signer=signer, receipts=journal)
    return service, Path(fields["registry"])


def _operator_tty() -> Any:
    """The operator's real console; never silently fall back to redirected stdin."""
    if os.name != "posix":
        raise SynProtocolError("SYN recovery console requires separately qualified host input")
    try:
        handle = open("/dev/tty", "r+", encoding="utf-8")
    except OSError as exc:
        raise SynProtocolError("SYN recovery requires an attached controlling terminal") from exc
    if not handle.isatty():
        handle.close()
        raise SynProtocolError("SYN recovery requires a real controlling terminal")
    return handle


def _read_recovery_phrase(tty: Any) -> str:
    """Never allow command-line/env/pipe/keyring password substitution."""
    # getpass opens /dev/tty and suppresses echo; _operator_tty rejects fallback.
    first = getpass.getpass("SYN 24-word recovery phrase (hidden): ")
    second = getpass.getpass("Confirm recovery phrase (hidden): ")
    import hmac
    if not hmac.compare_digest(first, second):
        raise SynProtocolError("SYN recovery phrase confirmation does not match")
    return first


def _show_recovery(phrase: str, tty: Any) -> None:
    """Sensitive material goes to the real terminal, NEVER process stdout."""
    tty.write("\nSYN RECOVERY WORDS — WRITE DOWN SECURELY, NEVER COPY TO LOGS:\n")
    tty.write(phrase + "\n")
    tty.flush()


def main(argv: list[str] | None = None) -> int:
    parser = _RedactedArgumentParser(description="Standalone SYN operator/agent local service")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "serve", "publish-receipt", "qualify-operator"):
        item = sub.add_parser(command)
        item.add_argument("--config", type=Path, required=True)
        if command == "publish-receipt":
            item.add_argument("--receipt", type=Path, required=True)
    temp = sub.add_parser("new-agent-temporary-key")
    temp.add_argument("--keyring-service", required=True)
    temp.add_argument("--keyring-account", required=True)
    temp.add_argument("--lock-dir", type=Path, required=True)
    operator = sub.add_parser("new-operator", help="Create fresh SYN operator HD keys in OS keystore")
    operator.add_argument("--keyring-service", required=True)
    operator.add_argument("--keyring-account", required=True)
    operator.add_argument("--lock-dir", type=Path, required=True)
    for command in ("show-recovery", "recover-operator"):
        recovery = sub.add_parser(command)
        recovery.add_argument("--keyring-service", required=True)
        recovery.add_argument("--keyring-account", required=True)
        recovery.add_argument("--expected-base-public-key-hash", required=True)
        if command == "recover-operator":
            recovery.add_argument("--lock-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "new-agent-temporary-key":
            public = create_agent_temporary_key(service=args.keyring_service,
                                                account=args.keyring_account, lock_dir=args.lock_dir)
            print(json.dumps(public, sort_keys=True))
            return 0
        if args.command == "new-operator":
            public = create_operator_identity(service=args.keyring_service,
                                              account=args.keyring_account,
                                              lock_dir=args.lock_dir)
            print(json.dumps(public, sort_keys=True))
            return 0
        if args.command == "show-recovery":
            with _operator_tty() as tty:
                tty.write("Enter SHOW RECOVERY to view the operator's 24 private words: ")
                tty.flush()
                if tty.readline().strip() != "SHOW RECOVERY":
                    raise SynProtocolError("SYN operator recovery confirmation denied")
                phrase = operator_recovery_words(
                    service=args.keyring_service, account=args.keyring_account,
                    expected_base_public_key_hash=args.expected_base_public_key_hash,
                )
                _show_recovery(phrase, tty)
            return 0
        if args.command == "recover-operator":
            with _operator_tty() as tty:
                tty.write("Restore into an EMPTY SYN keystore? Type RESTORE IDENTITY: ")
                tty.flush()
                if tty.readline().strip() != "RESTORE IDENTITY":
                    raise SynProtocolError("SYN operator identity restore not authorized")
                phrase = _read_recovery_phrase(tty)
                public = recover_operator_identity(
                    service=args.keyring_service, account=args.keyring_account,
                    lock_dir=args.lock_dir, words=phrase,
                    expected_base_public_key_hash=args.expected_base_public_key_hash,
                )
            # Public-only output; the sensitive phrase is NEVER serialized.
            print(json.dumps(public, sort_keys=True))
            return 0
        server, registry = _configure(args.config, read_only=args.command == "preflight")
        if args.command == "qualify-operator":
            config = _private_json(args.config)
            if config.get("schema_version") != 2 or config.get("mode") != "operator":
                raise SynProtocolError("SYN operator qualification requires the Python HD profile")
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            challenge = os.urandom(32)
            signing_bytes = b"syn.operator.qualify.v1\x00" + challenge
            signature = server.signer.sign(signing_bytes)
            try:
                Ed25519PublicKey.from_public_bytes(server.signer.public_key).verify(
                    signature, signing_bytes
                )
            except Exception as exc:
                raise SynProtocolError("SYN functional signature qualification failed") from exc
            print(json.dumps({
                "schema": "syn-operator-functional-qualification/v1",
                "base_public_key_hash": server.master_hash.hex(),
                "profile": "syn-icarus-ed25519-bip32-v2",
                "functional_local_signature": "verified",
                "qualification": "local-software-smoke-only",
                "os_keystore_independently_certified": False,
                "agent_adoption_authorized": False,
            }, sort_keys=True))
            return 0
        if args.command == "preflight":
            print(json.dumps({"schema": "syn-local-service-preflight/v1", "mode": server.mode,
                              "base_public_key_hash": server.master_hash.hex(),
                              "local_protocol_ready": True,
                              "read_only_preflight": True,
                              "hd_signer_os_custody": "software-os-credential-store-unqualified",
                              "agent_activation_authorized": False}, sort_keys=True))
            return 0
        if args.command == "publish-receipt":
            if server.receipts is None:
                raise SynProtocolError("operator public-receipt journal is not configured")
            record = _private_json(args.receipt)
            digest = server.receipts.publish(record, now=int(time.time()))
            print(json.dumps({"schema": "syn-public-adoption-import/v1", "receipt_sha256": digest,
                              "agent_activation_authorized": False}, sort_keys=True))
            return 0
        server.bind()
        try:
            entry = create_registry_entry(socket_path=server.path, mode=server.mode,
                                          base_public_key_hash=server.master_hash)
            publish_registry(registry, entry)
            stop = threading.Event()

            def halt(_signal: int, _frame: object) -> None:
                stop.set()

            signal.signal(signal.SIGTERM, halt)
            signal.signal(signal.SIGINT, halt)
            server.serve(stop)
        finally:
            server.close()
        return 0
    except (SynProtocolError, OSError, ValueError) as exc:
        # Never print OS-keystore contents, signer output or private payloads.
        print(json.dumps({"schema": "syn-local-service-error/v1", "state": "blocked",
                          "error": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
