"""An honest fixture producer using the common packet and invocation contracts.

This adapter performs no inference. Usage is synthetic (ceil UTF-8 bytes / 4),
its deployment identifies this adapter, and supplied responses are persisted
exactly. Live provider qualification remains a later phase.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.controller.coding import CodingStore, coding_row
from potbot.controller.execution import scoped_row
from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.coding import CODING_RESPONSE_SCHEMA, ScriptedCodingResponse
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.models.invocations import (
    GenerateRequest,
    GenerationResult,
    InvocationStore,
    PacketSource,
)
from potbot.models.registry import DeploymentSpec, ModelRegistry
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event


def scripted_deployment() -> DeploymentSpec:
    """Identify executable fixture behavior, never pretend to load model weights."""
    return DeploymentSpec(
        canonical_model="potbot/scripted-patch-fixture",
        weights_digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        backend="scripted-fixture-no-inference",
        backend_version="1",
        quantization="not-applicable",
        tokenizer_revision=hashlib.sha256(b"synthetic-ceil-utf8-bytes-divided-by-4-v1").hexdigest(),
        chat_template_digest=hashlib.sha256(b"manually-assembled-coding-packet-v1").hexdigest(),
        output_adapter="scripted-patch-v1",
        tool_protocol="none",
        usable_context_tokens=16_777_216,
        max_output_tokens=8_388_608,
        prompt_versions=(("coding", "manually-assembled-v1"),),
        roles=("coding",),
        response_schemas=(CODING_RESPONSE_SCHEMA,),
    )


class ScriptedCodingProducer:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self.database, self.artifacts = database, artifacts
        self.store = CodingStore(database, artifacts)
        self.invocations = InvocationStore(database)
        self.budgets = BudgetLedger(database)

    def _replay(
        self, context: WorkspaceContext, key: str, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self.database.transaction(context) as conn:
            prior = conn.execute(
                "SELECT request_hash,result FROM command_results WHERE workspace_id=%s AND operation='produce_scripted_coding' AND idempotency_key=%s",
                (context.workspace_id, key),
            ).fetchone()
            if prior is None:
                return None
            if prior["request_hash"] != content_hash(request):
                raise RevisionConflictError(
                    "This coding key was used for another response or packet."
                )
            return dict(prior["result"])

    def _settle(self, context: WorkspaceContext, result: dict[str, Any]) -> None:
        usage = result["content"]["synthetic_usage"]
        self.budgets.settle(
            context,
            UUID(result["reservation_id"]),
            {"calls": 1, "tokens": usage["input_tokens"] + usage["output_tokens"]},
            "synthetic",
        )

    def produce(
        self,
        context: WorkspaceContext,
        response: ScriptedCodingResponse,
        idempotency_key: str,
        *,
        packet_text: str | None = None,
        packet_sources: tuple[PacketSource, ...] = (),
    ) -> dict[str, Any]:
        if not isinstance(response, ScriptedCodingResponse):
            raise InvalidInputError("A typed scripted response is required.")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise InvalidInputError("An idempotency key of 1 to 128 characters is required.")
        if (
            not isinstance(packet_sources, tuple)
            or len(packet_sources) > 256
            or any(not isinstance(source, PacketSource) for source in packet_sources)
        ):
            raise InvalidInputError("Packet sources must be bounded typed references.")
        handoff = self.store.get_contract(context, response.implementation_id)
        if handoff["base_state_id"] != str(response.base_state_id):
            raise StateMismatchError("Coding response identifies a different repository base.")
        episode_id = UUID(handoff["episode_id"])
        with self.database.transaction(context) as conn:
            state = scoped_row(conn, context, "repository_states", response.base_state_id)
            default_sources = (
                PacketSource("artifact", state["snapshot_artifact_id"]),
                *(
                    PacketSource("artifact", UUID(receipt["artifact_id"]))
                    for receipt in handoff["content"]["search_receipts"]
                ),
            )
            sources = tuple(dict.fromkeys((*default_sources, *packet_sources)))
            for source in sources:
                table = "artifacts" if source.kind == "artifact" else "evidence_events"
                scoped_row(conn, context, table, source.reference_id)
        if packet_text is None:
            packet_text = canonical_json(
                {
                    "schema_version": 1,
                    "assembly": "manually_assembled_fixture_packet",
                    "implementation_contract": handoff["content"],
                    "source_references": [
                        {"kind": s.kind, "id": str(s.reference_id)} for s in sources
                    ],
                }
            )
        if not isinstance(packet_text, str) or not packet_text.strip() or "\x00" in packet_text:
            raise InvalidInputError("The exact manual context packet must be nonempty text.")
        packet_bytes = packet_text.encode("utf-8")
        if len(packet_bytes) > 4 * 1024 * 1024:
            raise InvalidInputError("The manual context packet exceeds its bounded size.")
        request = {
            "response": response.to_dict(),
            "packet_digest": hashlib.sha256(packet_bytes).hexdigest(),
            "sources": [
                {"kind": source.kind, "id": str(source.reference_id)} for source in sources
            ],
        }
        prior = self._replay(context, idempotency_key, request)
        if prior is not None:
            self._settle(context, prior)
            return prior
        with self.database.transaction(context) as conn:
            episode = lock_episode(conn, context, episode_id)
            if (
                episode["status"] != "running"
                or str(episode["active_contract_id"]) != handoff["contract_revision_id"]
            ):
                raise StateMismatchError("Coding requires a running task and its current handoff.")
        raw_response = canonical_json(
            {
                **response.to_dict(),
                "patch": response.patch.decode("utf-8") if response.patch is not None else None,
            }
        ).encode("utf-8")
        input_tokens, output_tokens = (len(packet_bytes) + 3) // 4, (len(raw_response) + 3) // 4
        deployment = ModelRegistry(self.database).register(context, scripted_deployment())
        budget_key = "scripted-coding:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        reservation = self.budgets.reserve(
            context,
            episode_id,
            {"calls": 1, "tokens": input_tokens + output_tokens},
            budget_key,
        )
        reservation_id = UUID(reservation["id"])
        try:
            with self.database.transaction(context) as conn:

                def record() -> dict[str, Any]:
                    from psycopg.types.json import Jsonb

                    episode = lock_episode(conn, context, episode_id)
                    if (
                        episode["status"] != "running"
                        or str(episode["active_contract_id"]) != handoff["contract_revision_id"]
                    ):
                        raise StateMismatchError(
                            "Coding handoff became stale before result publication."
                        )
                    live = scoped_row(conn, context, "budget_reservations", reservation_id)
                    if live["status"] != "reserved" or live["episode_id"] != episode_id:
                        raise StateMismatchError("Coding requires its own live reservation.")
                    packet_artifact = self.artifacts.put_in_transaction(
                        conn, context, packet_bytes, "text/plain; charset=utf-8"
                    )
                    packet = self.invocations.create_packet_in_transaction(
                        conn,
                        context,
                        deployment_id=deployment["id"],
                        artifact_id=packet_artifact["id"],
                        sources=sources,
                        token_count=input_tokens,
                        policy_version="manual-scripted-coding-v1",
                        episode_id=episode_id,
                    )
                    patch = (
                        None
                        if response.patch is None
                        else self.artifacts.put_in_transaction(
                            conn, context, response.patch, "text/x-diff"
                        )
                    )
                    payload = response.to_dict(patch["id"] if patch else None)
                    raw = self.artifacts.put_in_transaction(
                        conn, context, raw_response, "application/json"
                    )
                    generation_request = GenerateRequest(
                        deployment["id"],
                        "coding",
                        packet["id"],
                        CODING_RESPONSE_SCHEMA,
                        '{"temperature":0}',
                        max(1, output_tokens),
                        datetime.now(UTC) + timedelta(seconds=60),
                        uuid4(),
                    )
                    invocation = self.invocations.record_final_in_transaction(
                        conn,
                        context,
                        generation_request,
                        GenerationResult(
                            raw["id"],
                            canonical_json(payload),
                            input_tokens,
                            output_tokens,
                            0,
                            "scripted_fixture",
                            "parsed",
                            "succeeded",
                            usage_origin="synthetic",
                        ),
                    )
                    identity = uuid4()
                    content = {
                        **payload,
                        "deployment_id": str(deployment["id"]),
                        "packet_id": str(packet["id"]),
                        "invocation_id": str(invocation["id"]),
                        "reservation_id": str(reservation_id),
                        "synthetic_usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "method": "ceil_utf8_bytes_divided_by_4",
                            "elapsed_ms": 0,
                        },
                    }
                    conn.execute(
                        "INSERT INTO coding_results(workspace_id,id,episode_id,implementation_id,base_state_id,"
                        "deployment_id,packet_id,invocation_id,reservation_id,patch_artifact_id,inability_code,content,content_digest) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            identity,
                            episode_id,
                            response.implementation_id,
                            response.base_state_id,
                            deployment["id"],
                            packet["id"],
                            invocation["id"],
                            reservation_id,
                            patch["id"] if patch else None,
                            response.inability_code,
                            Jsonb(content),
                            content_hash(content),
                        ),
                    )
                    append_event(
                        conn,
                        context,
                        "coding_result_recorded",
                        episode_id=episode_id,
                        producer_kind="model",
                        producer_id="potbot.scripted-fixture",
                        deployment_id=deployment["id"],
                        payload_id=raw["id"],
                        details={
                            "coding_result_id": str(identity),
                            "implementation_id": str(response.implementation_id),
                            "invocation_id": str(invocation["id"]),
                            "producer_kind": "scripted_fixture",
                            "usage_origin": "synthetic",
                        },
                    )
                    return dict(json_record(coding_row(conn, context, "coding_results", identity)))

                result = idempotent_command(
                    conn, context, "produce_scripted_coding", idempotency_key, request, record
                )
        except IndeterminateExecutionError:
            # A lost SQL acknowledgment may have committed the complete result.
            # Retry the same key to recognize and settle it, without creating an invocation.
            raise
        except (InvalidInputError, ScopeDeniedError, StateMismatchError):
            if self._replay(context, idempotency_key, request) is None:
                self.budgets.release(
                    context,
                    reservation_id,
                    "Scripted generation was not recorded.",
                    confirmed_not_started=True,
                )
            raise
        self._settle(context, result)
        return result
