"""Host-local, explicitly authorized SYN inherited-key handoff commands.

No new daemon/socket mutators are introduced. Operator approval reads a Phase-41
private ledger plus BOTH signatures; recipient private keys stay in the agent's
OS credential store. CLI output contains public envelopes/receipts only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .cli import _operator_tty, _private_json
from .hd_keys import PythonHdOperatorSigner
from .inherited_adoption import (
    SynHandoffLedger, adopt_selected_key, confirm_selected_key,
    grant_selected_key, prepare_agent_request, seal_selected_key,
)
from .protocol import SynProtocolError, canonical, hex_bytes
from .receipts import PublicAdoptionJournal
from .selection_authority import SelectedPledgeAuthorizer, SELECTION_DOMAIN


def _redacted_parser() -> argparse.ArgumentParser:
    # argparse's default error message may echo a secret from bad input.
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            raise SynProtocolError("SYN adoption arguments are invalid or incomplete")
    parser = Parser(description="SYN operator-approved encrypted agent identity adoption")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("operator-grant", "operator-seal", "operator-confirm", "agent-request", "agent-adopt"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--output", type=Path, required=True)
        if name.startswith("operator-"):
            cmd.add_argument("--config", type=Path, required=True)
            cmd.add_argument("--phase41-ledger", type=Path, required=True)
            cmd.add_argument("--signed-selection", type=Path, required=True)
            cmd.add_argument("--network-id", required=True)
            cmd.add_argument("--handoff-ledger", type=Path, required=True)
            if name == "operator-grant":
                cmd.add_argument("--offer-sha256", required=True)
            else:
                cmd.add_argument("--grant", type=Path, required=True)
                if name == "operator-seal":
                    cmd.add_argument("--request", type=Path, required=True)
                else:
                    cmd.add_argument("--proof", type=Path, required=True)
                    cmd.add_argument("--receipt-journal", type=Path, required=True)
        else:
            cmd.add_argument("--keyring-service", required=True)
            cmd.add_argument("--keyring-account", required=True)
            cmd.add_argument("--lock-dir", type=Path, required=True)
            cmd.add_argument("--expected-master-hash", required=True)
            cmd.add_argument("--grant", type=Path, required=True)
            if name == "agent-adopt":
                cmd.add_argument("--request", type=Path, required=True)
                cmd.add_argument("--envelope", type=Path, required=True)
    return parser


def _private_output(path: Path, payload: dict[str, Any]) -> str:
    from .receipts import _private_directory
    if (not isinstance(path, Path) or not path.is_absolute() or path.is_symlink()):
        raise SynProtocolError("SYN output must be a new absolute ordinary file")
    _private_directory(path.parent)
    data = canonical(payload)
    if len(data) > 65536:
        raise SynProtocolError("SYN public handoff record exceeds the file limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fd = -1
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
    return hashlib.sha256(data).hexdigest()


def _operator_signer(config: Path) -> PythonHdOperatorSigner:
    raw = _private_json(config)
    if raw.get("schema_version") != 2 or raw.get("mode") != "operator":
        raise SynProtocolError("SYN adoption requires the Python HD operator mode")
    if not isinstance(raw.get("keyring_service"), str) or not isinstance(raw.get("keyring_account"), str):
        raise SynProtocolError("SYN operator keyring identity is absent")
    return PythonHdOperatorSigner(
        raw["keyring_service"], raw["keyring_account"],
        hex_bytes(raw.get("expected_base_public_key_hash"), 32, "master hash"),
    )


def _authorization(args: argparse.Namespace, signer: PythonHdOperatorSigner,
                   now: int) -> SelectedPledgeAuthorizer:
    packet = _private_json(args.signed_selection)
    return SelectedPledgeAuthorizer(
        ledger_path=args.phase41_ledger, expected_master_hash=signer.expected_public_hash,
        network_id=args.network_id, signed_selection=packet, now=now,
    )


def _selected_record(authorizer: SelectedPledgeAuthorizer, offer: str) -> dict[str, object]:
    hex_bytes(offer, 32, "pledge offer")
    authorizer._check_file()  # fail closed before opening the host-only SQLite journal
    uri = "file:" + quote(str(authorizer.path), safe="/") + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=3.0) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT offer_sha256,master_hash,temporary_public_key,inheritance,independence,"
                "selected_public_key_hash FROM syn_agent_pledges WHERE offer_sha256=?",
                (offer,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise SynProtocolError("SYN operator selected-enrollment journal unavailable") from exc
    if row is None:
        raise SynProtocolError("SYN chosen pledge is not present in the private ledger")
    packet = authorizer.selection
    proof = packet["selection"]
    signature = hex_bytes(packet["signature"], 64, "signed independence proof")
    values = {
        "offer_sha256": row["offer_sha256"],
        "master_public_key_hash": row["master_hash"],
        "temporary_public_key": row["temporary_public_key"],
        "inheritance": row["inheritance"],
        "independence": row["independence"],
        "selected_public_key_hash": row["selected_public_key_hash"],
        "selection_sha256": hashlib.sha256(SELECTION_DOMAIN + canonical(proof) + signature).hexdigest(),
    }
    authorizer.require_selected(values)
    return values


def _approve_on_tty(selection: dict[str, object]) -> None:
    """Require a human confirmation on the operator controlling terminal."""
    with _operator_tty() as tty:
        tty.write("SYN selected inherited key: " + str(selection["selected_public_key_hash"]) + "\n")
        tty.write("Issue ONE irreversible handoff for offer " + str(selection["offer_sha256"]) + "?\n")
        tty.write("Type ISSUE SYN INHERITANCE to approve: ")
        tty.flush()
        if tty.readline().strip() != "ISSUE SYN INHERITANCE":
            raise SynProtocolError("SYN operator did not approve the selected inheritance")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _redacted_parser().parse_args(argv)
        now = int(time.time())
        if args.command.startswith("operator-"):
            signer = _operator_signer(args.config)
            authorizer = _authorization(args, signer, now)
            ledger = SynHandoffLedger(args.handoff_ledger,
                                      operator_hash=signer.expected_public_hash)
            if args.command == "operator-grant":
                selected = _selected_record(authorizer, args.offer_sha256)
                _approve_on_tty(selected)
                output = grant_selected_key(selection=selected, authorizer=authorizer,
                                            signer=signer, ledger=ledger, now=now)
            elif args.command == "operator-seal":
                output = seal_selected_key(grant_packet=_private_json(args.grant),
                                           request_packet=_private_json(args.request), signer=signer,
                                           authorizer=authorizer, ledger=ledger, now=now)
            else:
                journal = PublicAdoptionJournal(args.receipt_journal,
                                                expected_master_hash=signer.expected_public_hash)
                output = confirm_selected_key(grant_packet=_private_json(args.grant),
                                              key_proof=_private_json(args.proof)["key_proof"],
                                              signer=signer, authorizer=authorizer,
                                              ledger=ledger, journal=journal, now=now)
        elif args.command == "agent-request":
            output = prepare_agent_request(grant_packet=_private_json(args.grant),
                                           service=args.keyring_service,
                                           account=args.keyring_account, lock_dir=args.lock_dir,
                                           expected_master_hash=hex_bytes(args.expected_master_hash,
                                                                          32, "master hash"), now=now)
        else:
            output = adopt_selected_key(grant_packet=_private_json(args.grant),
                                        request_packet=_private_json(args.request),
                                        envelope=_private_json(args.envelope),
                                        service=args.keyring_service, account=args.keyring_account,
                                        lock_dir=args.lock_dir,
                                        expected_master_hash=hex_bytes(args.expected_master_hash,
                                                                       32, "master hash"), now=now)
        digest = _private_output(args.output, output)
        print(json.dumps({"schema": "syn-inherited-adoption-command/v1", "state": "complete",
                          "output_sha256": digest, "agent_activation_authorized": False},
                         sort_keys=True))
        return 0
    except (SynProtocolError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"schema": "syn-inherited-adoption-command/v1", "state": "blocked",
                          "error": type(exc).__name__, "agent_activation_authorized": False}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
