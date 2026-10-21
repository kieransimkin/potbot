"""Transaction-aware source publication shared by embeddings and SYN imports."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactStore


def register_source_in_transaction(
    conn: Any,
    context: WorkspaceContext,
    artifacts: ArtifactStore,
    repository_id: UUID,
    source_key: str,
    text: str,
    *,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish a source using an already authenticated workspace transaction.

    Callers must obtain conn from Database.transaction(context). SQL metadata and
    current-head changes commit with the surrounding operation; rollback can leave
    unreferenced immutable artifact bytes, as in ArtifactStore's existing contract.
    """
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
    if (
        conn.execute(
            "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, repository_id),
        ).fetchone()
        is None
    ):
        raise ScopeDeniedError("record is unavailable")
    artifact = artifacts.put_in_transaction(
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
