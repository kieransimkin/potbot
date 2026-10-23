"""Use Syn's durable messages and independently verify its hash-chain receipts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from potbot.controller.syn_service import configured_client
from potbot.domain.errors import CapabilityUnavailableError, DomainError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot syn-service", description=__doc__)
    parser.add_argument("--workspace-id", required=True, type=UUID)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("head")
    verify = commands.add_parser("verify")
    verify.add_argument("--expected-head", type=Path, help="Previously retained JSON head anchor")
    messages = commands.add_parser("messages")
    messages.add_argument("--after", type=int, default=0)
    messages.add_argument("--limit", type=int, default=50)
    send = commands.add_parser("send")
    send.add_argument("--message-id", required=True, type=UUID, help="Reuse this UUID on retry")
    send.add_argument("--channel", required=True)
    send.add_argument("--sender", default="potbot")
    send.add_argument("--payload", required=True, type=Path, help="Approved JSON object to send")
    sync = commands.add_parser("sync-events")
    sync.add_argument("--principal-id", required=True, type=UUID)
    sync.add_argument("--schema", default="potbot")
    sync.add_argument("--after", type=int, default=0)
    sync.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        client = configured_client(workspace_id=args.workspace_id)
        if client is None:
            raise CapabilityUnavailableError("Set POTBOT_SYN_URL and workspace POTBOT_SYN_TOKEN.")
        workspace = str(args.workspace_id)
        if args.command == "head":
            result = client.head(workspace)
        elif args.command == "verify":
            target = None
            if args.expected_head:
                target = json.loads(args.expected_head.read_text(encoding="utf-8"))
            result = client.verify(workspace, expected_head=target)
        elif args.command == "messages":
            result = client.messages(workspace, after=args.after, limit=args.limit)
        elif args.command == "send":
            if args.payload.stat().st_size > 131_072:
                raise ValueError("Payload is too large")
            result = client.send(
                workspace,
                message_id=str(args.message_id),
                channel=args.channel,
                sender=args.sender,
                payload=json.loads(args.payload.read_text(encoding="utf-8")),
            )
        else:
            from potbot.api.jobs_cli import database_context
            from potbot.controller.execution_events import ExecutionEventService
            from potbot.controller.syn_service import sync_execution_events

            database, context = database_context(args)
            result = sync_execution_events(
                client,
                ExecutionEventService(database),
                context,
                after=args.after,
                limit=args.limit,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DomainError, OSError, ValueError, RuntimeError, TypeError, KeyError):
        print(
            "Syn operation failed; check configuration and retry the same message ID or source cursor.",
            file=sys.stderr,
        )
        return 1
