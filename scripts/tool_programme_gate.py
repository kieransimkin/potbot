#!/usr/bin/env python3
"""Emit all 48 stage reports using the existing runner and source-bound Cog gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from potbot import __version__
from potbot.evaluation.tool_programme import evaluate

try:
    from check import source_identity
except ImportError:
    from scripts.check import source_identity

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--mode", choices=("implemented", "full_programme"), default="implemented")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    stages = json.loads(
        (
            ROOT / "docs/potbot-tool-mcp-implementation-bundle/potbot-tool-integration-stages.json"
        ).read_text(encoding="utf-8")
    )
    report = evaluate(
        stages=stages,
        artifacts=args.artifacts,
        source=source_identity(),
        version=__version__,
        mode=args.mode,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "result",
                    "implemented_profile_passed",
                    "full_programme_qualified",
                    "blockers",
                )
            }
        )
    )
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
