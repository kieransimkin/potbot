"""Operator-side S11 approval consumer; never invoked inside an HTTP handler.

Claims one already-approved, PostgreSQL-scoped S9 test record; verifies it
against S9's protected journal before promotion. Every unknown result remains
indeterminate. Newly activated executables remain unusable until a separate
verified runtime repin finishes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.errors import ScopeDeniedError, StateMismatchError

if TYPE_CHECKING:
    from potbot.api.ui_tool_upgrades import ToolApprovalClaim, UiToolUpgradeStore
    from potbot.domain.auth import WorkspaceContext
    from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager


class ToolApprovalWorker:
    def __init__(self, *, approvals: UiToolUpgradeStore, manager: SharedToolUpgradeManager,
                 context: WorkspaceContext, proposal_dir: Path, agent_id: UUID,
                 setup_token: str) -> None:
        if not proposal_dir.is_dir() or proposal_dir.is_symlink() or (
                proposal_dir.stat().st_mode & 0o077):
            raise ScopeDeniedError("S11 operator proposal directory must be private")
        if not isinstance(agent_id, UUID):
            raise ScopeDeniedError("S11 approval worker requires enrolled agent identity")
        self.approvals, self.manager, self.context = approvals, manager, context
        if not isinstance(setup_token, str) or len(setup_token) < 32:
            raise ScopeDeniedError("S11 worker requires native-vault setup credential")
        self.agent_id = agent_id
        self.setup_token = setup_token
        self.proposal_dir = proposal_dir.resolve(strict=True)
        runtime = manager.runtime
        home, tools = runtime.home.resolve(strict=True), manager.store.root
        if (self.proposal_dir == home or self.proposal_dir.is_relative_to(home)
                or self.proposal_dir == tools or self.proposal_dir.is_relative_to(tools)):
            raise ScopeDeniedError("S11 operator proposal data must be outside the sandbox")

    def _evidence(self, claim: ToolApprovalClaim) -> dict[str, object]:
        tested = self.manager.inspect(
            workspace_id=self.context.workspace_id,
            agent_id=claim["agent_id"], operation_id=claim["id"],
        )
        for expected, actual in (
            ("id", str(claim["id"])), ("slug", claim["slug"]),
            ("old_commit", claim["old_commit"]),
            ("new_commit", claim["new_commit"]),
            ("test_evidence_sha256", claim["evidence_sha256"]),
        ):
            if tested.get(expected) != actual:
                raise StateMismatchError("S11 published evidence no longer matches S9")
        if tested["state"] != "ready" or tested.get("suite_sha256") != claim["suite_sha256"]:
            raise StateMismatchError("S11 upgrade not in tested/ready state")
        expected = claim["tests"]
        if isinstance(expected, str):
            expected = json.loads(expected)
        actual = [{"name": item["name"], "kind": item["kind"],
                   "elapsedMs": item["elapsed_ms"], "passed": item["passed"]}
                  for item in tested.get("test_results", [])]
        if expected != actual or len(actual) != claim["passed_tests"]:
            raise StateMismatchError("S11 user-visible test summary differs from S9 evidence")
        return tested

    def _save_pins(self, operation: UUID, proposal: dict[str, object]) -> None:
        """Write-only an operator proposal, not an automatic rootfs manifest edit."""
        if proposal.get("requires_operator_runtime_repin") is not True:
            raise StateMismatchError("S11 runtime repin proposal is incomplete")
        data = json.dumps({"schema": "potbot-s11-runtime-repin/v1",
                           "operationId": str(operation), "proposal": proposal},
                          sort_keys=True, separators=(",", ":")).encode()
        if len(data) > 32768:
            raise StateMismatchError("S11 repin proposal exceeds size limit")
        target = self.proposal_dir / f"s11-repin-{operation.hex}.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise StateMismatchError("S11 operator repin proposal could not be persisted")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        fd = os.open(self.proposal_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def run_once(self) -> dict[str, object]:
        claim = self.approvals.claim(self.context, agent_id=self.agent_id,
                                    setup_token=self.setup_token)
        if claim is None:
            return {"state": "idle"}
        if claim["agent_id"] != self.agent_id:
            raise ScopeDeniedError("S11 approval claim belongs to a different agent")
        operation = claim["id"]
        token = claim["worker_token"]
        if not isinstance(operation, UUID) or not isinstance(token, UUID):
            raise StateMismatchError("S11 worker claim identity unavailable")
        confirmed = False
        try:
            self._evidence(claim)
            result = self.manager.activate(
                workspace_id=self.context.workspace_id, agent_id=claim["agent_id"],
                operation_id=operation, slug=claim["slug"],
                expected_old_commit=claim["old_commit"], confirm=True,
            )
            if result.get("state") != "active" or result.get("new_commit") != claim["new_commit"]:
                raise StateMismatchError("S11 filesystem activation receipt differs")
            proposal = self.manager.runtime_entrypoint_updates(
                workspace_id=self.context.workspace_id,
                agent_id=claim["agent_id"], operation_id=operation,
            )
            if proposal.get("tested_sha256") != claim["evidence_sha256"]:
                raise StateMismatchError("S11 executable proposal differs from tested evidence")
            self._save_pins(operation, proposal)
            confirmed = True
        finally:
            # Even after an exception, an upgrade might physically have occurred.
            # Never release an unknown outcome as a successful activation.
            self.approvals.finish(self.context, operation=operation, worker_token=token,
                                  active=confirmed)
        return {"id": str(operation), "state": "awaiting_repin"}

    def confirm_repin(self, *, operation: UUID, agent_id: UUID) -> dict[str, object]:
        """Host-only after operator reloads pinned executable hashes and image."""
        data = self.approvals.inspect(self.context, operation)
        digest = data.get("evidenceSha256")
        if not isinstance(digest, str) or data["state"] != "awaiting_repin":
            raise StateMismatchError("S11 operation does not await runtime pins")
        inspected = self.manager.inspect(workspace_id=self.context.workspace_id,
                                         agent_id=agent_id, operation_id=operation)
        if inspected["state"] != "active" or inspected["test_evidence_sha256"] != digest:
            raise StateMismatchError("S11 operator reloaded a different tool revision")
        proposal = self.manager.runtime_entrypoint_updates(
            workspace_id=self.context.workspace_id, agent_id=agent_id, operation_id=operation,
        )
        live = dict(self.manager.runtime.tool_entrypoints)
        required = proposal["pins"]
        retired = proposal["remove_prior_pins"]
        if not isinstance(required, dict) or not isinstance(retired, list) or any(
                live.get(path) != digest for path, digest in required.items()
        ) or any(path in live for path in retired):
            raise StateMismatchError("S11 live runtime executable pins differ from approved release")
        return self.approvals.confirm_repin(self.context, operation=operation,
                                           tested_sha256=digest)
