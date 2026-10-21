"""Immutable scoped implementation handoffs and their recorded coding outputs."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.controller.execution import scoped_row
from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.coding import ImplementationSpec, coding_path, within_scope
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event


def coding_row(conn: Any, context: WorkspaceContext, table: str, identity: UUID) -> dict[str, Any]:
    if table not in {"implementation_contracts", "coding_results", "patch_application_intents"}:
        raise InvalidInputError("Unregistered coding reference.")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, identity),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Coding resource is unavailable in this workspace.")
    result = dict(row)
    if "content_digest" in result and content_hash(result["content"]) != result["content_digest"]:
        raise InvalidInputError("Coding record integrity check failed.")
    return result


class CodingStore:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self.database, self.artifacts = database, artifacts

    def create_contract(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        contract_revision_id: UUID,
        base_state_id: UUID,
        spec: ImplementationSpec,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(spec, ImplementationSpec):
            raise InvalidInputError("A typed implementation specification is required.")
        request = {
            "episode_id": str(episode_id),
            "contract_revision_id": str(contract_revision_id),
            "base_state_id": str(base_state_id),
            "spec": spec.to_dict(),
        }
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                episode = lock_episode(conn, context, episode_id)
                if episode["active_contract_id"] != contract_revision_id:
                    raise StateMismatchError(
                        "The coding handoff requires the active task contract."
                    )
                contract = scoped_row(conn, context, "contract_revisions", contract_revision_id)
                state = scoped_row(conn, context, "repository_states", base_state_id)
                if state["repository_id"] != episode["repository_id"]:
                    raise ScopeDeniedError("Coding base belongs to another repository.")
                task_scope = tuple(
                    coding_path(path) for path in contract["content"]["change_scope"]
                )
                if any(not within_scope(path, task_scope) for path in spec.permitted_paths):
                    raise ScopeDeniedError("Coding scope exceeds the task's change scope.")
                reuse_scope = spec.reuse_assessment.plan.scope
                if (
                    reuse_scope.task_id != episode_id
                    or reuse_scope.workspace_id != context.workspace_id
                    or reuse_scope.project_id != episode["repository_id"]
                ):
                    raise ScopeDeniedError(
                        "Reuse assessment belongs to another task or repository."
                    )
                references = {f"artifact:{identity}" for identity in spec.search_receipt_ids}
                if any(
                    search.evidence_ref not in references
                    for search in spec.reuse_assessment.searches
                ):
                    raise InvalidInputError(
                        "Each reuse search must reference a supplied receipt artifact."
                    )
                receipts = []
                for identity in spec.search_receipt_ids:
                    # Verify actual stored bytes, not only the existence of a UUID.
                    self.artifacts.read_in_transaction(conn, context, identity)
                    receipt = self.artifacts._metadata(conn, context, identity)
                    receipts.append({"artifact_id": str(identity), "digest": receipt["digest"]})
                identity = uuid4()
                content = {"schema_version": 1, **request, "search_receipts": receipts}
                conn.execute(
                    "INSERT INTO implementation_contracts(workspace_id,id,episode_id,contract_revision_id,"
                    "base_state_id,content,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        episode_id,
                        contract_revision_id,
                        base_state_id,
                        Jsonb(content),
                        content_hash(content),
                    ),
                )
                for receipt_id in spec.search_receipt_ids:
                    conn.execute(
                        "INSERT INTO implementation_search_receipts(workspace_id,implementation_id,artifact_id) VALUES(%s,%s,%s)",
                        (context.workspace_id, identity, receipt_id),
                    )
                append_event(
                    conn,
                    context,
                    "implementation_contract_frozen",
                    episode_id=episode_id,
                    details={
                        "implementation_id": str(identity),
                        "base_state_id": str(base_state_id),
                        "content_digest": content_hash(content),
                        "discovery_origin": "provided_receipts",
                    },
                )
                return dict(
                    json_record(coding_row(conn, context, "implementation_contracts", identity))
                )

            return idempotent_command(
                conn, context, "create_implementation_contract", idempotency_key, request, create
            )

    def get_contract(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return dict(
                json_record(coding_row(conn, context, "implementation_contracts", identity))
            )

    def get_result(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return dict(json_record(coding_row(conn, context, "coding_results", identity)))
