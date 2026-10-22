"""Task CLI using the same v1 service operations and database-backed identity."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot task", description=__doc__)
    parser.add_argument(
        "--dev-auth",
        action="store_true",
        required=True,
        help="explicitly use the provisioned database principal for local development",
    )
    parser.add_argument("--workspace", required=True, type=UUID)
    parser.add_argument("--principal", required=True, type=UUID)
    parser.add_argument("--schema", default="potbot")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="submit a versioned contract")
    submit.add_argument("--repository", required=True, type=UUID)
    submit.add_argument("--contract", required=True, type=Path)
    submit.add_argument("--key", required=True)
    for name in ("inspect", "history", "budget"):
        command = commands.add_parser(name)
        command.add_argument("task_id", type=UUID)
    revise = commands.add_parser("revise")
    revise.add_argument("task_id", type=UUID)
    revise.add_argument("--expected-contract", required=True, type=UUID)
    revise.add_argument("--contract", required=True, type=Path)
    revise.add_argument("--key", required=True)
    control = commands.add_parser("control")
    control.add_argument("task_id", type=UUID)
    control.add_argument("action", choices=("pause", "resume", "cancel"))
    control.add_argument("--expected-token", required=True, type=int)
    control.add_argument("--reason", required=True)
    control.add_argument("--key", required=True)
    start = commands.add_parser("start-generation", help="start a budgeted hosted-generation task")
    start.add_argument("task_id", type=UUID)
    start.add_argument("--expected-token", required=True, type=int)
    start.add_argument("--reason", required=True)
    start.add_argument("--key", required=True)
    serve = commands.add_parser("serve", help="serve the development API on loopback")
    serve.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    from potbot.api.tasks import DevelopmentBearerAuth, TaskAPI, TaskWSGI
    from potbot.api.wire import error_response
    from potbot.controller.tasks import TaskService
    from potbot.credentials import resolve_postgres_dsn
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.errors import InvalidInputError
    from potbot.persistence.database import Database

    try:
        dsn = resolve_postgres_dsn()
        context = WorkspaceContext(args.workspace, args.principal)
        service = TaskService(Database(dsn, schema=args.schema))
        if args.command == "serve":
            from wsgiref.simple_server import make_server

            token = os.environ.get("POTBOT_DEV_TOKEN", "")
            if not 1 <= args.port <= 65535:
                raise InvalidInputError("port must be between 1 and 65535")
            api = TaskAPI(service, DevelopmentBearerAuth({token: context}))
            # Deliberately local: deployment of a public listener needs its own
            # transport/authentication adapter, not an implicit development token.
            with make_server("127.0.0.1", args.port, TaskWSGI(api)) as server:
                print(
                    f"potbot development API listening on http://127.0.0.1:{args.port}", flush=True
                )
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return 0
        api = TaskAPI(service, lambda _authorization: context)
        payload: dict[str, object] = {}
        path = "/v1/tasks"
        method = "POST"
        if args.command == "submit":
            payload = {
                "repository_id": str(args.repository),
                "contract": json.loads(args.contract.read_text(encoding="utf-8")),
            }
        else:
            path += f"/{args.task_id}"
            if args.command in {"inspect", "history", "budget"}:
                method = "GET"
                path += {"inspect": "", "history": "/contracts", "budget": "/budget"}[args.command]
            elif args.command == "revise":
                path += "/contracts"
                payload = {
                    "expected_contract_id": str(args.expected_contract),
                    "contract": json.loads(args.contract.read_text(encoding="utf-8")),
                }
            elif args.command == "start-generation":
                path += "/start-generation"
                payload = {"expected_token": args.expected_token, "reason": args.reason}
            elif args.command == "control":
                path += "/control"
                payload = {
                    "expected_token": args.expected_token,
                    "action": args.action,
                    "reason": args.reason,
                }
        response = api.dispatch(
            context,
            method,
            path,
            {
                "schema_version": 1,
                "request_id": str(uuid4()),
                "idempotency_key": getattr(args, "key", None),
                "payload": payload,
            },
        )
    except (OSError, ValueError, TypeError):
        response = error_response(InvalidInputError("invalid task configuration or contract file"))
    except Exception as error:
        response = error_response(error)
    print(json.dumps(response.body, sort_keys=True, indent=2))
    return 0 if response.status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
