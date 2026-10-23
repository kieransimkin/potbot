"""Inspect durable jobs and run an explicitly configured hosted-generation worker."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from potbot.domain.auth import WorkspaceContext
    from potbot.models.hosted_gateway import HostedInferenceGateway
    from potbot.models.hosted_policy import HostedPolicy
    from potbot.persistence.database import Database
    from potbot.service_credentials import ServiceCredentials


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-id", "--workspace", required=True, type=UUID)
    parser.add_argument("--principal-id", "--principal", required=True, type=UUID)
    parser.add_argument("--schema", default="potbot")


def database_context(args: argparse.Namespace) -> tuple[Database, WorkspaceContext]:
    from potbot.credentials import resolve_postgres_dsn
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.database import Database

    return (
        Database(resolve_postgres_dsn(), schema=args.schema),
        WorkspaceContext(args.workspace_id, args.principal_id),
    )


def protected_values(database: Database, credentials: ServiceCredentials) -> tuple[str, ...]:
    """Known secrets are capture filters, never job payloads or printed settings."""
    from psycopg.conninfo import conninfo_to_dict

    from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
    from potbot.domain.syn_credentials import SynCredentialScope
    from potbot.service_credentials import PROVIDER_ENVIRONMENT

    values = [database.dsn]
    password = conninfo_to_dict(database.dsn).get("password")
    if isinstance(password, str) and password:
        values.append(password)
    references = ["secret://api/access-token"] + [
        f"secret://providers/{provider}" for provider in PROVIDER_ENVIRONMENT
    ]
    # Invalid scope configuration must not turn off redaction of injected tokens.
    syn_token = credentials.environment.get("POTBOT_SYN_TOKEN")
    if syn_token:
        values.append(syn_token)
    if credentials.environment.get("POTBOT_SYN_URL"):
        try:
            references.append(
                SynCredentialScope.from_environment(credentials.environment).reference
            )
        except InvalidInputError:
            pass
    for reference in references:
        try:
            values.append(credentials.resolve(reference))
        except CapabilityUnavailableError:
            # A worker needs only the credential for the selected provider.
            # Other locked/missing credentials must not disable all work.
            continue
    return tuple(dict.fromkeys(values))


def hosted_gateway(
    database: Database,
    policy: HostedPolicy | None = None,
    credentials: ServiceCredentials | None = None,
) -> HostedInferenceGateway:
    from potbot.models.hosted_gateway import HostedInferenceGateway
    from potbot.models.hosted_policy import HostedPolicy
    from potbot.service_credentials import ServiceCredentials

    credentials = credentials or ServiceCredentials()
    return HostedInferenceGateway(
        database,
        policy or HostedPolicy(),
        credentials.resolve,
        protected_values=protected_values(database, credentials),
    )


def _integer(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("expected an integer") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected an integer from {minimum} to {maximum}")
        return number

    return parse


def _interval(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a polling interval in seconds") from None
    if not math.isfinite(number) or not 0.1 <= number <= 30:
        raise argparse.ArgumentTypeError("polling interval must be between 0.1 and 30 seconds")
    return number


def _read_object(path: Path, *, maximum: int) -> dict[str, Any]:
    from potbot.domain.errors import InvalidInputError

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError

    try:
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise InvalidInputError("The input must be a readable, bounded JSON object.") from None


def _print_result(data: object) -> None:
    from potbot.api.wire import success_response
    from potbot.controller.tasks import json_record

    print(json.dumps(json_record(success_response(uuid4(), data).body), sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="potbot jobs", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker", help="run hosted jobs; no automatic replay after loss")
    worker.add_argument("--provider-policy", required=True, type=Path)
    worker.add_argument("--once", action="store_true", help="attempt one job and exit")
    worker.add_argument("--worker-id", default=None)
    worker.add_argument("--poll-interval", type=_interval, default=1.0)
    queue = commands.add_parser("queue", help="inspect workspace queue counts")
    listing = commands.add_parser("list", help="read a newest-first job page")
    listing.add_argument(
        "--state",
        choices=(
            "queued",
            "running",
            "cancelling",
            "succeeded",
            "failed",
            "cancelled",
            "indeterminate",
        ),
    )
    listing.add_argument("--after", help="opaque next_cursor from the previous jobs page")
    listing.add_argument("--limit", type=_integer(1, 200), default=50)
    inspect = commands.add_parser("inspect", help="inspect one accessible job")
    inspect.add_argument("job_id", type=UUID)
    submit = commands.add_parser("submit", help="submit a private generation-job JSON document")
    submit.add_argument("--input", required=True, type=Path)
    submit.add_argument("--key", required=True, help="stable idempotency key for this submission")
    cancel = commands.add_parser("cancel", help="request cancellation without replaying work")
    cancel.add_argument("job_id", type=UUID)
    cancel.add_argument("--key", required=True)
    events = commands.add_parser("events", help="read persisted events after an integer cursor")
    events.add_argument("--job-id", type=UUID)
    events.add_argument("--after", type=_integer(0, 2**53 - 1), default=0)
    events.add_argument("--limit", type=_integer(1, 200), default=100)
    expire = commands.add_parser("expire", help="mark expired worker leases indeterminate")
    expire.add_argument("--limit", type=_integer(1, 200), default=200)
    invocation = commands.add_parser("invocation", help="inspect safe hosted invocation metadata")
    invocation.add_argument("invocation_id", type=UUID)
    invocations = commands.add_parser("invocations", help="find all hosted invocations for one job")
    invocations.add_argument("job_id", type=UUID)
    reconcile = commands.add_parser("reconcile", help="settle known usage for a terminal job")
    reconcile.add_argument("invocation_id", type=UUID)
    reconcile.add_argument(
        "--actual", required=True, type=Path, help="JSON with four usage dimensions"
    )
    for command in (
        worker,
        queue,
        listing,
        inspect,
        submit,
        cancel,
        events,
        expire,
        invocation,
        invocations,
        reconcile,
    ):
        add_scope_arguments(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # Parsing help must work before optional PostgreSQL/provider dependencies
    # or a credential vault are installed and configured.
    from potbot.api.wire import error_response
    from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

    try:
        from potbot.controller.jobs import JobService
        from potbot.domain.jobs import JobSpec, identifier

        payload = None
        actual = None
        policy = None
        if args.command == "submit":
            payload = _read_object(args.input, maximum=1_048_576)
            JobSpec.from_dict(payload)
        elif args.command == "reconcile":
            actual = _read_object(args.actual, maximum=4096)
        elif args.command == "worker":
            from potbot.models.hosted_policy import HostedPolicy

            policy = HostedPolicy.read(args.provider_policy)
            args.worker_id = identifier(args.worker_id or f"worker-{uuid4().hex}", "Worker")

        database, context = database_context(args)
        service = JobService(database)
        if args.command == "queue":
            result: object = service.queue(context)
        elif args.command == "list":
            result = service.list(context, state=args.state, after=args.after, limit=args.limit)
        elif args.command == "inspect":
            result = service.inspect(context, args.job_id)
        elif args.command == "submit":
            result = service.submit(context, payload, args.key)
        elif args.command == "cancel":
            result = service.cancel(context, args.job_id, args.key)
        elif args.command == "events":
            result = service.events(context, args.job_id, after=args.after, limit=args.limit)
        elif args.command == "expire":
            result = {
                "expired": service.expire(context, limit=args.limit),
                "automatic_retry": False,
            }
        elif args.command == "invocation":
            result = hosted_gateway(database).inspect(context, args.invocation_id)
        elif args.command == "invocations":
            result = hosted_gateway(database).list_for_job(context, args.job_id)
        elif args.command == "reconcile":
            assert actual is not None
            result = hosted_gateway(database).reconcile(context, args.invocation_id, actual=actual)
        else:
            from potbot.controller.job_runner import HostedJobRunner

            # Fail before announcing a worker if these UUIDs do not match the
            # restricted application login's provisioned workspace authority.
            with database.transaction(context):
                pass
            runner = HostedJobRunner(service, hosted_gateway(database, policy))
            if args.once:
                job = runner.run_once(context, args.worker_id)
                result = {"job": job, "idle": job is None}
            else:
                _print_result({"worker_id": args.worker_id, "status": "listening"})
                while True:
                    job = runner.run_once(context, args.worker_id)
                    if job is None:
                        time.sleep(args.poll_interval)
                    else:
                        _print_result({"job": job, "worker_id": args.worker_id})
        _print_result(result)
        return 0
    except KeyboardInterrupt:
        _print_result({"status": "stopped", "incomplete_jobs_require_inspection": True})
        return 130
    except ImportError:
        response = error_response(
            CapabilityUnavailableError("Install the command's optional dependencies.")
        )
    except (OSError, ValueError, TypeError):
        response = error_response(InvalidInputError("The job command configuration is invalid."))
    except Exception as error:
        response = error_response(error)
    print(json.dumps(response.body, sort_keys=True), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
