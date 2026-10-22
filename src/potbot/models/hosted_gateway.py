"""Durable hosted inference admission, possible-dispatch journal and accounting.

Hosted identities are provider/model/protocol plus frozen operator policy. They
never masquerade as locally pinned weights or a qualified coding deployment.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.controller.budgets import BudgetLedger
from potbot.controller.jobs import JobService
from potbot.controller.tasks import json_record
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    OperationCancelledError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import JobLease
from potbot.domain.serialization import content_hash
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.providers import (
    HostedProviderClient,
    ProviderAPIError,
    ProviderCompleted,
    ProviderTextDelta,
)
from potbot.models.stream_redaction import StreamingRedactor
from potbot.persistence.database import Database
from potbot.tools.secrets import CaptureRedactor

if TYPE_CHECKING:
    import httpx


class HostedAdmissionError(DomainError):
    """A request failed validation before any journal or provider dispatch."""

    def __init__(self, error: DomainError) -> None:
        self.code = error.code
        super().__init__(error.message)


class HostedInferenceGateway:
    def __init__(
        self,
        database: Database,
        policy: HostedPolicy,
        secret_resolver: Callable[[str], str],
        *,
        http_client: httpx.Client | None = None,
        protected_values: tuple[str, ...] = (),
    ) -> None:
        self.database, self.policy, self.secret_resolver = database, policy, secret_resolver
        self.http_client, self.protected_values = http_client, protected_values
        self.budgets, self.jobs = BudgetLedger(database), JobService(database)

    @staticmethod
    def operation_id(context: WorkspaceContext, lease: JobLease, ordinal: int) -> UUID:
        return uuid5(
            NAMESPACE_URL, f"potbot:hosted:{context.workspace_id}:{lease.job_id}:{ordinal}"
        )

    @contextmanager
    def _lock(self, context: WorkspaceContext, identity: UUID) -> Iterator[None]:
        key = int.from_bytes(
            hashlib.sha256(f"hosted:{context.workspace_id}:{identity}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        with self.database.transaction(context) as conn:
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (key,))
            yield

    @staticmethod
    def _event(
        conn: Any, context: WorkspaceContext, identity: UUID, kind: str, details: dict[str, Any]
    ) -> None:
        from psycopg.types.json import Jsonb

        conn.execute(
            "INSERT INTO hosted_invocation_events(workspace_id,id,invocation_id,kind,details) VALUES(%s,%s,%s,%s,%s)",
            (context.workspace_id, uuid4(), identity, kind, Jsonb(details)),
        )

    def _state(
        self,
        context: WorkspaceContext,
        identity: UUID,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        reservation_id: UUID | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            row = conn.execute(
                "UPDATE hosted_invocations SET status=%s,result=COALESCE(%s,result),"
                "reservation_id=COALESCE(%s,reservation_id),updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s RETURNING id",
                (
                    state,
                    Jsonb(result) if result is not None else None,
                    reservation_id,
                    context.workspace_id,
                    identity,
                ),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("The hosted invocation is unavailable.")
            self._event(
                conn,
                context,
                identity,
                state,
                result or {"reservation_id": str(reservation_id) if reservation_id else None},
            )

    @staticmethod
    def _task_current(conn: Any, context: WorkspaceContext, lease: JobLease) -> None:
        job = JobService._lease_row(conn, context, lease)
        if (
            job["input"] != lease.input
            or job["selected_models"] != lease.selected_models
            or job["episode_id"] != lease.episode_id
            or job["contract_revision_id"] != lease.contract_revision_id
        ):
            raise RevisionConflictError("The worker lease does not match its frozen job.")
        if job["cancellation_requested"]:
            raise OperationCancelledError("Job cancellation preceded inference admission.")
        task = conn.execute(
            "SELECT status,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s FOR SHARE",
            (context.workspace_id, lease.episode_id),
        ).fetchone()
        if (
            task is None
            or task["status"] != "running"
            or task["active_contract_id"] != lease.contract_revision_id
        ):
            raise StateMismatchError(
                "Hosted inference requires the job's current running task revision."
            )

    def _reservation(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any] | None:
        """Find even a committed admission whose journal link was interrupted."""
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT b.* FROM hosted_requests r JOIN budget_reservations b "
                "ON b.workspace_id=r.workspace_id AND b.episode_id=r.episode_id "
                "AND b.reservation_key=%s WHERE r.workspace_id=%s AND r.id=%s",
                ("hosted:" + str(identity), context.workspace_id, identity),
            ).fetchone()
            return dict(row) if row is not None else None

    def _authorize_dispatch(
        self, context: WorkspaceContext, lease: JobLease, identity: UUID
    ) -> None:
        """Commit the final authority check and possible-dispatch marker together.

        The locked job and task establish one authorization point against
        cancellation, lease replacement and task revisions. The transport still
        checks cancellation immediately before sending and between reads.
        """
        with self.database.transaction(context) as conn:
            self._task_current(conn, context, lease)
            row = conn.execute(
                "UPDATE hosted_invocations SET status='dispatched',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND status='reserved' AND EXISTS ("
                "SELECT 1 FROM jobs j WHERE j.workspace_id=%s AND j.id=%s AND j.generation=%s "
                "AND j.lease_owner=%s AND j.state='running' AND NOT j.cancellation_requested "
                "AND j.lease_until>clock_timestamp()) RETURNING id",
                (
                    context.workspace_id,
                    identity,
                    context.workspace_id,
                    lease.job_id,
                    lease.generation,
                    lease.worker_id,
                ),
            ).fetchone()
            if row is None:
                raise RevisionConflictError(
                    "The hosted invocation or worker lease is not ready for dispatch."
                )
            self._event(conn, context, identity, "dispatched", {})

    def _finish_observed(
        self, context: WorkspaceContext, identity: UUID, observed: dict[str, Any]
    ) -> dict[str, Any]:
        """Complete a durable observation's accounting without another HTTP call."""
        result = dict(observed)
        reservation = self._reservation(context, identity)
        if reservation is not None:
            reservation_id = reservation["id"]
            if result["accounting"] == "not_reserved":
                result["accounting"] = "released"
            if result["accounting"] == "released":
                self.budgets.release(
                    context,
                    reservation_id,
                    "hosted request confirmed not sent",
                    confirmed_not_started=True,
                )
            elif result["accounted_amounts"] is not None:
                self.budgets.settle(
                    context, reservation_id, result["accounted_amounts"], "conservative_estimate"
                )
            else:
                self.budgets.mark_indeterminate(
                    context, reservation_id, "hosted terminal usage requires reconciliation"
                )
        self._state(
            context,
            identity,
            result["status"],
            result=result,
            reservation_id=reservation["id"] if reservation is not None else None,
        )
        return result

    def inspect(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT i.id,i.status,i.reservation_id,i.result,i.updated_at,r.job_id,r.episode_id,"
                "r.ordinal,r.request_hash,r.policy_hash,r.request,r.policy FROM hosted_invocations i "
                "JOIN hosted_requests r USING(workspace_id,id) WHERE i.workspace_id=%s AND i.id=%s",
                (context.workspace_id, identity),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("The hosted invocation is unavailable.")
            value = dict(row)
            request = value.pop("request")
            value.pop("policy")
            value["selection"] = request["generation"]["selection"]
            value["role"] = request["role"]
            value["qualified"] = False
        reservation = self._reservation(context, identity)
        if reservation is not None:
            value["reservation_id"] = reservation["id"]
            value["reservation_status"] = reservation["status"]
            value["accounting_origin"] = reservation["origin"]
            value["accounted_amounts"] = reservation["actual"]
        return dict(json_record(value))

    def list_for_job(self, context: WorkspaceContext, job_id: UUID) -> dict[str, Any]:
        """Find retained accounting even when a worker died before finishing its job."""
        self.jobs.inspect(context, job_id)
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id FROM hosted_requests WHERE workspace_id=%s AND job_id=%s ORDER BY ordinal",
                (context.workspace_id, job_id),
            ).fetchall()
        return {"job_id": str(job_id), "items": [self.inspect(context, row["id"]) for row in rows]}

    def generate(
        self,
        context: WorkspaceContext,
        lease: JobLease,
        ordinal: int,
        *,
        on_delta: Callable[[str], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        if type(ordinal) is not int or not 0 <= ordinal < len(lease.selected_models):
            raise InvalidInputError("The hosted request ordinal is invalid.")
        selection = lease.selected_models[ordinal]
        try:
            request, input_allowance, rate = self.policy.request(selection, lease.input)
            key = self.secret_resolver(request.selection.credential_ref)
            redactor = CaptureRedactor(tuple(dict.fromkeys((key, *self.protected_values))))
            prompt_bytes = (request.prompt + request.system).encode()
            if any(pattern in prompt_bytes for pattern in redactor.patterns):
                raise ScopeDeniedError(
                    "A service credential cannot be disclosed in a model prompt."
                )
        except DomainError as error:
            raise HostedAdmissionError(error) from None
        identity = self.operation_id(context, lease, ordinal)
        descriptor = {
            "generation": request.to_dict(),
            "role": selection["role"],
            "contract_revision_id": str(lease.contract_revision_id),
            "input_token_allowance": input_allowance,
            "input_accounting": "conservative_byte_estimate",
        }
        with self._lock(context, identity):
            with self.database.transaction(context) as conn:
                old = conn.execute(
                    "SELECT r.request_hash,r.policy_hash,i.status,i.reservation_id,i.result FROM hosted_requests r "
                    "JOIN hosted_invocations i USING(workspace_id,id) WHERE r.workspace_id=%s AND r.id=%s",
                    (context.workspace_id, identity),
                ).fetchone()
                if old is not None:
                    if (
                        old["request_hash"] != content_hash(descriptor)
                        or old["policy_hash"] != self.policy.fingerprint
                    ):
                        raise RevisionConflictError(
                            "A hosted operation cannot change its frozen request or policy."
                        )
                    if old["status"] == "observed":
                        return self._finish_observed(context, identity, dict(old["result"]))
                    if old["result"] is not None:
                        return dict(old["result"])
                    # The operation has durable admission but no completed result.
                    # Even a restart before a known send never silently repeats it.
                    reservation = self._reservation(context, identity)
                    if reservation is not None:
                        self._state(
                            context, identity, "indeterminate", reservation_id=reservation["id"]
                        )
                    return {
                        "invocation_id": str(identity),
                        "status": "indeterminate",
                        "accounting": reservation["origin"]
                        if reservation is not None and reservation["status"] == "settled"
                        else "pending_reconciliation",
                        "usage": None,
                        "error_code": "indeterminate",
                        "finish_reason": "unknown",
                    }
                self._task_current(conn, context, lease)
                conn.execute(
                    "INSERT INTO hosted_requests(workspace_id,id,job_id,episode_id,ordinal,request_hash,policy_hash,request,policy) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        lease.job_id,
                        lease.episode_id,
                        ordinal,
                        content_hash(descriptor),
                        self.policy.fingerprint,
                        Jsonb(descriptor),
                        Jsonb(self.policy.to_dict()),
                    ),
                )
                conn.execute(
                    "INSERT INTO hosted_invocations(workspace_id,id,status) VALUES(%s,%s,'prepared')",
                    (context.workspace_id, identity),
                )
                self._event(
                    conn,
                    context,
                    identity,
                    "prepared",
                    {
                        "request_hash": content_hash(descriptor),
                        "policy_hash": self.policy.fingerprint,
                    },
                )

            amounts = {
                "calls": 1,
                "tokens": input_allowance + request.max_output_tokens,
                "inference_ms": math.ceil(request.timeout_seconds * 1000),
                "money_microunits": rate.charge(input_allowance, request.max_output_tokens),
                "concurrency": 1,
            }
            reservation_id: UUID | None = None
            dispatched = False
            started = time.monotonic()
            terminal: ProviderCompleted | None = None
            failure_code: str | None = None
            stream_redactor = StreamingRedactor(redactor)
            client = HostedProviderClient(
                lambda reference: (
                    key
                    if reference == request.selection.credential_ref
                    else self.secret_resolver(reference)
                ),
                http_client=self.http_client,
                authorize=self.policy.authorize,
            )
            try:
                if cancelled():
                    raise OperationCancelledError("Inference cancelled before admission.")
                reservation = self.budgets.reserve(
                    context, lease.episode_id, amounts, "hosted:" + str(identity)
                )
                reservation_id = UUID(reservation["id"])
                self._state(context, identity, "reserved", reservation_id=reservation_id)
                self.jobs.mark_dispatched(context, lease)
                self._authorize_dispatch(context, lease, identity)
                dispatched = True
                for event in client.stream(request, identity, cancelled):
                    if isinstance(event, ProviderTextDelta):
                        safe = stream_redactor.feed(event.text)
                        if safe:
                            on_delta(safe)
                    elif isinstance(event, ProviderCompleted):
                        terminal = event
                safe = stream_redactor.feed("", final=True)
                if safe:
                    on_delta(safe)
                if terminal is None:
                    failure_code = "indeterminate"
            except ProviderAPIError as error:
                dispatched = error.dispatched
                failure_code = (
                    "cancelled" if error.provider_error_code == "cancelled" else "model_unavailable"
                )
            except DomainError as error:
                failure_code = error.code.value
            except Exception:
                failure_code = "internal_error"
            finally:
                client.close()

            if terminal is None:
                # Keep safe trailing output even after a broken stream; redact
                # any unfinished literal secret prefix before publishing it.
                safe = stream_redactor.feed("", final=True, complete=False)
                if safe:
                    try:
                        on_delta(safe)
                    except Exception:
                        failure_code = failure_code or "internal_error"

            elapsed = math.ceil((time.monotonic() - started) * 1000)
            usage = terminal.usage if terminal else None
            accounting = "not_reserved"
            actual: dict[str, int] | None = None
            if reservation_id is not None:
                if not dispatched:
                    accounting = "released"
                elif usage is not None:
                    actual = self.policy.usage_amounts(usage, rate, elapsed)
                    accounting = "conservative_estimate"
                else:
                    accounting = "pending_reconciliation"
            status = (
                "completed"
                if terminal is not None and usage is not None and failure_code is None
                else ("indeterminate" if dispatched else "rejected")
            )
            result: dict[str, Any] = {
                "invocation_id": str(identity),
                "status": status,
                "provider": selection["provider"],
                "model": selection["model"],
                "role": selection["role"],
                "finish_reason": terminal.finish_reason if terminal else "unknown",
                "accounting": accounting,
                "usage": usage.to_dict() if usage else None,
                "accounted_amounts": actual,
                "error_code": failure_code,
                "provider_response": redactor.redact_metadata(terminal.to_dict())
                if terminal
                else None,
                "qualified": False,
                "executable": False,
            }
            # The provider observation is committed before accounting. A crash
            # between these writes can resume settlement without re-dispatch.
            self._state(context, identity, "observed", result=result)
            return self._finish_observed(context, identity, result)

    def reconcile(
        self, context: WorkspaceContext, identity: UUID, *, actual: dict[str, int]
    ) -> dict[str, Any]:
        """Explicit operator accounting; does not replay calls or accept model output."""
        from potbot.domain.tasks import validate_amounts

        expected = {"calls", "tokens", "inference_ms", "money_microunits"}
        amounts = validate_amounts(actual)
        if set(amounts) != expected or amounts["calls"] not in {0, 1}:
            raise InvalidInputError("Reconciliation requires all four actual usage dimensions.")
        with self._lock(context, identity):
            state = self.inspect(context, identity)
            if state["reservation_id"] is None or state["status"] not in {
                "prepared",
                "dispatched",
                "observed",
                "indeterminate",
                "reconciled",
                "reserved",
            }:
                raise StateMismatchError("This invocation has no unresolved accounting.")
            # Never allow a concurrent operator to settle a still-active worker.
            with self.database.transaction(context) as conn:
                job = conn.execute(
                    "SELECT state FROM jobs WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, UUID(state["job_id"])),
                ).fetchone()
                if job is None or job["state"] in {"running", "cancelling", "queued"}:
                    raise StateMismatchError("Reconciliation requires a terminal job.")
            if (
                state["status"] == "observed"
                and state["result"].get("accounted_amounts") is not None
            ):
                if state["result"].get("accounted_amounts") != amounts:
                    raise StateMismatchError(
                        "Reconciliation differs from the retained terminal usage."
                    )
                return self._finish_observed(context, identity, state["result"])
            reservation = self._reservation(context, identity)
            assert reservation is not None
            origin = "provider_reported"
            if reservation["status"] == "settled":
                if reservation["actual"] != amounts:
                    raise StateMismatchError("Reconciliation differs from settled usage.")
                origin = reservation["origin"]
            self.budgets.settle(context, UUID(state["reservation_id"]), amounts, origin)
            result = {
                **(state["result"] or {}),
                "invocation_id": str(identity),
                "accounting": "provider_reported",
                "reconciled_actual": amounts,
                "executable": False,
                "qualified": False,
            }
            result["accounting"] = origin
            self._state(
                context, identity, "reconciled", result=result, reservation_id=reservation["id"]
            )
            return result
