"""Budgeted local embedding ingestion and exact, scoped pgvector retrieval.

This is the initial standard embedding channel. Its sources are explicitly
registered evidence text; learned memory admission and hybrid ranking are later
phases. A source head is checked again under lock before a vector is published.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.memory.embedding_profile import (
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingProvider,
    validate_vector,
)
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database


class EmbeddingService:
    def __init__(
        self, database: Database, artifacts: ArtifactStore, provider: EmbeddingProvider
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.provider = provider
        self.budgets = BudgetLedger(database)

    def register_space(
        self, context: WorkspaceContext, profile: EmbeddingProfile | None = None
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        profile = profile or self.provider.profile
        with self.database.transaction(context) as conn:
            conn.execute(
                "INSERT INTO embedding_spaces(workspace_id,id,content_hash,content,dimensions,metric) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,content_hash) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    profile.fingerprint,
                    Jsonb(profile.to_dict()),
                    profile.dimensions,
                    profile.metric,
                ),
            )
            row = conn.execute(
                "SELECT * FROM embedding_spaces WHERE workspace_id=%s AND content_hash=%s",
                (context.workspace_id, profile.fingerprint),
            ).fetchone()
            return dict(row)

    def register_source(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        source_key: str,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        metadata = metadata or {}
        if (
            not isinstance(source_key, str)
            or not 1 <= len(source_key) <= 1000
            or not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8")) > 2_000_000
        ):
            raise InvalidInputError("source requires a bounded key and nonempty text")
        if set(metadata) - {"path", "symbol", "revision", "kind", "license", "source_url"} or any(
            not isinstance(v, str) or len(v) > 2000 for v in metadata.values()
        ):
            raise InvalidInputError("unsupported source metadata")
        digest = content_hash({"text": text, "metadata": metadata})
        with self.database.transaction(context) as conn:
            if (
                conn.execute(
                    "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, repository_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
            artifact = self.artifacts.put_in_transaction(
                conn, context, text.encode("utf-8"), "text/plain; charset=utf-8"
            )
            conn.execute(
                "INSERT INTO embedding_sources(workspace_id,id,repository_id,source_key,artifact_id,content_hash,content,metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,repository_id,source_key,content_hash) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    repository_id,
                    source_key,
                    artifact["id"],
                    digest,
                    text,
                    Jsonb(metadata),
                ),
            )
            row = conn.execute(
                "SELECT * FROM embedding_sources WHERE workspace_id=%s AND repository_id=%s AND source_key=%s AND content_hash=%s",
                (context.workspace_id, repository_id, source_key, digest),
            ).fetchone()
            conn.execute(
                "INSERT INTO embedding_source_heads(workspace_id,id,repository_id,source_key,source_id,generation,active) VALUES (%s,%s,%s,%s,%s,1,true) ON CONFLICT (workspace_id,repository_id,source_key) DO NOTHING",
                (context.workspace_id, uuid4(), repository_id, source_key, row["id"]),
            )
            head = conn.execute(
                "SELECT * FROM embedding_source_heads WHERE workspace_id=%s AND repository_id=%s AND source_key=%s FOR UPDATE",
                (context.workspace_id, repository_id, source_key),
            ).fetchone()
            if head["source_id"] != row["id"] or not head["active"]:
                conn.execute(
                    "UPDATE embedding_source_heads SET source_id=%s,active=true,generation=generation+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                    (row["id"], context.workspace_id, head["id"]),
                )
            return dict(row)

    def retire_source(
        self, context: WorkspaceContext, repository_id: UUID, source_key: str
    ) -> None:
        with self.database.transaction(context) as conn:
            result = conn.execute(
                "UPDATE embedding_source_heads SET source_id=NULL,active=false,generation=generation+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND repository_id=%s AND source_key=%s RETURNING id",
                (context.workspace_id, repository_id, source_key),
            ).fetchone()
            if result is None:
                raise ScopeDeniedError("record is unavailable")

    def _space(self, conn: Any, context: WorkspaceContext, space_id: UUID) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM embedding_spaces WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, space_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("record is unavailable")
        if (
            row["content_hash"] != self.provider.profile.fingerprint
            or content_hash(row["content"]) != row["content_hash"]
        ):
            raise InvalidInputError("provider and stored embedding spaces are incompatible")
        return dict(row)

    @staticmethod
    def _episode_repository(
        conn: Any, context: WorkspaceContext, episode_id: UUID, repository_id: UUID
    ) -> None:
        if (
            conn.execute(
                "SELECT id FROM episodes WHERE workspace_id=%s AND id=%s AND repository_id=%s",
                (context.workspace_id, episode_id, repository_id),
            ).fetchone()
            is None
        ):
            raise ScopeDeniedError("record is unavailable")

    def _encode(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        text: str,
        kind: Literal["query", "document"],
        runtime_budget_ms: int,
    ) -> tuple[EmbeddingBatch, UUID]:
        if type(runtime_budget_ms) is not int or not 1 <= runtime_budget_ms <= 3_600_000:
            raise InvalidInputError("embedding runtime reservation must be bounded")
        # Reserve the configured maximum before model/tokenizer loading. This
        # deliberately conservative initial adapter never spends preparation
        # compute before admission; confirmed unused tokens are then released.
        self.provider.profile.prepare(text, kind)
        reservation = self.budgets.reserve(
            context,
            episode_id,
            {
                "tokens": self.provider.profile.max_input_tokens,
                "calls": 1,
                "tool_ms": runtime_budget_ms,
                "concurrency": 1,
            },
            f"embedding-{uuid4()}",
        )
        reservation_id = UUID(str(reservation["id"]))
        started = time.monotonic()
        encoding_started = False
        try:
            tokens = self.provider.count_tokens((text,), kind)
            if type(tokens) is not int or not 0 < tokens <= self.provider.profile.max_input_tokens:
                raise InvalidInputError("provider input exceeds the registered token bound")
            encoding_started = True
            result = self.provider.encode((text,), kind)
            if not isinstance(result, EmbeddingBatch):
                raise InvalidInputError("embedding provider returned an untyped result")
        except BaseException:
            if encoding_started:
                # Missing usage after dispatch is never assumed to be free.
                self.budgets.mark_indeterminate(
                    context, reservation_id, "embedding provider did not return confirmed usage"
                )
            else:
                self.budgets.settle(
                    context,
                    reservation_id,
                    {"tokens": 0, "calls": 0, "tool_ms": int((time.monotonic() - started) * 1000)},
                    origin="measured",
                )
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.budgets.settle(
            context,
            reservation_id,
            {
                "tokens": result.input_tokens,
                "calls": 1,
                "tool_ms": elapsed_ms if result.usage_origin == "measured" else result.elapsed_ms,
            },
            origin=result.usage_origin,
        )
        if len(result.vectors) != 1:
            raise InvalidInputError("embedding provider returned the wrong batch size")
        validate_vector(result.vectors[0])
        return result, reservation_id

    def index_source(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        source_id: UUID,
        space_id: UUID,
        *,
        runtime_budget_ms: int = 60_000,
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            self._space(conn, context, space_id)
            source = conn.execute(
                "SELECT s.*,h.generation FROM embedding_sources s JOIN embedding_source_heads h ON (h.workspace_id,h.source_id)=(s.workspace_id,s.id) WHERE s.workspace_id=%s AND s.id=%s AND h.active",
                (context.workspace_id, source_id),
            ).fetchone()
            if source is None:
                raise ScopeDeniedError("record is unavailable")
            self._episode_repository(conn, context, episode_id, source["repository_id"])
            existing = conn.execute(
                "SELECT id,source_id,space_id,content_hash,usage_origin FROM embedding_vectors WHERE workspace_id=%s AND source_id=%s AND space_id=%s",
                (context.workspace_id, source_id, space_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
        result, reservation_id = self._encode(
            context, episode_id, source["content"], "document", runtime_budget_ms
        )
        with self.database.transaction(context) as conn:
            head = conn.execute(
                "SELECT * FROM embedding_source_heads WHERE workspace_id=%s AND repository_id=%s AND source_key=%s FOR UPDATE",
                (context.workspace_id, source["repository_id"], source["source_key"]),
            ).fetchone()
            if (
                head is None
                or not head["active"]
                or head["source_id"] != source_id
                or head["generation"] != source["generation"]
            ):
                raise RevisionConflictError("source changed or was retired while encoding")
            conn.execute(
                "INSERT INTO embedding_vectors(workspace_id,id,source_id,space_id,embedding,content_hash,reservation_id,usage_origin) VALUES (%s,%s,%s,%s,%s::public.vector,%s,%s,%s) ON CONFLICT (workspace_id,source_id,space_id) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    source_id,
                    space_id,
                    json.dumps(result.vectors[0]),
                    source["content_hash"],
                    reservation_id,
                    result.usage_origin,
                ),
            )
            row = conn.execute(
                "SELECT id,source_id,space_id,content_hash,usage_origin FROM embedding_vectors WHERE workspace_id=%s AND source_id=%s AND space_id=%s",
                (context.workspace_id, source_id, space_id),
            ).fetchone()
            return dict(row)

    def search(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        repository_id: UUID,
        space_id: UUID,
        query: str,
        *,
        limit: int = 5,
        runtime_budget_ms: int = 60_000,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("retrieval limit must be between 1 and 100")
        with self.database.transaction(context) as conn:
            self._space(conn, context, space_id)
            self._episode_repository(conn, context, episode_id, repository_id)
            if (
                conn.execute(
                    "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, repository_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
        result, _ = self._encode(context, episode_id, query, "query", runtime_budget_ms)
        with self.database.transaction(context) as conn:
            # No approximate index is used in this first integration. Rejoining
            # current heads prevents retired or superseded sources reappearing.
            rows = conn.execute(
                "SELECT s.id,s.source_key,s.artifact_id,s.content,s.metadata,v.space_id,v.embedding OPERATOR(public.<=>) %s::public.vector AS distance FROM embedding_vectors v JOIN embedding_sources s ON (s.workspace_id,s.id)=(v.workspace_id,v.source_id) JOIN embedding_source_heads h ON (h.workspace_id,h.source_id)=(s.workspace_id,s.id) WHERE v.workspace_id=%s AND v.space_id=%s AND s.repository_id=%s AND h.active AND v.content_hash=s.content_hash ORDER BY distance,s.id LIMIT %s",
                (
                    json.dumps(result.vectors[0]),
                    context.workspace_id,
                    space_id,
                    repository_id,
                    limit,
                ),
            ).fetchall()
            return [dict(row) for row in rows]
