#!/usr/bin/env python3
"""Operator-only S17 *read-only* evidence gate for a completed real GUI run.

This is not a synthetic WSL2 qualification or process launcher. It refuses
missing kernel audit evidence, any unreconciled GUI jobs or unarchived Xpra logs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from potbot.tools.gui_audit_verification import inspect_recovery, verify_gui_run
from potbot.tools.xpra_log_policy_s17 import XpraLogError, inspect_xpra_logs


def qualify(audit_root: Path, log_root: Path, workspace_id: UUID, run_id: UUID,
            *, identity_kind: str = "principal", subject_id: UUID | None = None) -> dict[str, object]:
    evidence = verify_gui_run(audit_root, workspace_id, run_id,
                              identity_kind=identity_kind, subject_id=subject_id)
    outstanding = inspect_recovery(audit_root, workspace_id)
    logs = inspect_xpra_logs(log_root)
    if outstanding["requiresReconciliation"] or logs["requiresReconciliation"]:
        raise ValueError("unreconciled operator GUI evidence")
    archived = sum(instance["archived"] for instance in logs["instances"])
    if archived < 1:
        raise ValueError("host Xpra never produced an archived log")
    return {"schema": "potbot-gui-live-host-s17/v1", "qualified": True,
            "workspaceId": str(workspace_id), "runId": str(run_id),
            "identityKind": identity_kind, "tracedProcesses": evidence["tracedProcesses"],
            "fileEvents": evidence["fileEvents"], "archivedHostLogs": archived,
            "auditedRuns": outstanding["total"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audit-root", required=True, type=Path)
    p.add_argument("--log-root", required=True, type=Path)
    p.add_argument("--workspace-id", required=True, type=UUID)
    p.add_argument("--run-id", required=True, type=UUID)
    p.add_argument("--identity-kind", choices=("principal", "agent"), default="principal")
    p.add_argument("--subject-id", type=UUID)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        data = qualify(args.audit_root, args.log_root, args.workspace_id, args.run_id,
                       identity_kind=args.identity_kind, subject_id=args.subject_id)
        if args.report.exists() or args.report.is_symlink() or not args.report.is_absolute():
            raise ValueError("report must be a new absolute file")
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"qualified": True,
                          "reportSha256": hashlib.sha256(args.report.read_bytes()).hexdigest()}))
        return 0
    except (ValueError, KeyError, TypeError, OSError, XpraLogError):
        print('{"qualified":false,"error":"gui_host_evidence_unavailable"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
