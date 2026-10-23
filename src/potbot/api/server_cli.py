"""Run the scoped REST API, dashboard and WebSocket service independently of workers."""

from __future__ import annotations

import argparse
import ipaddress
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from potbot.api.jobs_cli import add_scope_arguments, database_context, hosted_gateway

if TYPE_CHECKING:
    from fastapi import FastAPI


def _origins(args: argparse.Namespace) -> tuple[str, ...]:
    from potbot.domain.errors import InvalidInputError

    if not 1 <= args.port <= 65535:
        raise InvalidInputError("The listener port must be between 1 and 65535.")
    if args.host == "localhost":
        loopback = True
    else:
        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            raise InvalidInputError("Use a literal bind IP address or localhost.") from None
    if args.allowed_origin:
        return tuple(dict.fromkeys(args.allowed_origin))
    if not loopback:
        raise InvalidInputError(
            "A non-loopback listener requires explicit allowed browser origins."
        )
    host = f"[{args.host}]" if ":" in args.host else args.host
    return tuple(
        dict.fromkeys(
            (
                f"http://{host}:{args.port}",
                f"http://127.0.0.1:{args.port}",
                f"http://localhost:{args.port}",
            )
        )
    )


def application(args: argparse.Namespace) -> FastAPI:
    """Build explicit service dependencies; construction never starts a worker."""
    from potbot.api.server import ServerSettings, create_app
    from potbot.api.tasks import DevelopmentBearerAuth
    from potbot.controller.execution_events import ExecutionEventService
    from potbot.controller.jobs import JobService
    from potbot.controller.syn_service import configured_client
    from potbot.controller.tasks import TaskService
    from potbot.models.hosted_catalog import HostedModelDirectory
    from potbot.models.hosted_policy import HostedPolicy
    from potbot.service_credentials import ServiceCredentials

    settings = ServerSettings(allowed_origins=_origins(args))
    policy = HostedPolicy.read(args.provider_policy)
    database, context = database_context(args)
    with database.transaction(context):
        pass
    credentials = ServiceCredentials()
    token = credentials.resolve("secret://api/access-token")
    authenticate = DevelopmentBearerAuth({token: context})
    directory = HostedModelDirectory(database, policy, credentials)
    gateway = hosted_gateway(database, policy, credentials)
    return create_app(
        task_service=TaskService(database),
        job_service=JobService(database),
        authenticate=authenticate,
        model_catalog=directory.catalog,
        discover_models=directory.discover,
        invocation_reader=gateway.inspect,
        invocation_list_reader=gateway.list_for_job,
        execution_events=ExecutionEventService(database),
        syn_service=configured_client(),
        settings=settings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot serve", description=__doc__)
    add_scope_arguments(parser)
    parser.add_argument("--provider-policy", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1", help="literal bind IP, default loopback")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allowed-origin", action="append", default=[], help="exact browser origin; repeatable"
    )
    args = parser.parse_args(argv)

    from potbot.api.wire import error_response
    from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

    try:
        import uvicorn

        app = application(args)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ws_max_size=65536,
            ws_max_queue=16,
            timeout_keep_alive=5,
            proxy_headers=False,
            access_log=False,
        )
        return 0
    except KeyboardInterrupt:
        return 0
    except ImportError:
        response = error_response(
            CapabilityUnavailableError("Install the web/provider/database extras.")
        )
    except (OSError, ValueError, TypeError):
        response = error_response(InvalidInputError("The HTTP service configuration is invalid."))
    except Exception as error:
        response = error_response(error)
    print(json.dumps(response.body, sort_keys=True), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
