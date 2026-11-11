"""Qualification helpers for a directed Context RPC work-result exchange."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.context.rpc_contract import WorkRequest, parse_request
from potbot.context.syn_context_route import SynContextRoute
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError
from potbot.persistence.database import Database

if TYPE_CHECKING:
    from scripts.context_rpc_qualification_fixture import ContextWorkerFixture


type LossPoint = Literal["before_commit", "after_commit"]


@dataclass(slots=True)
class ReplyLoss:
    point: LossPoint
    request_id: UUID
    dispatches: int = 0
    signed_reply_observed: bool = False


@contextmanager
def drop_work_result_reply(
    route: SynContextRoute, request_id: UUID, point: LossPoint
) -> Iterator[ReplyLoss]:
    """Arm a one-run fixture on the actual Syn endpoint or its verified reply.

    Use only on a disposable controller before launching its selected worker.
    Install no fault switch in the hosted worker's production configuration.
    """
    if point not in {"before_commit", "after_commit"}:
        raise ValueError("Unsupported qualification loss point")
    evidence = ReplyLoss(point, request_id)
    original_accept = route.accept
    original_round_trip = route._round_trip

    def selected(payload: bytes) -> bool:
        request = parse_request(payload)
        return (
            isinstance(request, WorkRequest)
            and request.method == "work.result"
            and request.request_id == request_id
        )

    def accept(header: Mapping[str, object], payload: bytes) -> bytes:
        if selected(payload):
            evidence.dispatches += 1
            if evidence.dispatches != 1 or point == "before_commit":
                raise ConnectionAbortedError("Qualification closed the Syn service before commit")
        return original_accept(header, payload)

    def round_trip(transport_id: UUID, payload: bytes) -> tuple[bytes, str]:
        reply = original_round_trip(transport_id, payload)
        if point == "after_commit" and selected(payload):
            evidence.signed_reply_observed = True
            raise ConnectionAbortedError("Qualification discarded the verified Syn reply")
        return reply

    route.accept = accept  # type: ignore[method-assign]
    route._round_trip = round_trip
    try:
        yield evidence
    finally:
        route.accept = original_accept  # type: ignore[method-assign]
        route._round_trip = original_round_trip


def verify_work_result_loss(
    db: Database,
    actor: WorkspaceContext,
    *,
    agent_id: UUID,
    run_id: UUID,
    observed: ReplyLoss,
    result_sha256: str,
) -> dict[str, object]:
    """Reconcile the original request under the same tenant login after shutdown."""
    if observed.dispatches != 1:
        raise AssertionError("Expected exactly one delivery of the selected request")
    expected_commit = observed.point == "after_commit"
    if observed.signed_reply_observed != expected_commit:
        raise AssertionError("Syn reply observation differs from the selected loss point")
    with db.transaction(actor) as connection:
        rows = connection.execute(
            "SELECT s.state, s.result_request_id, r.request_id, r.result_sha256 "
            "FROM workspace_context_sessions s "
            "LEFT JOIN workspace_context_work_results r "
            "ON (r.workspace_id,r.session_id)=(s.workspace_id,s.id) "
            "WHERE s.workspace_id=%s AND s.agent_id=%s AND s.run_id=%s",
            (actor.workspace_id, agent_id, run_id),
        ).fetchall()
    if len(rows) != 1 or rows[0]["state"] != "revoked":
        raise AssertionError("Expected exactly one revoked session for the selected invocation")
    row = rows[0]
    expected_id = observed.request_id if expected_commit else None
    expected_hash = result_sha256 if expected_commit else None
    if (
        row["request_id"] != expected_id
        or row["result_request_id"] != expected_id
        or row["result_sha256"] != expected_hash
    ):
        raise AssertionError("The durable result differs from the original request and loss point")
    return {
        "fault": observed.point,
        "request_id": str(observed.request_id),
        "dispatch_count": observed.dispatches,
        "syn_reply_verified_before_loss": observed.signed_reply_observed,
        "postgres_result_committed": expected_commit,
        "context_session": "revoked",
    }


async def qualify_worker_reply_loss(
    fixture: ContextWorkerFixture,
    route: SynContextRoute,
    agent_id: UUID,
    point: LossPoint,
) -> dict[str, object]:
    """Run one monitored worker fault in a fresh fixture workspace."""
    executor, operation = fixture.executor, fixture.operation
    if executor._syn_context_route is not route:
        raise AssertionError("Qualification must use the selected Syn route")
    actor = fixture.environment.context
    context = CogRunContext("a" * 64, uuid4(), uuid4(), uuid4())
    request_id = uuid4()
    model = fixture.model(agent_id)
    source = TypedCogValue(
        "syn://type/in",
        {"model_sha256": model["sha256"], "qualification_request_id": str(request_id)},
    )
    result = {
        "schema_version": 1,
        "type_ref": operation.binding.output_type_ref,
        "value": {"invocation_id": str(context.invocation_id), "model_revision": model["revision"]},
    }
    result_sha256 = hashlib.sha256(
        json.dumps(
            result, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    grant = executor.authorize_syn(operation, source, context, actor, agent_id)
    if grant is None:
        raise AssertionError("The native Agent did not authorize the selected qualification Cog")
    with drop_work_result_reply(route, request_id, point) as loss:
        try:
            await executor.execute(
                operation, source, context, actor, agent_id, syn_authorization=grant
            )
        except IndeterminateExecutionError:
            pass
        else:
            raise AssertionError("Lost Syn reply returned a successful worker result")
    evidence = verify_work_result_loss(
        fixture.environment.db,
        actor,
        agent_id=agent_id,
        run_id=context.invocation_id,
        observed=loss,
        result_sha256=result_sha256,
    )
    leases = executor.ledger.active(actor.workspace_id)
    if len(leases) != 1 or leases[0]["state"] not in {"active", "indeterminate"}:
        raise AssertionError("Lost reply prematurely released the workspace lease")
    return {**evidence, "workspace_lease": leases[0]["state"]}
