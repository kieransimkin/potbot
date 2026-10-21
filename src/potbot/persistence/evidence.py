"""Append-only evidence. Only trusted adapters produce observations."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash

if TYPE_CHECKING:
    from potbot.persistence.database import Database


def _scoped_reference(
    connection: Any, context: WorkspaceContext, table: str, identity: UUID | None
) -> None:
    # Table names are internal constants, never supplied by an API or a model.
    if table not in {"episodes", "artifacts", "evidence_events", "model_deployments"}:
        raise InvalidInputError("Unsupported evidence reference.")
    if identity is not None:
        row = connection.execute(
            f"SELECT id FROM {table} WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, identity),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Resource is unavailable in this workspace.")


def append_event(
    connection: Any,
    context: WorkspaceContext,
    event_type: str,
    *,
    payload_id: UUID | None = None,
    episode_id: UUID | None = None,
    producer_kind: str = "controller",
    producer_id: str = "potbot",
    deployment_id: UUID | None = None,
    causation_id: UUID | None = None,
    correlation_id: UUID | None = None,
    occurred_at: datetime | None = None,
    details: dict[str, Any] | None = None,
    run_id: UUID | None = None,
    node_id: UUID | None = None,
) -> dict[str, Any]:
    """Append within the caller's transaction; never commit partial state here.

    This is a trusted application boundary, not a model-callable tool. A model's
    self-reported verification remains a model proposal. Later tool adapters must
    bind their observation to their execution run; direct user/controller events
    intentionally require no execution run.
    """
    if not isinstance(event_type, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", event_type):
        raise InvalidInputError("Invalid event type.")
    if producer_kind not in {"controller", "human", "tool", "model"}:
        raise InvalidInputError("Unknown event producer kind.")
    if not isinstance(producer_id, str) or not 1 <= len(producer_id) <= 200:
        raise InvalidInputError("An event requires a producer identity.")
    if (producer_kind == "model") != (deployment_id is not None):
        raise InvalidInputError("Only a model producer requires a deployment identity.")
    required = {
        "tool_observation": "tool",
        "user_input": "human",
        "controller_decision": "controller",
    }
    if event_type in required and producer_kind != required[event_type]:
        raise InvalidInputError("The event type requires an authenticated producer of its kind.")
    if producer_kind == "human" and producer_id != str(context.principal_id):
        raise ScopeDeniedError("Human provenance must match the authenticated principal.")
    for table, identity in (
        ("artifacts", payload_id),
        ("episodes", episode_id),
        ("evidence_events", causation_id),
        ("model_deployments", deployment_id),
    ):
        _scoped_reference(connection, context, table, identity)
    for table, identity in (("action_runs", run_id), ("search_nodes", node_id)):
        if identity is not None:
            if episode_id is None:
                raise InvalidInputError("Execution evidence requires a task identity.")
            found = connection.execute(
                f"SELECT id FROM {table} WHERE workspace_id=%s AND episode_id=%s AND id=%s",
                (context.workspace_id, episode_id, identity),
            ).fetchone()
            if found is None:
                raise ScopeDeniedError("Execution evidence reference is unavailable.")
    details = {} if details is None else details
    if not isinstance(details, dict):
        raise InvalidInputError("Event details must be a JSON object.")
    timestamp = connection.execute("SELECT clock_timestamp() AS timestamp").fetchone()["timestamp"]
    occurred_at = timestamp if occurred_at is None else occurred_at
    if not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
        raise InvalidInputError("Event occurrence time must include a timezone.")
    occurred_at = occurred_at.astimezone(UTC)
    identity = uuid4()
    correlation_id = correlation_id or episode_id or identity
    body = {
        "schema_version": 1,
        "workspace_id": str(context.workspace_id),
        "id": str(identity),
        "episode_id": str(episode_id) if episode_id else None,
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat(),
        "recorded_at": timestamp.isoformat(),
        "producer_kind": producer_kind,
        "producer_id": producer_id,
        "deployment_id": str(deployment_id) if deployment_id else None,
        "causation_id": str(causation_id) if causation_id else None,
        "correlation_id": str(correlation_id),
        "payload_id": str(payload_id) if payload_id else None,
        "details": details,
    }
    # Preserve the digest contract of historical events without execution links.
    if run_id is not None or node_id is not None:
        body["run_id"] = str(run_id) if run_id else None
        body["node_id"] = str(node_id) if node_id else None
    execution_columns = ",run_id,node_id" if run_id is not None or node_id is not None else ""
    execution_values = ",%s,%s" if execution_columns else ""
    parameters: tuple[Any, ...] = (
        context.workspace_id,
        identity,
        episode_id,
        event_type,
        occurred_at,
        timestamp,
        producer_kind,
        producer_id,
        deployment_id,
        causation_id,
        correlation_id,
        payload_id,
        canonical_json(details),
        content_hash(body),
    )
    if execution_columns:
        parameters += (run_id, node_id)
    row = connection.execute(
        f"""INSERT INTO evidence_events
        (workspace_id,id,episode_id,event_type,occurred_at,recorded_at,producer_kind,
         producer_id,deployment_id,causation_id,correlation_id,payload_id,details,content_digest
         {execution_columns})
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s{execution_values}) RETURNING *""",
        parameters,
    ).fetchone()
    return dict(row)


class EvidenceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, context: WorkspaceContext, event_type: str, **fields: Any) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            return append_event(connection, context, event_type, **fields)

    def user_input(
        self, context: WorkspaceContext, payload_id: UUID, *, episode_id: UUID | None = None
    ) -> dict[str, Any]:
        return self.append(
            context,
            "user_input",
            payload_id=payload_id,
            episode_id=episode_id,
            producer_kind="human",
            producer_id=str(context.principal_id),
        )

    def controller_decision(
        self, context: WorkspaceContext, details: dict[str, Any], *, episode_id: UUID | None = None
    ) -> dict[str, Any]:
        return self.append(context, "controller_decision", details=details, episode_id=episode_id)

    def tool_observation(
        self,
        context: WorkspaceContext,
        payload_id: UUID,
        *,
        adapter_id: str,
        details: dict[str, Any],
        episode_id: UUID | None = None,
    ) -> dict[str, Any]:
        return self.append(
            context,
            "tool_observation",
            payload_id=payload_id,
            producer_kind="tool",
            producer_id=adapter_id,
            details=details,
            episode_id=episode_id,
        )

    def read(self, context: WorkspaceContext, event_id: UUID) -> dict[str, Any]:
        with self.db.transaction(context) as connection:
            row = connection.execute(
                "SELECT * FROM evidence_events WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, event_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Resource is unavailable in this workspace.")
            return dict(row)
