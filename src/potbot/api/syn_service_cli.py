"""Use Syn's durable messages and independently verify its hash-chain receipts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from potbot.controller.syn_service import configured_client
from potbot.domain.errors import CapabilityUnavailableError, DomainError, InvalidInputError
from potbot.domain.syn_credentials import SynCredentialScope
from potbot.domain.syn_exchange import MAX_EXCHANGE_BYTES, SynExportPolicy
from potbot.semantics.registry import strict_json


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
    sync.add_argument(
        "--export-policy",
        required=True,
        type=Path,
        help="Local approval for the v1 metadata profile and exact destination",
    )
    sync.add_argument(
        "--preview",
        action="store_true",
        help="Print projected messages without reading Syn credentials or sending",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "sync-events":
            from potbot.api.jobs_cli import database_context
            from potbot.controller.execution_events import ExecutionEventService
            from potbot.controller.syn_service import (
                preview_execution_events,
                sync_execution_events,
            )

            with args.export_policy.open("rb") as stream:
                raw = stream.read(MAX_EXCHANGE_BYTES + 1)
            if len(raw) > MAX_EXCHANGE_BYTES:
                raise InvalidInputError("Syn export policy exceeds its byte limit.")
            policy = SynExportPolicy.from_dict(strict_json(raw))
            # Check policy/configuration before reading either database or vault credentials.
            scope = SynCredentialScope.from_environment(os.environ)
            policy.require_scope(scope, args.workspace_id)
            database, context = database_context(args)
            source = ExecutionEventService(database)
            if args.preview:
                result = preview_execution_events(
                    source, context, policy=policy, scope=scope, after=args.after, limit=args.limit
                )
            else:
                client = configured_client(workspace_id=args.workspace_id)
                if client is None:
                    raise CapabilityUnavailableError("Configure the selected Syn service.")
                result = sync_execution_events(
                    client, source, context, policy=policy, after=args.after, limit=args.limit
                )
            print(json.dumps(result, sort_keys=True))
            return 0
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
            raise InvalidInputError("Unsupported Syn operation.")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DomainError, OSError, ValueError, RuntimeError, TypeError, KeyError):
        print(
            "Syn operation failed; check configuration and retry the same message ID or source cursor.",
            file=sys.stderr,
        )
        return 1
