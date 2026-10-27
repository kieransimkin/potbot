"""Standalone SYN local service CLI; deliberately independent from Potbot."""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .custody import NativeOperatorSigner, create_agent_temporary_key
from .local_service import SynLocalServer, create_registry_entry, private_directory, publish_registry
from .protocol import SynProtocolError, exact, hex_bytes, parse
from .receipts import PublicAdoptionJournal

CONFIG_TYPE = "synny://type/syn-local-service-config-v1"


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
    fields = exact(_private_json(path), {
        "schema_version", "type_ref", "mode", "socket", "registry", "receipt_db",
        "expected_base_public_key_hash", "native_signer_executable", "native_signer_sha256",
    }, "SYN local daemon configuration")
    if (type(fields["schema_version"]) is not int or fields["schema_version"] != 1
            or fields["type_ref"] != CONFIG_TYPE or fields["mode"] not in {"operator", "agent"}):
        raise SynProtocolError("SYN local daemon config is not v1")
    for name in ("socket", "registry", "native_signer_executable"):
        if not isinstance(fields[name], str) or not Path(fields[name]).is_absolute():
            raise SynProtocolError(f"SYN {name} must be an exact absolute path")
    if fields["receipt_db"] is not None and (
        not isinstance(fields["receipt_db"], str) or not Path(fields["receipt_db"]).is_absolute()
    ):
        raise SynProtocolError("SYN receipt journal path must be absolute or disabled")
    expected = hex_bytes(fields["expected_base_public_key_hash"], 32, "base hash")
    signer = NativeOperatorSigner(Path(fields["native_signer_executable"]),
                                   fields["native_signer_sha256"], expected)
    journal = None
    if fields["mode"] == "operator" and fields["receipt_db"] is not None and not read_only:
        journal = PublicAdoptionJournal(Path(fields["receipt_db"]), expected_master_hash=expected)
    if fields["mode"] == "agent" and fields["receipt_db"] is not None:
        raise SynProtocolError("agent installation cannot serve the operator receipt journal")
    service = SynLocalServer(socket_path=Path(fields["socket"]), mode=fields["mode"],
                             signer=signer, receipts=journal)
    return service, Path(fields["registry"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone SYN operator/agent local service")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "serve", "publish-receipt"):
        item = sub.add_parser(command)
        item.add_argument("--config", type=Path, required=True)
        if command == "publish-receipt":
            item.add_argument("--receipt", type=Path, required=True)
    temp = sub.add_parser("new-agent-temporary-key")
    temp.add_argument("--keyring-service", required=True)
    temp.add_argument("--keyring-account", required=True)
    temp.add_argument("--lock-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "new-agent-temporary-key":
            public = create_agent_temporary_key(service=args.keyring_service,
                                                account=args.keyring_account, lock_dir=args.lock_dir)
            print(json.dumps(public, sort_keys=True))
            return 0
        server, registry = _configure(args.config, read_only=args.command == "preflight")
        if args.command == "preflight":
            print(json.dumps({"schema": "syn-local-service-preflight/v1", "mode": server.mode,
                              "base_public_key_hash": server.master_hash.hex(),
                              "local_protocol_ready": True,
                              "read_only_preflight": True,
                              "hd_signer_os_custody": "requires_external_qualification",
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
