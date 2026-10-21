"""Durable repository states, owned candidates and expiring mutation leases.

Lease checks fence publication. They cannot revoke an OS process's file handles;
an expired or uncertain candidate remains unavailable until explicit reconciliation.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database
from potbot.tools.repository_snapshot import (
    RepositorySnapshot,
    SnapshotProfile,
    capture_candidate,
    capture_snapshot,
    materialize_snapshot,
    workspace_fingerprint,
)

__all__ = [
    "CandidateWorkspaceManager",
    "Lease",
    "RepositorySnapshot",
    "RepositoryStateStore",
    "SnapshotProfile",
    "assert_lease",
    "capture_snapshot",
    "materialize_snapshot",
    "workspace_fingerprint",
]

_STATE_NAMESPACE = UUID("97102e19-f22a-4655-896f-4bb19a390df9")


def _record(
    connection: Any, context: WorkspaceContext, table: str, identity: UUID, *, lock: bool = False
) -> dict[str, Any]:
    if table not in {"repository_states", "candidate_workspaces"}:
        raise InvalidInputError("Unknown repository record kind.")
    row = connection.execute(
        f"SELECT * FROM {table} WHERE workspace_id=%s AND id=%s" + (" FOR UPDATE" if lock else ""),
        (context.workspace_id, identity),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Repository record is unavailable in this workspace.")
    return dict(row)


class RepositoryStateStore:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self.database = database
        self.artifacts = artifacts

    def get(self, context: WorkspaceContext, state_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            return _record(connection, context, "repository_states", state_id)

    def snapshot(self, context: WorkspaceContext, state_id: UUID) -> RepositorySnapshot:
        row = self.get(context, state_id)
        result = RepositorySnapshot(self.artifacts.read(context, row["snapshot_artifact_id"]))
        if result.manifest != row["manifest"] or result.content_digest != row["content_digest"]:
            raise InvalidInputError("Repository snapshot provenance does not match its state.")
        return result

    def capture(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        source_path: Path,
        environment_id: UUID,
        *,
        profile: SnapshotProfile | None = None,
        committed_ref: str | None = None,
        parent_ids: tuple[UUID, ...] = (),
    ) -> dict[str, Any]:
        # Authorization precedes filesystem acquisition; the path is selected by
        # the trusted controller, never accepted as authority from repository text.
        with self.database.transaction(context) as connection:
            self._scope(connection, context, repository_id, environment_id)
        snapshot = capture_snapshot(source_path, profile=profile, committed_ref=committed_ref)
        with self.database.transaction(context) as connection:
            return self.publish_in_transaction(
                connection, context, repository_id, environment_id, snapshot, parent_ids=parent_ids
            )

    @staticmethod
    def _scope(
        connection: Any, context: WorkspaceContext, repository_id: UUID, environment_id: UUID
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM repositories r CROSS JOIN environment_manifests e "
            "WHERE r.workspace_id=%s AND r.id=%s AND e.workspace_id=r.workspace_id AND e.id=%s",
            (context.workspace_id, repository_id, environment_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Repository or environment is unavailable in this workspace.")

    def publish_in_transaction(
        self,
        connection: Any,
        context: WorkspaceContext,
        repository_id: UUID,
        environment_id: UUID,
        snapshot: RepositorySnapshot,
        *,
        parent_ids: tuple[UUID, ...] = (),
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        self._scope(connection, context, repository_id, environment_id)
        if (
            not isinstance(parent_ids, tuple)
            or len(parent_ids) > 64
            or any(not isinstance(parent, UUID) for parent in parent_ids)
            or len(set(parent_ids)) != len(parent_ids)
        ):
            raise InvalidInputError("Repository parents must be a bounded distinct tuple of UUIDs.")
        for parent in parent_ids:
            row = _record(connection, context, "repository_states", parent)
            if row["repository_id"] != repository_id:
                raise ScopeDeniedError("A predecessor must belong to the same repository.")
        artifact = self.artifacts.put_in_transaction(
            connection, context, snapshot.payload, "application/vnd.potbot.repository-snapshot+json"
        )
        manifest = snapshot.manifest
        identity_hash = content_hash(
            {
                "repository_id": str(repository_id),
                "environment_id": str(environment_id),
                "snapshot_digest": artifact["digest"],
                "parents": sorted(str(parent) for parent in parent_ids),
            }
        )
        identity = uuid5(_STATE_NAMESPACE, f"{context.workspace_id}:{identity_hash}")
        connection.execute(
            "INSERT INTO repository_states(workspace_id,id,repository_id,environment_id,"
            "identity_hash,content_digest,snapshot_artifact_id,commit_id,tree_id,manifest,parent_ids) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(workspace_id,repository_id,identity_hash) DO NOTHING",
            (
                context.workspace_id,
                identity,
                repository_id,
                environment_id,
                identity_hash,
                snapshot.content_digest,
                artifact["id"],
                manifest["commit_id"],
                manifest["tree_id"],
                Jsonb(manifest),
                list(parent_ids),
            ),
        )
        for parent in parent_ids:
            connection.execute(
                "INSERT INTO repository_state_parents(workspace_id,repository_id,state_id,parent_id) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (context.workspace_id, repository_id, identity, parent),
            )
        return _record(connection, context, "repository_states", identity)


@dataclass(frozen=True, slots=True)
class Lease:
    candidate_id: UUID
    owner: str
    generation: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_id, UUID)
            or not isinstance(self.owner, str)
            or not 1 <= len(self.owner) <= 200
            or type(self.generation) is not int
            or self.generation < 1
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
        ):
            raise InvalidInputError("Invalid workspace lease identity.")


def assert_lease(connection: Any, context: WorkspaceContext, lease: Lease) -> dict[str, Any]:
    """Lock candidate ownership in the caller's admission/publication transaction."""
    if not isinstance(lease, Lease):
        raise InvalidInputError("A workspace lease is required.")
    candidate = _record(connection, context, "candidate_workspaces", lease.candidate_id, lock=True)
    row = connection.execute(
        "SELECT *,expires_at>clock_timestamp() AS live FROM workspace_leases "
        "WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
        (context.workspace_id, lease.candidate_id),
    ).fetchone()
    if (
        row is None
        or row["owner"] != lease.owner
        or row["generation"] != lease.generation
        or row["status"] != "active"
        or candidate["status"] != "leased"
        or not row["live"]
    ):
        raise StateMismatchError("Workspace lease is stale, expired or requires reconciliation.")
    return candidate


class CandidateWorkspaceManager:
    """An operator-owned root containing per-workspace candidate directories.

    Ownership markers sit outside the command-visible `tree` directory. A
    disposable path is derived from UUIDs and checked against its database and
    marker records before any cleanup or execution admission.
    """

    def __init__(self, database: Database, artifacts: ArtifactStore, root: Path) -> None:
        self.database = database
        self.artifacts = artifacts
        self.states = RepositoryStateStore(database, artifacts)
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise InvalidInputError("Candidate root must be a real operator-owned directory.")
        marker = self.root / ".potbot-candidate-root.json"
        try:
            descriptor = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(canonical_json({"version": 1, "root_id": str(uuid4())}))
                stream.flush()
                os.fsync(stream.fileno())
        try:
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r") as stream:
                metadata = json.load(stream)
            if set(metadata) != {"version", "root_id"} or metadata["version"] != 1:
                raise ValueError
            self.root_id = UUID(metadata["root_id"])
        except (OSError, ValueError, TypeError, KeyError):
            raise InvalidInputError("Candidate root ownership metadata is invalid.") from None

    def _container(self, context: WorkspaceContext, candidate_id: UUID) -> Path:
        if not isinstance(candidate_id, UUID):
            raise InvalidInputError("Candidate identity must be a UUID.")
        return self.root / str(context.workspace_id) / str(candidate_id)

    def _owned_path(self, context: WorkspaceContext, row: dict[str, Any]) -> Path:
        if row["root_id"] != self.root_id or row["status"] == "removed":
            raise StateMismatchError("Candidate is unavailable at this storage root.")
        container = self._container(context, row["id"])
        tree = container / "tree"
        if container.resolve() != container or tree.resolve() != tree or not tree.is_dir():
            raise StateMismatchError("Candidate ownership path changed or is unavailable.")
        try:
            descriptor = os.open(container / "owner.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r") as stream:
                marker = json.load(stream)
        except (OSError, ValueError):
            raise StateMismatchError("Candidate ownership marker is unavailable.") from None
        expected = {
            "root_id": str(self.root_id),
            "workspace_id": str(context.workspace_id),
            "candidate_id": str(row["id"]),
            "ownership_token": str(row["ownership_token"]),
        }
        if marker != expected:
            raise StateMismatchError("Candidate ownership marker does not match.")
        return tree

    def get(self, context: WorkspaceContext, candidate_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            row = _record(connection, context, "candidate_workspaces", candidate_id)
        row["path"] = str(self._owned_path(context, row))
        return row

    def allocate(self, context: WorkspaceContext, state_id: UUID) -> dict[str, Any]:
        state = self.states.get(context, state_id)
        snapshot = self.states.snapshot(context, state_id)
        if snapshot.manifest["submodules"]:
            from potbot.domain.errors import CapabilityUnavailableError

            raise CapabilityUnavailableError(
                "Submodule contents have no qualified materialisation."
            )
        identity, ownership = uuid4(), uuid4()
        container = self._container(context, identity)
        container.parent.mkdir(exist_ok=True, mode=0o700)
        if container.parent.resolve() != container.parent:
            raise InvalidInputError("Workspace storage path traverses a symlink.")
        container.mkdir(mode=0o700)
        marker = {
            "root_id": str(self.root_id),
            "workspace_id": str(context.workspace_id),
            "candidate_id": str(identity),
            "ownership_token": str(ownership),
        }
        try:
            (container / "owner.json").write_text(canonical_json(marker))
            (container / "owner.json").chmod(0o600)
            materialize_snapshot(snapshot, container / "tree")
            with self.database.transaction(context) as connection:
                connection.execute(
                    "INSERT INTO candidate_workspaces(workspace_id,id,repository_id,state_id,"
                    "root_id,ownership_token,status) VALUES(%s,%s,%s,%s,%s,%s,'ready')",
                    (
                        context.workspace_id,
                        identity,
                        state["repository_id"],
                        state_id,
                        self.root_id,
                        ownership,
                    ),
                )
        except IndeterminateExecutionError:
            # A lost commit acknowledgment may already have published the row.
            # Retain its owned bytes for reconciliation rather than destroying
            # a potentially durable candidate. No process has been launched.
            raise
        except Exception:
            # This directory was created in this call and has never been offered
            # to a process. Failure cleanup therefore cannot erase user work.
            shutil.rmtree(container)
            raise
        return self.get(context, identity)

    @staticmethod
    def _ttl(ttl_seconds: int) -> None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86_400:
            raise InvalidInputError("Lease lifetime must be 1–86400 seconds.")

    def claim(
        self, context: WorkspaceContext, candidate_id: UUID, owner: str, ttl_seconds: int = 300
    ) -> Lease:
        self._ttl(ttl_seconds)
        if not isinstance(owner, str) or not 1 <= len(owner) <= 200:
            raise InvalidInputError("Lease owner must be an explicit bounded identifier.")
        expired = False
        result: Lease | None = None
        with self.database.transaction(context) as connection:
            row = _record(connection, context, "candidate_workspaces", candidate_id, lock=True)
            self._owned_path(context, row)
            prior = connection.execute(
                "SELECT *,expires_at>clock_timestamp() AS live FROM workspace_leases "
                "WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
                (context.workspace_id, candidate_id),
            ).fetchone()
            if row["status"] == "quarantined" or (
                prior is not None and prior["status"] == "quarantined"
            ):
                raise StateMismatchError("Candidate requires reconciliation before mutation.")
            if prior is not None and prior["status"] == "active":
                if prior["live"]:
                    raise StateMismatchError("Candidate already has an active mutation owner.")
                connection.execute(
                    "UPDATE candidate_workspaces SET status='quarantined',updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, candidate_id),
                )
                connection.execute(
                    "UPDATE workspace_leases SET status='quarantined',updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND candidate_id=%s",
                    (context.workspace_id, candidate_id),
                )
                expired = True
            else:
                generation = 1 if prior is None else prior["generation"] + 1
                saved = connection.execute(
                    "INSERT INTO workspace_leases(workspace_id,candidate_id,owner,generation,expires_at,status) "
                    "VALUES(%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'),'active') "
                    "ON CONFLICT(workspace_id,candidate_id) DO UPDATE SET owner=EXCLUDED.owner,"
                    "generation=EXCLUDED.generation,expires_at=EXCLUDED.expires_at,status='active',"
                    "updated_at=clock_timestamp() RETURNING expires_at",
                    (context.workspace_id, candidate_id, owner, generation, ttl_seconds),
                ).fetchone()
                connection.execute(
                    "UPDATE candidate_workspaces SET status='leased',updated_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, candidate_id),
                )
                result = Lease(candidate_id, owner, generation, saved["expires_at"])
        if expired:
            raise StateMismatchError(
                "Expired owner was quarantined; reconcile it or allocate a fresh candidate."
            )
        assert result is not None
        return result

    def check_lease(self, context: WorkspaceContext, lease: Lease) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            row = assert_lease(connection, context, lease)
            row["path"] = str(self._owned_path(context, row))
            return row

    def renew(self, context: WorkspaceContext, lease: Lease, ttl_seconds: int = 300) -> Lease:
        self._ttl(ttl_seconds)
        with self.database.transaction(context) as connection:
            row = assert_lease(connection, context, lease)
            self._owned_path(context, row)
            result = connection.execute(
                "UPDATE workspace_leases SET expires_at=clock_timestamp()+(%s * interval '1 second'),"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND candidate_id=%s RETURNING expires_at",
                (ttl_seconds, context.workspace_id, lease.candidate_id),
            ).fetchone()
            return Lease(lease.candidate_id, lease.owner, lease.generation, result["expires_at"])

    def release(self, context: WorkspaceContext, lease: Lease) -> None:
        """Release only after the controller knows the operation has stopped."""
        with self.database.transaction(context) as connection:
            assert_lease(connection, context, lease)
            connection.execute(
                "UPDATE workspace_leases SET status='released',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, lease.candidate_id),
            )
            connection.execute(
                "UPDATE candidate_workspaces SET status='ready',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, lease.candidate_id),
            )

    def quarantine(self, context: WorkspaceContext, lease: Lease) -> None:
        """Retire this exact generation even if its wall-clock lease has expired."""
        with self.database.transaction(context) as connection:
            _record(connection, context, "candidate_workspaces", lease.candidate_id, lock=True)
            row = connection.execute(
                "SELECT * FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
                (context.workspace_id, lease.candidate_id),
            ).fetchone()
            if row is None or row["owner"] != lease.owner or row["generation"] != lease.generation:
                raise StateMismatchError("Cannot quarantine a different workspace generation.")
            if row["status"] == "released":
                raise StateMismatchError("A released generation cannot be quarantined.")
            connection.execute(
                "UPDATE workspace_leases SET status='quarantined',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, lease.candidate_id),
            )
            connection.execute(
                "UPDATE candidate_workspaces SET status='quarantined',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, lease.candidate_id),
            )

    def reconcile(
        self,
        context: WorkspaceContext,
        candidate_id: UUID,
        *,
        expected_generation: int,
        observed_digest: str,
        reason: str,
        processes_stopped: bool,
    ) -> dict[str, Any]:
        if (
            processes_stopped is not True
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 2000
        ):
            raise InvalidInputError(
                "Reconciliation requires a stopped-process finding and explicit reason."
            )
        with self.database.transaction(context) as connection:
            candidate = _record(
                connection, context, "candidate_workspaces", candidate_id, lock=True
            )
            path = self._owned_path(context, candidate)
            row = connection.execute(
                "SELECT *,expires_at>clock_timestamp() AS live FROM workspace_leases "
                "WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
                (context.workspace_id, candidate_id),
            ).fetchone()
            if (
                row is None
                or row["generation"] != expected_generation
                or (row["status"] == "active" and row["live"])
                or row["status"] == "released"
            ):
                raise StateMismatchError("Workspace generation is not awaiting reconciliation.")
            state = _record(connection, context, "repository_states", candidate["state_id"])
            exclusions = tuple(state["manifest"]["profile"]["excluded_paths"])
            if workspace_fingerprint(path, excluded_paths=exclusions) != observed_digest:
                raise StateMismatchError(
                    "Reconciliation does not describe the actual candidate contents."
                )
            result = connection.execute(
                "INSERT INTO workspace_reconciliations(workspace_id,id,candidate_id,generation,"
                "principal_id,observed_digest,reason,processes_stopped) VALUES(%s,%s,%s,%s,%s,%s,%s,true) "
                "RETURNING *",
                (
                    context.workspace_id,
                    uuid4(),
                    candidate_id,
                    expected_generation,
                    context.principal_id,
                    observed_digest,
                    reason,
                ),
            ).fetchone()
            connection.execute(
                "UPDATE workspace_leases SET status='released',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, candidate_id),
            )
            connection.execute(
                "UPDATE candidate_workspaces SET status='ready',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, candidate_id),
            )
            return dict(result)

    def publish_state(
        self, context: WorkspaceContext, lease: Lease, *, environment_id: UUID | None = None
    ) -> dict[str, Any]:
        candidate = self.check_lease(context, lease)
        base = self.states.get(context, candidate["state_id"])
        snapshot = capture_candidate(
            Path(candidate["path"]), self.states.snapshot(context, base["id"])
        )
        with self.database.transaction(context) as connection:
            current = assert_lease(connection, context, lease)
            if current["state_id"] != base["id"]:
                raise StateMismatchError("Candidate state changed before publication.")
            actual = workspace_fingerprint(
                Path(candidate["path"]), excluded_paths=snapshot.profile.excluded_paths
            )
            if actual != snapshot.content_digest:
                raise StateMismatchError("Candidate changed before its state publication.")
            saved = self.states.publish_in_transaction(
                connection,
                context,
                base["repository_id"],
                base["environment_id"] if environment_id is None else environment_id,
                snapshot,
                parent_ids=(base["id"],),
            )
            # Artifact publication can take time. Recheck the actual database
            # deadline before publishing the candidate's new current state.
            assert_lease(connection, context, lease)
            connection.execute(
                "UPDATE candidate_workspaces SET state_id=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (saved["id"], context.workspace_id, lease.candidate_id),
            )
            return saved

    def cleanup(self, context: WorkspaceContext, candidate_id: UUID) -> None:
        with self.database.transaction(context) as connection:
            row = _record(connection, context, "candidate_workspaces", candidate_id, lock=True)
            if row["status"] != "ready":
                raise StateMismatchError("Only a stopped, reconciled candidate can be removed.")
            self._owned_path(context, row)
            lease = connection.execute(
                "SELECT status FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
                (context.workspace_id, candidate_id),
            ).fetchone()
            if lease is not None and lease["status"] != "released":
                raise StateMismatchError("Candidate mutation ownership remains unresolved.")
            shutil.rmtree(self._container(context, candidate_id))
            connection.execute(
                "UPDATE candidate_workspaces SET status='removed',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, candidate_id),
            )
