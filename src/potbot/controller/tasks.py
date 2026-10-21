"""Transactional task contracts and lifecycle controls."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.domain.tasks import (
    TERMINAL_STATUSES,
    ContractSpec,
    TaskStatus,
    check_transition,
    nonempty,
)
from potbot.persistence.database import Database, idempotent_command


def json_record(value: Any) -> Any:
    """Render a database result without relying on encoder-specific coercion."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_record(item) for item in value]
    return value


def lock_episode(conn: Any, context: WorkspaceContext, episode_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM episodes WHERE workspace_id=%s AND id=%s FOR UPDATE",
        (context.workspace_id, episode_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("record unavailable in this workspace")
    return dict(row)


def _event(
    conn: Any,
    context: WorkspaceContext,
    event_type: str,
    episode_id: UUID,
    details: dict[str, Any],
    *,
    human: bool = False,
) -> None:
    from potbot.persistence.evidence import append_event

    append_event(
        conn,
        context,
        event_type,
        episode_id=episode_id,
        producer_kind="human" if human else "controller",
        producer_id=str(context.principal_id) if human else "potbot.task-service",
        correlation_id=episode_id,
        details=details,
    )


def inspect_in_transaction(
    conn: Any, context: WorkspaceContext, episode_id: UUID
) -> dict[str, Any]:
    episode = conn.execute(
        "SELECT * FROM episodes WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, episode_id),
    ).fetchone()
    if episode is None:
        raise ScopeDeniedError("record unavailable in this workspace")
    contract = conn.execute(
        "SELECT id,predecessor_id,content,content_hash,created_at FROM contract_revisions "
        "WHERE workspace_id=%s AND episode_id=%s AND id=%s",
        (context.workspace_id, episode_id, episode["active_contract_id"]),
    ).fetchone()
    configuration = conn.execute(
        "SELECT id,content_hash,content FROM resolved_configurations "
        "WHERE workspace_id=%s AND episode_id=%s",
        (context.workspace_id, episode_id),
    ).fetchone()
    return {
        "schema_version": 1,
        "operation_id": str(episode_id),
        "task_id": str(episode_id),
        "repository_id": str(episode["repository_id"]),
        "deliverable_type": episode["deliverable_type"],
        "lifecycle": episode["status"],
        "update_token": episode["update_token"],
        "status_reason": episode["status_reason"],
        "blockers": [] if episode["blocked_on"] is None else [episode["blocked_on"]],
        "current_contract": json_record(dict(contract)) if contract is not None else None,
        "configuration": json_record(dict(configuration)) if configuration is not None else None,
        "created_at": json_record(episode["created_at"]),
        "updated_at": json_record(episode["updated_at"]),
    }


def _insert_contract(
    conn: Any,
    context: WorkspaceContext,
    episode_id: UUID,
    contract: ContractSpec,
    predecessor_id: UUID | None = None,
) -> UUID:
    from psycopg.types.json import Jsonb

    revision_id = uuid4()
    content = contract.to_dict()
    conn.execute(
        "INSERT INTO contract_revisions "
        "(workspace_id,id,episode_id,predecessor_id,content,content_hash) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            context.workspace_id,
            revision_id,
            episode_id,
            predecessor_id,
            Jsonb(content),
            content_hash(content),
        ),
    )
    return revision_id


class TaskService:
    """The API and CLI share this boundary; authenticated context is out-of-band."""

    def __init__(
        self,
        database: Database,
        *,
        configuration_layers: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.database = database
        self._configuration_layers = copy.deepcopy(configuration_layers)

    def submit(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        contract: ContractSpec,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        from potbot.models.configuration import resolve_configuration, store_configuration

        if not isinstance(repository_id, UUID) or not isinstance(contract, ContractSpec):
            raise InvalidInputError("submission requires a repository UUID and validated contract")
        request = {"repository_id": str(repository_id), "contract": contract.to_dict()}
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                resolved = resolve_configuration(self._configuration_layers)
                repository = conn.execute(
                    "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, repository_id),
                ).fetchone()
                if repository is None:
                    raise ScopeDeniedError("record unavailable in this workspace")
                episode_id = uuid4()
                conn.execute(
                    "INSERT INTO episodes(workspace_id,id,repository_id,deliverable_type) "
                    "VALUES(%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        episode_id,
                        repository_id,
                        contract.deliverable_type.value,
                    ),
                )
                revision_id = _insert_contract(conn, context, episode_id, contract)
                conn.execute(
                    "UPDATE episodes SET active_contract_id=%s WHERE workspace_id=%s AND id=%s",
                    (revision_id, context.workspace_id, episode_id),
                )
                conn.execute(
                    "INSERT INTO budget_limits(workspace_id,id,dimensions) VALUES(%s,%s,%s)",
                    (context.workspace_id, episode_id, Jsonb(dict(contract.budget))),
                )
                store_configuration(conn, context, resolved, episode_id=episode_id)
                _event(
                    conn,
                    context,
                    "task_submitted",
                    episode_id,
                    {
                        "contract_revision_id": str(revision_id),
                        "contract_hash": content_hash(contract.to_dict()),
                        "deliverable_type": contract.deliverable_type.value,
                    },
                    human=True,
                )
                return inspect_in_transaction(conn, context, episode_id)

            return idempotent_command(
                conn, context, "submit_task", idempotency_key, request, create
            )

    def inspect(self, context: WorkspaceContext, episode_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return inspect_in_transaction(conn, context, episode_id)

    def history(self, context: WorkspaceContext, episode_id: UUID) -> list[dict[str, Any]]:
        with self.database.transaction(context) as conn:
            inspect_in_transaction(conn, context, episode_id)
            rows = conn.execute(
                "SELECT id,predecessor_id,content,content_hash,created_at FROM contract_revisions "
                "WHERE workspace_id=%s AND episode_id=%s ORDER BY created_at,id",
                (context.workspace_id, episode_id),
            ).fetchall()
            return [dict(json_record(dict(row))) for row in rows]

    def revise(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        expected_contract_id: UUID,
        contract: ContractSpec,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        from potbot.controller.budgets import budget_totals

        if not isinstance(contract, ContractSpec) or not isinstance(expected_contract_id, UUID):
            raise InvalidInputError("revision requires a validated contract and expected revision")
        request = {
            "episode_id": str(episode_id),
            "expected_contract_id": str(expected_contract_id),
            "contract": contract.to_dict(),
        }
        with self.database.transaction(context) as conn:

            def revise_contract() -> dict[str, Any]:
                episode = lock_episode(conn, context, episode_id)
                if episode["active_contract_id"] != expected_contract_id:
                    raise RevisionConflictError("active contract changed; inspect before revising")
                if TaskStatus(episode["status"]) in TERMINAL_STATUSES:
                    raise StateMismatchError("terminal tasks cannot be revised")
                if episode["deliverable_type"] != contract.deliverable_type.value:
                    raise InvalidInputError(
                        "deliverable_type cannot change within an existing task"
                    )
                used, held = budget_totals(conn, context, episode_id)
                limits = dict(contract.budget)
                for dimension in set(used) | set(held):
                    if limits.get(dimension, 0) < used.get(dimension, 0) + held.get(dimension, 0):
                        raise InvalidInputError(
                            "new budget cannot remove consumed or reserved capacity"
                        )
                revision_id = _insert_contract(
                    conn, context, episode_id, contract, expected_contract_id
                )
                conn.execute(
                    "UPDATE episodes SET active_contract_id=%s,update_token=update_token+1,"
                    "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                    (revision_id, context.workspace_id, episode_id),
                )
                conn.execute(
                    "UPDATE budget_limits SET dimensions=%s,updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (Jsonb(limits), context.workspace_id, episode_id),
                )
                _event(
                    conn,
                    context,
                    "contract_revised",
                    episode_id,
                    {
                        "predecessor_id": str(expected_contract_id),
                        "contract_revision_id": str(revision_id),
                        "contract_hash": content_hash(contract.to_dict()),
                    },
                    human=True,
                )
                return inspect_in_transaction(conn, context, episode_id)

            return idempotent_command(
                conn, context, "revise_contract", idempotency_key, request, revise_contract
            )

    def transition(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        expected_token: int,
        target: TaskStatus,
        reason: str,
        *,
        blocked_on: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Trusted controller lifecycle transition, separate from human acceptance."""
        if not isinstance(target, TaskStatus):
            raise InvalidInputError("target must be a TaskStatus")
        return self._change_state(
            context,
            episode_id,
            expected_token,
            target.value,
            reason,
            blocked_on=blocked_on,
            idempotency_key=idempotency_key,
            human=False,
        )

    def control(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        expected_token: int,
        action: str,
        reason: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(action, str) or action not in {"pause", "resume", "cancel"}:
            raise InvalidInputError("control action must be pause, resume, or cancel")
        target = {"pause": "paused", "cancel": "cancelled", "resume": "resume"}[action]
        return self._change_state(
            context,
            episode_id,
            expected_token,
            target,
            reason,
            blocked_on=None,
            idempotency_key=idempotency_key,
            human=True,
        )

    def _change_state(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        expected_token: int,
        action: str,
        reason: str,
        *,
        blocked_on: str | None,
        idempotency_key: str,
        human: bool,
    ) -> dict[str, Any]:
        if type(expected_token) is not int or expected_token < 0:
            raise InvalidInputError("expected_token must be a nonnegative integer")
        nonempty(reason, "reason")
        if action == TaskStatus.BLOCKED:
            nonempty(blocked_on, "blocked_on")
        elif blocked_on is not None:
            raise InvalidInputError("blocked_on only applies to the blocked state")
        request = {
            "episode_id": str(episode_id),
            "expected_token": expected_token,
            "action": action,
            "reason": reason,
            "blocked_on": blocked_on,
        }
        with self.database.transaction(context) as conn:

            def change() -> dict[str, Any]:
                episode = lock_episode(conn, context, episode_id)
                if episode["update_token"] != expected_token:
                    raise RevisionConflictError("task changed; inspect before issuing control")
                current = TaskStatus(episode["status"])
                previous: str | None = episode["previous_status"]
                if action == "resume":
                    if current not in {TaskStatus.PAUSED, TaskStatus.BLOCKED} or previous is None:
                        raise StateMismatchError("only a suspended task can resume")
                    target = TaskStatus(previous)
                    if target not in {
                        TaskStatus.SUBMITTED,
                        TaskStatus.INITIALIZING,
                        TaskStatus.RUNNING,
                    }:
                        raise StateMismatchError("task has no resumable lifecycle state")
                    previous = None
                else:
                    target = TaskStatus(action)
                    check_transition(current, target)
                    if target in {TaskStatus.PAUSED, TaskStatus.BLOCKED}:
                        previous = current.value
                    elif target in TERMINAL_STATUSES:
                        previous = None
                conn.execute(
                    "UPDATE episodes SET status=%s,status_reason=%s,blocked_on=%s,previous_status=%s,"
                    "update_token=update_token+1,updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (target.value, reason, blocked_on, previous, context.workspace_id, episode_id),
                )
                _event(
                    conn,
                    context,
                    "task_state_changed",
                    episode_id,
                    {
                        "from": current.value,
                        "to": target.value,
                        "reason": reason,
                        "blocked_on": blocked_on,
                        "expected_token": expected_token,
                    },
                    human=human,
                )
                return inspect_in_transaction(conn, context, episode_id)

            return idempotent_command(
                conn, context, "control_task", idempotency_key, request, change
            )
