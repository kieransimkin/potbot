"""Authenticated, pinned producer for the existing workspace execution ledger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

from potbot.cogs.contracts import CogRunContext, CogSignature, TypedCogValue
from potbot.controller.execution_events import ExecutionEventService
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.execution_contracts import EVENT, TERMINAL, WorkBinding

TRANSPORT_EFFECT_KINDS = frozenset(
    {"api", "mcp_server", "mcp_session", "mcp_tool", "mcp_workflow", "script"}
)
TRANSPORT_EFFECT_PROFILE = "sandbox-transport-effect/v1"
TRANSPORT_DISPATCH_PHASE = "transport_dispatch_committed"


def inspect_sandbox_transport_effect(
    database: Any, artifacts: Any, actor: Any, run_id: UUID
) -> dict[str, Any]:
    """Inspect one owned attempt after reconnect; never dispatch, settle or authorize.

    The dispatch marker is a write-ahead boundary, not proof of process launch.
    A retained local completion receipt is not independent remote verification.
    """
    if not isinstance(run_id, UUID):
        raise InvalidInputError("Transport effect inspection requires a run UUID")
    with database.transaction(actor) as connection:
        row = connection.execute(
            "SELECT c.scope,c.scope_hash,e.stage,e.output_artifact_ref,e.output_sha256,"
            "EXISTS (SELECT 1 FROM syn_execution_events d WHERE d.workspace_id=c.workspace_id "
            "AND d.run_id=c.run_id AND d.stage='updated' AND d.telemetry->>'phase'=%s) AS dispatched "
            "FROM execution_run_contracts c JOIN LATERAL ("
            "SELECT stage,output_artifact_ref,output_sha256 FROM syn_execution_events "
            "WHERE workspace_id=c.workspace_id AND run_id=c.run_id "
            "ORDER BY sequence DESC LIMIT 1) e ON true "
            "WHERE c.workspace_id=%s AND c.run_id=%s AND c.owner_principal_id=%s",
            (TRANSPORT_DISPATCH_PHASE, actor.workspace_id, run_id, actor.principal_id),
        ).fetchone()
    if row is None or row["scope"].get("transport_effect_profile") != TRANSPORT_EFFECT_PROFILE:
        raise ScopeDeniedError("Transport effect record is unavailable in this scope")
    if content_hash(row["scope"]) != row["scope_hash"]:
        raise StateMismatchError("Transport effect intent scope is not intact")
    stage, dispatched = row["stage"], row["dispatched"]
    receipt_ref = receipt_hash = None
    if stage == "completed":
        if not dispatched or not row["output_artifact_ref"] or not row["output_sha256"]:
            raise StateMismatchError("Transport completion lacks its dispatch marker or receipt")
        raw = artifacts.read(actor, PinnedExecutionJournal._artifact(row["output_artifact_ref"]))
        if hashlib.sha256(raw).hexdigest() != row["output_sha256"]:
            raise StateMismatchError("Transport completion receipt bytes changed")
        state = "receipt_recorded"
        receipt_ref, receipt_hash = row["output_artifact_ref"], row["output_sha256"]
    elif dispatched or stage == "indeterminate":
        state = "indeterminate"
    elif stage in {"failed", "cancelled"}:
        state = "stopped_before_dispatch"
    else:
        state = "intent_recorded"
    return {
        "run_id": str(run_id),
        "state": state,
        "dispatch_marker_recorded": dispatched,
        "receipt_ref": receipt_ref,
        "receipt_sha256": receipt_hash,
        "external_effect_verified": False,
        "automatic_retry_permitted": False,
    }


class PinnedExecutionJournal:
    """A per-invocation journal; the host supplies identity independently of JSON."""

    def __init__(
        self,
        database: Any,
        artifacts: Any,
        actor: Any,
        *,
        snapshot_id: UUID,
        signature: CogSignature,
        source: TypedCogValue,
        context: CogRunContext,
        binding: WorkBinding,
        emitter: UUID,
        isolated_transport_effect: bool = False,
    ) -> None:
        from potbot.cogs.artifact_ranges import RANGE_COGS, artifact_range_snapshot
        from potbot.cogs.artifact_uploads import UPLOAD_COGS, artifact_upload_snapshot
        from potbot.cogs.browser import BROWSER_COGS, browser_cog_snapshot
        from potbot.cogs.execution import EXECUTION_COGS, execution_cog_snapshot

        allowed: tuple[CogSignature, ...] = EXECUTION_COGS
        expected_catalog = execution_cog_snapshot().fingerprint
        if context.cog_snapshot_hash != expected_catalog:
            expected_catalog = artifact_range_snapshot().fingerprint
            allowed = (*EXECUTION_COGS, *RANGE_COGS)
        if context.cog_snapshot_hash != expected_catalog:
            expected_catalog = artifact_upload_snapshot().fingerprint
            allowed = (*EXECUTION_COGS, *RANGE_COGS, *UPLOAD_COGS)
        if context.cog_snapshot_hash != expected_catalog:
            # Browser and artifact profiles are separate sealed catalogues.
            # Do not union their direct operators or admit a caller's snapshot.
            expected_catalog = browser_cog_snapshot().fingerprint
            allowed = (*EXECUTION_COGS, *BROWSER_COGS)

        if (
            signature not in allowed
            or context.cog_snapshot_hash != expected_catalog
            or source.type_ref != signature.input_type_ref
        ):
            raise ScopeDeniedError("Journal requires the exact installed Cog and semantic revision")
        self.db, self.artifacts, self.actor = database, artifacts, actor
        self.snapshot_id, self.signature, self.source = snapshot_id, signature, source
        self.context, self.binding, self.emitter = context, binding, emitter
        self.service = ExecutionEventService(database)
        self.scope = {
            **binding.to_dict(),
            "catalog_hash": context.cog_snapshot_hash,
            "task_revision_id": str(context.task_revision_id),
            "repository_state_id": str(context.repository_state_id),
            "cog_ref": str(signature.cog_ref),
            "binding_hash": signature.binding_hash,
            "input_value_hash": source.fingerprint,
            "policy_hash": binding.policy.fingerprint,
        }
        if isolated_transport_effect:
            if binding.kind not in TRANSPORT_EFFECT_KINDS or binding.effect_class == "read_only":
                raise ScopeDeniedError("Transport intent requires an installed mutating work route")
            self.scope["transport_effect_profile"] = TRANSPORT_EFFECT_PROFILE
        self.isolated_transport_effect = isolated_transport_effect
        self.scope_hash = content_hash(self.scope)

    def _require_clear_transport_lane(self, connection: Any, run_id: UUID) -> None:
        # The existing workspace cursor lock serializes this check with intent
        # creation and settlement. The lane is stable across controller/agent,
        # task and policy revisions; a fresh UUID cannot evade an unknown write.
        pending = connection.execute(
            "SELECT 1 FROM execution_run_contracts c JOIN LATERAL ("
            "SELECT stage FROM syn_execution_events WHERE workspace_id=c.workspace_id "
            "AND run_id=c.run_id ORDER BY sequence DESC LIMIT 1) e ON true "
            "WHERE c.workspace_id=%s AND c.run_id<>%s AND c.scope->>'kind'=%s "
            "AND c.scope->>'operation_ref'=%s AND c.scope->>'effect_class'<>'read_only' "
            "AND e.stage IN ('started','updated','indeterminate') LIMIT 1",
            (self.actor.workspace_id, run_id, self.binding.kind, self.binding.operation_ref),
        ).fetchone()
        if pending:
            raise ScopeDeniedError("Transport operation has an unresolved durable attempt")

    def append(self, actor: Any, event: ExecutionEventInput) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        if actor != self.actor or (
            event.run_id != self.context.invocation_id
            or event.emitter_run_id != self.emitter
            or event.event_type_ref != EVENT
            or event.event_type_version != "1"
            or event.subject_type_ref != self.signature.cog_type_ref
            or event.subject_version != "1"
        ):
            raise ScopeDeniedError(
                "Execution event differs from the authenticated installed binding"
            )
        expected = canonical_json(
            {"type_ref": self.source.type_ref, "value": self.source.value}
        ).encode("utf-8")
        input_id = self._artifact(event.input_artifact_ref)
        if (
            hashlib.sha256(expected).hexdigest() != event.trigger_input_sha256
            or self.artifacts.read(actor, input_id) != expected
        ):
            raise StateMismatchError("Private input bytes differ from the admitted invocation")
        if event.output_artifact_ref:
            raw = self.artifacts.read(actor, self._artifact(event.output_artifact_ref))
            if hashlib.sha256(raw).hexdigest() != event.output_sha256:
                raise StateMismatchError("Output artifact bytes differ from the completion digest")
        telemetry = json.loads(event.telemetry_json)
        if any(
            telemetry.get(k) != v
            for k, v in (
                ("cog_ref", str(self.signature.cog_ref)),
                ("binding_hash", self.signature.binding_hash),
                ("semantic_snapshot_hash", self.context.cog_snapshot_hash),
                ("task_revision_id", str(self.context.task_revision_id)),
                ("repository_state_id", str(self.context.repository_state_id)),
            )
        ):
            raise StateMismatchError("Work telemetry changed its pinned invocation identity")
        telemetry["execution_scope_hash"] = self.scope_hash
        telemetry["policy_hash"] = self.binding.policy.fingerprint
        telemetry["work_kind"] = self.binding.kind
        telemetry["trace_id"], telemetry["span_id"] = self.binding.trace_id, self.binding.span_id
        telemetry["clock_offset_ms"] = self.binding.policy.host_clock_offset_ms
        telemetry["clock_uncertainty_ms"] = self.binding.policy.host_clock_uncertainty_ms
        transport_marker = telemetry.get("phase") == TRANSPORT_DISPATCH_PHASE
        with self.db.transaction(actor) as connection:
            self.service._lock_cursor(connection, actor)
            previous = connection.execute(
                "SELECT * FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
                "ORDER BY sequence DESC LIMIT 1",
                (actor.workspace_id, event.run_id),
            ).fetchone()
            contract = connection.execute(
                "SELECT * FROM execution_run_contracts WHERE workspace_id=%s AND run_id=%s",
                (actor.workspace_id, event.run_id),
            ).fetchone()
            if transport_marker and (
                not self.isolated_transport_effect
                or event.stage != "updated"
                or previous is None
                or previous["stage"] != "updated"
                or previous["telemetry"].get("phase") != "admitted_dispatch"
            ):
                raise ScopeDeniedError(
                    "Transport dispatch requires its preceding durable admission"
                )
            if self.isolated_transport_effect and event.stage in {
                "completed",
                "failed",
                "cancelled",
            }:
                marker = connection.execute(
                    "SELECT 1 FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
                    "AND stage='updated' AND telemetry->>'phase'=%s LIMIT 1",
                    (actor.workspace_id, event.run_id, TRANSPORT_DISPATCH_PHASE),
                ).fetchone()
                if bool(marker) != (event.stage == "completed"):
                    raise ScopeDeniedError(
                        "Transport terminal state differs from its dispatch boundary"
                    )
            if contract is None:
                if (
                    event.parent_event_id is not None
                    or event.parent_run_id is not None
                    or event.trigger_kind != "api_request"
                    or event.trigger_ref != f"cog-request:{event.run_id}"
                ):
                    raise ScopeDeniedError(
                        "Initial observation differs from its host-created request"
                    )
                if (
                    previous
                    or event.stage not in {"started", "failed"}
                    or (event.stage == "failed" and telemetry.get("phase") != "admission_denied")
                ):
                    raise ScopeDeniedError("Orphan, reused or unadmitted execution run")
                snapshot = connection.execute(
                    "SELECT source_hash FROM syn_snapshots WHERE workspace_id=%s AND id=%s",
                    (actor.workspace_id, self.snapshot_id),
                ).fetchone()
                if snapshot is None or snapshot["source_hash"] != self.context.cog_snapshot_hash:
                    raise ScopeDeniedError("Execution catalog is not the sealed installed revision")
                if self.isolated_transport_effect and event.stage == "started":
                    self._require_clear_transport_lane(connection, event.run_id)
                parent_run = None
                for cause in (
                    *self.binding.links,
                    *((self.binding.parent_event_id,) if self.binding.parent_event_id else ()),
                ):
                    parent = connection.execute(
                        "SELECT e.run_id,e.stage,c.scope,c.owner_principal_id FROM syn_execution_events e "
                        "JOIN execution_run_contracts c USING(workspace_id,run_id) "
                        "WHERE e.workspace_id=%s AND e.id=%s",
                        (actor.workspace_id, cause),
                    ).fetchone()
                    if (
                        parent is None
                        or parent["run_id"] == event.run_id
                        or parent["owner_principal_id"] != actor.principal_id
                    ):
                        raise ScopeDeniedError(
                            "Causal event has no authenticated workspace contract"
                        )
                    if any(
                        parent["scope"][k] != self.scope[k]
                        for k in (
                            "catalog_hash",
                            "task_revision_id",
                            "repository_state_id",
                            "policy_hash",
                        )
                    ):
                        raise ScopeDeniedError("Causal event belongs to another execution scope")
                    if cause == self.binding.parent_event_id:
                        last = connection.execute(
                            "SELECT id,stage FROM syn_execution_events WHERE workspace_id=%s AND run_id=%s "
                            "ORDER BY sequence DESC LIMIT 1",
                            (actor.workspace_id, parent["run_id"]),
                        ).fetchone()
                        if last["id"] != cause or last["stage"] in TERMINAL:
                            raise ScopeDeniedError("Child work requires its active direct parent")
                        parent_run = parent["run_id"]
                connection.execute(
                    "INSERT INTO execution_run_contracts(workspace_id,run_id,owner_principal_id,snapshot_id,"
                    "scope_hash,scope,input_artifact_id,input_sha256,parent_run_id,parent_event_id,emitter_run_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        actor.workspace_id,
                        event.run_id,
                        actor.principal_id,
                        self.snapshot_id,
                        self.scope_hash,
                        Jsonb(self.scope),
                        input_id,
                        event.trigger_input_sha256,
                        parent_run,
                        self.binding.parent_event_id,
                        self.emitter,
                    ),
                )
                event = replace(
                    event,
                    parent_run_id=parent_run,
                    parent_event_id=self.binding.parent_event_id,
                    trigger_kind="parent_event" if parent_run else "user_input",
                    trigger_ref=f"syn://event/{self.binding.parent_event_id}"
                    if parent_run
                    else f"artifact:{input_id}",
                )
            elif (
                previous is None
                or previous["stage"] in TERMINAL
                or contract["scope_hash"] != self.scope_hash
                or contract["owner_principal_id"] != actor.principal_id
                or contract["emitter_run_id"] != self.emitter
                or event.parent_event_id != previous["id"]
                or event.parent_run_id != event.run_id
                or contract["input_artifact_id"] != input_id
                or contract["input_sha256"] != event.trigger_input_sha256
                or event.stage == "started"
            ):
                raise ScopeDeniedError("Execution lifecycle, emitter or immutable input changed")
            if event.stage in TERMINAL:
                active_child = connection.execute(
                    "SELECT 1 FROM execution_run_contracts c JOIN LATERAL ("
                    "SELECT stage FROM syn_execution_events e WHERE e.workspace_id=c.workspace_id "
                    "AND e.run_id=c.run_id ORDER BY sequence DESC LIMIT 1) e ON true "
                    "WHERE c.workspace_id=%s AND c.parent_run_id=%s "
                    "AND e.stage NOT IN ('completed','failed','cancelled','indeterminate') LIMIT 1",
                    (actor.workspace_id, event.run_id),
                ).fetchone()
                if active_child:
                    raise ScopeDeniedError("Parent cannot finish while a child is active")
            return self.service._append(
                connection,
                actor,
                replace(event, telemetry_json=canonical_json(telemetry)),
                bound=True,
            )

    @staticmethod
    def _artifact(ref: str | None) -> UUID:
        if not isinstance(ref, str) or not ref.startswith("artifact:"):
            raise ScopeDeniedError("Work input/output requires a private artifact identity")
        from potbot.tools.execution_contracts import identifier

        return identifier(ref.removeprefix("artifact:"))
