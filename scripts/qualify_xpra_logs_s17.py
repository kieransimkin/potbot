#!/usr/bin/env python3
"""Operator-only, read-only Xpra log inventory; no contents or host paths in report."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from potbot.tools.xpra_log_policy_s17 import XpraLogError, inspect_xpra_logs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_xpra_logs(args.log_root)
        if args.report.exists() or args.report.is_symlink() or not args.report.is_absolute():
            raise XpraLogError("log inventory report must be a new absolute file")
        # Report is private because it contains forensic run counts and hashes.
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(result, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256(args.report.read_bytes()).hexdigest()
        print(json.dumps({"qualified": not result["requiresReconciliation"],
                          "recoveryRequired": result["requiresReconciliation"],
                          "reportSha256": digest}, sort_keys=True))
        return 2 if result["requiresReconciliation"] else 0
    except (OSError, TypeError, ValueError, XpraLogError):
        print('{"qualified":false,"error":"xpra_log_inventory_unavailable"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
