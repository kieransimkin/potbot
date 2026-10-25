"""Run the scoped REST API, dashboard and WebSocket service independently of workers."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
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
    from potbot.api.jobs_cli import protected_values
    from potbot.api.observability import EventLogger
    from potbot.api.openai_compat import OpenAICompatibility
    from potbot.api.server import ServerSettings, create_app
    from potbot.api.tasks import DevelopmentBearerAuth
    from potbot.controller.execution_events import ExecutionEventService
    from potbot.controller.jobs import JobService
    from potbot.controller.reasoning_budget import ReasoningBudgetJournal
    from potbot.controller.reasoning_investigation import ReasoningInvestigator
    from potbot.controller.reasoning_runtime import PostgresReasoningStore, PostgresSteeringInbox
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
    events = EventLogger(
        log_path=args.log_file,
        log_content=args.log_content,
        protected_values=protected_values(database, credentials) if args.log_content else (),
    )
    state_store = PostgresReasoningStore(database) if args.reasoning_mode == "tree" else None
    budget_journal = ReasoningBudgetJournal(database, policy) if state_store is not None else None
    investigator = (
        ReasoningInvestigator(
            root=args.reasoning_root, state_store=state_store,
            steering_inbox=PostgresSteeringInbox(database),
            max_rounds=args.reasoning_rounds,
            budget_journal=budget_journal,
        ) if args.reasoning_mode == "tree" else None
    )
    compatibility = OpenAICompatibility(directory.catalog, policy, credentials,
                                         log=events, reasoning_investigator=investigator)
    events.emit("server.ready", host=args.host, port=args.port,
                content_logging=args.log_content, compatibility_api=True)
    from potbot.tools.worker_telemetry import OperatorTelemetryReader

    audit_root = os.environ.get("POTBOT_OPERATOR_AUDIT_ROOT")
    monitor_reader = OperatorTelemetryReader(Path(audit_root)) if audit_root else None
    gallery_fields = (
        "browser_gallery_index", "browser_artifact_root", "browser_rootfs",
        "browser_home", "browser_model_staging", "browser_shared_tools",
    )
    configured = tuple(getattr(args, field, None) for field in gallery_fields)
    browser_gallery = None
    if any(path is not None for path in configured):
        from potbot.domain.errors import InvalidInputError
        from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
        from potbot.tools.browser_gallery import BrowserEvidenceGallery
        if any(path is None for path in configured):
            raise InvalidInputError("Browser gallery needs all six explicit operator paths")
        index, artifact_root, rootfs, home, staging, tools = configured
        browser_gallery = BrowserEvidenceGallery(
            index, ArtifactStore(database, LocalArtifactBackend(artifact_root)),
            forbidden_roots=(rootfs, home, staging, tools, artifact_root),
        )
    app = create_app(
        task_service=TaskService(database),
        job_service=JobService(database),
        authenticate=authenticate,
        model_catalog=directory.catalog,
        discover_models=directory.discover,
        invocation_reader=gateway.inspect,
        invocation_list_reader=gateway.list_for_job,
        execution_events=ExecutionEventService(database),
        openai_compat=compatibility,
        reasoning_store=state_store,
        reasoning_budget=budget_journal,
        worker_telemetry_reader=monitor_reader,
        browser_gallery=browser_gallery,
        settings=settings,
    )
    from potbot.api.ui_resources import register_ui_routes
    from potbot.api.ui_api_setup import register_api_setup_routes

    register_ui_routes(app, database, authenticate, settings.allowed_origins)
    # The separate setup-token capability is resolved at request time. Ordinary
    # workspace bearers can never mutate the host's credential vault alone.
    # The setup bridge is only enabled on an operator loopback listener.
    if args.host == "localhost" or args.host in {"127.0.0.1", "::1"}:
        register_api_setup_routes(
            app, database, authenticate, settings.allowed_origins, credentials,
            changed=directory.invalidate_provider,
        )
        from potbot.api.ui_api_setup import ApiSetupCredentials
        from potbot.api.ui_tool_upgrades import register_tool_upgrade_routes

        register_tool_upgrade_routes(
            app, database, authenticate, settings.allowed_origins,
            ApiSetupCredentials(credentials).authorize_setup,
        )
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot serve", description=__doc__)
    add_scope_arguments(parser)
    parser.add_argument("--provider-policy", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1", help="literal bind IP, default loopback")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allowed-origin", action="append", default=[], help="exact browser origin; repeatable"
    )
    parser.add_argument("--log-file", type=Path, default=None,
                        help="rotating structured event log path")
    parser.add_argument("--reasoning-mode", choices=("tree", "fixed"), default="tree",
                        help="tree enables R3/R4; fixed retains the earlier three-pass model")
    parser.add_argument("--reasoning-rounds", type=int, default=2,
                        help="bounded evidence-guided reasoning rounds (1-4)")
    parser.add_argument("--reasoning-root", type=Path, default=Path(".potbot/reasoning"),
                        help="trusted private run snapshot directory (single writer)")
    parser.add_argument("--browser-gallery-index", type=Path,
                        help="private derived SQLite browser index; requires all browser path flags")
    parser.add_argument("--browser-artifact-root", type=Path,
                        help="exact existing ArtifactStore local backend root")
    parser.add_argument("--browser-rootfs", type=Path, help="qualified sandbox rootfs")
    parser.add_argument("--browser-home", type=Path, help="workspace persistent home")
    parser.add_argument("--browser-model-staging", type=Path, help="workspace model staging root")
    parser.add_argument("--browser-shared-tools", type=Path, help="shared tool root")
    parser.add_argument("--log-content", action="store_true",
                        help="opt in to redacted prompt/response logging (sensitive)")
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
