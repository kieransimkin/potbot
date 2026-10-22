#!/usr/bin/env python3
"""Generate or check the opt-in security catalog from its single authored source."""

from __future__ import annotations

import argparse
from pathlib import Path

from potbot.semantics.agent_security import security_type_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "vocab" / "agent-security.snapshot.json"
    expected = security_type_snapshot().to_json() + "\n"
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            print("Agent security catalog is stale; run scripts/generate_agent_security.py")
            return 1
        print("Agent security catalog matches its authored Type definitions")
        return 0
    path.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
