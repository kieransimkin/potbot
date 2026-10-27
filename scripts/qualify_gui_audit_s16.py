#!/usr/bin/env python3
"""S16 strict host-only GUI audit qualification; raw evidence remains private."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from potbot.tools.gui_audit_verification import verify_gui_run


def _publish(report: Path, result: dict[str, object]) -> str:
    if not report.is_absolute() or report.exists() or report.is_symlink() or not report.parent.is_dir():
        raise ValueError("qualification report requires a fresh absolute path")
    raw = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        report.unlink(missing_ok=True)
        raise
    return hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--agent-id", type=UUID)
    identity.add_argument("--principal-id", type=UUID)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        kind = "agent" if args.agent_id is not None else "principal"
        result = verify_gui_run(args.audit_root, args.workspace_id, args.run_id,
                                identity_kind=kind, subject_id=args.agent_id or args.principal_id)
        digest = _publish(args.report, result)
        print(json.dumps({"qualified": True, "reportSha256": digest}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print('{"qualified":false,"error":"gui_audit_unavailable"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
