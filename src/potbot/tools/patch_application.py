"""Apply a frozen coding output to a fenced isolated candidate through the gateway."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.controller.coding import CodingStore, coding_row
from potbot.controller.execution import ExecutionStore
from potbot.controller.tasks import json_record
from potbot.domain.auth import WorkspaceContext
from potbot.domain.coding import within_scope
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, ToolDefinition
from potbot.domain.serialization import canonical_json
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event
from potbot.tools.gateway import ToolGateway
from potbot.tools.repository import CandidateWorkspaceManager


def patch_runtime_roots() -> tuple[Path, ...]:
    """Mount just the trusted standalone adapter, never controller artifact storage."""
    return (Path(__file__).with_name("patch_worker.py").resolve(),)


def patch_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        "apply_patch",
        "patch",
        (sys.executable, "-I", str(patch_runtime_roots()[0])),
        mutates=True,
        accepts_stdin=True,
        timeout_ms=30_000,
        max_output_bytes=65_536,
    )


def _key(prefix: str, key: str) -> str:
    return prefix + ":" + hashlib.sha256(key.encode()).hexdigest()


class PatchApplicationService:
    def __init__(
        self,
        database: Database,
        artifacts: ArtifactStore,
        candidates: CandidateWorkspaceManager,
        gateway: ToolGateway,
    ) -> None:
        self.database, self.artifacts = database, artifacts
        self.candidates, self.gateway = candidates, gateway
        self.coding = CodingStore(database, artifacts)
        self.execution = ExecutionStore(database, gateway.registry)
        self.budgets = BudgetLedger(database)

    def _prepare(
        self,
        context: WorkspaceContext,
        coding_result_id: UUID,
        key: str,
        candidate_id: UUID | None,
        imported_from: str | None,
    ) -> dict[str, Any]:
        if imported_from is not None and (
            not isinstance(imported_from, str)
            or not imported_from.strip()
            or len(imported_from) > 2000
            or "\x00" in imported_from
        ):
            raise InvalidInputError("Patch import provenance must be explicit and bounded.")
        request = {
            "coding_result_id": str(coding_result_id),
            "candidate_id": str(candidate_id) if candidate_id else None,
            "imported_from": imported_from,
        }
        with self.database.transaction(context) as conn:

            def prepare() -> dict[str, Any]:
                result = coding_row(conn, context, "coding_results", coding_result_id)
                if result["patch_artifact_id"] is None:
                    raise InvalidInputError(
                        "A classified coding inability cannot create a candidate."
                    )
                handoff = coding_row(
                    conn, context, "implementation_contracts", result["implementation_id"]
                )
                if result["base_state_id"] != handoff["base_state_id"]:
                    raise StateMismatchError("Coding result does not match the frozen base.")
                # Read and integrity-check the patch before allocating any workspace.
                patch = self.artifacts.read_in_transaction(
                    conn, context, result["patch_artifact_id"]
                )
                if hashlib.sha256(patch).hexdigest() != result["content"]["patch_sha256"]:
                    raise InvalidInputError("Coding patch digest differs from its response.")
                episode = conn.execute(
                    "SELECT status,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, result["episode_id"]),
                ).fetchone()
                if (
                    episode["status"] != "running"
                    or episode["active_contract_id"] != handoff["contract_revision_id"]
                ):
                    raise StateMismatchError(
                        "Patch requires a running task and the active handoff."
                    )
                # Subsequent service calls own their own transactions; do not hold
                # an episode row lock here while those calls acquire their locks.
                if candidate_id is None:
                    candidate = self.candidates.allocate(context, result["base_state_id"])
                else:
                    candidate = self.candidates.get(context, candidate_id)
                    if candidate["state_id"] != result["base_state_id"]:
                        raise StateMismatchError(
                            "Import target does not match the exact patch base."
                        )
                    if imported_from is None:
                        raise InvalidInputError(
                            "Importing a patch requires its explicit provenance."
                        )
                scopes = tuple(handoff["content"]["spec"]["permitted_paths"])
                stdin = self.artifacts.put(
                    context,
                    canonical_json(
                        {
                            "schema_version": 1,
                            "patch_base64": base64.b64encode(patch).decode("ascii"),
                            "permitted_paths": list(scopes),
                        }
                    ).encode("utf-8"),
                    "application/vnd.potbot.patch-input+json",
                )
                inputs = self.execution.create_inputs(
                    context,
                    result["episode_id"],
                    handoff["contract_revision_id"],
                    result["base_state_id"],
                    _key("patch-inputs", key),
                )
                action = self.execution.propose(
                    context,
                    UUID(inputs["node_id"]),
                    ActionProposal(
                        "apply_patch",
                        output_paths=scopes,
                        expected_output="A scoped text patch applied to the exact base; applicability alone is not verification.",
                    ),
                    _key("patch-proposal", key),
                    deployment_id=result["deployment_id"],
                    stdin_artifact_id=stdin["id"],
                )
                identity = uuid4()
                conn.execute(
                    "INSERT INTO patch_application_intents(workspace_id,id,episode_id,coding_result_id,implementation_id,candidate_id,"
                    "base_state_id,patch_artifact_id,action_id,imported_from) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        result["episode_id"],
                        coding_result_id,
                        result["implementation_id"],
                        candidate["id"],
                        result["base_state_id"],
                        result["patch_artifact_id"],
                        UUID(action["id"]),
                        imported_from,
                    ),
                )
                append_event(
                    conn,
                    context,
                    "patch_application_prepared",
                    episode_id=result["episode_id"],
                    details={
                        "application_id": str(identity),
                        "candidate_id": str(candidate["id"]),
                        "coding_result_id": str(coding_result_id),
                        "imported_from": imported_from,
                    },
                )
                return dict(
                    json_record(coding_row(conn, context, "patch_application_intents", identity))
                )

            return idempotent_command(
                conn, context, "prepare_patch_application", key, request, prepare
            )

    @staticmethod
    def _inspect(conn: Any, context: WorkspaceContext, application_id: UUID) -> dict[str, Any]:
        intent = coding_row(conn, context, "patch_application_intents", application_id)
        result = conn.execute(
            "SELECT run_id,result_state_id,status,details,created_at AS completed_at FROM patch_applications WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, application_id),
        ).fetchone()
        return dict(
            json_record(
                {
                    **intent,
                    "status": "pending",
                    "result_state_id": None,
                    **(dict(result) if result else {}),
                    "verification_status": "unverified",
                }
            )
        )

    def inspect(self, context: WorkspaceContext, application_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return self._inspect(conn, context, application_id)

    def _finish(
        self, context: WorkspaceContext, intent: dict[str, Any], execution: dict[str, Any]
    ) -> dict[str, Any]:
        completion = execution["completion"]
        if completion is None:
            raise IndeterminateExecutionError(
                "Patch application has no confirmed execution receipt."
            )
        try:
            report = json.loads(
                self.artifacts.read(context, UUID(completion["stdout_artifact_id"]))
            )
        except (ValueError, UnicodeError):
            report = {"status": "rejected", "error_code": "schema_violation"}
        if not isinstance(report, dict):
            report = {"status": "rejected", "error_code": "schema_violation"}
        base = self.candidates.states.get(context, UUID(intent["base_state_id"]))
        resulting = self.candidates.states.get(context, UUID(completion["result_state_id"]))
        before = {entry["path"]: entry for entry in base["manifest"]["files"]}
        after = {entry["path"]: entry for entry in resulting["manifest"]["files"]}
        changed = sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        handoff = self.coding.get_contract(context, UUID(intent["implementation_id"]))
        scopes = tuple(handoff["content"]["spec"]["permitted_paths"])
        reported = report.get("changed_paths")
        applied = (
            completion["outcome"] == "completed"
            and completion["exit_code"] == 0
            and not completion["details"]["stdout_truncated"]
            and report.get("status") == "applied"
            and type(report.get("schema_version")) is int
            and report["schema_version"] == 1
            and isinstance(reported, list)
            and all(isinstance(path, str) for path in reported)
            and bool(changed)
            and sorted(reported) == changed
            and all(within_scope(path, scopes) for path in changed)
        )
        application_id, run_id = UUID(intent["id"]), UUID(execution["run"]["id"])
        with self.database.transaction(context) as conn:

            def record() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                details = {
                    "worker_report": report,
                    "changed_paths": changed,
                    "verification_status": "unverified",
                    "imported_from": intent["imported_from"],
                }
                conn.execute(
                    "INSERT INTO patch_applications(workspace_id,id,episode_id,run_id,result_state_id,status,details) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        application_id,
                        UUID(intent["episode_id"]),
                        run_id,
                        UUID(completion["result_state_id"]),
                        "applied" if applied else "failed",
                        Jsonb(details),
                    ),
                )
                append_event(
                    conn,
                    context,
                    "patch_application_recorded",
                    episode_id=UUID(intent["episode_id"]),
                    run_id=run_id,
                    details={
                        "application_id": str(application_id),
                        "status": "applied" if applied else "failed",
                        "verification_status": "unverified",
                    },
                )
                return self._inspect(conn, context, application_id)

            return idempotent_command(
                conn,
                context,
                "record_patch_application",
                str(application_id),
                {"run_id": str(run_id)},
                record,
            )

    def apply(
        self,
        context: WorkspaceContext,
        coding_result_id: UUID,
        idempotency_key: str,
        *,
        candidate_id: UUID | None = None,
        imported_from: str | None = None,
    ) -> dict[str, Any]:
        intent = self._prepare(
            context, coding_result_id, idempotency_key, candidate_id, imported_from
        )
        current = self.inspect(context, UUID(intent["id"]))
        if current["status"] != "pending":
            return current
        action_id = UUID(intent["action_id"])
        execution = self.execution.inspect(context, action_id)
        if execution["run"] is not None:
            if execution["completion"] is None:
                raise IndeterminateExecutionError(
                    "Reconcile the existing patch run before retrying this application."
                )
            execution = self.gateway.reconcile(context, UUID(execution["run"]["id"]))
            return self._finish(context, intent, execution)
        lease = self.candidates.claim(
            context, UUID(intent["candidate_id"]), f"patch-application:{intent['id']}"
        )
        reservation = None
        try:
            definition = self.gateway.registry.get("apply_patch")
            if definition.fingerprint != patch_tool_definition().fingerprint:
                raise StateMismatchError(
                    "Patch application requires the installed bounded adapter."
                )
            reservation = self.budgets.reserve(
                context,
                UUID(intent["episode_id"]),
                {"calls": 1, "tool_ms": definition.timeout_ms},
                _key("patch-budget", idempotency_key),
            )
            execution = self.gateway.execute(context, action_id, lease, UUID(reservation["id"]))
            return self._finish(context, intent, execution)
        finally:
            # Only release when the durable record proves no dispatch or a stopped,
            # completed operation. Unknown execution remains fenced for reconciliation.
            observed = self.execution.inspect(context, action_id)
            if observed["run"] is None:
                if reservation is not None:
                    self.budgets.release(
                        context,
                        UUID(reservation["id"]),
                        "Patch process was not dispatched.",
                        confirmed_not_started=True,
                    )
                self.candidates.release(context, lease)
            elif observed["completion"] is not None:
                self.candidates.release(context, lease)
