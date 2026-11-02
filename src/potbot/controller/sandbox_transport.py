"""Bind the queue's durable target result to the existing task budget."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.controller.budgets import BudgetLedger
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.tasks import _event, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.tools.queue_transport import queue_target, resolution_worker
from potbot.tools.queue_transport_worker import REQUEST, RESULT, request
from potbot.tools.sandbox_cog_worker import SandboxedCogOperation

if TYPE_CHECKING:
    from potbot.controller.execution_cogs import ExecutionCogRuntime


@dataclass(frozen=True, slots=True)
class QueueEffect:
    target: str
    effect: str
    digest: str
    agent_id: UUID
    installation: str
    max_wall_ms: int

    @classmethod
    def installed(cls, op: SandboxedCogOperation, payload: object, agent: UUID) -> QueueEffect:
        effect, _, digest = request(payload)
        return cls(
            queue_target(op),
            effect,
            digest,
            agent,
            content_hash(
                {
                    "binding": op.binding.to_dict(),
                    "argv": list(op.argv),
                    "executable": op.executable_sha256,
                }
            ),
            op.binding.policy.max_wall_ms,
        )

    def receipt(self, value: object) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "target_id",
                "effect_id",
                "request_sha256",
                "outcome",
            }
            or (
                value["target_id"] != self.target
                or value["effect_id"] != self.effect
                or value["request_sha256"] != self.digest
                or value["outcome"] not in ("applied", "cancelled")
            )
        ):
            raise StateMismatchError("Queue receipt differs from the durable intent")
        return value

    def reserve(
        self,
        connection: Any,
        ledger: BudgetLedger,
        actor: WorkspaceContext,
        run_id: UUID,
        episode_id: UUID,
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM sandbox_transport_attempts WHERE workspace_id=%s AND target_id=%s "
            "AND effect_id=%s",
            (actor.workspace_id, self.target, self.effect),
        ).fetchone():
            raise ScopeDeniedError("Queue effect ID already has an attempt")
        reservation = ledger.reserve_in_transaction(
            connection,
            actor,
            episode_id,
            {"calls": 1, "tool_ms": self.max_wall_ms, "concurrency": 1},
            "sandbox-queue:" + str(run_id),
        )
        connection.execute(
            "INSERT INTO sandbox_transport_attempts "
            "(workspace_id,run_id,target_id,effect_id,request_sha256,episode_id,reservation_id,"
            "agent_id,installation_sha256,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared')",
            (
                actor.workspace_id,
                run_id,
                self.target,
                self.effect,
                self.digest,
                episode_id,
                UUID(reservation["id"]),
                self.agent_id,
                self.installation,
            ),
        )

    def finish(
        self,
        connection: Any,
        ledger: BudgetLedger,
        actor: WorkspaceContext,
        row: dict[str, Any],
        value: object,
    ) -> str:
        from psycopg.types.json import Jsonb

        receipt = None if value is None else self.receipt(value)
        if row["state"] in ("applied", "cancelled"):
            if receipt is not None and receipt != row["receipt"]:
                raise StateMismatchError("Queue effect already has a different outcome")
            return str(row["state"])
        if row["state"] == "prepared" and receipt is None:
            ledger.release(
                actor,
                row["reservation_id"],
                "queue dispatch was never committed",
                confirmed_not_started=True,
                connection=connection,
            )
            outcome = "cancelled"
        elif row["state"] == "dispatched" and receipt is not None:
            # The durable dispatch reserves one business call and its full
            # deadline. Recovery has the same charge even after lost timing.
            ledger.settle(
                actor,
                row["reservation_id"],
                {"calls": 1, "tool_ms": self.max_wall_ms},
                "conservative_estimate",
                connection=connection,
            )
            outcome = receipt["outcome"]
        else:
            raise StateMismatchError("Queue outcome requires evidence for its dispatch state")
        connection.execute(
            "UPDATE sandbox_transport_attempts SET state=%s,receipt=%s "
            "WHERE workspace_id=%s AND run_id=%s",
            (outcome, Jsonb(receipt) if receipt else None, actor.workspace_id, row["run_id"]),
        )
        _event(
            connection,
            actor,
            "sandbox_queue_resolved",
            row["episode_id"],
            {
                "run_id": str(row["run_id"]),
                "target_id": self.target,
                "effect_id": self.effect,
                "outcome": outcome,
                "request_sha256": self.digest,
            },
        )
        return str(outcome)


def effect_episode(
    conn: Any,
    actor: WorkspaceContext,
    revision: UUID,
    repository_state: UUID,
    *,
    active: bool,
) -> UUID:
    row = conn.execute(
        "SELECT c.episode_id FROM contract_revisions c JOIN episodes e "
        "ON e.workspace_id=c.workspace_id AND e.id=c.episode_id JOIN repository_states r "
        "ON r.workspace_id=e.workspace_id AND r.repository_id=e.repository_id "
        "WHERE c.workspace_id=%s AND c.id=%s AND r.id=%s",
        (actor.workspace_id, revision, repository_state),
    ).fetchone()
    if not row:
        raise ScopeDeniedError("Queue requires an authenticated task and repository revision")
    episode = lock_episode(conn, actor, row["episode_id"])
    if active and (episode["status"] != "running" or episode["active_contract_id"] != revision):
        raise StateMismatchError("Queue task revision is no longer active")
    return UUID(str(row["episode_id"]))


def attempt(conn: Any, actor: WorkspaceContext, run: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT a.*,c.scope,c.scope_hash,c.input_artifact_id,c.input_sha256 "
        "FROM sandbox_transport_attempts a JOIN execution_run_contracts c USING(workspace_id,run_id) "
        "WHERE a.workspace_id=%s AND a.run_id=%s AND c.owner_principal_id=%s",
        (actor.workspace_id, run, actor.principal_id),
    ).fetchone()
    return dict(row) if row else None


async def reconcile_queue_effect(runtime: ExecutionCogRuntime, run_id: UUID) -> dict[str, Any]:
    """Resolve the target's immutable outcome through a fresh admitted worker."""
    from potbot.controller.execution_journal import inspect_sandbox_transport_effect

    actor, executor, agent = runtime.actor, runtime.sandbox_executor, runtime.sandbox_agent_id
    if (
        not isinstance(run_id, UUID)
        or executor is None
        or agent is None
        or runtime.admit_transport_recovery is None
    ):
        raise ScopeDeniedError("Queue reconciliation requires explicit operator admission")
    with runtime.db.transaction(actor) as conn:
        row = attempt(conn, actor, run_id)
    if row is None or content_hash(row["scope"]) != row["scope_hash"]:
        raise ScopeDeniedError("Queue attempt is unavailable in this scope")
    runtime.admit_transport_recovery(run_id)
    scope = row["scope"]
    op = runtime.operations.get(("api", scope["operation_ref"]))
    if op is None or scope["catalog_hash"] != runtime.registry.snapshot.fingerprint:
        raise ScopeDeniedError("Queue recovery requires its pinned installation and catalogue")
    raw = runtime.artifacts.read(actor, row["input_artifact_id"])
    if hashlib.sha256(raw).hexdigest() != row["input_sha256"]:
        raise StateMismatchError("Queue intent input changed")
    saved = json.loads(raw)["value"]["input"]
    if saved["type_ref"] != REQUEST:
        raise StateMismatchError("Queue intent has a different input type")
    payload = TypedCogValue(REQUEST, saved["value"])
    runtime.catalog.validate(payload)
    effect = QueueEffect.installed(op, payload.value, agent)
    if (
        row["agent_id"] != agent
        or row["installation_sha256"] != effect.installation
        or str(row["target_id"]) != effect.target
        or str(row["effect_id"]) != effect.effect
        or row["request_sha256"] != effect.digest
    ):
        raise ScopeDeniedError("Queue recovery installation differs from the durable attempt")
    ledger = BudgetLedger(runtime.db)
    with runtime.db.transaction(actor) as conn:
        lock_episode(conn, actor, row["episode_id"])
        ExecutionEventService._lock_cursor(conn, actor)
        current = attempt(conn, actor, run_id)
        if current is None:
            raise StateMismatchError("Queue attempt disappeared")
        if current["state"] == "prepared":
            effect.finish(conn, ledger, actor, current, None)
        dispatched = current["state"] == "dispatched"
    if dispatched:
        recovery = resolution_worker(op)
        context = CogRunContext(
            scope["catalog_hash"],
            UUID(scope["task_revision_id"]),
            UUID(scope["repository_state_id"]),
            uuid4(),
        )
        grant = executor.authorize_syn(recovery, payload, context, actor, agent)
        result = await executor.execute(
            recovery,
            payload,
            context,
            actor,
            agent,
            syn_authorization=grant,
        )
        if result.type_ref != RESULT:
            raise StateMismatchError("Queue recovery returned a different receipt type")
        runtime.catalog.validate(result)
        with runtime.db.transaction(actor) as conn:
            lock_episode(conn, actor, row["episode_id"])
            ExecutionEventService._lock_cursor(conn, actor)
            current = attempt(conn, actor, run_id)
            if current is None:
                raise StateMismatchError("Queue attempt disappeared")
            effect.finish(conn, ledger, actor, current, result.value)
    return inspect_sandbox_transport_effect(runtime.db, runtime.artifacts, actor, run_id)
