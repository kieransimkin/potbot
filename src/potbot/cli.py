"""Small, dependency-free entry point with explicit capability boundaries."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from potbot import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch commands without importing adapters for unrelated operations."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "fixture":
        from potbot.evaluation.repository_fixture import main as fixture_main

        return fixture_main(args[1:])
    if args and args[0] == "db":
        from potbot.persistence.harness import main as database_main

        return database_main(args[1:])

    parser = argparse.ArgumentParser(
        prog="potbot",
        description="Foundation for an evidence-driven, multi-model coding agent.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("info", help="show implemented capabilities as JSON")
    commands.add_parser("fixture", help="create, check, repair, and clean up real Git fixtures")
    commands.add_parser("db", help="manage isolated PostgreSQL integration-test schemas")
    parsed = parser.parse_args(args)
    if parsed.command == "info":
        print(
            json.dumps(
                {
                    "package": "potbot",
                    "version": __version__,
                    "implementation_phase": 1,
                    "capabilities": [
                        "versioned_domain_contracts",
                        "real_git_fixtures",
                        "postgresql_vector_test_harness",
                        "reuse_discovery_policy",
                    ],
                    "model_inference_available": False,
                    "model_downloads_required": False,
                },
                sort_keys=True,
            )
        )
    else:
        parser.print_help()
    return 0
