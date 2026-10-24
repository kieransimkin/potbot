"""Durable bounded upload slots on the existing artifact store, with host admission."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Iterator
from typing import Any

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.artifact_ranges import MAX_ARTIFACT_BYTES, MAX_LEASE_SECONDS, MAX_RANGE_BYTES
from potbot.tools.artifact_uploads import PAGE_SLOTS
from potbot.tools.execution_contracts import ExecutionPolicy, digest, fields, identifier


class ArtifactUploadService:
    def __init__(
        self, database: Any, artifacts: Any, actor: Any, *, policy: ExecutionPolicy
    ) -> None:
        self.db, self.artifacts, self.actor, self.policy = database, artifacts, actor, policy

    def _row(
        self, connection: Any, request: dict[str, Any], *, lock: bool = False
    ) -> dict[str, Any]:
        upload_id, expected = identifier(request["upload_id"]), digest(request["artifact_sha256"])
        if lock:
            # Immutable tables intentionally grant no UPDATE privilege. Serialize
            # cooperating writers without weakening that boundary; the subsequent
            # statement observes a fresh READ COMMITTED snapshot after this lock.
            connection.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                (f"potbot-upload:{self.actor.workspace_id}:{upload_id}",),
            ).fetchone()
        row = connection.execute(
            "SELECT s.*,floor(extract(epoch FROM s.expires_at))::bigint AS expires_at_epoch,"
            "s.expires_at>clock_timestamp() AS unexpired,"
            "(SELECT t.state FROM artifact_upload_terminals t WHERE t.workspace_id=s.workspace_id "
            "AND t.upload_id=s.id) AS terminal_state,"
            "(SELECT t.artifact_id FROM artifact_upload_terminals t WHERE t.workspace_id=s.workspace_id "
            "AND t.upload_id=s.id) AS committed_id FROM artifact_uploads s "
            "WHERE s.workspace_id=%s AND s.id=%s AND s.owner_principal_id=%s",
            (self.actor.workspace_id, upload_id, self.actor.principal_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Upload is unavailable in this actor scope.")
        result = dict(row)
        if (
            result["policy_hash"] != self.policy.fingerprint
            or result["artifact_sha256"] != expected
        ):
            raise StateMismatchError("Upload policy or pinned content revision changed.")
        return result

    @staticmethod
    def _live(row: dict[str, Any]) -> None:
        if row["terminal_state"] is not None or not row["unexpired"]:
            raise ScopeDeniedError("Upload is closed or expired.")

    @staticmethod
    def _count(row: dict[str, Any]) -> int:
        return (int(row["byte_length"]) + MAX_RANGE_BYTES - 1) // MAX_RANGE_BYTES

    @staticmethod
    def _pin(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "upload_id": str(row["id"]),
            "artifact_sha256": row["artifact_sha256"],
        }

    def open(self, request: object, *, authorize: Callable[[], None]) -> dict[str, Any]:
        value = fields(
            request,
            {
                "schema_version",
                "upload_id",
                "artifact_sha256",
                "byte_length",
                "media_type",
                "ttl_seconds",
            },
        )
        upload_id, expected = identifier(value["upload_id"]), digest(value["artifact_sha256"])
        size, ttl, mime = value["byte_length"], value["ttl_seconds"], value["media_type"]
        if (
            type(size) is not int
            or not 0 <= size <= MAX_ARTIFACT_BYTES
            or type(ttl) is not int
            or not 1 <= ttl <= MAX_LEASE_SECONDS
            or not isinstance(mime, str)
            or not 1 <= len(mime) <= 255
        ):
            raise InvalidInputError("Upload size, media type or lifetime exceeds its bound.")
        if getattr(self.artifacts.backend, "supports_chunk_uploads", False) is not True:
            raise CapabilityUnavailableError("Artifact backend has no qualified chunk publisher.")
        backend_limit = getattr(self.artifacts.backend, "max_bytes", None)
        if type(backend_limit) is not int or size > backend_limit:
            raise InvalidInputError("Upload exceeds the backend's configured artifact size limit.")
        with self.db.transaction(self.actor) as connection:
            authorize()
            connection.execute(
                "INSERT INTO artifact_uploads(workspace_id,id,owner_principal_id,artifact_sha256,"
                "byte_length,media_type,policy_hash,ttl_seconds,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s)) "
                "ON CONFLICT(workspace_id,id) DO NOTHING",
                (
                    self.actor.workspace_id,
                    upload_id,
                    self.actor.principal_id,
                    expected,
                    size,
                    mime,
                    self.policy.fingerprint,
                    ttl,
                    ttl,
                ),
            )
            row = self._row(connection, value, lock=True)
            self._live(row)
            if (row["byte_length"], row["media_type"], row["ttl_seconds"]) != (size, mime, ttl):
                raise StateMismatchError("Upload identity was reused for different parameters.")
        return {
            **self._pin(row),
            "byte_length": size,
            "expires_at_epoch": row["expires_at_epoch"],
            "chunk_bytes": MAX_RANGE_BYTES,
            "expected_chunks": self._count(row),
            "state": "open",
        }

    def chunk(self, request: object, *, authorize: Callable[[], None]) -> dict[str, Any]:
        value = fields(
            request,
            {
                "schema_version",
                "upload_id",
                "artifact_sha256",
                "offset",
                "chunk_sha256",
                "data_base64",
            },
        )
        offset, encoded = value["offset"], value["data_base64"]
        expected = digest(value["chunk_sha256"])
        if (
            type(offset) is not int
            or offset < 0
            or offset % MAX_RANGE_BYTES
            or not isinstance(encoded, str)
            or len(encoded) > 4 * ((MAX_RANGE_BYTES + 2) // 3)
        ):
            raise InvalidInputError("Upload chunk offset or encoded size is invalid.")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise InvalidInputError("Upload chunk encoding is invalid.") from error
        if not 1 <= len(raw) <= MAX_RANGE_BYTES or hashlib.sha256(raw).hexdigest() != expected:
            raise InvalidInputError("Upload chunk digest or length differs.")
        with self.db.transaction(self.actor) as connection:
            row = self._row(connection, value, lock=True)
            self._live(row)
            if offset >= row["byte_length"] or len(raw) != min(
                MAX_RANGE_BYTES, row["byte_length"] - offset
            ):
                raise InvalidInputError("Upload chunk does not fill its exact declared slot.")
            index = offset // MAX_RANGE_BYTES
            prior = connection.execute(
                "SELECT * FROM artifact_upload_chunks WHERE workspace_id=%s AND upload_id=%s AND chunk_index=%s",
                (self.actor.workspace_id, row["id"], index),
            ).fetchone()
            if prior and (prior["chunk_sha256"] != expected or prior["byte_length"] != len(raw)):
                raise StateMismatchError("Upload slot already contains different bytes.")
            authorize()
            artifact = self.artifacts.put_in_transaction(connection, self.actor, raw)
            self._live(self._row(connection, value))
            authorize()
            if not prior:
                connection.execute(
                    "INSERT INTO artifact_upload_chunks(workspace_id,upload_id,owner_principal_id,"
                    "chunk_index,artifact_id,chunk_sha256,byte_length) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.actor.workspace_id,
                        row["id"],
                        self.actor.principal_id,
                        index,
                        artifact["id"],
                        expected,
                        len(raw),
                    ),
                )
        return {
            **self._pin(row),
            "offset": offset,
            "chunk_sha256": expected,
            "stored_bytes": len(raw),
            "duplicate": bool(prior),
        }

    def inspect(self, request: object, *, authorize: Callable[[], None]) -> dict[str, Any]:
        value = fields(request, {"schema_version", "upload_id", "artifact_sha256", "cursor"})
        with self.db.transaction(self.actor) as connection:
            row = self._row(connection, value, lock=True)
            count, cursor = self._count(row), value["cursor"]
            if type(cursor) is not int or not 0 <= cursor <= count:
                raise InvalidInputError("Upload inspection cursor is outside the pinned source.")
            end = min(cursor + PAGE_SLOTS, count)
            present = connection.execute(
                "SELECT chunk_index FROM artifact_upload_chunks WHERE workspace_id=%s AND upload_id=%s "
                "AND chunk_index>=%s AND chunk_index<%s ORDER BY chunk_index",
                (self.actor.workspace_id, row["id"], cursor, end),
            ).fetchall()
            stats = connection.execute(
                "SELECT count(*) AS chunks,coalesce(sum(byte_length),0) AS bytes FROM artifact_upload_chunks "
                "WHERE workspace_id=%s AND upload_id=%s",
                (self.actor.workspace_id, row["id"]),
            ).fetchone()
            authorize()
        indices = {r["chunk_index"] for r in present}
        return {
            **self._pin(row),
            "byte_length": row["byte_length"],
            "state": row["terminal_state"] or ("open" if row["unexpired"] else "expired"),
            "expected_chunks": count,
            "received_chunks": stats["chunks"],
            "received_bytes": stats["bytes"],
            "missing_offsets": [
                i * MAX_RANGE_BYTES for i in range(cursor, end) if i not in indices
            ],
            "next_cursor": end if end < count else None,
        }

    def _committed(self, connection: Any, row: dict[str, Any]) -> dict[str, Any]:
        artifact = self.artifacts._metadata(connection, self.actor, row["committed_id"])
        if (
            artifact["digest"] != row["artifact_sha256"]
            or artifact["byte_length"] != row["byte_length"]
        ):
            raise StateMismatchError("Committed artifact differs from the upload revision.")
        return {
            **self._pin(row),
            "byte_length": row["byte_length"],
            "state": "committed",
            "artifact_id": str(artifact["id"]),
            "media_type": artifact["media_type"],
        }

    def commit(self, request: object, *, authorize: Callable[[], None]) -> dict[str, Any]:
        value = fields(request, {"schema_version", "upload_id", "artifact_sha256"})
        with self.db.transaction(self.actor) as connection:
            row = self._row(connection, value, lock=True)
            authorize()
            if row["terminal_state"] == "committed":
                self.artifacts.read_range(
                    self.actor,
                    row["committed_id"],
                    expected_sha256=row["artifact_sha256"],
                    expected_byte_length=row["byte_length"],
                    offset=0,
                    length=1,
                )
                authorize()
                return self._committed(connection, row)
            self._live(row)
            chunks = connection.execute(
                "SELECT * FROM artifact_upload_chunks WHERE workspace_id=%s AND upload_id=%s ORDER BY chunk_index",
                (self.actor.workspace_id, row["id"]),
            ).fetchall()
            if len(chunks) != self._count(row):
                raise StateMismatchError(
                    "Upload is incomplete; inspect missing slots before committing."
                )

            def check() -> None:
                self._live(self._row(connection, value))
                authorize()

            def pieces() -> Iterator[bytes]:
                for index, chunk in enumerate(chunks):
                    metadata = self.artifacts._metadata(
                        connection, self.actor, chunk["artifact_id"]
                    )
                    expected_length = min(
                        MAX_RANGE_BYTES, row["byte_length"] - index * MAX_RANGE_BYTES
                    )
                    if (
                        chunk["chunk_index"] != index
                        or chunk["byte_length"] != expected_length
                        or metadata["byte_length"] != expected_length
                        or metadata["digest"] != chunk["chunk_sha256"]
                    ):
                        raise StateMismatchError(
                            "Upload chunk metadata differs from its bounded slot."
                        )
                    data = self.artifacts.read_in_transaction(
                        connection, self.actor, chunk["artifact_id"]
                    )
                    if (
                        len(data) != expected_length
                        or hashlib.sha256(data).hexdigest() != chunk["chunk_sha256"]
                    ):
                        raise StateMismatchError("Upload chunk differs from its recorded slot.")
                    check()
                    yield data

            artifact = self.artifacts.put_chunks_in_transaction(
                connection,
                self.actor,
                pieces(),
                expected_digest=row["artifact_sha256"],
                byte_length=row["byte_length"],
                media_type=row["media_type"],
                before_publish=check,
            )
            check()
            connection.execute(
                "INSERT INTO artifact_upload_terminals(workspace_id,upload_id,owner_principal_id,state,artifact_id) "
                "VALUES(%s,%s,%s,'committed',%s)",
                (self.actor.workspace_id, row["id"], self.actor.principal_id, artifact["id"]),
            )
            return self._committed(connection, self._row(connection, value))

    def abort(self, request: object, *, authorize: Callable[[], None]) -> dict[str, Any]:
        value = fields(request, {"schema_version", "upload_id", "artifact_sha256"})
        with self.db.transaction(self.actor) as connection:
            row = self._row(connection, value, lock=True)
            if row["terminal_state"] == "committed":
                raise StateMismatchError(
                    "A committed artifact cannot be aborted or deleted by this tool."
                )
            authorize()
            connection.execute(
                "INSERT INTO artifact_upload_terminals(workspace_id,upload_id,owner_principal_id,state) "
                "VALUES(%s,%s,%s,'aborted') ON CONFLICT(workspace_id,upload_id) DO NOTHING",
                (self.actor.workspace_id, row["id"], self.actor.principal_id),
            )
        return {**self._pin(row), "state": "aborted", "staged_bytes_deleted": False}
