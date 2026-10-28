"""SYN-owned *read-only* authorizer for Phase-41 selected enrollments.

An independently running SYN operator accepts a pledge ONLY when the private
Phase-41 journal has selected state, the operator-signed 8-way offer matches
it, and the same temporary agent signed the selected independence. This file
never imports Potbot, upgrades its database or handles a raw hardware ID.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol import SynProtocolError, canonical, exact, hex_bytes
from .receipts import _private_directory

CANDIDATE_DOMAIN = b"syn.agent.candidates.v2\x00"
SELECTION_DOMAIN = b"syn.agent.independence.v2\x00"


def _signed_verify(public: bytes, signature: bytes, payload: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise SynProtocolError("SYN selection authority evidence signature is invalid") from exc


class SelectedPledgeAuthorizer:
    """Read-only adapter; cannot install identities or approve pending offers.

    The signed-selection packet is the *actual* agent reply from Phase 41. The
    journal has no copy of its signature, so the packet is a mandatory separate
    operator-only input. It must be preserved at the point of selection.
    """

    def __init__(self, *, ledger_path: Path, expected_master_hash: bytes,
                 network_id: str, signed_selection: dict[str, Any],
                 now: int | None = None):
        if type(expected_master_hash) is not bytes or len(expected_master_hash) != 32:
            raise SynProtocolError("SYN selection requires a pinned operator hash")
        if (not isinstance(ledger_path, Path) or not ledger_path.is_absolute()
                or ledger_path.is_symlink()):
            raise SynProtocolError("SYN selection journal must be an absolute, non-symlink file")
        _private_directory(ledger_path.parent)
        if not isinstance(network_id, str) or not 1 <= len(network_id) <= 128:
            raise SynProtocolError("SYN selection requires the operator's network")
        outer = exact(signed_selection, {"selection", "temporary_public_key", "signature"},
                      "agent signed independence")
        fields = exact(outer["selection"], {
            "schema_version", "type_ref", "target_master_public_key_hash",
            "inheritance", "independence", "chosen_short_public_key_hash",
            "candidate_offer_digest",
        }, "agent independence payload")
        if (type(fields["schema_version"]) is not int or fields["schema_version"] != 2
                or fields["type_ref"] != "synny://type/syn-agent-independence-selection-v2"):
            raise SynProtocolError("SYN selection revision is not version 2")
        self.selection = signed_selection
        self.path = ledger_path
        self.master_hash = expected_master_hash
        self.network_id = network_id
        self._at = now
        if now is not None and (type(now) is not int or now < 1):
            raise SynProtocolError("SYN test clock must be a positive integer")
        self._check_file()

    def _check_file(self) -> None:
        _private_directory(self.path.parent)
        if self.path.is_symlink():
            raise SynProtocolError("SYN selected-pledge journal cannot be a link")
        try:
            st = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise SynProtocolError("SYN selected-pledge journal missing") from exc
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o077):
            raise SynProtocolError("SYN selected-pledge journal is not owner-private")

    def require_selected(self, selection: dict[str, object]) -> None:
        """Fail closed on expiry, mismatches, replayed choices and row tampering."""
        expected = exact(selection, {
            "offer_sha256", "master_public_key_hash", "temporary_public_key",
            "inheritance", "independence", "selected_public_key_hash", "selection_sha256",
        }, "selected enrollment commitment")
        for name in ("offer_sha256", "master_public_key_hash", "temporary_public_key",
                     "selected_public_key_hash", "selection_sha256"):
            hex_bytes(expected[name], 32, name)
        if expected["master_public_key_hash"] != self.master_hash.hex():
            raise SynProtocolError("SYN selected enrollment belongs to another operator")
        self._check_file()
        uri = "file:" + quote(str(self.path), safe="/") + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=3.0) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT * FROM syn_agent_pledges WHERE offer_sha256=? AND master_hash=?",
                    (expected["offer_sha256"], self.master_hash.hex()),
                ).fetchone()
        except sqlite3.Error as exc:
            raise SynProtocolError("SYN selected enrollment journal unavailable") from exc
        if row is None or row["state"] != "selected" or row["network_id"] != self.network_id:
            raise SynProtocolError("SYN pledge has not reached authorized selected state")
        clock = int(time.time()) if self._at is None else self._at
        if (type(row["expires_at"]) is not int or row["expires_at"] < clock
                or row["master_hash"] != self.master_hash.hex()
                or row["temporary_public_key"] != expected["temporary_public_key"]
                or row["inheritance"] != expected["inheritance"]
                or row["independence"] != expected["independence"]
                or row["selected_public_key_hash"] != expected["selected_public_key_hash"]
                or not isinstance(row["device_handle"], str)
                or len(row["device_handle"]) != 64):
            raise SynProtocolError("SYN selected enrollment journal is stale or contradictory")
        try:
            candidates = json.loads(row["candidate_json"])
            full = json.loads(row["full_candidate_hashes_json"])
        except (ValueError, TypeError) as exc:
            raise SynProtocolError("SYN selected enrollment evidence is invalid") from exc
        signed_candidates = exact(candidates, {"candidates", "operator_public_key", "signature"},
                                  "operator signed candidates")
        body = exact(signed_candidates["candidates"], {
            "schema_version", "type_ref", "pledge_offer_sha256",
            "target_master_public_key_hash", "operator_byte", "inheritance",
            "inheritance_hex", "short_public_key_hashes",
        }, "candidate announcement")
        if (type(body["schema_version"]) is not int or body["schema_version"] != 2
                or body["type_ref"] != "synny://type/syn-agent-identity-candidates-v2"
                or body["pledge_offer_sha256"] != expected["offer_sha256"]
                or body["target_master_public_key_hash"] != self.master_hash.hex()
                or body["inheritance"] != expected["inheritance"]
                or body["inheritance_hex"] != f"{expected['inheritance']:06x}"
                or type(body["operator_byte"]) is not int
                or not 0 <= body["operator_byte"] <= 255):
            raise SynProtocolError("SYN candidate announcement differs from selected offer")
        pub = hex_bytes(signed_candidates["operator_public_key"], 32, "operator public key")
        if hashlib.sha256(pub).digest() != self.master_hash:
            raise SynProtocolError("SYN candidate signer is not the pinned operator")
        _signed_verify(pub, hex_bytes(signed_candidates["signature"], 64, "candidate signature"),
                       CANDIDATE_DOMAIN + canonical(body))
        short = body["short_public_key_hashes"]
        if (not isinstance(full, list) or len(full) != 8 or not isinstance(short, list)
                or len(short) != 8):
            raise SynProtocolError("SYN candidate hashes are incomplete")
        verified = [hex_bytes(h, 32, "full candidate") for h in full]
        if ([key[:3].hex() for key in verified] != short
                or len({key[:3] for key in verified}) != 8
                or verified[expected["independence"] - 1].hex() != expected["selected_public_key_hash"]):
            raise SynProtocolError("SYN candidate hashes disagree with signed selection")
        packet = self.selection
        choice = packet["selection"]
        if (choice["target_master_public_key_hash"] != self.master_hash.hex()
                or choice["inheritance"] != expected["inheritance"]
                or choice["independence"] != expected["independence"]
                or choice["chosen_short_public_key_hash"] != short[expected["independence"] - 1]
                or choice["candidate_offer_digest"] != hashlib.sha256(
                    CANDIDATE_DOMAIN + canonical(body)).hexdigest()
                or packet["temporary_public_key"] != expected["temporary_public_key"]):
            raise SynProtocolError("SYN signed independence selection differs from chosen key")
        temp_pub = hex_bytes(packet["temporary_public_key"], 32, "temporary identity")
        sig = hex_bytes(packet["signature"], 64, "temporary selection signature")
        _signed_verify(temp_pub, sig, SELECTION_DOMAIN + canonical(choice))
        digest = hashlib.sha256(SELECTION_DOMAIN + canonical(choice) + sig).hexdigest()
        if digest != expected["selection_sha256"]:
            raise SynProtocolError("SYN selected proof digest does not match signed choice")
