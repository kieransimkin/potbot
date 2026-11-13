"""Durable accounting and readback for installed API read Cogs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.controller.budgets import BudgetLedger
from potbot.controller.sandbox_transport import effect_episode
from potbot.controller.tasks import lock_episode
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.evidence import append_event
from potbot.tools.api_contracts import ApiCallScope, validate_json
from potbot.tools.capability_contracts import ToolInvocationIntent, ToolObservation
from potbot.tools.rest_cog import graphql_operation_ref, grpc_operation_ref, rest_operation_ref

if TYPE_CHECKING:
    from potbot.cogs.contracts import CogRunContext, TypedCogValue
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.artifacts import ArtifactStore
    from potbot.persistence.database import Database
    from potbot.tools.api_grpc import InstalledGrpcRead
    from potbot.tools.api_https import InstalledGraphqlRead, InstalledRestRead


_REST_EVENTS = ("rest_read_dispatched", "rest_read_observed", "rest_read_unresolved")
_GRAPHQL_EVENTS = ("graphql_read_dispatched", "graphql_read_observed", "graphql_read_unresolved")
_GRPC_EVENTS = ("grpc_read_dispatched", "grpc_read_observed", "grpc_read_unresolved")


def _events(
    conn: Any, actor: WorkspaceContext, run: UUID, event_types: tuple[str, str, str]
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id,episode_id,event_type,details,payload_id,causation_id FROM evidence_events "
        "WHERE workspace_id=%s AND correlation_id=%s AND event_type=ANY(%s)",
        (actor.workspace_id, run, list(event_types)),
    ).fetchall()
    result = {row["event_type"]: row for row in rows}
    if len(rows) != len(result):
        raise StateMismatchError("API read has conflicting dispatch evidence")
    return result


class ApiReadAttempt:
    def __init__(
        self,
        db: Database,
        artifacts: ArtifactStore,
        actor: WorkspaceContext,
        read: InstalledRestRead | InstalledGraphqlRead | InstalledGrpcRead,
        source: TypedCogValue,
        cog: CogRunContext,
    ) -> None:
        self.db, self.artifacts, self.actor = db, artifacts, actor
        self.read, self.cog = read, cog
        from potbot.tools.api_grpc import InstalledGrpcRead
        from potbot.tools.api_https import InstalledGraphqlRead

        if isinstance(read, InstalledGraphqlRead):
            self.event_types = _GRAPHQL_EVENTS
            self.operation_ref = graphql_operation_ref(read)
        elif isinstance(read, InstalledGrpcRead):
            self.event_types = _GRPC_EVENTS
            self.operation_ref = grpc_operation_ref(read)
        else:
            self.event_types = _REST_EVENTS
            self.operation_ref = rest_operation_ref(read)
        self.arguments = json.loads(canonical_json(source.value["arguments"]))
        op = read.operation
        validate_json(op.input_schema_json, self.arguments, label="API Cog arguments")
        self.intent = ToolInvocationIntent(
            cog.invocation_id,
            actor.workspace_id,
            cog.task_revision_id,
            read.installation.fingerprint,
            op.input_type_ref,
            content_hash(self.arguments),
            "read_only",
            read.installation.policy_revision_hash,
            op.timeout_ms,
            op.max_output_bytes,
            str(cog.invocation_id),
        )

    def _require_cog(self, conn: Any) -> None:
        run = conn.execute(
            "SELECT c.scope,c.scope_hash,e.stage FROM execution_run_contracts c "
            "JOIN LATERAL (SELECT stage FROM syn_execution_events "
            "WHERE workspace_id=c.workspace_id AND run_id=c.run_id "
            "ORDER BY sequence DESC LIMIT 1) e ON true "
            "WHERE c.workspace_id=%s AND c.run_id=%s AND c.owner_principal_id=%s",
            (self.actor.workspace_id, self.cog.invocation_id, self.actor.principal_id),
        ).fetchone()
        if (
            run is None
            or run["stage"] != "updated"
            or content_hash(run["scope"]) != run["scope_hash"]
            or run["scope"]["operation_ref"] != self.operation_ref
            or run["scope"]["task_revision_id"] != str(self.cog.task_revision_id)
            or run["scope"]["repository_state_id"] != str(self.cog.repository_state_id)
        ):
            raise ScopeDeniedError("API read requires its admitted and running Syn Cog")

    def execute(self) -> dict[str, Any]:
        """Called by the signed work.input handler while the worker lease is live."""
        actor, cog = self.actor, self.cog
        op = self.read.operation
        dispatched, observed, unresolved = self.event_types
        ledger = BudgetLedger(self.db)
        with self.db.transaction(actor) as conn:
            episode = effect_episode(
                conn,
                actor,
                cog.task_revision_id,
                cog.repository_state_id,
                active=True,
            )
            self._require_cog(conn)
            if _events(conn, actor, cog.invocation_id, self.event_types):
                raise ScopeDeniedError("API invocation already has a durable dispatch")
            reserved = ledger.reserve_in_transaction(
                conn,
                actor,
                episode,
                {"calls": 1, "tool_ms": op.timeout_ms, "concurrency": 1},
                f"syn-{op.protocol}-read:" + str(cog.invocation_id),
            )
            reservation = UUID(reserved["id"])
            dispatch = append_event(
                conn,
                actor,
                dispatched,
                episode_id=episode,
                correlation_id=cog.invocation_id,
                details={
                    "principal_id": str(actor.principal_id),
                    "reservation_id": str(reservation),
                    "operation_sha256": op.fingerprint,
                    "intent_sha256": self.intent.fingerprint,
                    "reserved_tool_ms": op.timeout_ms,
                },
            )["id"]

        def authorize(*_: Any) -> None:
            with self.db.transaction(actor) as conn:
                current = lock_episode(conn, actor, episode)
                self._require_cog(conn)
                row = conn.execute(
                    "SELECT status FROM budget_reservations WHERE workspace_id=%s AND id=%s",
                    (actor.workspace_id, reservation),
                ).fetchone()
                if (
                    current["status"] != "running"
                    or current["active_contract_id"] != cog.task_revision_id
                    or row is None
                    or row["status"] != "reserved"
                ):
                    raise ScopeDeniedError("API dispatch lost its live task or budget")

        try:
            result = asyncio.run(
                self.read.call(
                    actor,
                    self.arguments,
                    ApiCallScope(
                        self.read.installation,
                        self.intent,
                        op.fingerprint,
                        authorize,
                    ),
                )
            )
            # The unary adapter reports grpc_unavailable when no response was obtained.
            confirmed = (
                result.error_code != "grpc_unavailable"
                if op.protocol == "grpc"
                else result.http_status is not None
            )
            if result.attempts != 1 or not confirmed:
                raise IndeterminateExecutionError("API provider response was not confirmed")
            observation = result.to_observation(self.intent, op).to_dict()
            receipt = {"observation": observation, "value": result.value}
            raw = canonical_json(receipt).encode("utf-8")
            with self.db.transaction(actor) as conn:
                lock_episode(conn, actor, episode)
                self._require_cog(conn)
                artifact = self.artifacts.put_in_transaction(conn, actor, raw, "application/json")
                ledger.settle(
                    actor,
                    reservation,
                    {"calls": 1, "tool_ms": op.timeout_ms},
                    "conservative_estimate",
                    connection=conn,
                )
                append_event(
                    conn,
                    actor,
                    observed,
                    episode_id=episode,
                    correlation_id=cog.invocation_id,
                    causation_id=dispatch,
                    payload_id=UUID(str(artifact["id"])),
                    details={
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "reservation_id": str(reservation),
                    },
                )
        except Exception as exc:
            try:
                with self.db.transaction(actor) as conn:
                    lock_episode(conn, actor, episode)
                    if observed not in _events(conn, actor, cog.invocation_id, self.event_types):
                        ledger.mark_indeterminate(
                            actor,
                            reservation,
                            "API dispatch requires reconciliation",
                            connection=conn,
                        )
                        append_event(
                            conn,
                            actor,
                            unresolved,
                            episode_id=episode,
                            correlation_id=cog.invocation_id,
                            causation_id=dispatch,
                        )
            except Exception:
                # The committed reservation still holds and duplicate dispatch stays fenced.
                raise IndeterminateExecutionError("API dispatch evidence is unavailable") from exc
            raise IndeterminateExecutionError("API dispatch requires retained evidence") from exc
        return {
            "status": result.status,
            "value": result.value,
            "observation_sha256": content_hash(observation),
        }


def recover_rest_read(
    db: Database,
    artifacts: ArtifactStore,
    actor: WorkspaceContext,
    run_id: UUID,
) -> dict[str, Any]:
    return _recover_read(db, artifacts, actor, run_id, _REST_EVENTS)


def recover_graphql_read(
    db: Database,
    artifacts: ArtifactStore,
    actor: WorkspaceContext,
    run_id: UUID,
) -> dict[str, Any]:
    return _recover_read(db, artifacts, actor, run_id, _GRAPHQL_EVENTS)


def recover_grpc_read(
    db: Database,
    artifacts: ArtifactStore,
    actor: WorkspaceContext,
    run_id: UUID,
) -> dict[str, Any]:
    return _recover_read(db, artifacts, actor, run_id, _GRPC_EVENTS)


def _recover_read(
    db: Database,
    artifacts: ArtifactStore,
    actor: WorkspaceContext,
    run_id: UUID,
    event_types: tuple[str, str, str],
) -> dict[str, Any]:
    """Read the original provider receipt and settlement without opening a route."""
    if not isinstance(run_id, UUID):
        raise ScopeDeniedError("API read recovery requires its original run UUID")
    with db.transaction(actor) as conn:
        events = _events(conn, actor, run_id, event_types)
        sent, observed = events.get(event_types[0]), events.get(event_types[1])
        if sent is None or sent["details"]["principal_id"] != str(actor.principal_id):
            raise ScopeDeniedError("API read dispatch is unavailable to this principal")
        reservation = sent["details"]["reservation_id"]
        budget = conn.execute(
            "SELECT status,actual FROM budget_reservations WHERE workspace_id=%s AND id=%s",
            (actor.workspace_id, UUID(reservation)),
        ).fetchone()
        terminal = conn.execute(
            "SELECT stage FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
            "ORDER BY sequence DESC LIMIT 1",
            (actor.workspace_id, run_id),
        ).fetchone()
    if budget is None or terminal is None:
        raise StateMismatchError("API receipt has no matching Cog and budget")
    if observed is None:
        return {
            "state": "unresolved",
            "cog_stage": terminal["stage"],
            "budget_status": budget["status"],
            "automatic_retry_permitted": False,
        }
    raw = artifacts.read(actor, observed["payload_id"])
    if (
        budget["status"] != "settled"
        or budget["actual"] != {"calls": 1, "tool_ms": sent["details"]["reserved_tool_ms"]}
        or observed["episode_id"] != sent["episode_id"]
        or observed["causation_id"] != sent["id"]
        or observed["details"]["reservation_id"] != reservation
        or hashlib.sha256(raw).hexdigest() != observed["details"]["sha256"]
    ):
        raise StateMismatchError("API receipt differs from its retained settlement")
    receipt = json.loads(raw)
    observation = ToolObservation.from_dict(receipt["observation"])
    if observation.invocation_hash != sent["details"][
        "intent_sha256"
    ] or observation.output_hash != (
        content_hash(receipt["value"]) if observation.status == "succeeded" else None
    ):
        raise StateMismatchError("API receipt differs from its admitted intent or output")
    return {
        "state": "receipt_recorded",
        "cog_stage": terminal["stage"],
        "budget_status": budget["status"],
        "usage": budget["actual"],
        "receipt": receipt,
        "automatic_retry_permitted": False,
    }
