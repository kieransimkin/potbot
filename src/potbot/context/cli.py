"""Host-only context audit/recovery CLI. Never install this command in guest images.

All requests use Potbot's restricted PostgreSQL transaction identity; passing
principal UUIDs does not bypass the database's session_user membership check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from uuid import UUID

from potbot.domain.errors import DomainError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot context", description=__doc__)
    parser.add_argument("--workspace", required=True, type=UUID)
    parser.add_argument("--principal", required=True, type=UUID)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("inspect", help="Inspect the current document and history metadata")
    inspect.add_argument("--limit", type=int, default=30)
    diff = sub.add_parser("diff", help="Show a bounded unified diff of two revisions")
    diff.add_argument("--before", required=True, type=int)
    diff.add_argument("--after", required=True, type=int)
    restore = sub.add_parser("restore", help="Create a new revision using historical content")
    restore.add_argument("--source", required=True, type=int)
    restore.add_argument("--expected", required=True, type=int)
    restore.add_argument("--idempotency-key", required=True)
    restore.add_argument("--reason", default="operator-reviewed restore")
    restore.add_argument("--confirm", action="store_true", help="Required for durable writes")
    args = parser.parse_args(argv)
    if args.action == "restore" and not args.confirm:
        parser.error("restore requires explicit --confirm")
    dsn = os.environ.get("POTBOT_DSN")
    if not dsn:
        print('{"error":"host PostgreSQL connection is not configured"}', file=sys.stderr)
        return 2
    try:
        from potbot.context.inspection import ContextHistoryInspector
        from potbot.context.service import WorkspaceContextService
        from potbot.domain.auth import WorkspaceContext
        from potbot.persistence.database import Database

        context = WorkspaceContext(args.workspace, args.principal)
        service = ContextHistoryInspector(WorkspaceContextService(Database(dsn)))
        target = (context, args.kind, args.name, args.role)
        if args.action == "inspect":
            result = service.inspect(*target, history_limit=args.limit)
        elif args.action == "diff":
            print(service.diff(*target, args.before, args.after), end="")
            return 0
        else:
            result = service.restore(*target, source_revision=args.source,
                                     expected_revision=args.expected,
                                     idempotency_key=args.idempotency_key,
                                     reason=args.reason)
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except DomainError as exc:
        # No connection strings, credentials or raw SQL errors in ordinary logs.
        print(json.dumps({"error": exc.code.value}), file=sys.stderr)
        return 1
    except Exception:
        print('{"error":"internal_error"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
