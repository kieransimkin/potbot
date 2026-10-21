"""Immutable context and generation records for scripted, preflighted transports."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, OperationTimeoutError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database


@dataclass(frozen=True, slots=True)
class PacketSource:
    kind: str
    reference_id: UUID

    def __post_init__(self) -> None:
        if self.kind not in {"artifact", "event"} or not isinstance(self.reference_id, UUID):
            raise InvalidInputError("packet source must identify an artifact or evidence event")


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    deployment_id: UUID
    role: str
    context_packet_id: UUID
    response_schema: str
    sampling_json: str
    output_limit: int
    deadline: datetime
    cancellation_ref: UUID
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or type(self.schema_version) is not int
            or any(
                not isinstance(v, UUID)
                for v in (self.deployment_id, self.context_packet_id, self.cancellation_ref)
            )
        ):
            raise InvalidInputError("invalid generation identity")
        if (
            self.role not in {"reasoning", "coding"}
            or not isinstance(self.response_schema, str)
            or not self.response_schema
        ):
            raise InvalidInputError("generation role and schema are required")
        if (
            type(self.output_limit) is not int
            or self.output_limit <= 0
            or not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise InvalidInputError("generation limits and deadline are required")
        try:
            sampling = json.loads(self.sampling_json)
        except (TypeError, ValueError) as error:
            raise InvalidInputError("invalid sampling parameters") from error
        if (
            not isinstance(sampling, dict)
            or set(sampling) - {"temperature", "top_p", "seed"}
            or "temperature" not in sampling
        ):
            raise InvalidInputError("unsupported or missing sampling parameters")
        for name in ("temperature", "top_p"):
            if name in sampling and (
                type(sampling[name]) not in (int, float)
                or not math.isfinite(sampling[name])
                or not 0 <= sampling[name] <= (2 if name == "temperature" else 1)
            ):
                raise InvalidInputError("sampling parameters outside bounds")
        if "seed" in sampling and (
            type(sampling["seed"]) is not int or not 0 <= sampling["seed"] < 2**63
        ):
            raise InvalidInputError("invalid sampling seed")
        object.__setattr__(self, "sampling_json", canonical_json(sampling))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "deployment_id": str(self.deployment_id),
            "role": self.role,
            "context_packet_id": str(self.context_packet_id),
            "response_schema": self.response_schema,
            "sampling": json.loads(self.sampling_json),
            "output_limit": self.output_limit,
            "deadline": self.deadline.astimezone(UTC).isoformat(),
            "cancellation_ref": str(self.cancellation_ref),
        }


@dataclass(frozen=True, slots=True)
class GenerationResult:
    raw_output_artifact_id: UUID
    parsed_result_json: str | None
    input_tokens: int
    output_tokens: int
    elapsed_ms: int
    stop_reason: str
    parse_status: str
    status: str
    error_code: str | None = None
    usage_origin: str = "synthetic"

    def __post_init__(self) -> None:
        if not isinstance(self.raw_output_artifact_id, UUID) or any(
            type(v) is not int or v < 0
            for v in (self.input_tokens, self.output_tokens, self.elapsed_ms)
        ):
            raise InvalidInputError("invalid generation output identity or usage")
        if (
            self.usage_origin not in {"synthetic", "measured", "provider_reported"}
            or self.parse_status not in {"parsed", "invalid", "partial", "not_attempted"}
            or self.status not in {"succeeded", "failed", "cancelled", "timed_out", "indeterminate"}
            or not isinstance(self.stop_reason, str)
            or not self.stop_reason
        ):
            raise InvalidInputError("invalid generation outcome")
        if self.status == "succeeded" and (
            self.parse_status != "parsed"
            or self.parsed_result_json is None
            or self.error_code is not None
        ):
            raise InvalidInputError("successful generation requires a parsed result and no error")
        if self.status != "succeeded" and self.error_code not in {
            "schema_violation",
            "model_unavailable",
            "timeout",
            "cancelled",
            "indeterminate",
            "context_overflow",
            "tool_failure",
        }:
            raise InvalidInputError("failed generation requires a classified error")
        if self.parsed_result_json is not None:
            try:
                object.__setattr__(
                    self, "parsed_result_json", canonical_json(json.loads(self.parsed_result_json))
                )
            except (ValueError, TypeError) as error:
                raise InvalidInputError("invalid parsed result JSON") from error


class InvocationStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_packet(
        self,
        context: WorkspaceContext,
        *,
        deployment_id: UUID,
        artifact_id: UUID,
        sources: tuple[PacketSource, ...],
        token_count: int,
        policy_version: str = "packet-v1",
        episode_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return self.create_packet_in_transaction(
                conn,
                context,
                deployment_id=deployment_id,
                artifact_id=artifact_id,
                sources=sources,
                token_count=token_count,
                policy_version=policy_version,
                episode_id=episode_id,
            )

    def create_packet_in_transaction(
        self,
        conn: Any,
        context: WorkspaceContext,
        *,
        deployment_id: UUID,
        artifact_id: UUID,
        sources: tuple[PacketSource, ...],
        token_count: int,
        policy_version: str = "packet-v1",
        episode_id: UUID | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        from potbot.semantics.releases import packet_snapshot, pin_packet

        if not isinstance(sources, tuple) or any(
            not isinstance(source, PacketSource) for source in sources
        ):
            raise InvalidInputError("context sources must be an ordered tuple of typed references")
        if (
            type(token_count) is not int
            or token_count < 0
            or not isinstance(policy_version, str)
            or not policy_version
        ):
            raise InvalidInputError("invalid context packet metadata")
        for table, identity in (
            ("model_deployments", deployment_id),
            ("artifacts", artifact_id),
        ):
            if (
                conn.execute(
                    f"SELECT id FROM {table} WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, identity),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
        for source in sources:
            table = "artifacts" if source.kind == "artifact" else "evidence_events"
            if (
                conn.execute(
                    f"SELECT id FROM {table} WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, source.reference_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
        semantic_snapshot = packet_snapshot(conn, context, episode_id)
        payload = {
            "schema_version": 2,
            "deployment_id": str(deployment_id),
            "artifact_id": str(artifact_id),
            "sources": [{"kind": s.kind, "id": str(s.reference_id)} for s in sources],
            "token_count": token_count,
            "policy_version": policy_version,
            "semantic_release": semantic_snapshot,
        }
        packet_id = uuid4()
        row = conn.execute(
            "INSERT INTO context_packets(workspace_id,id,episode_id,deployment_id,artifact_id,content_hash,content,token_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                context.workspace_id,
                packet_id,
                episode_id,
                deployment_id,
                artifact_id,
                content_hash(payload),
                Jsonb(payload),
                token_count,
            ),
        ).fetchone()
        pin_packet(conn, context, packet_id, semantic_snapshot)
        for ordinal, source in enumerate(sources):
            conn.execute(
                "INSERT INTO context_packet_sources(workspace_id,id,packet_id,ordinal,artifact_id,event_id) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    uuid4(),
                    packet_id,
                    ordinal,
                    source.reference_id if source.kind == "artifact" else None,
                    source.reference_id if source.kind == "event" else None,
                ),
            )
        return dict(row)

    def preflight(self, context: WorkspaceContext, request: GenerateRequest) -> dict[str, Any]:
        if request.deadline <= datetime.now(UTC):
            raise OperationTimeoutError("generation deadline expired before transport admission")
        with self.database.transaction(context) as conn:
            return self._preflight(conn, context, request)

    @staticmethod
    def _preflight(
        conn: Any, context: WorkspaceContext, request: GenerateRequest
    ) -> dict[str, Any]:
        from potbot.semantics.releases import check_packet_pin

        check_packet_pin(conn, context, request.context_packet_id)
        row = conn.execute(
            "SELECT d.content AS deployment,d.content_hash AS deployment_hash,p.content_hash AS packet_hash,p.token_count FROM context_packets p JOIN model_deployments d ON (d.workspace_id,d.id)=(p.workspace_id,p.deployment_id) WHERE p.workspace_id=%s AND p.id=%s AND p.deployment_id=%s",
            (context.workspace_id, request.context_packet_id, request.deployment_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("record is unavailable")
        deployment = row["deployment"]
        if (
            request.role not in deployment["roles"]
            or request.response_schema not in deployment["response_schemas"]
        ):
            raise InvalidInputError(
                "deployment is incompatible with generation role or response schema"
            )
        if (
            request.output_limit > deployment["max_output_tokens"]
            or request.output_limit + row["token_count"] > deployment["usable_context_tokens"]
        ):
            raise InvalidInputError("generation exceeds the registered deployment limits")
        manifest = {
            "request": request.to_dict(),
            "deployment_hash": row["deployment_hash"],
            "context_packet_hash": row["packet_hash"],
        }
        return {"input_hash": content_hash(manifest), "input_manifest": manifest}

    def record_final(
        self, context: WorkspaceContext, request: GenerateRequest, result: GenerationResult
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return self.record_final_in_transaction(conn, context, request, result)

    def record_final_in_transaction(
        self,
        conn: Any,
        context: WorkspaceContext,
        request: GenerateRequest,
        result: GenerationResult,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        frozen = self._preflight(conn, context, request)
        if (
            conn.execute(
                "SELECT id FROM artifacts WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, result.raw_output_artifact_id),
            ).fetchone()
            is None
        ):
            raise ScopeDeniedError("record is unavailable")
        row = conn.execute(
            "INSERT INTO model_invocations(workspace_id,id,deployment_id,context_packet_id,input_hash,input_manifest,raw_output_artifact_id,parsed_result,input_tokens,output_tokens,elapsed_ms,stop_reason,parse_status,status,error_code,usage_origin) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                context.workspace_id,
                uuid4(),
                request.deployment_id,
                request.context_packet_id,
                frozen["input_hash"],
                Jsonb(frozen["input_manifest"]),
                result.raw_output_artifact_id,
                Jsonb(json.loads(result.parsed_result_json))
                if result.parsed_result_json is not None
                else None,
                result.input_tokens,
                result.output_tokens,
                result.elapsed_ms,
                result.stop_reason,
                result.parse_status,
                result.status,
                result.error_code,
                result.usage_origin,
            ),
        ).fetchone()
        return dict(row)
