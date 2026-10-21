"""Scoped immutable SYN imports, exact inventories and named-vector integration.

The bare URL deliberately carries no workspace, network host or revision. Every
lookup therefore selects a sealed snapshot explicitly. Native class assertions
remain source reports; importing one does not validate or execute its referent.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.embeddings import DEFAULT_EMBEDDING_NAME
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import (
    MAX_SYN_SNAPSHOT_BYTES,
    SynnyRef,
    SynRef,
    SynSnapshot,
    parse_ref,
    parse_type_ref,
)
from potbot.persistence.artifacts import ArtifactIntegrityError, ArtifactStore
from potbot.persistence.database import Database
from potbot.persistence.embedding_sources import register_source_in_transaction

from .catalog import (
    catalog_fingerprint,
    catalog_provenance,
    data_type_ref,
    merge_types,
    type_ancestors,
)

if TYPE_CHECKING:
    from potbot.memory.embeddings import EmbeddingService


def _scope(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        "workspace_id": str(snapshot["workspace_id"]),
        "namespace_id": str(snapshot["namespace_id"]),
        "namespace": snapshot["namespace"],
        "snapshot_id": str(snapshot["id"]),
        "repository_id": str(snapshot["repository_id"]),
    }


def _snapshot_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": _scope(snapshot),
        "snapshot": {
            key: str(snapshot[key])
            for key in (
                "content_hash",
                "source_hash",
                "catalog_hash",
                "source_label",
                "catalog_mode",
                "artifact_id",
            )
        },
        "type_count": snapshot["type_count"],
        "object_count": snapshot["object_count"],
        "coverage": "imported_snapshot_only",
    }


def _item(row: dict[str, Any]) -> dict[str, Any]:
    kind = row["kind"]
    primary = "dt:syn.type_definition" if kind == "type" else "dt:syn.object_record"
    return {
        "ref": row["ref"],
        "kind": kind,
        "semantic_type_ref": row["type_ref"],
        "primary_data_type": primary,
        "primary_data_type_ref": str(data_type_ref(primary)),
        "source_id": str(row["source_id"]),
        "artifact_id": str(row["artifact_id"]),
    }


class SynService:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        if database != artifacts.db:
            raise InvalidInputError("SYN and artifact services must use the same database")
        self.database = database
        self.artifacts = artifacts

    @staticmethod
    def _snapshot(conn: Any, context: WorkspaceContext, snapshot_id: UUID) -> dict[str, Any]:
        row = conn.execute(
            """SELECT s.*,n.name AS namespace FROM syn_snapshots s
               JOIN syn_snapshot_seals p
                 ON (p.workspace_id,p.snapshot_id)=(s.workspace_id,s.id)
               JOIN syn_namespaces n
                 ON (n.workspace_id,n.id)=(s.workspace_id,s.namespace_id)
               WHERE s.workspace_id=%s AND s.id=%s""",
            (context.workspace_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("SYN snapshot is unavailable")
        return dict(row)

    def import_snapshot(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        namespace: str,
        snapshot: SynSnapshot,
    ) -> dict[str, Any]:
        """Publish one complete, sealed graph, or reuse its identical effective bytes.

        Database metadata is atomic. A rollback may leave unreferenced immutable
        bytes in the artifact backend, following the existing publication contract.
        """
        from psycopg.types.json import Jsonb

        if (
            not isinstance(namespace, str)
            or not namespace.strip()
            or len(namespace) > 200
            or "\x00" in namespace
        ):
            raise InvalidInputError(
                "SYN namespace requires a nonblank name of at most 200 characters"
            )
        try:
            namespace.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidInputError("SYN namespace must be valid UTF-8") from error
        if not isinstance(snapshot, SynSnapshot):
            raise InvalidInputError("import requires a checked SYN snapshot")
        types = merge_types(snapshot)
        ancestors = type_ancestors(types)
        type_refs = {record.ref.slug: record.ref for record in types}
        for obj in snapshot.objects:
            if obj.type_ref.slug not in type_refs:
                raise InvalidInputError(f"object refers to a missing SYN type: {obj.type_ref}")
        catalog_hash = catalog_fingerprint(types)
        effective = canonical_json(
            {
                "schema_version": 1,
                "source": snapshot.to_dict(),
                "catalog": {
                    "fingerprint": catalog_hash,
                    "provenance": catalog_provenance(),
                    "types": [record.to_dict() for record in types],
                },
            }
        ).encode("utf-8")
        if len(effective) > MAX_SYN_SNAPSHOT_BYTES:
            raise InvalidInputError("effective SYN snapshot artifact exceeds the 64 MiB limit")
        digest = hashlib.sha256(effective).hexdigest()
        with self.database.transaction(context) as conn:
            if (
                conn.execute(
                    "SELECT id FROM repositories WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, repository_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
            conn.execute(
                """INSERT INTO syn_namespaces(workspace_id,id,name,repository_id)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id,name) DO NOTHING""",
                (context.workspace_id, uuid4(), namespace, repository_id),
            )
            binding = conn.execute(
                "SELECT * FROM syn_namespaces WHERE workspace_id=%s AND name=%s",
                (context.workspace_id, namespace),
            ).fetchone()
            if binding["repository_id"] != repository_id:
                raise RevisionConflictError("SYN namespace is already bound to another repository")
            prior = conn.execute(
                """SELECT id FROM syn_snapshots
                   WHERE workspace_id=%s AND namespace_id=%s AND content_hash=%s""",
                (context.workspace_id, binding["id"], digest),
            ).fetchone()
            if prior is not None:
                return _snapshot_result(self._snapshot(conn, context, prior["id"]))
            artifact = self.artifacts.put_in_transaction(
                conn, context, effective, "application/json"
            )
            inserted = conn.execute(
                """INSERT INTO syn_snapshots(
                   workspace_id,id,namespace_id,repository_id,content_hash,source_hash,
                   catalog_hash,source_label,catalog_mode,artifact_id,type_count,object_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,namespace_id,content_hash) DO NOTHING RETURNING id""",
                (
                    context.workspace_id,
                    uuid4(),
                    binding["id"],
                    repository_id,
                    digest,
                    snapshot.fingerprint,
                    catalog_hash,
                    snapshot.source_label,
                    snapshot.catalog_mode,
                    artifact["id"],
                    len(types),
                    len(snapshot.objects),
                ),
            ).fetchone()
            if inserted is None:
                # A concurrent importer won the unique effective-content key.
                prior = conn.execute(
                    """SELECT id FROM syn_snapshots
                       WHERE workspace_id=%s AND namespace_id=%s AND content_hash=%s""",
                    (context.workspace_id, binding["id"], digest),
                ).fetchone()
                return _snapshot_result(self._snapshot(conn, context, prior["id"]))
            identity = inserted["id"]

            def source(ref: SynRef | SynnyRef, text: str) -> dict[str, Any]:
                return register_source_in_transaction(
                    conn,
                    context,
                    self.artifacts,
                    repository_id,
                    f"syn:{binding['id']}:{identity}:{ref}",
                    text,
                    metadata={
                        "source_url": str(ref),
                        "revision": digest,
                        "kind": "syn_type" if ref.kind == "type" else "syn_object",
                    },
                )

            for record in types:
                registered = source(record.ref, canonical_json(record.to_dict()))
                conn.execute(
                    """INSERT INTO syn_types(workspace_id,snapshot_id,repository_id,ref,
                       definition,source_id,artifact_id) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        context.workspace_id,
                        identity,
                        repository_id,
                        str(record.ref),
                        Jsonb(record.to_dict()),
                        registered["id"],
                        registered["artifact_id"],
                    ),
                )
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO syn_type_parents(workspace_id,snapshot_id,child_ref,parent_ref)
                       VALUES (%s,%s,%s,%s)""",
                    (
                        (context.workspace_id, identity, str(record.ref), str(parent))
                        for record in types
                        for parent in record.parents
                    ),
                )
                cursor.executemany(
                    """INSERT INTO syn_type_ancestry(
                       workspace_id,snapshot_id,descendant_ref,ancestor_ref) VALUES (%s,%s,%s,%s)""",
                    (
                        (context.workspace_id, identity, descendant, ancestor)
                        for descendant, inherited in ancestors.items()
                        for ancestor in inherited
                    ),
                )
            for obj in snapshot.objects:
                registered = source(obj.ref, obj.payload_json)
                conn.execute(
                    """INSERT INTO syn_objects(workspace_id,snapshot_id,repository_id,ref,kind,
                       type_ref,source_id,artifact_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        context.workspace_id,
                        identity,
                        repository_id,
                        str(obj.ref),
                        obj.ref.kind,
                        str(type_refs[obj.type_ref.slug]),
                        registered["id"],
                        registered["artifact_id"],
                    ),
                )
            conn.execute(
                "INSERT INTO syn_snapshot_seals(workspace_id,snapshot_id) VALUES (%s,%s)",
                (context.workspace_id, identity),
            )
            return _snapshot_result(self._snapshot(conn, context, identity))

    @staticmethod
    def _record(
        conn: Any, context: WorkspaceContext, snapshot_id: UUID, ref: SynRef | SynnyRef
    ) -> dict[str, Any]:
        table = "syn_types" if ref.kind == "type" else "syn_objects"
        fields = (
            "'type'::text AS kind,NULL::text AS type_ref"
            if ref.kind == "type"
            else "o.kind,o.type_ref"
        )
        predicate = "split_part(o.ref,'/',4)=%s" if ref.kind == "type" else "o.ref=%s"
        row = conn.execute(
            f"""SELECT o.ref,o.source_id,o.artifact_id,{fields},
                       s.content,s.content_hash,s.metadata
                FROM {table} o JOIN embedding_sources s
                  ON (s.workspace_id,s.id)=(o.workspace_id,o.source_id)
                WHERE o.workspace_id=%s AND o.snapshot_id=%s AND {predicate}""",
            (context.workspace_id, snapshot_id, ref.slug if ref.kind == "type" else str(ref)),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("SYN reference is unavailable in this snapshot")
        return dict(row)

    def resolve(self, context: WorkspaceContext, snapshot_id: UUID, ref: str) -> dict[str, Any]:
        reference = parse_ref(ref)
        with self.database.transaction(context) as conn:
            snapshot = self._snapshot(conn, context, snapshot_id)
            row = self._record(conn, context, snapshot_id, reference)
            raw = self.artifacts.read_in_transaction(conn, context, row["artifact_id"])
            if (
                raw != row["content"].encode("utf-8")
                or content_hash(
                    {
                        "text": row["content"],
                        "metadata": row["metadata"],
                    }
                )
                != row["content_hash"]
            ):
                raise ArtifactIntegrityError("SYN public record provenance failed validation")
        manifest = self.artifacts.embedding_manifest(context, row["artifact_id"])
        # Identical public bytes can belong to other namespaces or snapshots.
        # Artifact-level deduplication must not broaden this resolved identity.
        groups = {
            name: selected
            for name, references in manifest["named_embeddings"].items()
            if (
                selected := [
                    value for value in references if value["source_id"] == str(row["source_id"])
                ]
            )
        }
        return {
            **_snapshot_result(snapshot),
            **_item(row),
            "requested_ref": str(reference),
            "source_content_hash": row["content_hash"],
            "artifact_digest": manifest["artifact_digest"],
            "byte_length": manifest["byte_length"],
            "media_type": manifest["media_type"],
            "payload_json": row["content"],
            "semantic_assertion_origin": "source_report" if reference.kind != "type" else None,
            "named_embeddings": groups,
        }

    def catalog(
        self,
        context: WorkspaceContext,
        snapshot_id: UUID,
        *,
        type_ref: str | None = None,
        summary: bool = False,
    ) -> dict[str, Any]:
        """Return every pinned type declaration, separately from instance membership."""
        if type(summary) is not bool:
            raise InvalidInputError("catalog summary must be boolean")
        selected = None
        if type_ref is not None:
            selected = (
                data_type_ref(type_ref) if type_ref.startswith("dt:") else parse_type_ref(type_ref)
            )
        with self.database.transaction(context) as conn:
            snapshot = self._snapshot(conn, context, snapshot_id)
            rows = conn.execute(
                """SELECT definition FROM syn_types
                   WHERE workspace_id=%s AND snapshot_id=%s
                     AND (%s::text IS NULL OR split_part(ref,'/',4)=%s)
                   ORDER BY ref COLLATE "C"
                """,
                (
                    context.workspace_id,
                    snapshot_id,
                    selected.slug if selected is not None else None,
                    selected.slug if selected is not None else None,
                ),
            ).fetchall()
            if selected is not None and not rows:
                raise InvalidInputError("type is absent from the selected snapshot catalog")
            result = {**_snapshot_result(snapshot), "selected_count": len(rows)}
            if not summary:
                result["types"] = [row["definition"] for row in rows]
            return result

    @staticmethod
    def _filter(
        conn: Any,
        context: WorkspaceContext,
        snapshot_id: UUID,
        type_ref: str | None,
        primary_data_type: str | None,
        include_subtypes: bool,
    ) -> tuple[str, list[Any], dict[str, Any]]:
        if type(include_subtypes) is not bool:
            raise InvalidInputError("include_subtypes must be boolean")
        clauses = ["o.workspace_id=%s", "o.snapshot_id=%s"]
        values: list[Any] = [context.workspace_id, snapshot_id]
        native = parse_type_ref(type_ref) if type_ref is not None else None
        primary = data_type_ref(primary_data_type) if primary_data_type is not None else None
        if native is not None:
            resolved = conn.execute(
                """SELECT ref FROM syn_types WHERE workspace_id=%s AND snapshot_id=%s
                   AND split_part(ref,'/',4)=%s""",
                (context.workspace_id, snapshot_id, native.slug),
            ).fetchone()
            if resolved is None:
                raise InvalidInputError("type filter is absent from the selected snapshot")
            native = parse_type_ref(resolved["ref"])
            if include_subtypes:
                clauses.append("""EXISTS (SELECT 1 FROM syn_type_ancestry a
                    WHERE (a.workspace_id,a.snapshot_id,a.descendant_ref)=
                          (o.workspace_id,o.snapshot_id,o.type_ref) AND a.ancestor_ref=%s)""")
            else:
                clauses.append("o.type_ref=%s")
            values.append(str(native))
        if primary is not None:
            record_type = str(data_type_ref("dt:syn.object_record"))
            if include_subtypes:
                clauses.append("""EXISTS (SELECT 1 FROM syn_type_ancestry a
                    WHERE (a.workspace_id,a.snapshot_id)=(o.workspace_id,o.snapshot_id)
                    AND a.descendant_ref=%s AND a.ancestor_ref=%s)""")
                values.extend((record_type, str(primary)))
            else:
                clauses.append("%s::text=%s::text")
                values.extend((record_type, str(primary)))
        return (
            " AND ".join(clauses),
            values,
            {
                "type_ref": str(native) if native is not None else None,
                "primary_data_type_ref": str(primary) if primary is not None else None,
                "include_subtypes": include_subtypes,
            },
        )

    def list_objects(
        self,
        context: WorkspaceContext,
        snapshot_id: UUID,
        *,
        type_ref: str | None = None,
        primary_data_type: str | None = None,
        include_subtypes: bool = False,
        page_size: int = 100,
        cursor: UUID | None = None,
    ) -> dict[str, Any]:
        """Enumerate every matching instance in fixed bytewise reference order.

        Opaque cursors bind the principal, snapshot, filters and page size. A new
        snapshot, retired embedding or duplicate ancestry path cannot change a page.
        """
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise InvalidInputError("SYN inventory page size must be between 1 and 1000")
        if cursor is not None and not isinstance(cursor, UUID):
            raise InvalidInputError("SYN inventory cursor must be an opaque UUID")
        with self.database.transaction(context) as conn:
            snapshot = self._snapshot(conn, context, snapshot_id)
            where, values, query = self._filter(
                conn,
                context,
                snapshot_id,
                type_ref,
                primary_data_type,
                include_subtypes,
            )
            query_hash = content_hash({"query": query, "page_size": page_size})
            last_ref = None
            delivered = 0
            if cursor is not None:
                state = conn.execute(
                    """SELECT * FROM syn_inventory_cursors WHERE workspace_id=%s AND id=%s
                       AND principal_id=%s AND snapshot_id=%s AND query_hash=%s AND page_size=%s""",
                    (
                        context.workspace_id,
                        cursor,
                        context.principal_id,
                        snapshot_id,
                        query_hash,
                        page_size,
                    ),
                ).fetchone()
                if state is None:
                    raise ScopeDeniedError("SYN inventory cursor is unavailable for this query")
                last_ref, delivered, total = (
                    state["last_ref"],
                    state["delivered_count"],
                    state["total_count"],
                )
            else:
                total = conn.execute(
                    f"SELECT count(*) AS n FROM syn_objects o WHERE {where}",
                    values,
                ).fetchone()["n"]
            rows = conn.execute(
                f"""SELECT o.ref,o.kind,o.type_ref,o.source_id,o.artifact_id FROM syn_objects o
                    WHERE {where} AND (%s::text IS NULL OR o.ref>%s COLLATE "C")
                    ORDER BY o.ref COLLATE "C" LIMIT %s""",
                (*values, last_ref, last_ref, page_size + 1),
            ).fetchall()
            page = rows[:page_size]
            delivered += len(page)
            next_cursor = None
            if len(rows) > page_size:
                next_cursor = uuid4()
                conn.execute(
                    """INSERT INTO syn_inventory_cursors(workspace_id,id,snapshot_id,principal_id,
                       query_hash,last_ref,delivered_count,total_count,page_size)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        context.workspace_id,
                        next_cursor,
                        snapshot_id,
                        context.principal_id,
                        query_hash,
                        page[-1]["ref"],
                        delivered,
                        total,
                        page_size,
                    ),
                )
            return {
                **_snapshot_result(snapshot),
                "query": query,
                "order": "ref_bytewise_ascending",
                "items": [_item(dict(row)) for row in page],
                "page_size": page_size,
                "total_count": total,
                "delivered_count": delivered,
                "next_cursor": str(next_cursor) if next_cursor else None,
                "complete": delivered == total,
            }

    def _check_embeddings(self, embeddings: EmbeddingService) -> None:
        if embeddings.database != self.database or embeddings.artifacts.db != self.database:
            raise InvalidInputError("SYN and embedding services must use the same database")

    def index(
        self,
        context: WorkspaceContext,
        snapshot_id: UUID,
        ref: str,
        embeddings: EmbeddingService,
        episode_id: UUID,
        space_id: UUID,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        runtime_budget_ms: int = 60_000,
    ) -> dict[str, Any]:
        self._check_embeddings(embeddings)
        reference = parse_ref(ref)
        with self.database.transaction(context) as conn:
            snapshot = self._snapshot(conn, context, snapshot_id)
            row = self._record(conn, context, snapshot_id, reference)
        vector = embeddings.index_source(
            context,
            episode_id,
            row["source_id"],
            space_id,
            embedding_name=embedding_name,
            runtime_budget_ms=runtime_budget_ms,
        )
        return {**_snapshot_result(snapshot), **_item(row), "embedding": vector}

    def search(
        self,
        context: WorkspaceContext,
        snapshot_id: UUID,
        query: str,
        embeddings: EmbeddingService,
        episode_id: UUID,
        space_id: UUID,
        *,
        embedding_name: str = DEFAULT_EMBEDDING_NAME,
        limit: int = 5,
        runtime_budget_ms: int = 60_000,
        type_ref: str | None = None,
        primary_data_type: str | None = None,
        include_subtypes: bool = False,
        record_kind: str = "objects",
    ) -> dict[str, Any]:
        self._check_embeddings(embeddings)
        if record_kind not in {"objects", "types"}:
            raise InvalidInputError("SYN search record kind must be objects or types")
        if not isinstance(query, str) or not query.strip():
            raise InvalidInputError("SYN search requires a nonempty query")
        with self.database.transaction(context) as conn:
            snapshot = self._snapshot(conn, context, snapshot_id)
            if record_kind == "types":
                if type_ref is not None or primary_data_type is not None or include_subtypes:
                    raise InvalidInputError(
                        "instance type filters do not apply to type definitions"
                    )
                rows = conn.execute(
                    """SELECT ref,'type'::text AS kind,NULL::text AS type_ref,source_id,artifact_id
                       FROM syn_types WHERE workspace_id=%s AND snapshot_id=%s ORDER BY ref COLLATE "C"
                    """,
                    (context.workspace_id, snapshot_id),
                ).fetchall()
                filters: dict[str, Any] = {
                    "type_ref": None,
                    "primary_data_type_ref": None,
                    "include_subtypes": False,
                }
            else:
                where, values, filters = self._filter(
                    conn,
                    context,
                    snapshot_id,
                    type_ref,
                    primary_data_type,
                    include_subtypes,
                )
                rows = conn.execute(
                    f"""SELECT o.ref,o.kind,o.type_ref,o.source_id,o.artifact_id FROM syn_objects o
                        WHERE {where} ORDER BY o.ref COLLATE "C"
                    """,
                    values,
                ).fetchall()
        candidates = {row["source_id"]: dict(row) for row in rows}
        results = embeddings.search(
            context,
            episode_id,
            snapshot["repository_id"],
            space_id,
            query,
            embedding_name=embedding_name,
            limit=limit,
            runtime_budget_ms=runtime_budget_ms,
            source_ids=tuple(candidates),
        )
        return {
            **_snapshot_result(snapshot),
            "query": query,
            "filters": filters,
            "record_kind": record_kind,
            "candidate_count": len(candidates),
            "embedding_name": embedding_name,
            "space_id": str(space_id),
            "items": [{**_item(candidates[row["id"]]), "embedding": row} for row in results],
            "exhaustive": False,
        }
