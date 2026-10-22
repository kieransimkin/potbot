"""Read-only post-publication audit for exact Phase 7 repository research.

This independently replays the source query using immutable bytes and checks
its retained PostgreSQL receipt, output artifact and observation rows. Source
verification is deliberately *not* patch verification, Cog activation, license
clearance, information-need resolution or authorization to execute code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from potbot.controller.research_worker import _action, _run
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database
from potbot.research.contracts import ResearchAction, ResearchObservation
from potbot.research.repository import SnapshotResearchAdapter
from potbot.tools.repository_snapshot import RepositorySnapshot

_MAX_RESULT_BYTES = 8 * 1024 * 1024
_BATCH_TYPE = "synny://type/potbot-research-observation-batch"


def replay_source_batch(
    action: ResearchAction,
    snapshot: RepositorySnapshot,
    run_id: UUID,
    output: bytes,
) -> tuple[dict[str, Any], tuple[ResearchObservation, ...]]:
    """Fail closed on forged excerpts, result identities and noncanonical JSON.

    The timestamp is extracted from the retained observation, then every
    observation is recomputed from actual immutable source bytes. This accepts
    no model-produced statement about the inspected bytes.
    """
    if not isinstance(action, ResearchAction) or not isinstance(snapshot, RepositorySnapshot):
        raise InvalidInputError("Replay requires typed research action and immutable snapshot")
    if not isinstance(run_id, UUID) or not isinstance(output, bytes):
        raise InvalidInputError("Replay requires a run identity and exact output bytes")
    if not output or len(output) > _MAX_RESULT_BYTES:
        raise StateMismatchError("Research batch is missing or exceeds its audit bound")
    try:
        body = json.loads(output.decode("utf-8"))
        if canonical_json(body).encode("utf-8") != output:
            raise StateMismatchError("Research output must be exact canonical JSON")
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as error:
        raise StateMismatchError("Research output is invalid JSON") from error
    if not isinstance(body, dict) or set(body) != {
        "schema_version",
        "primary_type_ref",
        "run_id",
        "action_id",
        "action_identity",
        "source_artifact_id",
        "source_revision",
        "observations",
    }:
        raise StateMismatchError("Research batch has an unknown envelope")
    if (
        body["schema_version"] != 1
        or body["primary_type_ref"] != _BATCH_TYPE
        or body["run_id"] != str(run_id)
        or body["action_id"] != str(action.action_id)
        or body["action_identity"] != action.identity
        or body["source_revision"] != snapshot.content_digest
        or not isinstance(body["observations"], list)
        or not 1 <= len(body["observations"]) <= 100
    ):
        raise StateMismatchError("Research batch identities or result count differ")
    first = body["observations"][0]
    if not isinstance(first, dict):
        raise StateMismatchError("Research observation is not an object")
    try:
        stamp = datetime.fromisoformat(first["retrieved_at"])
    except (KeyError, ValueError, TypeError) as error:
        raise StateMismatchError("Research observation timestamp is invalid") from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise StateMismatchError("Research observation timestamp must be timezone-aware")
    replay = SnapshotResearchAdapter(
        snapshot,
        repository_state_id=action.scope.repository_state_id,
        workspace_id=action.scope.workspace_id,
        task_id=action.scope.task_id,
    ).inspect(action, now=stamp)
    if body["observations"] != [item.to_dict() for item in replay]:
        raise StateMismatchError("Research observations differ from actual repository bytes")
    return body, replay


class ResearchAuditStore:
    """Authenticated read-only end-to-end audit, even after task revisions change."""

    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        if not isinstance(database, Database) or artifacts.db != database:
            raise InvalidInputError("Research audit requires one shared authenticated database")
        self.database, self.artifacts = database, artifacts

    def audit(self, context: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        if not isinstance(run_id, UUID):
            raise InvalidInputError("Audit requires a research run UUID")
        with self.database.transaction(context) as conn:
            run = _run(conn, context, run_id)
            if run["state"] != "succeeded" or run["result_artifact_id"] is None:
                raise StateMismatchError("Research run has no completed artifact to audit")
            action, source = _action(conn, context, run["action_id"])
            if (
                run["episode_id"] != action.scope.task_id
                or run["source_artifact_id"] != source["snapshot_artifact_id"]
                or run["source_revision"] != source["content_digest"]
                or run["action_identity"] != action.identity
            ):
                raise StateMismatchError("Research run source identity has changed")
            source_raw = self.artifacts.read_in_transaction(
                conn, context, run["source_artifact_id"]
            )
            snapshot = RepositorySnapshot(source_raw)
            if (
                snapshot.content_digest != run["source_revision"]
                or snapshot.manifest != source["manifest"]
            ):
                raise StateMismatchError("Retained research snapshot is not the admitted source")
            result_raw = self.artifacts.read_in_transaction(
                conn, context, run["result_artifact_id"]
            )
            body, observations = replay_source_batch(action, snapshot, run_id, result_raw)
            if body["source_artifact_id"] != str(run["source_artifact_id"]):
                raise StateMismatchError("Research result refers to a different source artifact")
            receipt_row = conn.execute(
                "SELECT receipt_digest,content,result_artifact_id FROM research_run_receipts "
                "WHERE workspace_id=%s AND episode_id=%s AND run_id=%s",
                (context.workspace_id, run["episode_id"], run_id),
            ).fetchone()
            if receipt_row is None:
                raise StateMismatchError("Research run is missing its retained receipt")
            receipt = receipt_row["content"]
            if (
                not isinstance(receipt, dict)
                or content_hash(receipt) != receipt_row["receipt_digest"]
                or receipt_row["receipt_digest"] != run["receipt_digest"]
                or receipt_row["result_artifact_id"] != run["result_artifact_id"]
            ):
                raise StateMismatchError("Research receipt digest or output binding differs")
            identities = receipt.get("observation_ids")
            if (
                not isinstance(identities, list)
                or len(identities) != len(observations)
                or len(set(map(str, identities))) != len(identities)
            ):
                raise StateMismatchError("Research receipt observation identities are invalid")
            try:
                ids = tuple(UUID(identity) for identity in identities)
            except (TypeError, ValueError, AttributeError) as error:
                raise StateMismatchError(
                    "Research receipt observation IDs are not UUIDs"
                ) from error
            if list(map(str, ids)) != identities:
                raise StateMismatchError("Research receipt requires canonical observation UUIDs")
            expected = {
                "schema_version": 1,
                "run_id": str(run_id),
                "action_id": str(action.action_id),
                "action_identity": action.identity,
                "workspace_id": str(context.workspace_id),
                "task_id": str(action.scope.task_id),
                "contract_revision_id": str(action.scope.contract_revision_id),
                "need_revision_id": str(action.scope.need_revision_id),
                "repository_state_id": str(action.scope.repository_state_id),
                "semantic_release_digest": action.scope.semantic_release_digest,
                "source_snapshot_artifact_id": str(run["source_artifact_id"]),
                "source_revision": snapshot.content_digest,
                "output_artifact_id": str(run["result_artifact_id"]),
                "output_sha256": hashlib.sha256(result_raw).hexdigest(),
                "observation_fingerprints": [item.fingerprint for item in observations],
                "observation_ids": identities,
                "lease_generation": run["generation"],
                "status": "source_inspection_completed",
                "cog_activation": "pending_pinned_syn_registration",
                "execution_authorized": False,
                "need_resolved": False,
                "license_assessment": "unknown",
            }
            if receipt != expected:
                raise StateMismatchError("Research receipt does not match replayed source evidence")
            rows = conn.execute(
                "SELECT id,episode_id,action_id,source_artifact_id,observation_digest,"
                "status,content FROM research_observations WHERE workspace_id=%s "
                "AND episode_id=%s AND id=ANY(%s)",
                (context.workspace_id, action.scope.task_id, list(ids)),
            ).fetchall()
            by_id = {row["id"]: row for row in rows}
            if len(by_id) != len(ids):
                raise StateMismatchError("Retained source observation rows are incomplete")
            for item_id, observation in zip(ids, observations, strict=True):
                row = by_id[item_id]
                if (
                    row["episode_id"] != action.scope.task_id
                    or row["action_id"] != action.action_id
                    or row["source_artifact_id"] != run["source_artifact_id"]
                    or row["observation_digest"] != observation.fingerprint
                    or row["status"] != observation.status.value
                    or row["content"] != observation.to_dict()
                ):
                    raise StateMismatchError(
                        "An observation row differs from verified source bytes"
                    )
            return {
                "schema_version": 1,
                "run_id": str(run_id),
                "qualification": "verified_immutable_repository_source_only",
                "source_revision": snapshot.content_digest,
                "receipt_digest": receipt_row["receipt_digest"],
                "observation_count": len(observations),
                "complete_scan": all(item.scan_complete for item in observations),
                "cog_activated": False,
                "need_resolved": False,
                "patch_verified": False,
                "external_source_license_verified": False,
            }
