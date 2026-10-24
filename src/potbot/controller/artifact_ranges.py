"""Workspace-scoped bounded artifact reads; the host supplies independent admission."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.artifact_ranges import (
    MAX_ARTIFACT_BYTES,
    MAX_LEASE_SECONDS,
    MAX_RANGE_BYTES,
    READ_BLOCK_BYTES,
)
from potbot.tools.execution_contracts import ExecutionPolicy, digest, fields, identifier


class ArtifactRangeService:
    def __init__(
        self, database: Any, artifacts: Any, actor: Any, *, policy: ExecutionPolicy
    ) -> None:
        self.db, self.artifacts, self.actor, self.policy = database, artifacts, actor, policy

    def _lease(self, request: dict[str, Any], *, live: bool = True) -> dict[str, Any]:
        lease_id = identifier(request["lease_id"])
        expected = digest(request["artifact_sha256"])
        with self.db.transaction(self.actor) as connection:
            row = connection.execute(
                "SELECT l.*,floor(extract(epoch FROM l.expires_at))::bigint AS expires_at_epoch,"
                "l.expires_at>clock_timestamp() AS unexpired,"
                "EXISTS(SELECT 1 FROM artifact_read_revocations r WHERE r.workspace_id=l.workspace_id "
                "AND r.lease_id=l.id) AS revoked FROM artifact_read_leases l "
                "WHERE l.workspace_id=%s AND l.id=%s AND l.owner_principal_id=%s",
                (self.actor.workspace_id, lease_id, self.actor.principal_id),
            ).fetchone()
        if row is None or (live and (not row["unexpired"] or row["revoked"])):
            raise ScopeDeniedError("Artifact read lease is unavailable in this actor scope.")
        value = dict(row)
        if value["policy_hash"] != self.policy.fingerprint or value["artifact_sha256"] != expected:
            raise StateMismatchError("Artifact lease policy or pinned revision changed.")
        return value

    def open(self, request: object) -> dict[str, Any]:
        value = fields(
            request,
            {
                "schema_version",
                "lease_id",
                "artifact_id",
                "artifact_sha256",
                "byte_length",
                "ttl_seconds",
            },
        )
        lease_id, artifact_id = identifier(value["lease_id"]), identifier(value["artifact_id"])
        expected = digest(value["artifact_sha256"])
        size, ttl = value["byte_length"], value["ttl_seconds"]
        if (
            type(size) is not int
            or not 0 <= size <= MAX_ARTIFACT_BYTES
            or type(ttl) is not int
            or not 1 <= ttl <= MAX_LEASE_SECONDS
        ):
            raise InvalidInputError("Artifact read size or lease lifetime exceeds its bound.")
        metadata = self.artifacts.metadata(self.actor, artifact_id)
        if metadata["digest"] != expected or metadata["byte_length"] != size:
            raise StateMismatchError("Artifact differs from the independently pinned revision.")
        with self.db.transaction(self.actor) as connection:
            connection.execute(
                "INSERT INTO artifact_read_leases(workspace_id,id,owner_principal_id,artifact_id,"
                "artifact_sha256,byte_length,policy_hash,ttl_seconds,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s)) "
                "ON CONFLICT(workspace_id,id) DO NOTHING",
                (
                    self.actor.workspace_id,
                    lease_id,
                    self.actor.principal_id,
                    artifact_id,
                    expected,
                    size,
                    self.policy.fingerprint,
                    ttl,
                    ttl,
                ),
            )
        lease = self._lease(value)
        if (
            lease["artifact_id"] != artifact_id
            or lease["byte_length"] != size
            or lease["ttl_seconds"] != ttl
        ):
            raise StateMismatchError("Read lease identity was reused for different parameters.")
        return {
            "schema_version": 1,
            "lease_id": str(lease_id),
            "artifact_id": str(artifact_id),
            "artifact_sha256": expected,
            "byte_length": size,
            "media_type": metadata["media_type"],
            "expires_at_epoch": lease["expires_at_epoch"],
            "max_range_bytes": MAX_RANGE_BYTES,
            "read_block_bytes": READ_BLOCK_BYTES,
            "integrity": "checked_before_each_range_returns",
        }

    def read(self, request: object) -> dict[str, Any]:
        value = fields(
            request, {"schema_version", "lease_id", "artifact_sha256", "offset", "length"}
        )
        lease = self._lease(value)
        offset, length = value["offset"], value["length"]
        if (
            type(offset) is not int
            or not 0 <= offset <= lease["byte_length"]
            or type(length) is not int
            or not 1 <= length <= MAX_RANGE_BYTES
        ):
            raise InvalidInputError("Artifact read range exceeds its bound.")
        result = self.artifacts.read_range(
            self.actor,
            lease["artifact_id"],
            expected_sha256=lease["artifact_sha256"],
            expected_byte_length=lease["byte_length"],
            offset=offset,
            length=length,
        )
        # Recheck revocation/expiry after I/O, before any private range leaves.
        self._lease(value)
        return {
            "schema_version": 1,
            "lease_id": str(lease["id"]),
            "artifact_id": str(lease["artifact_id"]),
            "artifact_sha256": lease["artifact_sha256"],
            "byte_length": lease["byte_length"],
            "offset": offset,
            "requested_length": length,
            "returned_length": len(result.data),
            "data_base64": base64.b64encode(result.data).decode("ascii"),
            "range_sha256": hashlib.sha256(result.data).hexdigest(),
            "verified_byte_length": result.verified_byte_length,
            "read_calls": result.read_calls,
            "maximum_read_request_bytes": READ_BLOCK_BYTES,
            "range_ends_at_eof": offset + len(result.data) == lease["byte_length"],
            "integrity": "full_sha256_verified",
        }

    def close(self, request: object) -> dict[str, Any]:
        value = fields(request, {"schema_version", "lease_id", "artifact_sha256"})
        lease = self._lease(value, live=False)
        with self.db.transaction(self.actor) as connection:
            connection.execute(
                "INSERT INTO artifact_read_revocations(workspace_id,lease_id,owner_principal_id) "
                "VALUES(%s,%s,%s) ON CONFLICT(workspace_id,lease_id) DO NOTHING",
                (self.actor.workspace_id, lease["id"], self.actor.principal_id),
            )
        return {
            "schema_version": 1,
            "lease_id": str(lease["id"]),
            "artifact_sha256": lease["artifact_sha256"],
            "state": "closed",
            "artifact_deleted": False,
        }
