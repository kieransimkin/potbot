#!/usr/bin/env python3
"""Explicit S9 operator CLI. Maintenance mode only; never callable by a guest.

The S7 watcher is exclusive. Stop the normal operator service cleanly (with all
leases drained) before using test/activate/rollback/reconcile here. Inspect is
read-only and does not start another journal watcher. This CLI cannot prepare
an untrusted download or compile a new tool; those remain separately admitted.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.operator_cli import parse_manifest
from potbot.tools.shared_tool_store import SharedToolDefinition, SharedToolStore
from potbot.tools.shared_tool_upgrades import (
    SharedToolUpgradeManager,
    ToolTestCase,
    ToolTestSuite,
    stage_name,
)
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


def _uuid(value: str) -> UUID:
    try:
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise InvalidInputError("A canonical operator UUID is required") from None


def _plan(path: Path) -> tuple[SharedToolDefinition, ToolTestSuite, str]:
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve(strict=True) != path or path.stat().st_size > 32_768
            or path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise ScopeDeniedError("S9 test plan must be a private, exact absolute file")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
        if not isinstance(data, dict) or set(data) != {"schema", "expected_old_commit", "replacement", "tests"}:
            raise ValueError
        if data["schema"] != "potbot-shared-tool-upgrade-plan/v1":
            raise ValueError
        record = data["replacement"]
        tests = data["tests"]
        if not isinstance(record, dict) or not isinstance(tests, list):
            raise ValueError
        definition = SharedToolDefinition.from_dict(record)
        case_list = []
        for item in tests:
            if not isinstance(item, dict) or set(item) != {"name", "kind", "argv", "timeout_ms"}:
                raise ValueError
            if not isinstance(item["argv"], list):
                raise ValueError
            case_list.append(ToolTestCase(item["name"], item["kind"],
                                          tuple(item["argv"]), item["timeout_ms"]))
        suite = ToolTestSuite(tuple(case_list))
        prior = data["expected_old_commit"]
        if not isinstance(prior, str) or len(prior) != 40 or any(c not in "0123456789abcdef" for c in prior):
            raise ValueError
        return definition, suite, prior
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
        raise InvalidInputError("S9 test plan is invalid or ambiguous") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--slug", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("test", help="run sandbox tests + cleanup proof")
    prepare.add_argument("--plan", type=Path, required=True)
    activate = actions.add_parser("activate", help="exchange tested candidate into active")
    activate.add_argument("--expected-old-commit", required=True)
    activate.add_argument("--confirm", action="store_true")
    rollback = actions.add_parser("rollback", help="revert to prior source/assets")
    rollback.add_argument("--expected-current-commit", required=True)
    rollback.add_argument("--confirm", action="store_true")
    recover = actions.add_parser("reconcile", help="recover interrupted exchange")
    recover.add_argument("--confirm", action="store_true")
    actions.add_parser("inspect", help="inspect sanitized durable tool-test record")
    actions.add_parser("runtime-pins", help="propose reviewed executable admission pins")
    args = parser.parse_args(argv)
    try:
        from potbot.tools.agent_homes import AgentHomeRegistry
        from potbot.tools.ai_workspace_operator import AiWorkspaceOperator

        paths = parse_manifest(args.manifest)
        agent_id, operation_id = _uuid(args.agent_id), _uuid(args.operation_id)
        ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
        homes = AgentHomeRegistry(ledger, workspace_name=args.workspace_name)
        def approved(workspace: UUID, actor: UUID) -> bool:
            return (workspace == paths.workspace_id and actor == agent_id
                    and homes.lookup(workspace, actor) is not None)

        store = SharedToolStore(ledger=ledger, shared_root=paths.shared_tools,
                                authorize=approved)
        runtime = paths.runtime()
        if args.action in {"inspect", "runtime-pins"}:
            manager = SharedToolUpgradeManager(store=store, runtime=runtime,
                                               homes=homes, authorize=approved)
            data = (manager.inspect(workspace_id=paths.workspace_id, agent_id=agent_id,
                                    operation_id=operation_id)
                    if args.action == "inspect" else
                    manager.runtime_entrypoint_updates(workspace_id=paths.workspace_id,
                                                       agent_id=agent_id, operation_id=operation_id))
        else:
            with AiWorkspaceOperator(workspace_id=paths.workspace_id, ledger=ledger,
                                     runtime=runtime) as operator:
                manager = SharedToolUpgradeManager(
                    store=store, runtime=runtime, homes=homes, authorize=approved,
                    ai_monitor=operator.monitor,
                )
                common = dict(workspace_id=paths.workspace_id, agent_id=agent_id,
                              operation_id=operation_id)
                if args.action == "test":
                    definition, suite, old = _plan(args.plan)
                    if definition.slug != args.slug or not (paths.shared_tools / stage_name(args.slug, operation_id)).is_dir():
                        raise InvalidInputError("S9 test plan does not match staged tool")
                    data = manager.prepare(**common, expected_old_commit=old,
                                           replacement=definition, suite=suite)
                elif args.action == "activate":
                    data = manager.activate(**common, slug=args.slug,
                        expected_old_commit=args.expected_old_commit, confirm=args.confirm)
                elif args.action == "rollback":
                    data = manager.rollback(**common, slug=args.slug,
                        expected_current_commit=args.expected_current_commit, confirm=args.confirm)
                else:
                    data = manager.reconcile(**common, slug=args.slug, confirm=args.confirm)
        print(json.dumps({"schema": "potbot-shared-tool-s9/v1", "data": data}, sort_keys=True))
        return 0
    except Exception as error:
        # No untrusted path, Git diagnostics, tool stdout, key or raw error text.
        from potbot.domain.errors import DomainError
        code = error.code.value if isinstance(error, DomainError) else "internal_error"
        print(json.dumps({"schema": "potbot-shared-tool-s9/v1", "error": code}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
