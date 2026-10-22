"""Convert settled hosted output via registered Syn Cogs with retained receipts.

Transformation observations never bypass invocation, budget, implementation
handoff, scope, or exact-state verification gates.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.cogs import builtin
from potbot.cogs.contracts import CodingScope, CogRunContext, TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.solve_store import SolveStore, _run
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command


class SolveCogBridge:
    def __init__(self, database: Database, artifacts: ArtifactStore,
                 registry: BoundCogRegistry | None = None) -> None:
        self.database, self.artifacts = database, artifacts
        self.registry = registry or BoundCogRegistry.builtins()
        self.store = SolveStore(database)

    @staticmethod
    def _completed_invocation(conn: Any, context: WorkspaceContext,
                              task_id: UUID, invocation_id: UUID, artifact_id: UUID) -> None:
        """Only retained exact output from a settled, completed hosted call is admissible."""
        row = conn.execute(
            "SELECT r.episode_id,i.status,i.result,i.reservation_id,b.status AS accounting_status "
            "FROM hosted_requests r JOIN hosted_invocations i USING(workspace_id,id) "
            "LEFT JOIN budget_reservations b ON b.workspace_id=i.workspace_id "
            "AND b.id=i.reservation_id WHERE r.workspace_id=%s AND r.id=%s",
            (context.workspace_id, invocation_id),
        ).fetchone()
        if row is None or row["episode_id"] != task_id:
            raise ScopeDeniedError("The provider observation is unavailable")
        result = row["result"]
        if (
            row["status"] != "completed" or not isinstance(result, dict)
            or result.get("status") != "completed"
            or result.get("finish_reason") != "stop"
            or result.get("error_code") is not None
            or row["reservation_id"] is None
            or row["accounting_status"] != "settled"
            or result.get("raw_output_artifact_id") != str(artifact_id)
        ):
            raise StateMismatchError("Cog input requires completed settled provider output")

    def transform_hosted(
        self, context: WorkspaceContext, run_id: UUID, node_id: UUID,
        invocation_id: UUID, input_artifact_id: UUID, output_type: str,
        key: str, *, scope: CodingScope | None = None,
    ) -> dict[str, Any]:
        """Execute a pinned route and publish each output artifact and receipt once.

        Do not trust caller-provided provider content; fetch exact retained bytes
        from the authorized artifact store after validating the invocation state.
        No external model or tool is dispatched by this method.
        """
        if not all(isinstance(value, UUID) for value in (
            run_id, node_id, invocation_id, input_artifact_id,
        )):
            raise InvalidInputError("Cog transformation requires typed IDs")
        request = {
            "run_id": str(run_id), "node_id": str(node_id),
            "invocation_id": str(invocation_id), "input_artifact_id": str(input_artifact_id),
            "target_type": output_type,
            "scope_hash": scope.fingerprint if scope else None,
            "snapshot_hash": self.registry.snapshot.fingerprint,
        }
        # Workspace artifact metadata and bytes are checked by ArtifactStore;
        # this read doesn't use model-supplied paths, names or network locations.
        with self.database.transaction(context) as conn:
            run = _run(conn, context, run_id)
            if run["status"] != "running":
                raise StateMismatchError("Cog conversion requires an active run")
            node = conn.execute(
                "SELECT id FROM solve_nodes WHERE workspace_id=%s AND run_id=%s AND id=%s",
                (context.workspace_id, run_id, node_id),
            ).fetchone()
            if node is None:
                raise ScopeDeniedError("The graph node is unavailable")
            episode = conn.execute(
                "SELECT active_contract_id,status FROM episodes WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, run["episode_id"]),
            ).fetchone()
            if episode is None or episode["status"] != "running" or episode["active_contract_id"] != run["contract_revision_id"]:
                raise RevisionConflictError("Task revision changed before Cog conversion")
            if scope is not None and (
                scope.contract_revision_id != run["contract_revision_id"]
                or scope.base_state_id != run["repository_state_id"]
            ):
                raise RevisionConflictError("Coding Cog authority is from another baseline")
            self._completed_invocation(conn, context, run["episode_id"],
                                       invocation_id, input_artifact_id)
        raw = self.artifacts.read(context, input_artifact_id)
        try:
            source = TypedCogValue(builtin.COMPLETION_TEXT, raw.decode("utf-8"),
                                   source_artifact_id=input_artifact_id)
        except UnicodeDecodeError:
            raise InvalidInputError("Completed provider output is not UTF-8") from None
        cog_context = CogRunContext(
            cog_snapshot_hash=self.registry.snapshot.fingerprint,
            task_revision_id=run["contract_revision_id"],
            repository_state_id=run["repository_state_id"],
            invocation_id=invocation_id,
        )
        outcomes = self.registry.convert(source, output_type, cog_context, scope=scope)
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            def record() -> dict[str, Any]:
                current = _run(conn, context, run_id, lock=True)
                if current["status"] != "running" or current["policy_digest"] != run["policy_digest"]:
                    raise RevisionConflictError("Run changed during Cog conversion")
                episode = conn.execute(
                    "SELECT active_contract_id,status FROM episodes WHERE workspace_id=%s AND id=%s FOR SHARE",
                    (context.workspace_id, run["episode_id"]),
                ).fetchone()
                if episode is None or episode["status"] != "running" or episode["active_contract_id"] != run["contract_revision_id"]:
                    raise RevisionConflictError("Task changed during Cog conversion")
                self._completed_invocation(conn, context, run["episode_id"],
                                           invocation_id, input_artifact_id)
                receipts: list[dict[str, Any]] = []
                input_id = input_artifact_id
                for index, item in enumerate(outcomes):
                    body = canonical_json({"type_ref": item.value.type_ref,
                                           "value": item.value.value}).encode("utf-8")
                    output = self.artifacts.put_in_transaction(
                        conn, context, body, "application/vnd.potbot.cog-output+json"
                    )
                    receipt = item.receipt.to_dict()
                    conn.execute(
                        "INSERT INTO solve_cog_receipts(workspace_id,id,run_id,node_id,"
                        "invocation_id,step_index,receipt,receipt_digest,input_artifact_id,"
                        "output_artifact_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (context.workspace_id, uuid4(), run_id, node_id, invocation_id,
                         index, Jsonb(receipt), item.receipt.fingerprint, input_id, output["id"]),
                    )
                    receipts.append({"step": index, "receipt": item.receipt.fingerprint,
                                     "output_artifact_id": str(output["id"]),
                                     "output_type": item.value.type_ref})
                    input_id = output["id"]
                return {"run_id": str(run_id), "node_id": str(node_id),
                        "invocation_id": str(invocation_id), "steps": receipts,
                        "final_type": outcomes[-1].value.type_ref,
                        "executable": False}
            return idempotent_command(conn, context, "solve_cog_transform", key,
                                      request, record)
