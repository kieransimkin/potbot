#!/usr/bin/env python3
"""S10 host-only staged build followed by S9 sandbox test admission.

Run in an operator maintenance window with normal watcher/service quiesced.
No model-controlled command or automatic network fetch is accepted.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import DomainError, InvalidInputError, ScopeDeniedError
from potbot.tools.tool_stage_s10 import ToolBuildPlan, build_and_stage, command_digest


def _json_file(path: Path) -> dict[str, object]:
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 16384 or path.stat().st_mode & 0o022):
        raise ScopeDeniedError("Operator build plan must be an exact private file")

    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise InvalidInputError("Duplicate operator build-plan key")
            result[key] = value
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise InvalidInputError("Invalid operator build-plan JSON") from None
    if not isinstance(data, dict) or set(data) != {"schema", "argv", "sha256", "timeoutMs"}:
        raise InvalidInputError("Invalid S10 build-plan fields")
    if data["schema"] != "potbot-s10-build-plan/v1":
        raise InvalidInputError("Unknown S10 build-plan version")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--agent-id", type=UUID, required=True)
    parser.add_argument("--operation-id", type=UUID, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--build-plan", type=Path, required=True)
    parser.add_argument("--upgrade-plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
        from potbot.tools.agent_homes import AgentHomeRegistry
        from potbot.tools.operator_cli import parse_manifest
        from potbot.tools.shared_tool_store import SharedToolStore
        from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager
        from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
        from tool_upgrade_s9 import _plan

        operator_paths = parse_manifest(args.manifest)
        replacement, suite, expected_old = _plan(args.upgrade_plan)
        build = _json_file(args.build_plan)
        if not isinstance(build["argv"], list) or any(
                not isinstance(item, str) for item in build["argv"]):
            raise InvalidInputError("S10 argv must be an exact list")
        argv = tuple(build["argv"])
        if command_digest(argv) != build["sha256"]:
            raise ScopeDeniedError("S10 build command hash mismatch")
        plan = ToolBuildPlan(
            operator_paths.workspace_id, args.agent_id, args.operation_id,
            replacement.slug, argv, build["sha256"], expected_old,
            replacement.commit, build["timeoutMs"],
        )
        ledger = WorkspaceLeaseLedger(operator_paths.controller_ledger,
                                      workspace_home=operator_paths.home)
        homes = AgentHomeRegistry(ledger, workspace_name=args.workspace_name)
        def authorized(workspace: UUID, agent: UUID) -> bool:
            return workspace == operator_paths.workspace_id and agent == args.agent_id \
                and homes.lookup(workspace, agent) is not None
        store = SharedToolStore(ledger=ledger, shared_root=operator_paths.shared_tools,
                                authorize=authorized)
        with AiWorkspaceOperator(workspace_id=operator_paths.workspace_id,
                                 ledger=ledger, runtime=operator_paths.runtime()) as operator:
            receipt = build_and_stage(plan=plan, operator=operator, store=store, homes=homes,
                                      evidence_dir=args.evidence_dir, built_tree=args.build_root)
            manager = SharedToolUpgradeManager(
                store=store, runtime=operator.runtime, homes=homes,
                authorize=authorized, ai_monitor=operator.monitor,
            )
            tested = manager.prepare(
                workspace_id=operator_paths.workspace_id, agent_id=args.agent_id,
                operation_id=args.operation_id, expected_old_commit=expected_old,
                replacement=replacement, suite=suite,
            )
        print(json.dumps({"schema": "potbot-s10-pipeline/v1", "staging": receipt,
                          "tests": tested}, sort_keys=True))
        return 0
    except Exception as exc:
        code = exc.code.value if isinstance(exc, DomainError) else "internal_error"
        print(json.dumps({"error": code, "schema": "potbot-s10-pipeline/v1"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
