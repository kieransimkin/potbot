"""SYN-owned LAN P2P discovery and encrypted-pledge CLI.

No hardware identifier or secret may appear in argv, stdout, environment,
operator pulse, or general logs. There is intentionally no network command to
approve inheritance, issue private keys, or activate an agent.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
from pathlib import Path

from .cli import _private_json
from .custody import AgentTemporaryOsSigner
from .hd_keys import PythonHdOperatorSigner
from .p2p_inbox import PledgeInbox
from .p2p_transport import SynP2PClient, SynP2PServer
from .protocol import SynProtocolError, hex_bytes


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise SynProtocolError("SYN P2P command arguments are invalid or incomplete")


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(description="SYN authenticated LAN pulse + encrypted pledge transport")
    cmds = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for label in ("operator-init", "operator-serve", "operator-list", "agent-pledge"):
        cmd = cmds.add_parser(label)
        if label.startswith("operator-"):
            cmd.add_argument("--config", type=Path, required=True)
            cmd.add_argument("--inbox", type=Path, required=True)
            cmd.add_argument("--network-id", required=True)
            cmd.add_argument("--keyring-service", required=True)
            cmd.add_argument("--keyring-account", required=True)
            if label == "operator-init":
                cmd.add_argument("--lock-dir", type=Path, required=True)
            if label == "operator-serve":
                cmd.add_argument("--bind-host", required=True)
                cmd.add_argument("--advertised-host", required=True)
                cmd.add_argument("--tcp-port", type=int, required=True)
                cmd.add_argument("--broadcast-host", required=True)
                cmd.add_argument("--broadcast-port", type=int, required=True)
                cmd.add_argument("--broadcast-period", type=float, default=10.0)
        else:
            cmd.add_argument("--network-id", required=True)
            cmd.add_argument("--pinned-master-hash", required=True)
            cmd.add_argument("--keyring-service", required=True)
            cmd.add_argument("--keyring-account", required=True)
            cmd.add_argument("--pinned-temp-hash", required=True)
            cmd.add_argument("--hardware-id-file", type=Path, required=True)
            cmd.add_argument("--udp-port", type=int, required=True)
            cmd.add_argument("--timeout", type=float, default=5.0)
        cmd.add_argument("--allow-loopback", action="store_true")
    return parser.parse_args(argv)


def _operator_signer(config: Path) -> PythonHdOperatorSigner:
    cfg = _private_json(config)
    if (cfg.get("schema_version") != 2 or cfg.get("mode") != "operator"
            or cfg.get("type_ref") != "synny://type/syn-local-service-config-v2"):
        raise SynProtocolError("SYN P2P operator must use the qualified Python HD profile")
    return PythonHdOperatorSigner(
        cfg["keyring_service"], cfg["keyring_account"],
        hex_bytes(cfg["expected_base_public_key_hash"], 32, "operator identity"),
    )


def _private_hardware_file(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise SynProtocolError("SYN hardware ID must come from a private local file")
    details = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
            or details.st_nlink != 1 or details.st_mode & 0o077
            or not 6 <= details.st_size <= 256):
        raise SynProtocolError("SYN hardware-ID source is not an owner-private bounded file")
    # This source is a *claim*. Independent hardware attestation is still needed
    # before the operator may approve it as a unique physical identity.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_dev != details.st_dev
                or opened.st_ino != details.st_ino or opened.st_uid != os.getuid()
                or opened.st_mode & 0o077 or not 6 <= opened.st_size <= 256):
            raise SynProtocolError("SYN hardware-ID file changed before read")
        return os.read(fd, 257)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _args(argv)
        if args.command.startswith("operator-"):
            signer = _operator_signer(args.config)
            if args.command == "operator-init":
                PledgeInbox.create(path=args.inbox, service=args.keyring_service,
                                   account=args.keyring_account,
                                   master_hash=signer.expected_public_hash,
                                   network_id=args.network_id, lock_dir=args.lock_dir)
                output: dict[str, object] = {"state": "initialized", "activation_authorized": False}
            else:
                inbox = PledgeInbox(path=args.inbox, service=args.keyring_service,
                                    account=args.keyring_account,
                                    master_hash=signer.expected_public_hash,
                                    network_id=args.network_id)
                if args.command == "operator-list":
                    pending = inbox.pending_digests()
                    output = {"state": "ready", "pending_digests": list(pending),
                              "count": len(pending), "activation_authorized": False}
                else:
                    if not 1 <= args.tcp_port <= 65535:
                        raise SynProtocolError("SYN P2P TCP listening port must be explicit")
                    server = SynP2PServer(
                        signer=signer, inbox=inbox, network_id=args.network_id,
                        bind_host=args.bind_host, advertised_host=args.advertised_host,
                        tcp_port=args.tcp_port, allow_loopback=args.allow_loopback,
                    )
                    stop = threading.Event()
                    previous = {}
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous[signum] = signal.getsignal(signum)
                        signal.signal(signum, lambda _signum, _frame: stop.set())
                    try:
                        server.serve(stop, broadcast_host=args.broadcast_host,
                                     broadcast_port=args.broadcast_port,
                                     period_seconds=args.broadcast_period)
                    finally:
                        server.close()
                        for signum, handler in previous.items():
                            signal.signal(signum, handler)
                    output = {"state": "stopped", "activation_authorized": False}
        else:
            expected_master = hex_bytes(args.pinned_master_hash, 32, "pinned operator")
            signer = AgentTemporaryOsSigner(
                args.keyring_service, args.keyring_account,
                hex_bytes(args.pinned_temp_hash, 32, "temporary signing identity"),
            )
            agent = SynP2PClient(master_hash=expected_master, network_id=args.network_id,
                                 allow_loopback=args.allow_loopback)
            # Discover and authenticate the operator BEFORE reading hardware ID.
            advert = agent.discover(udp_port=args.udp_port, timeout_seconds=args.timeout)
            device_id = _private_hardware_file(args.hardware_id_file)
            output = agent.pledge(advert=advert, temporary_signer=signer, hardware_id=device_id)
        print(json.dumps({"schema": "syn-p2p-command/v1", **output}, sort_keys=True))
        return 0
    except (SynProtocolError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"schema": "syn-p2p-command/v1", "state": "blocked",
                          "error": type(exc).__name__, "activation_authorized": False},
                         sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
