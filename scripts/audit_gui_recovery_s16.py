#!/usr/bin/env python3
"""Read-only S16 GUI monitoring restart/reconciliation inventory."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from potbot.tools.gui_audit_verification import inspect_recovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_recovery(args.audit_root, args.workspace_id)
        if not args.report.is_absolute() or args.report.exists() or args.report.is_symlink():
            raise ValueError("recovery report needs a new absolute file")
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(result, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"inspected": result["total"], "requiresReconciliation": result["requiresReconciliation"]}))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print('{"inspected":false,"error":"gui_recovery_unavailable"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
