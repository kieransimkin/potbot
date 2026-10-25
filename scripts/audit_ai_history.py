#!/usr/bin/env python3
"""Operator-only, read-only audit of every /etc/ai reversal and recorded byte.

Run with the workspace operator stopped; never treat exit=0 as proof of
per-syscall write attribution or a safe-to-restore authorization.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.ai_history_audit import audit_ai_history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--past", required=True, type=Path,
                        help="Operator-owned backing path for guest /etc/ai-past")
    parser.add_argument("--config", type=Path,
                        help="Optional backing path for live guest /etc/ai comparison")
    parser.add_argument("--max-revisions", type=int, default=50_000)
    parser.add_argument("--max-archive-bytes", type=int, default=2 * 1024**3)
    args = parser.parse_args(argv)
    try:
        report = audit_ai_history(
            args.past, config_dir=args.config,
            max_revisions=args.max_revisions,
            max_archive_bytes=args.max_archive_bytes,
        )
        print(json.dumps(report.to_dict(), sort_keys=True))
        return 0
    except (CapabilityUnavailableError, InvalidInputError, ScopeDeniedError,
            StateMismatchError, OSError, ValueError, TypeError, KeyError) as exc:
        print("AI history audit failed: " + type(exc).__name__ + ": " + str(exc),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
