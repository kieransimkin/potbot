"""Local operator commands for repository capture and qualified tool execution."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.execution import ToolDefinition, ToolRegistry


def default_tools() -> ToolRegistry:
    python = sys.executable
    return ToolRegistry(
        (
            ToolDefinition(
                "inspect.files",
                "inspect",
                (
                    python,
                    "-I",
                    "-B",
                    "-c",
                    "from pathlib import Path; print('\\n'.join(sorted(str(p) for p in Path('.').rglob('*') if p.is_file())))",
                ),
                mutates=False,
            ),
            ToolDefinition(
                "compile.python", "compile", (python, "-I", "-B", "-m", "compileall", "-q", ".")
            ),
            ToolDefinition(
                "test.python",
                "test",
                (python, "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"),
            ),
        )
    )


def _exit_status(command: str, result: dict[str, Any]) -> int:
    """Inspection succeeds independently of the inspected command's outcome."""
    if command not in {"run", "reconcile"}:
        return 0
    completion = result.get("completion")
    if not isinstance(completion, dict):
        return 1
    outcome = completion.get("outcome")
    if outcome == "completed":
        return 0
    if outcome == "timeout":
        return 124
    if outcome == "cancelled":
        return 130
    code = completion.get("exit_code")
    if outcome == "nonzero_exit" and type(code) is int:
        if 1 <= code <= 255:
            return code
        if -127 <= code < 0:
            return 128 - code
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot execution", description=__doc__)
    parser.add_argument("--dev-auth", action="store_true", required=True)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--principal", type=UUID, required=True)
    parser.add_argument("--schema", default="potbot")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--receipts", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="capture a task baseline and allocate an isolated candidate"
    )
    prepare.add_argument("task_id", type=UUID)
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument(
        "--include", action="append", default=[], help="explicit untracked/generated relative input"
    )
    prepare.add_argument(
        "--exclude", action="append", default=[], help="relative sensitive input to exclude"
    )
    run = commands.add_parser("run", help="run one registered operation under the active contract")
    run.add_argument("task_id", type=UUID)
    run.add_argument("--candidate", type=UUID, required=True)
    run.add_argument("--tool", choices=default_tools().inventory(), required=True)
    run.add_argument("--key", required=True)
    inspect = commands.add_parser("inspect", help="read a durable action, run and completion")
    inspect.add_argument("action_id", type=UUID)
    reconcile = commands.add_parser(
        "reconcile", help="reconcile an interrupted run without repeating its command"
    )
    reconcile.add_argument("run_id", type=UUID)
    resume = commands.add_parser(
        "resume", help="reconstruct unresolved operations and budget; does not start work"
    )
    resume.add_argument("task_id", type=UUID)
    args = parser.parse_args(argv)

    from potbot.api.wire import error_response
    from potbot.controller.budgets import BudgetLedger
    from potbot.controller.execution import ExecutionStore
    from potbot.controller.tasks import TaskService, json_record
    from potbot.credentials import resolve_postgres_dsn
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.errors import InvalidInputError, StateMismatchError
    from potbot.domain.execution import ActionProposal, CapabilityContext
    from potbot.domain.tasks import TaskStatus
    from potbot.models.configuration import EnvironmentManifest, store_environment
    from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
    from potbot.persistence.database import Database
    from potbot.tools.gateway import ToolGateway
    from potbot.tools.receipts import RunReceiptStore
    from potbot.tools.repository import (
        CandidateWorkspaceManager,
        RepositoryStateStore,
        SnapshotProfile,
    )

    try:
        database = Database(resolve_postgres_dsn(), args.schema)
        context = WorkspaceContext(args.workspace, args.principal)
        from potbot.workspace_sandbox import load_workspace_sandbox

        sandbox = load_workspace_sandbox(database, context)
        artifact_root = (
            sandbox.confine(args.artifacts, area="artifacts")
            if args.artifacts is not None
            else sandbox.path("artifacts") / "objects"
        )
        candidate_root = (
            sandbox.confine(args.candidates, area="candidates")
            if args.candidates is not None
            else sandbox.path("candidates")
        )
        receipt_root = (
            sandbox.confine(args.receipts, area="receipts")
            if args.receipts is not None
            else sandbox.path("receipts")
        )
        artifacts = ArtifactStore(database, LocalArtifactBackend(artifact_root))
        manager = CandidateWorkspaceManager(database, artifacts, candidate_root)
        registry = default_tools()
        gateway = ToolGateway(
            database,
            artifacts,
            manager,
            registry,
            CapabilityContext(registry.inventory()),
            RunReceiptStore(receipt_root),
            workspace_sandbox=sandbox,
        )
        tasks = TaskService(database)
        result: dict[str, Any]
        if args.command == "prepare":
            task = tasks.inspect(context, args.task_id)
            if task["lifecycle"] not in {"submitted", "initializing", "running"}:
                raise StateMismatchError(
                    "Task must be active before preparing an execution workspace."
                )
            config = task["configuration"]
            manifest = EnvironmentManifest(
                UUID(config["id"]),
                config["content_hash"],
                platform.platform(),
                f"Python {platform.python_version()} at {sys.executable}",
                (("python", platform.python_version()),),
                secret_refs=tuple(config["content"]["secret_refs"].items()),
            )
            with database.transaction(context) as conn:
                environment = store_environment(conn, context, manifest)
            source = sandbox.confine(args.source, area="repository", must_exist=True)
            state = RepositoryStateStore(database, artifacts).capture(
                context,
                UUID(task["repository_id"]),
                source,
                environment["id"],
                profile=SnapshotProfile(
                    untracked_paths=tuple(args.include), excluded_paths=tuple(args.exclude)
                ),
            )
            candidate = manager.allocate(context, state["id"])
            if task["lifecycle"] == "submitted":
                task = tasks.transition(
                    context,
                    args.task_id,
                    task["update_token"],
                    TaskStatus.INITIALIZING,
                    "Captured repository execution inputs.",
                    idempotency_key=f"prepare-init:{state['id']}",
                )
            if task["lifecycle"] == "initializing":
                task = tasks.transition(
                    context,
                    args.task_id,
                    task["update_token"],
                    TaskStatus.RUNNING,
                    "Isolated candidate is ready.",
                    idempotency_key=f"prepare-run:{state['id']}",
                )
            result = {
                "task_id": str(args.task_id),
                "state": state,
                "candidate": candidate,
                "environment_id": str(environment["id"]),
                "lifecycle": task["lifecycle"],
            }
        elif args.command == "run":
            task = tasks.inspect(context, args.task_id)
            store = ExecutionStore(database, registry)
            command_key = f"execution:{args.key}:proposal"
            with database.transaction(context) as conn:
                replay = conn.execute(
                    "SELECT result FROM command_results WHERE workspace_id=%s AND operation='propose_action' AND idempotency_key=%s",
                    (context.workspace_id, command_key),
                ).fetchone()
            if replay is not None:
                action_id = UUID(replay["result"]["id"])
                existing = store.inspect(context, action_id)
                if (
                    existing["action"]["episode_id"] != str(args.task_id)
                    or existing["action"]["kind"] != args.tool
                ):
                    raise StateMismatchError("Command key belongs to a different task or tool.")
                if existing["run"] is not None:
                    if existing["run"]["candidate_id"] != str(args.candidate):
                        raise StateMismatchError("Command key belongs to a different candidate.")
                    if existing["completion"] is None:
                        raise StateMismatchError(
                            "Run requires explicit reconciliation before further work."
                        )
                    result = gateway.reconcile(context, UUID(existing["run"]["id"]))
                    print(json.dumps({"schema_version": 1, "data": json_record(result)}, indent=2))
                    return _exit_status(args.command, result)
            else:
                candidate = manager.get(context, args.candidate)
                inputs = store.create_inputs(
                    context,
                    args.task_id,
                    UUID(task["current_contract"]["id"]),
                    candidate["state_id"],
                    f"execution:{args.key}:inputs",
                )
                proposal = store.propose(
                    context, UUID(inputs["node_id"]), ActionProposal(args.tool), command_key
                )
                action_id = UUID(proposal["id"])
            definition = registry.get(args.tool)
            lease = manager.claim(context, args.candidate, f"cli:{args.key}", ttl_seconds=120)
            try:
                reservation = BudgetLedger(database).reserve(
                    context,
                    args.task_id,
                    {"calls": 1, "tool_ms": definition.timeout_ms},
                    f"execution:{args.key}:budget",
                )
            except BaseException:
                # No dispatch intent exists yet, so a denied reservation cannot
                # leave an otherwise idle candidate leased to this CLI process.
                manager.release(context, lease)
                raise
            try:
                result = gateway.execute(context, action_id, lease, UUID(reservation["id"]))
            except BaseException:
                # Keep running/indeterminate ownership for explicit reconciliation.
                current = store.inspect(context, action_id)
                if current["run"] is None:
                    try:
                        BudgetLedger(database).release(
                            context,
                            UUID(reservation["id"]),
                            "Admission rejected before process dispatch.",
                            confirmed_not_started=True,
                        )
                    finally:
                        manager.release(context, lease)
                raise
            else:
                manager.release(context, lease)
        elif args.command == "inspect":
            result = gateway.store.inspect(context, args.action_id)
        elif args.command == "reconcile":
            result = gateway.reconcile(context, args.run_id)
        elif args.command == "resume":
            result = gateway.resume(context, args.task_id)
        else:
            raise InvalidInputError("Unknown execution command.")
        print(json.dumps({"schema_version": 1, "data": json_record(result)}, indent=2))
        return _exit_status(args.command, result)
    except Exception as error:
        response = error_response(error)
        print(json.dumps(response.body, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
