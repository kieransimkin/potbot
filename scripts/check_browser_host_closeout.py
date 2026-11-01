#!/usr/bin/env python3
"""Fail-closed manifest gate for independently executed live Potbot browser drills.

This is a *validator*, never a host-test substitute. It does not create
positive evidence, start agents or bypass operator authority. Every field is
checked against supplied immutable host observations, but the operator must
still establish provenance and sign off on the evidence source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

SCHEMA = "potbot-browser-host-closeout/v1"
REQUIRED = (
    "two_registered_agents",
    "private_x11_and_home",
    "view_only_streams",
    "ticket_replay_and_epoch_denial",
    "operator_loss_fencing",
    "network_https_tls_dns_denial",
    "postgres_artifacts_and_rls",
    "agent_logs_and_archive",
    "worker_tree_audit_and_settlement",
)
HEX = re.compile(r"[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_MANIFEST = 96 * 1024
MAX_EVIDENCE = 4 * 1024 * 1024


def unique_json(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MANIFEST:
        raise ValueError("missing or oversized host manifest")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output = {}
        for k, v in items:
            if k in output:
                raise ValueError("duplicate host manifest property")
            output[k] = v
        return output

    try:
        obj = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("invalid host manifest JSON") from error
    if not isinstance(obj, dict):
        raise ValueError("host manifest must be an object")
    return obj


def _uuid(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("invalid host identity")
    try:
        parsed = UUID(raw)
    except ValueError as error:
        raise ValueError("invalid host identity") from error
    if str(parsed) != raw:
        raise ValueError("noncanonical host identity")
    return raw


def _sha(raw: object) -> str:
    if not isinstance(raw, str) or not HEX.fullmatch(raw):
        raise ValueError("invalid source/artifact hash")
    return raw


def _source(root: Path, raw: dict[str, Any]) -> None:
    if set(raw) != {"commit", "tree_dirty", "files"}:
        raise ValueError("incomplete source binding")
    if not isinstance(raw["commit"], str) or not SHA.fullmatch(raw["commit"]):
        raise ValueError("source commit is not a full Git SHA")
    if raw["tree_dirty"] is not False:
        raise ValueError("dirty source cannot qualify a pinned browser")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=10
    ).strip()
    if head != raw["commit"]:
        raise ValueError("host acceptance references a different source commit")
    files = raw["files"]
    if not isinstance(files, dict) or len(files) < 3 or len(files) > 40:
        raise ValueError("required committed source hashes absent")
    for key, expected in files.items():
        if (
            not isinstance(key, str)
            or len(key) > 200
            or key.startswith("/")
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise ValueError("invalid source path")
        _sha(expected)
        committed = subprocess.check_output(["git", "show", f"{head}:{key}"], cwd=root, timeout=10)
        actual = root / key
        if (
            actual.is_symlink()
            or not actual.is_file()
            or hashlib.sha256(actual.read_bytes()).hexdigest() != expected
            or hashlib.sha256(committed).hexdigest() != expected
        ):
            raise ValueError("source bytes differ from committed acceptance source")
    for required in (
        "src/potbot/tools/xpra_browser_worker.py",
        "src/potbot/tools/xpra_browser_control.py",
        "src/potbot/tools/agent_xpra_streams.py",
    ):
        if required not in files:
            raise ValueError("missing browser/source pin")


def _proof(path: Path, recorded_hash: str, directory: Path) -> None:
    """Evidence is read only from an operator-owned directory outside guests."""
    _sha(recorded_hash)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or path.parent != directory
        or path.resolve(strict=True) != path
    ):
        raise ValueError("proof must be an ordinary file in the operator report directory")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or not 0 < info.st_size <= MAX_EVIDENCE
    ):
        raise ValueError("proof is not operator-private and bounded")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        with os.fdopen(os.dup(fd), "rb") as stream:
            observed_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(fd)
        if (
            observed_hash != recorded_hash
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_size != after.st_size
        ):
            raise ValueError("host evidence was modified or does not match its hash")
    finally:
        os.close(fd)


def verify(data: dict[str, Any], *, directory: Path, root: Path) -> dict[str, Any]:
    if (
        not isinstance(data, dict)
        or set(data)
        != {
            "schema",
            "source",
            "workspace_id",
            "agent_ids",
            "image_digest",
            "qualification_sha256",
            "operator_epoch",
            "drills",
        }
        or data["schema"] != SCHEMA
    ):
        raise ValueError("host closeout schema is incomplete")
    _source(root, data["source"])
    _uuid(data["workspace_id"])
    agents = data["agent_ids"]
    if (
        not isinstance(agents, list)
        or len(agents) != 2
        or len({_uuid(agent) for agent in agents}) != 2
    ):
        raise ValueError("two distinct registered agents are mandatory")
    _sha(data["image_digest"])
    _sha(data["qualification_sha256"])
    if type(data["operator_epoch"]) is not int or data["operator_epoch"] < 1:
        raise ValueError("missing positive operator epoch")
    checks = data["drills"]
    if not isinstance(checks, dict) or set(checks) != set(REQUIRED):
        raise ValueError("missing mandatory browser host drill")
    used_evidence: set[str] = set()
    for _, row in checks.items():
        if not isinstance(row, dict) or set(row) != {
            "passed",
            "workspace_id",
            "agent_ids",
            "image_digest",
            "operator_epoch",
            "evidence_file",
            "evidence_sha256",
            "observed_process_tree_stopped",
            "operator_observation",
        }:
            raise ValueError("incomplete drill evidence")
        if (
            row["passed"] is not True
            or row["workspace_id"] != data["workspace_id"]
            or row["agent_ids"] != agents
            or row["image_digest"] != data["image_digest"]
            or row["operator_epoch"] != data["operator_epoch"]
            or row["observed_process_tree_stopped"] is not True
            or row["operator_observation"] != "direct_live_host"
        ):
            raise ValueError("drill scope/exit/evidence contradicts operator qualification")
        name = row["evidence_file"]
        if (
            not isinstance(name, str)
            or len(name) > 100
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or not name.endswith(".json")
        ):
            raise ValueError("invalid operator-owned evidence file name")
        if name in used_evidence:
            raise ValueError("independent host drills must have separate evidence files")
        used_evidence.add(name)
        _proof(directory / name, row["evidence_sha256"], directory)
    return {
        "schema": SCHEMA,
        "manifest_valid": True,
        "machine_checked_drills": len(REQUIRED),
        "source_commit": data["source"]["commit"],
        "workspace_id": data["workspace_id"],
        "agent_count": len(agents),
        "qualification": "operator_supplied_host_evidence_checked_not_remote_attestation",
        "production_qualified": False,
        "operator_signoff_required": True,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        if args.manifest.is_symlink() or not args.manifest.is_file():
            raise ValueError("missing private host manifest")
        manifest_info = args.manifest.stat()
        if (
            manifest_info.st_uid != os.geteuid()
            or manifest_info.st_mode & 0o077
            or manifest_info.st_nlink != 1
            or manifest_info.st_size > MAX_MANIFEST
        ):
            raise ValueError("host manifest must be an operator-private regular file")
        result = verify(
            unique_json(args.manifest.read_bytes()),
            directory=args.manifest.resolve(strict=True).parent,
            root=args.root.resolve(strict=True),
        )
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("host closeout result must be a fresh path")
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(result, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"host browser closeout incomplete: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
