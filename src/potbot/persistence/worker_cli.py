"""Restartable fixture worker with explicit environment or native vault credentials."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.credentials import resolve_postgres_dsn
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database
from potbot.persistence.outbox import FixtureWorker, Outbox


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, prog="potbot worker")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--principal-id", required=True, type=UUID)
    parser.add_argument("--schema", default="potbot")
    parser.add_argument("--artifact-root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue-fixture", help="commit a harmless event and fixture job")
    enqueue.add_argument("--key", required=True)
    enqueue.add_argument("--text", default="fixture payload")
    run = commands.add_parser("run-once", help="claim and complete at most one fixture job")
    run.add_argument("--worker-id", default="fixture-worker")
    run.add_argument("--lease-seconds", type=float, default=30)
    inspect = commands.add_parser("inspect", help="read a workspace-scoped job")
    inspect.add_argument("job_id", type=UUID)
    args = parser.parse_args(argv)
    try:
        dsn = resolve_postgres_dsn()
        db = Database(dsn, args.schema)
        context = WorkspaceContext(args.workspace_id, args.principal_id)
        outbox = Outbox(db)
        artifacts = ArtifactStore(db, LocalArtifactBackend(args.artifact_root))
        result: dict[str, Any] | None
        if args.command == "enqueue-fixture":
            payload = artifacts.put(context, args.text.encode(), "text/plain")
            result = outbox.publish(
                context,
                key=args.key,
                event_type="fixture_requested",
                details={},
                payload_id=payload["id"],
            )
        elif args.command == "run-once":
            result = FixtureWorker(outbox, artifacts, args.worker_id).run_once(
                context, lease_seconds=args.lease_seconds
            )
        else:
            result = outbox.inspect(context, args.job_id)
        print(json.dumps({"schema_version": 1, "result": result}, default=str, sort_keys=True))
        return 0
    except DomainError as error:
        print(json.dumps({"schema_version": 1, "error": error.to_dict()}), file=sys.stderr)
        return 2
    except Exception:
        print(
            '{"schema_version":1,"error":{"code":"internal_error",'
            '"message":"The worker operation failed."}}',
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
