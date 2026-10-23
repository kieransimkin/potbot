"""Cog work observations on Potbot's existing execution ledger and artifact store."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from potbot.cogs.contracts import CogOutcome, CogRunContext, CogSignature, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import canonical_json


class EventJournal(Protocol):
    def append(self, context: WorkspaceContext, event: ExecutionEventInput) -> dict[str, Any]: ...


class ArtifactJournal(Protocol):
    def put(self, context: WorkspaceContext, data: bytes, media_type: str) -> dict[str, Any]: ...


class CogWorkMonitor:
    """Trusted producer; production uses ExecutionEventService and ArtifactStore.

    No private replacement ledger or JSONL log is created. Test doubles establish
    contracts only; real PostgreSQL/API replay is a separate required gate.
    """

    def __init__(
        self,
        events: EventJournal,
        artifacts: ArtifactJournal,
        actor: WorkspaceContext,
        *,
        emitter_run_id: UUID,
        event_type_ref: str,
        event_type_version: str = "1",
    ) -> None:
        self.events, self.artifacts, self.actor = events, artifacts, actor
        self.emitter = emitter_run_id
        self.event_type, self.event_version = event_type_ref, event_type_version

    def begin(
        self,
        signature: CogSignature,
        source: TypedCogValue,
        context: CogRunContext,
        *,
        implementation: Mapping[str, Any] | None = None,
    ) -> CogWorkRun:
        return CogWorkRun(self, signature, source, context, implementation=implementation)

    def denied(
        self, signature: CogSignature, source: TypedCogValue, context: CogRunContext
    ) -> None:
        CogWorkRun(self, signature, source, context, admitted=False)


class CogWorkRun:
    def __init__(
        self,
        owner: CogWorkMonitor,
        signature: CogSignature,
        source: TypedCogValue,
        context: CogRunContext,
        *,
        implementation: Mapping[str, Any] | None = None,
        admitted: bool = True,
    ) -> None:
        self.owner, self.signature, self.context = owner, signature, context
        self.started_at = datetime.now(UTC).isoformat()
        self.clock, self.cpu = time.monotonic_ns(), time.process_time_ns()
        self.terminal = False
        self.implementation = dict(implementation or {})
        self.previous: dict[str, Any] | None = None
        # Input bytes are private workspace artifacts; public telemetry has hashes only.
        self.input = owner.artifacts.put(
            owner.actor,
            canonical_json({"type_ref": source.type_ref, "value": source.value}).encode("utf-8"),
            "application/json",
        )
        self._emit(
            "started" if admitted else "failed", "executing" if admitted else "admission_denied"
        )

    def _emit(
        self,
        stage: Any,
        phase: str,
        *,
        output: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if self.terminal:
            raise RuntimeError("A Cog attempt already has a terminal work observation")
        elapsed = max(0, (time.monotonic_ns() - self.clock) // 1_000_000)
        unavailable = {"value": None, "unit": None, "provenance": "unavailable"}
        metrics: dict[str, dict[str, Any]] = {
            name: dict(unavailable)
            for name in (
                "target_cpu_ms",
                "peak_ram_bytes",
                "gpu_ms",
                "vram_bytes",
                "disk_read_bytes",
                "disk_write_bytes",
                "network_bytes",
                "input_tokens",
                "output_tokens",
                "provider_time_ms",
                "financial_cost",
                "budget_settlement",
            )
        }
        for name, metric in metrics.items():
            metric["unit"] = (
                "ms"
                if name.endswith("_ms")
                else "bytes"
                if name.endswith("_bytes")
                else "tokens"
                if name.endswith("_tokens")
                else "unspecified"
            )
        metrics["elapsed_wall_ms"] = {"value": elapsed, "unit": "ms", "provenance": "measured"}
        metrics["controller_cpu_ms"] = {
            "value": max(0, (time.process_time_ns() - self.cpu) // 1_000_000),
            "unit": "ms",
            "provenance": "measured",
            "scope": "controller_process_interval",
        }
        telemetry = {
            "schema_version": 1,
            "cog_ref": str(self.signature.cog_ref),
            "binding_hash": self.signature.binding_hash,
            "semantic_snapshot_hash": self.context.cog_snapshot_hash,
            "task_revision_id": str(self.context.task_revision_id),
            "repository_state_id": str(self.context.repository_state_id),
            "phase": phase,
            "started_at": self.started_at,
            "observed_at": datetime.now(UTC).isoformat(),
            "clock_basis": "monotonic",
            "metrics": metrics,
            "child_runs": [],
            "implementation": self.implementation,
        }
        if stage in {"completed", "failed", "cancelled", "indeterminate"}:
            telemetry["finished_at"] = telemetry["observed_at"]
        if reason:
            telemetry["reason_class"] = reason
        previous = self.previous
        record = self.owner.events.append(
            self.owner.actor,
            ExecutionEventInput(
                event_type_ref=self.owner.event_type,
                event_type_version=self.owner.event_version,
                subject_type_ref=self.signature.cog_type_ref,
                subject_version=str(self.signature.implementation_revision),
                run_id=self.context.invocation_id,
                trigger_kind="api_request" if previous is None else "parent_event",
                trigger_ref=f"cog-request:{self.context.invocation_id}"
                if previous is None
                else previous["event_ref"],
                trigger_input_sha256=self.input["digest"],
                emitter_run_id=self.owner.emitter,
                stage=stage,
                parent_event_id=UUID(previous["id"]) if previous else None,
                parent_run_id=self.context.invocation_id if previous else None,
                input_artifact_ref=f"artifact:{self.input['id']}",
                output_artifact_ref=f"artifact:{output['id']}" if output else None,
                output_sha256=output["digest"] if output else None,
                elapsed_ms=elapsed,
                telemetry_json=canonical_json(telemetry),
            ),
        )
        self.previous = record
        self.terminal = stage in {"completed", "failed", "cancelled", "indeterminate"}
        return record

    def progress(self, phase: str = "executing") -> None:
        self._emit("updated", phase)

    def complete(self, outcome: CogOutcome) -> dict[str, Any]:
        output = self.owner.artifacts.put(
            self.owner.actor,
            canonical_json(
                {
                    "type_ref": outcome.value.type_ref,
                    "value": outcome.value.value,
                    "cog_receipt": outcome.receipt.to_dict(),
                }
            ).encode("utf-8"),
            "application/json",
        )
        return self._emit("completed", "result_received", output=output)

    def fail(self, *, uncertain: bool, cancelled: bool = False, reason: str) -> None:
        self._emit(
            "cancelled" if cancelled else "indeterminate" if uncertain else "failed",
            "terminal",
            reason=reason,
        )
