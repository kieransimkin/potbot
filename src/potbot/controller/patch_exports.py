"""Immutable, exact-state patch manifests retained beyond candidate cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.coding import coding_row
from potbot.controller.execution import scoped_row
from potbot.controller.tasks import json_record, lock_episode
from potbot.controller.verification import verification_row
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.models.configuration import read_environment
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command


class PatchExportService:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self.database, self.artifacts = database, artifacts

    def create(
        self, context: WorkspaceContext, assessment_id: UUID, idempotency_key: str
    ) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            assessment = verification_row(conn, context, "criterion_assessments", assessment_id)
            episode = lock_episode(conn, context, assessment["episode_id"])
            if episode["active_contract_id"] != assessment["contract_revision_id"]:
                raise StateMismatchError(
                    "A contract revision invalidates finalization of an older assessment."
                )

            def publish() -> dict[str, Any]:
                intent = coding_row(
                    conn, context, "patch_application_intents", assessment["application_id"]
                )
                application = conn.execute(
                    "SELECT * FROM patch_applications WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, intent["id"]),
                ).fetchone()
                coding = coding_row(conn, context, "coding_results", intent["coding_result_id"])
                handoff = coding_row(
                    conn, context, "implementation_contracts", coding["implementation_id"]
                )
                contract = scoped_row(
                    conn, context, "contract_revisions", assessment["contract_revision_id"]
                )
                base = scoped_row(conn, context, "repository_states", intent["base_state_id"])
                result = scoped_row(
                    conn, context, "repository_states", application["result_state_id"]
                )
                baseline = verification_row(
                    conn, context, "verification_runs", assessment["baseline_run_id"]
                )
                verification = verification_row(
                    conn, context, "verification_runs", assessment["candidate_run_id"]
                )
                profile = verification_row(
                    conn, context, "verification_profiles", verification["profile_id"]
                )
                if (
                    assessment["content"]["result_state_id"] != str(result["id"])
                    or assessment["content"]["active_contract_id"] != str(contract["id"])
                    or verification["application_id"] != intent["id"]
                    or application["status"] != "applied"
                ):
                    raise StateMismatchError(
                        "Export identities do not match the selected verified state."
                    )
                observations = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM verification_observations WHERE workspace_id=%s AND verification_run_id IN (%s,%s) ORDER BY verification_run_id,check_id",
                        (context.workspace_id, baseline["id"], verification["id"]),
                    ).fetchall()
                ]
                execution_ids = {
                    application["run_id"],
                    *(
                        row["execution_run_id"]
                        for row in observations
                        if row["execution_run_id"] is not None
                    ),
                }
                executions = []
                artifact_ids = {
                    coding["patch_artifact_id"],
                    base["snapshot_artifact_id"],
                    result["snapshot_artifact_id"],
                }
                for run_id in sorted(execution_ids, key=str):
                    run = scoped_row(conn, context, "action_runs", run_id)
                    completion = conn.execute(
                        "SELECT * FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                        (context.workspace_id, run_id),
                    ).fetchone()
                    if completion is None:
                        raise StateMismatchError(
                            "An export cannot reference an unconfirmed execution as observed evidence."
                        )
                    action = scoped_row(conn, context, "actions", run["action_id"])
                    execution_input = scoped_row(
                        conn, context, "repository_states", run["input_state_id"]
                    )
                    execution_result = scoped_row(
                        conn, context, "repository_states", completion["result_state_id"]
                    )
                    executions.append(
                        {
                            "run": run,
                            "action": action,
                            "completion": dict(completion),
                            "input_state": execution_input,
                            "result_state": execution_result,
                        }
                    )
                    artifact_ids.update(
                        (completion["stdout_artifact_id"], completion["stderr_artifact_id"])
                    )
                    artifact_ids.update(
                        (
                            execution_input["snapshot_artifact_id"],
                            execution_result["snapshot_artifact_id"],
                        )
                    )
                    if action["stdin_artifact_id"] is not None:
                        artifact_ids.add(action["stdin_artifact_id"])
                invocation = conn.execute(
                    "SELECT * FROM model_invocations WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, coding["invocation_id"]),
                ).fetchone()
                packet = conn.execute(
                    "SELECT * FROM context_packets WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, coding["packet_id"]),
                ).fetchone()
                deployment = scoped_row(conn, context, "model_deployments", coding["deployment_id"])
                artifact_ids.update((invocation["raw_output_artifact_id"], packet["artifact_id"]))
                packet_sources = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM context_packet_sources WHERE workspace_id=%s AND packet_id=%s ORDER BY ordinal",
                        (context.workspace_id, packet["id"]),
                    ).fetchall()
                ]
                for source in packet_sources:
                    if source["artifact_id"] is not None:
                        artifact_ids.add(source["artifact_id"])
                    else:
                        event = scoped_row(conn, context, "evidence_events", source["event_id"])
                        if event["payload_id"] is not None:
                            artifact_ids.add(event["payload_id"])
                for receipt in handoff["content"]["search_receipts"]:
                    artifact_ids.add(UUID(receipt["artifact_id"]))
                # Validate every linked object's actual bytes now. Database IDs
                # and recorded hashes alone are not proof that bytes survived.
                artifacts = []
                for artifact_id in sorted(artifact_ids, key=str):
                    if artifact_id is None:
                        raise StateMismatchError("Patch bytes are missing.")
                    data = self.artifacts.read_in_transaction(conn, context, artifact_id)
                    metadata = self.artifacts._metadata(conn, context, artifact_id)
                    artifacts.append(
                        {
                            "id": str(artifact_id),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "byte_length": len(data),
                            "media_type": metadata["media_type"],
                        }
                    )
                limitations = list(assessment["content"]["limitations"])
                limitations.extend(
                    (
                        "Coding output was supplied by the scripted fixture adapter; no model inference was performed.",
                        "Discovery records were supplied receipts; no live online or MCP search was performed.",
                    )
                )
                manifest = json_record(
                    {
                        "schema_version": 1,
                        "kind": "potbot.patch-verification-manifest",
                        "data_type": "synny://type/potbot-data-document-json",
                        "outcome": assessment["content"]["outcome"],
                        "episode_id": episode["id"],
                        "assessment_id": assessment["id"],
                        "contract": contract,
                        "implementation_contract": handoff,
                        "application": {**intent, **dict(application)},
                        "base_state": base,
                        "result_state": result,
                        "environment": {
                            "id": result["environment_id"],
                            "content": read_environment(conn, context, result["environment_id"]),
                        },
                        "profile": profile,
                        "baseline": baseline,
                        "verification": verification,
                        "criteria": assessment["content"]["criteria"],
                        "checks": assessment["content"]["checks"],
                        "observations": observations,
                        "executions": executions,
                        "participating_deployments": [deployment],
                        "coding": {
                            "result": coding,
                            "invocation": dict(invocation),
                            "packet": dict(packet),
                            "packet_sources": packet_sources,
                        },
                        "artifacts": artifacts,
                        "limitations": limitations,
                        "patch": {
                            "artifact_id": coding["patch_artifact_id"],
                            "sha256": coding["content"]["patch_sha256"],
                        },
                    }
                )
                data = canonical_json(manifest).encode("utf-8")
                artifact = self.artifacts.put_in_transaction(
                    conn, context, data, "application/json"
                )
                identity = uuid4()
                conn.execute(
                    "INSERT INTO patch_exports(workspace_id,id,episode_id,assessment_id,manifest_artifact_id,patch_artifact_id,manifest_digest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        episode["id"],
                        assessment_id,
                        artifact["id"],
                        coding["patch_artifact_id"],
                        hashlib.sha256(data).hexdigest(),
                    ),
                )
                return dict(json_record(verification_row(conn, context, "patch_exports", identity)))

            return idempotent_command(
                conn,
                context,
                "export_patch",
                idempotency_key,
                {"assessment_id": str(assessment_id)},
                publish,
            )

    def inspect(self, context: WorkspaceContext, export_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = verification_row(conn, context, "patch_exports", export_id)
            manifest_bytes = self.artifacts.read_in_transaction(
                conn, context, row["manifest_artifact_id"]
            )
            if hashlib.sha256(manifest_bytes).hexdigest() != row["manifest_digest"]:
                raise StateMismatchError("Export manifest integrity check failed.")
            manifest = json.loads(manifest_bytes)
            for reference in manifest["artifacts"]:
                data = self.artifacts.read_in_transaction(conn, context, UUID(reference["id"]))
                if (
                    hashlib.sha256(data).hexdigest() != reference["sha256"]
                    or len(data) != reference["byte_length"]
                ):
                    raise StateMismatchError(
                        "A retained export artifact no longer matches its manifest."
                    )
            patch = self.artifacts.read_in_transaction(conn, context, row["patch_artifact_id"])
            if hashlib.sha256(patch).hexdigest() != manifest["patch"]["sha256"]:
                raise StateMismatchError("Export patch integrity check failed.")
            episode = lock_episode(conn, context, row["episode_id"])
            return dict(
                json_record(
                    {
                        **row,
                        "manifest": manifest,
                        "current_contract_matches": str(episode["active_contract_id"])
                        == manifest["contract"]["id"],
                    }
                )
            )

    def write_bundle(
        self, context: WorkspaceContext, export_id: UUID, directory: Path
    ) -> dict[str, Any]:
        inspected = self.inspect(context, export_id)
        manifest = self.artifacts.read(context, UUID(inspected["manifest_artifact_id"]))
        patch = self.artifacts.read(context, UUID(inspected["patch_artifact_id"]))
        directory = Path(os.path.abspath(directory))
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise ScopeDeniedError("Export paths must not traverse symlinks.")
        directory.mkdir(parents=True, exist_ok=True)
        files = {"manifest.json": manifest, "candidate.patch": patch}
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()
        ).encode("ascii")
        for name, data in files.items():
            destination = directory / name
            try:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or destination.read_bytes() != data
                ):
                    raise StateMismatchError(
                        "Export destination already contains different or unsafe data."
                    ) from None
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        return {
            "export_id": str(export_id),
            "outcome": inspected["manifest"]["outcome"],
            "current_contract_matches": inspected["current_contract_matches"],
            "directory": str(directory),
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        }
