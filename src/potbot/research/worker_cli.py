"""Explicit operator CLI for Phase 7 immutable repository research (no network)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from potbot.controller.research_worker import LocalResearchWorker, ResearchRunStore
from potbot.credentials import resolve_postgres_dsn
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, prog="potbot phase7-research")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--principal-id", required=True, type=UUID)
    parser.add_argument("--schema", default="potbot")
    parser.add_argument("--artifact-root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    queue = commands.add_parser("queue", help="queue one admitted repository action")
    queue.add_argument("action_id", type=UUID)
    queue.add_argument("--key", required=True)
    run = commands.add_parser("run-once", help="inspect at most one queued snapshot")
    run.add_argument("--worker-id", required=True)
    inspect = commands.add_parser("inspect", help="inspect a research run and its receipt")
    inspect.add_argument("run_id", type=UUID)
    audit = commands.add_parser("audit", help="replay immutable source evidence")
    audit.add_argument("run_id", type=UUID)
    link = commands.add_parser("link-source", help="link audited source to a solve graph")
    link.add_argument("research_run_id", type=UUID)
    link.add_argument("solve_run_id", type=UUID)
    link.add_argument("parent_node_id", type=UUID)
    link.add_argument("--key", required=True)
    commands.add_parser("quarantine-expired", help="mark expired claims indeterminate")
    args = parser.parse_args(argv)
    try:
        context = WorkspaceContext(args.workspace_id, args.principal_id)
        db = Database(resolve_postgres_dsn(), args.schema)
        artifacts = ArtifactStore(db, LocalArtifactBackend(args.artifact_root))
        store = ResearchRunStore(db, artifacts)
        if args.command == "queue":
            result: object = store.queue(context, args.action_id, args.key)
        elif args.command == "run-once":
            result = LocalResearchWorker(store).run_once(context, args.worker_id)
        elif args.command == "inspect":
            result = store.inspect(context, args.run_id)
        elif args.command == "audit":
            from potbot.controller.research_audit import ResearchAuditStore

            result = ResearchAuditStore(db, artifacts).audit(context, args.run_id)
        elif args.command == "link-source":
            from potbot.controller.research_solve_bridge import ResearchSolveBridge

            result = ResearchSolveBridge(db, artifacts).link(
                context,
                args.research_run_id,
                args.solve_run_id,
                args.parent_node_id,
                args.key,
            )
        else:
            result = {"quarantined": store.quarantine_expired(context)}
        print(json.dumps({"schema_version": 1, "result": result}, sort_keys=True))
        return 0
    except DomainError as error:
        print(json.dumps({"schema_version": 1, "error": error.to_dict()}), file=sys.stderr)
        return 2
    except Exception:
        print(
            '{"schema_version":1,"error":{"code":"internal_error",'
            '"message":"Research operation failed."}}',
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
