"""Durable admission, replay and accounting for bounded offline inference.

The controller owns operation identities and authority. This first gateway uses
the existing PostgreSQL command journal and budget ledger, avoiding a competing
queue or a migration. A committed dispatch marker without a completion is never
silently retried. Real provider transport awaits the shared outbound policy and
live deployment qualification; only explicitly synthetic transports are admitted.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.controller.execution import scoped_row
from potbot.controller.tasks import lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    BudgetUnavailableError,
    InvalidInputError,
    RevisionConflictError,
    SchemaViolationError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.tasks import validate_amounts
from potbot.models.context import ContextPacketService
from potbot.models.envelopes import MAX_MODEL_OUTPUT_BYTES, RepairPolicy, parse_model_response
from potbot.models.invocations import GenerateRequest, GenerationResult, InvocationStore
from potbot.models.transport import OfflineTransport, Terminal, TextDelta, TokenUsage
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event

MAX_RESPONSE_BYTES = MAX_MODEL_OUTPUT_BYTES
MAX_STREAM_EVENTS = 4096
_PREFIX = "inference_v1_"


def _request(value: dict[str, Any]) -> GenerateRequest:
    return GenerateRequest(
        UUID(value["deployment_id"]),
        value["role"],
        UUID(value["context_packet_id"]),
        value["response_schema"],
        canonical_json(value["sampling"]),
        value["output_limit"],
        datetime.fromisoformat(value["deadline"]),
        UUID(value["cancellation_ref"]),
    )


class InferenceGateway:
    def __init__(
        self, database: Database, artifacts: ArtifactStore, transport: OfflineTransport
    ) -> None:
        if (
            transport.fixture_only is not True
            or transport.backend != "potbot-offline-fixture"
            or transport.backend_version != "1"
        ):
            raise InvalidInputError(
                "This gateway currently admits only the offline fixture adapter."
            )
        self.database, self.artifacts, self.transport = database, artifacts, transport
        self.contexts = ContextPacketService(database, artifacts)
        self.invocations = InvocationStore(database)
        self.budgets = BudgetLedger(database)

    @contextmanager
    def _lock(self, context: WorkspaceContext, key: str) -> Iterator[None]:
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise InvalidInputError("An inference key of 1 to 128 characters is required.")
        lock = int.from_bytes(
            hashlib.sha256(f"inference:{context.workspace_id}:{key}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        with self.database.transaction(context) as conn:
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock,))
            yield

    def _read(self, context: WorkspaceContext, stage: str, key: str) -> dict[str, Any] | None:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT result FROM command_results WHERE workspace_id=%s AND operation=%s "
                "AND idempotency_key=%s",
                (context.workspace_id, _PREFIX + stage, key),
            ).fetchone()
            return None if row is None else dict(row["result"])

    def _write(
        self,
        context: WorkspaceContext,
        stage: str,
        key: str,
        request: dict[str, Any],
        callback: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return idempotent_command(
                conn, context, _PREFIX + stage, key, request, lambda: callback(conn)
            )

    def _operation(self, context: WorkspaceContext, key: str) -> dict[str, Any]:
        result = self._read(context, "operation", key)
        if result is None:
            raise ScopeDeniedError("Inference operation is unavailable in this workspace.")
        return result

    def _check_task(self, conn: Any, context: WorkspaceContext, operation: dict[str, Any]) -> None:
        identity = operation["identity"]
        episode = lock_episode(conn, context, UUID(identity["episode_id"]))
        if (
            episode["status"] != "running"
            or str(episode["active_contract_id"]) != identity["contract_revision_id"]
        ):
            raise StateMismatchError("Inference context is no longer current for a running task.")

    def prepare(
        self,
        context: WorkspaceContext,
        node_id: UUID,
        request: GenerateRequest,
        idempotency_key: str,
        *,
        inference_ms: int,
        money_microunits: int = 0,
        purpose: str = "generation",
        max_output_bytes: int = MAX_RESPONSE_BYTES,
    ) -> dict[str, Any]:
        """Queue only after task-wide reservation; no backend is contacted here."""
        if purpose not in {"generation", "repair", "reviewer"} or not isinstance(node_id, UUID):
            raise InvalidInputError("Invalid inference purpose or controller node.")
        amounts = validate_amounts(
            {"inference_ms": inference_ms, "money_microunits": money_microunits}
        )
        if inference_ms <= 0:
            raise InvalidInputError("Inference requires a positive time reservation.")
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_RESPONSE_BYTES:
            raise InvalidInputError("Inference output byte bound is invalid.")
        descriptor = {
            "node_id": str(node_id),
            "request": request.to_dict(),
            "inference_ms": inference_ms,
            "money_microunits": money_microunits,
            "purpose": purpose,
            "max_output_bytes": max_output_bytes,
            "backend": self.transport.backend,
            "backend_version": self.transport.backend_version,
        }
        with self._lock(context, idempotency_key):
            operation = self._read(context, "operation", idempotency_key)
            if operation is not None:
                if operation["descriptor"] != descriptor:
                    raise RevisionConflictError("Inference key was used for a different request.")
                finished = self._read(context, "finished", idempotency_key)
                if finished is not None:
                    return self._deliver(context, operation, finished)
            else:
                packet = self.contexts.read(context, request.context_packet_id)
                if (
                    packet["identity"]["node_id"] != str(node_id)
                    or packet["deployment_id"] != str(request.deployment_id)
                    or packet["role"] != request.role
                    or packet["response_schema"] != request.response_schema
                    or packet["output_limit"] != request.output_limit
                ):
                    raise StateMismatchError("Generation does not match its exact rendered packet.")
                frozen = self.invocations.preflight(context, request)
                with self.database.transaction(context) as conn:
                    deployment = scoped_row(
                        conn, context, "model_deployments", request.deployment_id
                    )
                    spec = deployment["content"]
                    if (
                        spec["backend"] != self.transport.backend
                        or spec["backend_version"] != self.transport.backend_version
                        or not spec["canonical_model"].startswith("potbot/")
                    ):
                        raise InvalidInputError(
                            "Synthetic transport requires its own explicitly named fixture deployment."
                        )
                amounts.update(
                    {
                        "calls": 1,
                        "tokens": packet["input_tokens"] + request.output_limit,
                        "concurrency": 1,
                    }
                )

                def define(conn: Any) -> dict[str, Any]:
                    value = {
                        "operation_id": str(uuid4()),
                        "descriptor": descriptor,
                        "identity": packet["identity"],
                        "request": request.to_dict(),
                        "input_hash": frozen["input_hash"],
                        "input_tokens": packet["input_tokens"],
                        "amounts": amounts,
                        "max_output_bytes": max_output_bytes,
                        "fixture_only": True,
                    }
                    self._check_task(conn, context, value)
                    append_event(
                        conn,
                        context,
                        "inference_requested",
                        episode_id=UUID(value["identity"]["episode_id"]),
                        node_id=node_id,
                        producer_id="potbot.inference-gateway",
                        details={
                            "operation_id": value["operation_id"],
                            "purpose": purpose,
                            "deployment_id": str(request.deployment_id),
                            "fixture_only": True,
                        },
                    )
                    return value

                operation = self._write(context, "operation", idempotency_key, descriptor, define)
            admitted = self._read(context, "admitted", idempotency_key)
            if admitted is not None:
                return {**admitted, "status": "queued"}
            try:
                reservation = self.budgets.reserve(
                    context,
                    UUID(operation["identity"]["episode_id"]),
                    operation["amounts"],
                    "inference:" + content_hash([idempotency_key]),
                )
            except BudgetUnavailableError:
                return self._finish_without_dispatch(
                    context, idempotency_key, operation, None, "budget_unavailable"
                )
            admitted = {
                "operation_id": operation["operation_id"],
                "reservation_id": reservation["id"],
            }
            return {
                **self._write(
                    context, "admitted", idempotency_key, operation, lambda conn: admitted
                ),
                "status": "queued",
            }

    def _finish_without_dispatch(
        self,
        context: WorkspaceContext,
        key: str,
        operation: dict[str, Any],
        admitted: dict[str, Any] | None,
        outcome: str,
    ) -> dict[str, Any]:
        if admitted is not None:
            self.budgets.release(
                context,
                UUID(admitted["reservation_id"]),
                "Inference was stopped before its dispatch marker.",
                confirmed_not_started=True,
            )
        result = {
            "operation_id": operation["operation_id"],
            "status": outcome,
            "reservation_id": admitted["reservation_id"] if admitted else None,
            "invocation_id": None,
            "raw_output_artifact_id": None,
            "parsed_result": None,
            "usage": None,
            "usable": False,
            "executable": False,
            "fixture_only": True,
            "accounting": "released" if admitted else "not_reserved",
        }

        def record(conn: Any) -> dict[str, Any]:
            append_event(
                conn,
                context,
                "inference_not_dispatched",
                episode_id=UUID(operation["identity"]["episode_id"]),
                node_id=UUID(operation["identity"]["node_id"]),
                details={"operation_id": operation["operation_id"], "outcome": outcome},
            )
            return result

        return self._write(context, "finished", key, operation, record)

    def cancel_queued(self, context: WorkspaceContext, key: str) -> dict[str, Any]:
        with self._lock(context, key):
            operation = self._operation(context, key)
            finished = self._read(context, "finished", key)
            if finished is not None:
                return self._deliver(context, operation, finished)
            if self._read(context, "started", key) is not None:
                raise StateMismatchError("Dispatched inference requires outcome reconciliation.")
            admitted = self._read(context, "admitted", key)
            if admitted is None:
                # Recover the narrow crash window after reserve committed but
                # before its admission journal was published.
                with self.database.transaction(context) as conn:
                    row = conn.execute(
                        "SELECT id FROM budget_reservations WHERE workspace_id=%s "
                        "AND episode_id=%s AND reservation_key=%s",
                        (
                            context.workspace_id,
                            UUID(operation["identity"]["episode_id"]),
                            "inference:" + content_hash([key]),
                        ),
                    ).fetchone()
                if row is not None:
                    admitted = {"reservation_id": str(row["id"])}
            return self._finish_without_dispatch(context, key, operation, admitted, "cancelled")

    def dispatch(
        self,
        context: WorkspaceContext,
        key: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Dispatch once, retaining partial output and uncertainty on interruptions."""
        with self._lock(context, key):
            operation = self._operation(context, key)
            finished = self._read(context, "finished", key)
            if finished is not None:
                return self._deliver(context, operation, finished)
            admitted = self._read(context, "admitted", key)
            if admitted is None:
                raise StateMismatchError("Inference has no successful budget admission.")
            request = _request(operation["request"])
            if self._read(context, "started", key) is not None:
                return self._finish(
                    context, key, operation, admitted, b"", None, 0, "indeterminate", False
                )
            if cancelled is not None and cancelled():
                return self._finish_without_dispatch(context, key, operation, admitted, "cancelled")
            if request.deadline <= datetime.now(UTC):
                return self._finish_without_dispatch(context, key, operation, admitted, "timed_out")
            try:
                packet = self.contexts.read(context, request.context_packet_id)

                def start(conn: Any) -> dict[str, Any]:
                    self._check_task(conn, context, operation)
                    reservation = scoped_row(
                        conn, context, "budget_reservations", UUID(admitted["reservation_id"])
                    )
                    if reservation["status"] != "reserved":
                        raise StateMismatchError("Inference reservation is no longer runnable.")
                    return {
                        "operation_id": operation["operation_id"],
                        "dispatch_may_have_started": True,
                    }

                self._write(context, "started", key, operation, start)
            except StateMismatchError:
                return self._finish_without_dispatch(
                    context, key, operation, admitted, "stale_context"
                )
            except (InvalidInputError, ScopeDeniedError):
                return self._finish_without_dispatch(
                    context, key, operation, admitted, "invalid_context"
                )

            started = time.monotonic()
            data = bytearray()
            terminal: Terminal | None = None
            outcome = "incomplete_stream"
            stop_confirmed = False
            iterator: Iterator[Any] | None = None
            try:
                iterator = self.transport.stream(
                    request, packet["wire_text"], UUID(operation["operation_id"])
                )
                for ordinal, event in enumerate(iterator):
                    if cancelled is not None and cancelled():
                        outcome = "cancelled"
                        break
                    elapsed = math.ceil((time.monotonic() - started) * 1000)
                    if (
                        request.deadline <= datetime.now(UTC)
                        or elapsed > operation["amounts"]["inference_ms"]
                    ):
                        outcome = "timed_out"
                        break
                    if ordinal >= MAX_STREAM_EVENTS or terminal is not None:
                        outcome = "schema_violation"
                        terminal = None
                        break
                    if isinstance(event, TextDelta):
                        remaining = operation["max_output_bytes"] - len(data)
                        data.extend(event.data[:remaining])
                        if len(event.data) > remaining:
                            outcome = "output_limit"
                            break
                    elif isinstance(event, Terminal):
                        terminal = event
                        outcome = {
                            "stop": "complete",
                            "length": "incomplete_stream",
                            "cancelled": "cancelled",
                            "failed": "transport_failure",
                        }[event.reason]
                    else:
                        outcome = "schema_violation"
                        break
                if outcome != "complete":
                    stop_confirmed = self.transport.stop(UUID(operation["operation_id"])) is True
            except Exception:
                # Provider exceptions can contain secrets or arbitrary prompt text.
                # Retain classification, never the raw exception message.
                outcome = "transport_failure"
                terminal = None
                try:
                    stop_confirmed = self.transport.stop(UUID(operation["operation_id"])) is True
                except Exception:
                    stop_confirmed = False
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        outcome = "transport_failure"
                        stop_confirmed = False
            elapsed_ms = max(0, math.ceil((time.monotonic() - started) * 1000))
            # A task revision or cancellation racing the terminal event fences delivery.
            if outcome == "complete":
                if cancelled is not None and cancelled():
                    outcome = "cancelled"
                elif (
                    request.deadline <= datetime.now(UTC)
                    or elapsed_ms > operation["amounts"]["inference_ms"]
                ):
                    outcome = "timed_out"
                else:
                    try:
                        self.contexts.read(context, request.context_packet_id)
                    except StateMismatchError:
                        outcome = "stale_context"
                    except (InvalidInputError, ScopeDeniedError):
                        outcome = "invalid_context"
            return self._finish(
                context,
                key,
                operation,
                admitted,
                bytes(data),
                terminal.usage if terminal else None,
                elapsed_ms,
                outcome,
                stop_confirmed,
            )

    def _finish(
        self,
        context: WorkspaceContext,
        key: str,
        operation: dict[str, Any],
        admitted: dict[str, Any],
        raw: bytes,
        usage: TokenUsage | None,
        elapsed_ms: int,
        outcome: str,
        stop_confirmed: bool,
    ) -> dict[str, Any]:
        request = _request(operation["request"])
        parsed: dict[str, Any] | None = None
        output_overrun = usage is not None and usage.output_tokens > request.output_limit
        usage_provenance_rejected = usage is not None and usage.origin != "synthetic"
        reported_usage = (
            None
            if usage is None
            else {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "money_microunits": usage.money_microunits,
                "origin": usage.origin,
            }
        )
        input_usage_mismatch = usage is not None and usage.input_tokens != operation["input_tokens"]
        if usage is not None and usage.origin != "synthetic":
            # This backend did not call a provider or run a real tokenizer.
            usage = None
            if outcome in {"complete", "incomplete_stream"}:
                outcome = "transport_failure"
        if input_usage_mismatch:
            usage = None
            if outcome in {"complete", "incomplete_stream"}:
                outcome = "usage_unresolved"
        if output_overrun and outcome in {"complete", "incomplete_stream"}:
            outcome = "output_limit"
        if outcome == "complete":
            try:
                parsed = parse_model_response(
                    raw,
                    response_schema=request.response_schema,
                    max_output_bytes=operation["max_output_bytes"],
                ).to_dict()
                outcome = "accepted" if usage is not None else "usage_unresolved"
            except (InvalidInputError, SchemaViolationError, ValueError):
                outcome = "schema_violation"
        status = "succeeded" if outcome == "accepted" else "failed"
        error = None if outcome == "accepted" else "schema_violation"
        if outcome in {"indeterminate", "transport_failure", "usage_unresolved"}:
            status, error = "indeterminate", "indeterminate"
        elif outcome in {"cancelled", "stale_context"}:
            status, error = "cancelled", "cancelled"
        elif outcome == "timed_out":
            status, error = "timed_out", "timeout"
        parse_status = (
            "parsed"
            if parsed is not None
            else (
                "partial"
                if outcome
                in {
                    "incomplete_stream",
                    "cancelled",
                    "timed_out",
                    "transport_failure",
                    "indeterminate",
                }
                else "invalid"
            )
        )
        actual = (
            None
            if usage is None
            else {
                "calls": 1,
                "tokens": usage.input_tokens + usage.output_tokens,
                "inference_ms": elapsed_ms,
                "money_microunits": usage.money_microunits,
            }
        )

        def record(conn: Any) -> dict[str, Any]:
            artifact = self.artifacts.put_in_transaction(
                conn, context, raw, "application/octet-stream"
            )
            invocation = self.invocations.record_final_in_transaction(
                conn,
                context,
                request,
                GenerationResult(
                    artifact["id"],
                    canonical_json(parsed) if parsed is not None else None,
                    usage.input_tokens if usage else 0,
                    usage.output_tokens if usage else 0,
                    elapsed_ms,
                    outcome,
                    parse_status,
                    status,
                    error,
                    usage.origin if usage else "synthetic",
                ),
            )
            result = {
                "operation_id": operation["operation_id"],
                "status": outcome,
                "reservation_id": admitted["reservation_id"],
                "invocation_id": str(invocation["id"]),
                "raw_output_artifact_id": str(artifact["id"]),
                "parsed_result": parsed,
                "usage": actual,
                "usage_origin": usage.origin if usage else None,
                "elapsed_ms": elapsed_ms,
                "stop_confirmed": stop_confirmed,
                "output_limit_exceeded": output_overrun,
                "usage_provenance_rejected": usage_provenance_rejected,
                "input_usage_mismatch": input_usage_mismatch,
                "reported_usage": reported_usage,
                "usable": outcome == "accepted",
                "executable": False,
                "semantic_validation": "required",
                "fixture_only": True,
                "accounting": "settlement_pending" if usage else "indeterminate",
            }
            append_event(
                conn,
                context,
                "inference_completed",
                episode_id=UUID(operation["identity"]["episode_id"]),
                node_id=UUID(operation["identity"]["node_id"]),
                producer_id="potbot.inference-gateway",
                details={
                    "operation_id": operation["operation_id"],
                    "invocation_id": str(invocation["id"]),
                    "outcome": outcome,
                    "usage_known": usage is not None,
                    "deployment_id": str(request.deployment_id),
                    "fixture_only": True,
                },
            )
            return result

        result = self._write(context, "finished", key, operation, record)
        return self._deliver(context, operation, result)

    def _deliver(
        self, context: WorkspaceContext, operation: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(result)
        reservation_id = result["reservation_id"]
        if reservation_id is not None and result["invocation_id"] is not None:
            if result["usage"] is not None:
                self.budgets.settle(
                    context, UUID(reservation_id), result["usage"], result["usage_origin"]
                )
            else:
                with self.database.transaction(context) as conn:
                    reservation = scoped_row(
                        conn, context, "budget_reservations", UUID(reservation_id)
                    )
                if reservation["status"] != "settled":
                    self.budgets.mark_indeterminate(
                        context, UUID(reservation_id), "Inference terminal usage is unresolved."
                    )
            with self.database.transaction(context) as conn:
                reservation = scoped_row(conn, context, "budget_reservations", UUID(reservation_id))
            result["accounting"] = reservation["status"]
        if result["usable"]:
            try:
                self.contexts.read(context, UUID(operation["request"]["context_packet_id"]))
            except StateMismatchError:
                result["usable"] = False
                result["delivery_blocker"] = "stale_context"
            except (InvalidInputError, ScopeDeniedError):
                result["usable"] = False
                result["delivery_blocker"] = "invalid_context"
        return result

    def generate(
        self,
        context: WorkspaceContext,
        node_id: UUID,
        request: GenerateRequest,
        idempotency_key: str,
        *,
        inference_ms: int,
        money_microunits: int = 0,
        purpose: str = "generation",
        cancelled: Callable[[], bool] | None = None,
        max_output_bytes: int = MAX_RESPONSE_BYTES,
    ) -> dict[str, Any]:
        prepared = self.prepare(
            context,
            node_id,
            request,
            idempotency_key,
            inference_ms=inference_ms,
            money_microunits=money_microunits,
            purpose=purpose,
            max_output_bytes=max_output_bytes,
        )
        if prepared["status"] != "queued":
            return prepared
        return self.dispatch(context, idempotency_key, cancelled=cancelled)

    def inspect(self, context: WorkspaceContext, key: str) -> dict[str, Any]:
        operation = self._operation(context, key)
        finished = self._read(context, "finished", key)
        if finished is not None:
            if finished["reservation_id"] is not None:
                with self.database.transaction(context) as conn:
                    row = scoped_row(
                        conn, context, "budget_reservations", UUID(finished["reservation_id"])
                    )
                finished["accounting"] = row["status"]
            return {
                **operation,
                "result": finished,
                "reconciliation": self._read(context, "reconciled", key),
            }
        return {
            **operation,
            "admission": self._read(context, "admitted", key),
            "dispatch": self._read(context, "started", key),
        }

    def reconcile(
        self, context: WorkspaceContext, key: str, usage: TokenUsage, *, elapsed_ms: int
    ) -> dict[str, Any]:
        """Explicitly settle unresolved accounting; never resurrect a usable proposal."""
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise InvalidInputError("Reconciliation needs nonnegative elapsed time.")
        with self._lock(context, key):
            operation = self._operation(context, key)
            finished = self._read(context, "finished", key)
            if (
                finished is None
                or finished["usage"] is not None
                or finished["invocation_id"] is None
            ):
                raise StateMismatchError("This operation has no unresolved inference usage.")
            if usage.origin != "synthetic" or usage.input_tokens != operation["input_tokens"]:
                raise InvalidInputError(
                    "Fixture reconciliation must preserve its exact rendered input count and origin."
                )
            actual = {
                "calls": 1,
                "tokens": usage.input_tokens + usage.output_tokens,
                "inference_ms": elapsed_ms,
                "money_microunits": usage.money_microunits,
            }
            self.budgets.settle(context, UUID(finished["reservation_id"]), actual, usage.origin)
            return self._write(
                context,
                "reconciled",
                key,
                {
                    "operation_id": operation["operation_id"],
                    "actual": actual,
                    "origin": usage.origin,
                },
                lambda conn: {
                    "operation_id": operation["operation_id"],
                    "actual": actual,
                    "usable": False,
                },
            )

    def generate_with_repair(
        self,
        context: WorkspaceContext,
        node_id: UUID,
        request: GenerateRequest,
        idempotency_key: str,
        *,
        inference_ms: int,
        repair_request: Callable[[dict[str, Any], int], GenerateRequest],
        policy: RepairPolicy | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Bounded format repair with fresh packets and independent accounting.

        The trusted caller builds repair packets using ContextPacketService's
        repair_of invocation reference. Incomplete or uncertain transport is
        never converted into an automatic retry.
        """
        if policy is None:
            policy = RepairPolicy()
        if not isinstance(policy, RepairPolicy) or not callable(repair_request):
            raise InvalidInputError("Repair requires a bounded policy and packet builder.")
        descriptor = {
            "node_id": str(node_id),
            "initial_request": request.to_dict(),
            "inference_ms": inference_ms,
            "max_repairs": policy.max_repairs,
            "max_output_bytes": policy.max_output_bytes,
        }
        self._write(context, "repair_session", idempotency_key, descriptor, lambda conn: descriptor)
        initial = request
        attempts = []
        key_prefix = "repair:" + content_hash([idempotency_key])
        for ordinal in range(policy.total_attempts):
            result = self.generate(
                context,
                node_id,
                request,
                f"{key_prefix}:{ordinal}",
                inference_ms=inference_ms,
                purpose="generation" if ordinal == 0 else "repair",
                cancelled=cancelled,
                max_output_bytes=policy.max_output_bytes,
            )
            attempts.append(result)
            if not policy.should_repair(
                len(attempts),
                result["status"],
                complete=result["accounting"] == "settled",
            ):
                break
            next_request = repair_request(result, ordinal + 1)
            if (
                not isinstance(next_request, GenerateRequest)
                or next_request.deployment_id != initial.deployment_id
                or next_request.role != initial.role
                or next_request.response_schema != initial.response_schema
                or next_request.cancellation_ref != initial.cancellation_ref
                or next_request.deadline > initial.deadline
                or next_request.context_packet_id == request.context_packet_id
            ):
                raise InvalidInputError(
                    "Repair must use a fresh packet for the same bounded request."
                )
            packet = self.contexts.read(context, next_request.context_packet_id)
            if (
                packet["repair_of"] != result["invocation_id"]
                or packet["identity"]["node_id"] != str(node_id)
                or not any(
                    source["kind"] == "artifact"
                    and source["id"] == result["raw_output_artifact_id"]
                    for source in packet["sources"]
                )
            ):
                raise InvalidInputError("Repair packet must retain the preceding raw response.")
            request = next_request
        return {
            "status": attempts[-1]["status"],
            "attempts": attempts,
            "usable": attempts[-1]["usable"],
            "fixture_only": True,
        }
