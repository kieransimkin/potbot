#!/usr/bin/env python3
"""Install locked development dependencies, then run the application CLI."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        installed = subprocess.run(
            ["uv", "sync", "--locked", "--all-extras", "--dev"], cwd=ROOT, check=False
        )
        if installed.returncode:
            return installed.returncode
        return subprocess.run(
            ["uv", "run", "--locked", "potbot", *(sys.argv[1:] or ["--help"])],
            cwd=ROOT,
            check=False,
        ).returncode
    except FileNotFoundError:
        print("Install uv before running scripts/dev.py.", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
