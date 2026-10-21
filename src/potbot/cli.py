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
    if args and args[0] == "admin":
        from potbot.persistence.admin_cli import main as admin_main

        return admin_main(args[1:])
    if args and args[0] == "task":
        from potbot.api.task_cli import main as task_main

        return task_main(args[1:])
    if args and args[0] == "worker":
        from potbot.persistence.worker_cli import main as worker_main

        return worker_main(args[1:])
    if args and args[0] == "embedding":
        from potbot.memory.embedding_cli import main as embedding_main

        return embedding_main(args[1:])

    parser = argparse.ArgumentParser(
        prog="potbot",
        description="Foundation for an evidence-driven, multi-model coding agent.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("info", help="show implemented capabilities as JSON")
    commands.add_parser("fixture", help="create, check, repair, and clean up real Git fixtures")
    commands.add_parser("db", help="manage isolated PostgreSQL integration-test schemas")
    commands.add_parser("admin", help="migrate and provision application workspaces")
    commands.add_parser("task", help="submit, inspect, revise, and control durable tasks")
    commands.add_parser("worker", help="run recoverable fixture outbox jobs")
    commands.add_parser("embedding", help="set up, index, and query local Qwen embeddings")
    parsed = parser.parse_args(args)
    if parsed.command == "info":
        print(
            json.dumps(
                {
                    "package": "potbot",
                    "version": __version__,
                    "implementation_phase": 2,
                    "capabilities": [
                        "versioned_domain_contracts",
                        "real_git_fixtures",
                        "postgresql_vector_test_harness",
                        "reuse_discovery_policy",
                        "scoped_durable_tasks",
                        "immutable_evidence_and_artifacts",
                        "leased_outbox_workers",
                        "task_budget_ledger",
                        "model_deployment_and_invocation_records",
                        "local_standard_vector_embeddings",
                    ],
                    "model_inference_available": False,
                    "model_downloads_required": False,
                    "embedding_model_download_required_for_local_encoding": True,
                },
                sort_keys=True,
            )
        )
    else:
        parser.print_help()
    return 0
