"""Atomic task-wide resource admission and conservative usage reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.tasks import _event, json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    BudgetUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import TERMINAL_STATUSES, TaskStatus, nonempty, validate_amounts
from potbot.persistence.database import Database, idempotent_command


def budget_totals(
    conn: Any,
    context: WorkspaceContext,
    episode_id: UUID,
) -> tuple[dict[str, int], dict[str, int]]:
    used: dict[str, int] = {}
    held: dict[str, int] = {}
    rows = conn.execute(
        "SELECT amounts FROM budget_reservations WHERE workspace_id=%s AND episode_id=%s "
        "AND status IN ('reserved','indeterminate')",
        (context.workspace_id, episode_id),
    ).fetchall()
    for row in rows:
        for dimension, amount in row["amounts"].items():
            held[dimension] = held.get(dimension, 0) + amount
    # Settled usage is authoritative in the append-only ledger. Reservation
    # status is only the current coordination projection, not accounting history.
    usage_rows = conn.execute(
        "SELECT amounts FROM budget_usage WHERE workspace_id=%s AND episode_id=%s",
        (context.workspace_id, episode_id),
    ).fetchall()
    for row in usage_rows:
        for dimension, amount in row["amounts"].items():
            if dimension != "concurrency":
                used[dimension] = used.get(dimension, 0) + amount
    return used, held


def _reservation(conn: Any, context: WorkspaceContext, reservation_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM budget_reservations WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, reservation_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("record unavailable in this workspace")
    return dict(row)


class BudgetLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    def inspect(self, context: WorkspaceContext, episode_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            lock_episode(conn, context, episode_id)
            row = conn.execute(
                "SELECT dimensions FROM budget_limits WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, episode_id),
            ).fetchone()
            if row is None:
                raise StateMismatchError("task budget has not been initialized")
            used, held = budget_totals(conn, context, episode_id)
            limits = row["dimensions"]
            return {
                "limits": limits,
                "used": used,
                "held": held,
                "available": {
                    key: max(0, limit - used.get(key, 0) - held.get(key, 0))
                    for key, limit in limits.items()
                },
            }

    def reserve(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        amounts: Mapping[str, int],
        idempotency_key: str,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        requested = validate_amounts(amounts)
        requested.setdefault("concurrency", 1)
        request = {"episode_id": str(episode_id), "amounts": requested}
        with self.database.transaction(context) as conn:

            def admit() -> dict[str, Any]:
                episode = lock_episode(conn, context, episode_id)
                if episode["status"] != TaskStatus.RUNNING:
                    raise StateMismatchError("task is not running; no new work can be admitted")
                budget = conn.execute(
                    "SELECT dimensions FROM budget_limits WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, episode_id),
                ).fetchone()
                if budget is None:
                    raise StateMismatchError("task budget has not been initialized")
                limits = budget["dimensions"]
                if not set(requested).issubset(limits):
                    raise InvalidInputError("requested resource dimension has no configured limit")
                used, held = budget_totals(conn, context, episode_id)
                for dimension, amount in requested.items():
                    if amount + used.get(dimension, 0) + held.get(dimension, 0) > limits[dimension]:
                        raise BudgetUnavailableError("task resource allowance is unavailable")
                reservation_id = uuid4()
                conn.execute(
                    "INSERT INTO budget_reservations "
                    "(workspace_id,id,episode_id,reservation_key,amounts,status) "
                    "VALUES(%s,%s,%s,%s,%s,'reserved')",
                    (
                        context.workspace_id,
                        reservation_id,
                        episode_id,
                        idempotency_key,
                        Jsonb(requested),
                    ),
                )
                _event(
                    conn,
                    context,
                    "budget_reserved",
                    episode_id,
                    {"reservation_id": str(reservation_id), "amounts": requested},
                )
                return dict(json_record(_reservation(conn, context, reservation_id)))

            return idempotent_command(
                conn, context, "reserve_budget", idempotency_key, request, admit
            )

    def settle(
        self,
        context: WorkspaceContext,
        reservation_id: UUID,
        actual: Mapping[str, int],
        origin: str,
    ) -> dict[str, Any]:
        """Retain actual overruns; they exhaust the task instead of erasing usage."""
        from psycopg.types.json import Jsonb

        if not isinstance(actual, Mapping):
            raise InvalidInputError("actual usage must be a mapping")
        measured = validate_amounts(actual) if actual else {}
        if "concurrency" in measured:
            raise InvalidInputError("concurrency is released on completion, not a cumulative usage")
        if origin not in {"measured", "synthetic", "provider_reported"}:
            raise InvalidInputError(
                "usage origin must be measured, synthetic, or provider_reported"
            )
        with self.database.transaction(context) as conn:
            initial = _reservation(conn, context, reservation_id)
            episode_id = initial["episode_id"]
            episode = lock_episode(conn, context, episode_id)
            reservation = _reservation(conn, context, reservation_id)
            if reservation["status"] == "settled":
                if reservation["actual"] == measured and reservation["origin"] == origin:
                    return dict(json_record(reservation))
                raise StateMismatchError("reservation already settled with different usage")
            if reservation["status"] not in {"reserved", "indeterminate"}:
                raise StateMismatchError("reservation cannot be settled")
            required = set(reservation["amounts"]) - {"concurrency"}
            if set(measured) != required:
                raise InvalidInputError(
                    "settlement must account for every reserved usage dimension"
                )
            conn.execute(
                "UPDATE budget_reservations SET status='settled',actual=%s,origin=%s,"
                "reason=NULL,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (Jsonb(measured), origin, context.workspace_id, reservation_id),
            )
            conn.execute(
                "INSERT INTO budget_usage(workspace_id,id,episode_id,reservation_id,amounts,origin) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    uuid4(),
                    episode_id,
                    reservation_id,
                    Jsonb(measured),
                    origin,
                ),
            )
            used, held = budget_totals(conn, context, episode_id)
            limits = conn.execute(
                "SELECT dimensions FROM budget_limits WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, episode_id),
            ).fetchone()["dimensions"]
            overrun = any(
                used.get(key, 0) + held.get(key, 0) > limit for key, limit in limits.items()
            )
            if overrun and TaskStatus(episode["status"]) not in TERMINAL_STATUSES:
                conn.execute(
                    "UPDATE episodes SET status='budget_exhausted',status_reason=%s,blocked_on=NULL,"
                    "previous_status=NULL,update_token=update_token+1,updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (
                        "measured usage exceeded the task allowance",
                        context.workspace_id,
                        episode_id,
                    ),
                )
                _event(
                    conn,
                    context,
                    "task_state_changed",
                    episode_id,
                    {
                        "from": episode["status"],
                        "to": "budget_exhausted",
                        "reason": "measured usage exceeded the task allowance",
                    },
                )
            _event(
                conn,
                context,
                "budget_settled",
                episode_id,
                {
                    "reservation_id": str(reservation_id),
                    "actual": measured,
                    "origin": origin,
                    "overrun": overrun,
                },
            )
            return dict(json_record(_reservation(conn, context, reservation_id)))

    def mark_indeterminate(
        self,
        context: WorkspaceContext,
        reservation_id: UUID,
        reason: str,
    ) -> dict[str, Any]:
        return self._resolve_without_usage(context, reservation_id, "indeterminate", reason)

    def release(
        self,
        context: WorkspaceContext,
        reservation_id: UUID,
        reason: str,
        *,
        confirmed_not_started: bool,
    ) -> dict[str, Any]:
        if confirmed_not_started is not True:
            raise InvalidInputError("release requires confirmation that execution never started")
        return self._resolve_without_usage(context, reservation_id, "released", reason)

    def _resolve_without_usage(
        self,
        context: WorkspaceContext,
        reservation_id: UUID,
        target: str,
        reason: str,
    ) -> dict[str, Any]:
        nonempty(reason, "reason")
        with self.database.transaction(context) as conn:
            initial = _reservation(conn, context, reservation_id)
            episode_id = initial["episode_id"]
            lock_episode(conn, context, episode_id)
            reservation = _reservation(conn, context, reservation_id)
            if reservation["status"] == target and reservation["reason"] == reason:
                return dict(json_record(reservation))
            if reservation["status"] not in {"reserved", "indeterminate"}:
                raise StateMismatchError("reservation has a final accounting result")
            if target == "released" and reservation["status"] == "indeterminate":
                raise StateMismatchError(
                    "uncertain usage must be reconciled with explicit actual usage"
                )
            conn.execute(
                "UPDATE budget_reservations SET status=%s,reason=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (target, reason, context.workspace_id, reservation_id),
            )
            _event(
                conn,
                context,
                f"budget_{target}",
                episode_id,
                {"reservation_id": str(reservation_id), "reason": reason},
            )
            return dict(json_record(_reservation(conn, context, reservation_id)))
