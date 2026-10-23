"""Append-only workspace event recording and commit-ordered replay."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.persistence.database import Database


class ExecutionEventService:
    """Trusted controller boundary; producer admission remains an upstream duty."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def append(self, context: WorkspaceContext, event: ExecutionEventInput) -> dict[str, Any]:
        if not isinstance(event, ExecutionEventInput):
            raise InvalidInputError("Expected validated execution event")
        if event.trigger_kind == "parent_event" and event.trigger_ref != (
            f"syn://event/{event.parent_event_id}"
        ):
            raise InvalidInputError("Parent-event trigger must reference its exact event identity")
        with self._db.transaction(context) as connection:
            if event.parent_event_id is not None:
                parent = connection.execute(
                    "SELECT run_id,sequence FROM syn_execution_events "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, event.parent_event_id),
                ).fetchone()
                if parent is None or parent["run_id"] != event.parent_run_id:
                    raise ScopeDeniedError("Causal parent cannot be verified in this workspace")
            # The same cursor row is locked and incremented by legacy job events.
            connection.execute(
                "INSERT INTO job_event_cursors(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (context.workspace_id,),
            )
            counter = connection.execute(
                "UPDATE job_event_cursors SET last_sequence=last_sequence+1 "
                "WHERE workspace_id=%s RETURNING last_sequence",
                (context.workspace_id,),
            ).fetchone()
            identifier = uuid4()
            row = connection.execute(
                """INSERT INTO syn_execution_events (
                    workspace_id,sequence,id,event_ref,event_type_ref,event_type_version,
                    subject_type_ref,subject_version,run_id,parent_run_id,parent_event_id,
                    trigger_kind,trigger_ref,trigger_input_sha256,emitter_run_id,
                    emitter_principal_id,stage,input_artifact_ref,output_artifact_ref,
                    output_sha256,elapsed_ms
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *""",
                (
                    context.workspace_id, counter["last_sequence"], identifier,
                    f"syn://event/{identifier}", event.event_type_ref, event.event_type_version,
                    event.subject_type_ref, event.subject_version, event.run_id,
                    event.parent_run_id, event.parent_event_id, event.trigger_kind,
                    event.trigger_ref, event.trigger_input_sha256, event.emitter_run_id,
                    context.principal_id, event.stage, event.input_artifact_ref,
                    event.output_artifact_ref, event.output_sha256, event.elapsed_ms,
                ),
            ).fetchone()
            return self._public(dict(row))

    def events(
        self, context: WorkspaceContext, *, after: int = 0,
        run_id: UUID | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        if type(after) is not int or not 0 <= after <= 9007199254740991:
            raise InvalidInputError("Invalid event cursor")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInputError("Event page must contain 1..200 records")
        if run_id is not None and not isinstance(run_id, UUID):
            raise InvalidInputError("Run filter must be a UUID")
        with self._db.transaction(context) as connection:
            if run_id is None:
                rows = connection.execute(
                    "SELECT * FROM syn_execution_events WHERE workspace_id=%s "
                    "AND sequence>%s ORDER BY sequence LIMIT %s",
                    (context.workspace_id, after, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM syn_execution_events WHERE workspace_id=%s "
                    "AND run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                    (context.workspace_id, run_id, after, limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        items = [self._public(dict(row)) for row in rows[:limit]]
        return {"items": items, "has_more": has_more,
                "next_cursor": items[-1]["sequence"] if items else after}

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        # Never expose future private payload columns by accidental SELECT * projection.
        from potbot.controller.tasks import json_record

        fields = (
            "sequence", "id", "event_ref", "event_type_ref", "event_type_version",
            "subject_type_ref", "subject_version", "run_id", "parent_run_id",
            "parent_event_id", "trigger_kind", "trigger_ref", "trigger_input_sha256",
            "emitter_run_id", "stage", "input_artifact_ref", "output_artifact_ref",
            "output_sha256", "elapsed_ms", "recorded_at",
        )
        return {"schema_version": 1, **json_record({key: row[key] for key in fields})}
