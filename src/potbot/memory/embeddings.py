"""Budgeted local embedding ingestion and exact, scoped pgvector retrieval.

This is the initial standard embedding channel. Its sources are explicitly
registered evidence text; learned memory admission and hybrid ranking are later
phases. A source head is checked again under lock before a vector is published.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.domain.auth import WorkspaceContext
from potbot.domain.embeddings import (
    DEFAULT_EMBEDDING_NAME,
    SOURCE_TEXT_INPUT_FORMAT,
    validate_embedding_name,
)
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
from potbot.persistence.embedding_sources import register_source_in_transaction


class EmbeddingService:
    def __init__(
        self, database: Database, artifacts: ArtifactStore, provider: EmbeddingProvider
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.provider = provider
        self.budgets = BudgetLedger(database)

    def register_space(
        self,
        context: WorkspaceContext,
        profile: EmbeddingProfile | None = None,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
    ) -> dict[str, Any]:
        """Register an immutable profile and a named raw-text view of that space.

        Names select representations; the profile fingerprint still defines
        coordinate compatibility. Several names can bind the same exact space,
        and a name can retain several explicitly selected space generations.
        """
        from psycopg.types.json import Jsonb

        validate_embedding_name(embedding_name)
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
            conn.execute(
                "INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id,input_format) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,embedding_name,space_id) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    embedding_name,
                    row["id"],
                    SOURCE_TEXT_INPUT_FORMAT,
                ),
            )
            view = conn.execute(
                "SELECT id,input_format FROM embedding_views WHERE workspace_id=%s AND embedding_name=%s AND space_id=%s",
                (context.workspace_id, embedding_name, row["id"]),
            ).fetchone()
            return {
                **dict(row),
                "embedding_name": embedding_name,
                "view_id": view["id"],
                "input_format": view["input_format"],
            }

    def register_source(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        source_key: str,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return register_source_in_transaction(
                conn, context, self.artifacts, repository_id, source_key, text, metadata=metadata
            )

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

    def _space(
        self, conn: Any, context: WorkspaceContext, space_id: UUID, profile: EmbeddingProfile
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM embedding_spaces WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, space_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("record is unavailable")
        if (
            row["content_hash"] != profile.fingerprint
            or content_hash(row["content"]) != row["content_hash"]
        ):
            raise InvalidInputError("provider and stored embedding spaces are incompatible")
        return dict(row)

    def _view(
        self,
        conn: Any,
        context: WorkspaceContext,
        space_id: UUID,
        embedding_name: str,
        profile: EmbeddingProfile,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        space = self._space(conn, context, space_id, profile)
        row = conn.execute(
            "SELECT * FROM embedding_views WHERE workspace_id=%s AND embedding_name=%s AND space_id=%s",
            (context.workspace_id, embedding_name, space_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("named embedding view is unavailable")
        if row["input_format"] != SOURCE_TEXT_INPUT_FORMAT:
            raise InvalidInputError("unsupported named embedding input format")
        return space, dict(row)

    def _describe_vector(
        self,
        vector: Any,
        source: Any,
        space: dict[str, Any],
        view: dict[str, Any],
        profile: EmbeddingProfile,
    ) -> dict[str, Any]:
        prepared = profile.prepare(source["content"], "document")
        return {
            **dict(vector),
            "artifact_id": source["artifact_id"],
            "embedding_name": view["embedding_name"],
            "view_id": view["id"],
            "input_format": view["input_format"],
            "profile_hash": space["content_hash"],
            "encoding_input_hash": hashlib.sha256(prepared.encode("utf-8")).hexdigest(),
            "dimensions": space["dimensions"],
            "metric": space["metric"],
            "generation": space["content"]["generation"],
        }

    def _assert_provider(self, provider: EmbeddingProvider, profile: EmbeddingProfile) -> None:
        if self.provider is not provider or provider.profile.fingerprint != profile.fingerprint:
            raise InvalidInputError("embedding provider or profile changed during the operation")

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
        *,
        provider: EmbeddingProvider,
        profile: EmbeddingProfile,
    ) -> tuple[EmbeddingBatch, UUID]:
        if type(runtime_budget_ms) is not int or not 1 <= runtime_budget_ms <= 3_600_000:
            raise InvalidInputError("embedding runtime reservation must be bounded")
        # Reserve the configured maximum before model/tokenizer loading. This
        # deliberately conservative initial adapter never spends preparation
        # compute before admission; confirmed unused tokens are then released.
        self._assert_provider(provider, profile)
        profile.prepare(text, kind)
        reservation = self.budgets.reserve(
            context,
            episode_id,
            {
                "tokens": profile.max_input_tokens,
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
            tokens = provider.count_tokens((text,), kind)
            self._assert_provider(provider, profile)
            if type(tokens) is not int or not 0 < tokens <= profile.max_input_tokens:
                raise InvalidInputError("provider input exceeds the registered token bound")
            encoding_started = True
            result = provider.encode((text,), kind)
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
        # Confirmed compute is charged even when a mutable provider changed its
        # configuration during dispatch. Such a vector never enters the old space.
        self._assert_provider(provider, profile)
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
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        runtime_budget_ms: int = 60_000,
    ) -> dict[str, Any]:
        validate_embedding_name(embedding_name)
        provider = self.provider
        profile = provider.profile
        with self.database.transaction(context) as conn:
            space, view = self._view(conn, context, space_id, embedding_name, profile)
            source = conn.execute(
                "SELECT s.*,h.generation FROM embedding_sources s JOIN embedding_source_heads h ON (h.workspace_id,h.source_id)=(s.workspace_id,s.id) WHERE s.workspace_id=%s AND s.id=%s AND h.active",
                (context.workspace_id, source_id),
            ).fetchone()
            if source is None:
                raise ScopeDeniedError("record is unavailable")
            self._episode_repository(conn, context, episode_id, source["repository_id"])
            existing = conn.execute(
                "SELECT id,source_id,space_id,embedding_name,content_hash,usage_origin FROM embedding_vectors WHERE workspace_id=%s AND source_id=%s AND embedding_name=%s AND space_id=%s",
                (context.workspace_id, source_id, embedding_name, space_id),
            ).fetchone()
            if existing is not None:
                return self._describe_vector(existing, source, space, view, profile)
        result, reservation_id = self._encode(
            context,
            episode_id,
            source["content"],
            "document",
            runtime_budget_ms,
            provider=provider,
            profile=profile,
        )
        with self.database.transaction(context) as conn:
            self._assert_provider(provider, profile)
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
                "INSERT INTO embedding_vectors(workspace_id,id,source_id,space_id,embedding_name,embedding,content_hash,reservation_id,usage_origin) VALUES (%s,%s,%s,%s,%s,%s::public.vector,%s,%s,%s) ON CONFLICT (workspace_id,source_id,embedding_name,space_id) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    source_id,
                    space_id,
                    embedding_name,
                    json.dumps(result.vectors[0]),
                    source["content_hash"],
                    reservation_id,
                    result.usage_origin,
                ),
            )
            row = conn.execute(
                "SELECT id,source_id,space_id,embedding_name,content_hash,usage_origin FROM embedding_vectors WHERE workspace_id=%s AND source_id=%s AND embedding_name=%s AND space_id=%s",
                (context.workspace_id, source_id, embedding_name, space_id),
            ).fetchone()
            return self._describe_vector(row, source, space, view, profile)

    def search(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        repository_id: UUID,
        space_id: UUID,
        query: str,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        limit: int = 5,
        runtime_budget_ms: int = 60_000,
        source_ids: tuple[UUID, ...] | None = None,
    ) -> list[dict[str, Any]]:
        validate_embedding_name(embedding_name)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("retrieval limit must be between 1 and 100")
        if source_ids is not None and (
            not isinstance(source_ids, tuple)
            or len(source_ids) > 100_000
            or any(not isinstance(identity, UUID) for identity in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            raise InvalidInputError("source filter requires at most 100000 unique UUIDs")
        provider = self.provider
        profile = provider.profile
        with self.database.transaction(context) as conn:
            space, view = self._view(conn, context, space_id, embedding_name, profile)
            self._episode_repository(conn, context, episode_id, repository_id)
            if (
                conn.execute(
                    "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, repository_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
        if source_ids == ():
            return []
        result, _ = self._encode(
            context,
            episode_id,
            query,
            "query",
            runtime_budget_ms,
            provider=provider,
            profile=profile,
        )
        with self.database.transaction(context) as conn:
            self._assert_provider(provider, profile)
            # No approximate index is used in this first integration. Rejoining
            # current heads prevents retired or superseded sources reappearing.
            rows = conn.execute(
                "SELECT s.id,s.source_key,s.artifact_id,s.content,s.metadata,s.content_hash AS source_content_hash,v.id AS vector_id,v.space_id,v.embedding_name,v.usage_origin,v.embedding OPERATOR(public.<=>) %s::public.vector AS distance FROM embedding_vectors v JOIN embedding_sources s ON (s.workspace_id,s.id)=(v.workspace_id,v.source_id) JOIN embedding_source_heads h ON (h.workspace_id,h.source_id)=(s.workspace_id,s.id) WHERE v.workspace_id=%s AND v.space_id=%s AND v.embedding_name=%s AND s.repository_id=%s AND h.active AND v.content_hash=s.content_hash AND (%s::uuid[] IS NULL OR s.id=ANY(%s::uuid[])) ORDER BY distance,s.id LIMIT %s",
                (
                    json.dumps(result.vectors[0]),
                    context.workspace_id,
                    space_id,
                    embedding_name,
                    repository_id,
                    None if source_ids is None else list(source_ids),
                    None if source_ids is None else list(source_ids),
                    limit,
                ),
            ).fetchall()
            return [self._describe_vector(row, row, space, view, profile) for row in rows]
