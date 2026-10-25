#!/usr/bin/env python3
"""Strict *preflight* for an agent-shared tool install (no repository mutations).

The trusted installer must also register the pinned checkout with SharedToolStore,
run its real cleanup, and verify installed binary/environment artifacts. The JSON
probe is supplied by an instrumented, isolated, network-disabled operator test,
never by model claims. Passing this syntax gate does not grant execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.shared_tool_store import review_manifest




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = json.loads(args.probe.read_text(encoding="utf-8"))
        print(json.dumps(review_manifest(data), sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ScopeDeniedError, InvalidInputError) as exc:
        print(f"shared tool admission failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
