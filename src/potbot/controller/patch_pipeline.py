"""Restartable scripted task-to-patch orchestration, with explicit checkpoints."""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.coding import CodingStore
from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.patch_exports import PatchExportService
from potbot.controller.reuse import plan_reuse_search, review_reuse_plan
from potbot.controller.tasks import TaskService, json_record, lock_episode
from potbot.controller.verification import VerificationService, verification_row
from potbot.domain.auth import WorkspaceContext
from potbot.domain.coding import ImplementationSpec, ScriptedCodingResponse
from potbot.domain.errors import InvalidInputError, RevisionConflictError, StateMismatchError
from potbot.domain.reuse import ReuseAction, ReuseDecision, ReuseScope, SearchEvidence, SearchStatus
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.tasks import ContractSpec, TaskStatus
from potbot.domain.verification import VerificationProfile
from potbot.models.configuration import EnvironmentManifest, store_environment
from potbot.models.scripted import ScriptedCodingProducer
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.tools.gateway import ToolGateway
from potbot.tools.patch_application import PatchApplicationService
from potbot.tools.repository import CandidateWorkspaceManager, RepositoryStateStore


class PatchPipeline:
    def __init__(
        self,
        database: Database,
        artifacts: ArtifactStore,
        candidates: CandidateWorkspaceManager,
        gateway: ToolGateway,
    ) -> None:
        self.database, self.artifacts = database, artifacts
        self.candidates, self.gateway = candidates, gateway
        self.tasks = TaskService(database)
        self.verification = VerificationService(database, artifacts, candidates, gateway)
        self.exports = PatchExportService(database, artifacts)

    def create(
        self,
        context: WorkspaceContext,
        repository_id: UUID,
        source: Path,
        contract: ContractSpec,
        profile: VerificationProfile,
        patch: bytes,
        idempotency_key: str,
    ) -> dict[str, Any]:
        # Validate the response bound before any task or repository allocation.
        ScriptedCodingResponse(uuid4(), uuid4(), patch=patch)
        request = {
            "repository_id": str(repository_id),
            "source": str(source.absolute()),
            "contract": contract.to_dict(),
            "profile": profile.to_dict(),
            "patch_digest": content_hash({"patch": patch.decode("utf-8")}),
        }
        key = "pipeline-create:" + content_hash(idempotency_key)
        with self.database.transaction(context) as conn:

            def create_pipeline() -> dict[str, Any]:
                task = self.tasks.submit(context, repository_id, contract, key + ":task")
                episode_id = UUID(task["task_id"])
                contract_id = UUID(task["current_contract"]["id"])
                config = task["configuration"]
                environment_spec = EnvironmentManifest(
                    UUID(config["id"]),
                    config["content_hash"],
                    platform.platform(),
                    f"Python {platform.python_version()} at {sys.executable}",
                    tuple(
                        (name, self.gateway.registry.get(name).fingerprint)
                        for name in self.gateway.registry.inventory()
                    ),
                    secret_refs=tuple(config["content"]["secret_refs"].items()),
                )
                with self.database.transaction(context) as environment_conn:
                    environment = store_environment(environment_conn, context, environment_spec)
                state = RepositoryStateStore(self.database, self.artifacts).capture(
                    context, repository_id, source, environment["id"]
                )
                frozen = self.verification.create_profile(
                    context, episode_id, contract_id, state["id"], profile, key + ":profile"
                )
                # Reinspect, because an earlier interrupted call may have already
                # performed one or both idempotent lifecycle transitions.
                task = self.tasks.inspect(context, episode_id)
                if task["lifecycle"] == "submitted":
                    task = self.tasks.transition(
                        context,
                        episode_id,
                        task["update_token"],
                        TaskStatus.INITIALIZING,
                        "Captured the scripted pipeline baseline.",
                        idempotency_key=key + ":init",
                    )
                if task["lifecycle"] == "initializing":
                    task = self.tasks.transition(
                        context,
                        episode_id,
                        task["update_token"],
                        TaskStatus.RUNNING,
                        "Frozen baseline and verification profile are ready.",
                        idempotency_key=key + ":run",
                    )
                if task["lifecycle"] != "running":
                    raise StateMismatchError("The scripted pipeline requires an active task.")
                patch_record = self.artifacts.put_in_transaction(
                    conn, context, patch, "text/x-diff"
                )
                receipt = self.artifacts.put_in_transaction(
                    conn,
                    context,
                    canonical_json(
                        {
                            "schema_version": 1,
                            "origin": "scripted_fixture",
                            "live_search_performed": False,
                            "scope": {
                                "episode_id": str(episode_id),
                                "repository_id": str(repository_id),
                            },
                            "observations": [
                                "The fixture supplies a bounded patch and installed verification tools.",
                                "External library, external tool and internal workflow discovery adapters are unavailable in this scripted phase.",
                            ],
                        }
                    ).encode("utf-8"),
                    "application/json",
                )
                identity = uuid4()
                conn.execute(
                    "INSERT INTO patch_pipelines(workspace_id,id,episode_id,contract_revision_id,base_state_id,profile_id,patch_artifact_id,receipt_artifact_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        episode_id,
                        contract_id,
                        state["id"],
                        UUID(frozen["id"]),
                        patch_record["id"],
                        receipt["id"],
                    ),
                )
                return dict(
                    json_record(verification_row(conn, context, "patch_pipelines", identity))
                )

            return idempotent_command(
                conn, context, "create_patch_pipeline", key, request, create_pipeline
            )

    def inspect(self, context: WorkspaceContext, pipeline_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            pipeline = verification_row(conn, context, "patch_pipelines", pipeline_id)
            export = self._own_export(conn, context, pipeline_id)
            need_commands = conn.execute(
                "SELECT result FROM command_results WHERE workspace_id=%s "
                "AND operation='create_information_need' AND idempotency_key LIKE %s "
                "ORDER BY idempotency_key,id",
                (context.workspace_id, f"pipeline:{pipeline_id}:reassess:%"),
            ).fetchall()
            reassessment_needs = [
                InformationNeedStore._inspect(conn, context, UUID(command["result"]["id"]))
                for command in need_commands
            ]
        return {
            **json_record(pipeline),
            "task": self.tasks.inspect(context, pipeline["episode_id"]),
            "exports": [] if export is None else [json_record(export)],
            "reassessment_needs": reassessment_needs,
        }

    @staticmethod
    def _own_export(
        conn: Any, context: WorkspaceContext, pipeline_id: UUID
    ) -> dict[str, Any] | None:
        command = conn.execute(
            "SELECT result FROM command_results WHERE workspace_id=%s AND operation='export_patch' AND idempotency_key=%s",
            (context.workspace_id, f"pipeline:{pipeline_id}:export"),
        ).fetchone()
        if command is None:
            return None
        return verification_row(conn, context, "patch_exports", UUID(command["result"]["id"]))

    def resume(
        self,
        context: WorkspaceContext,
        pipeline_id: UUID,
        *,
        stop_after: str = "export",
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._resume(context, pipeline_id, stop_after=stop_after, progress=progress)
        except (StateMismatchError, RevisionConflictError):
            # The failed execution transaction has rolled back before this
            # separate durable question is recorded. No new work is dispatched.
            self._record_reassessment_need(context, pipeline_id)
            raise

    def _record_reassessment_need(self, context: WorkspaceContext, pipeline_id: UUID) -> None:
        with self.database.transaction(context) as conn:
            pipeline = verification_row(conn, context, "patch_pipelines", pipeline_id)
        task = self.tasks.inspect(context, pipeline["episode_id"])
        current = task["current_contract"]
        if current["id"] == str(pipeline["contract_revision_id"]):
            return
        current_id = current["id"]
        spec = NeedSpec(
            question=f"What revised implementation handoff, verification profile and fresh checks are needed for contract {current_id}?",
            pending_decision="Reassess the selected patch against the current acceptance criteria before finalization.",
            alternatives=(
                "Retain the selected patch after a revised handoff and fresh applicable checks.",
                "Produce a revised patch and verify it against the current contract.",
            ),
            required_evidence=(
                f"Current acceptance criteria in contract {current_id}, content digest {current['content_hash']}.",
                "A revised bounded implementation handoff identifying its exact base and required checks.",
                "A current verification profile mapping every acceptance criterion to check definitions.",
                "Fresh baseline and candidate checks bound to the selected application and execution environment.",
            ),
            resolution_criteria=(
                "The implementation handoff and verification profile bind the current contract revision.",
                "Every current acceptance criterion and mandatory check has an explicit reassessed status.",
                "Superseded observations are retained as history and do not establish current verification.",
            ),
            scope=f"pipeline:{pipeline_id}; episode:{pipeline['episode_id']}; contract:{current_id}",
            priority=90,
        )
        InformationNeedStore(self.database).create(
            context,
            pipeline["episode_id"],
            spec,
            f"pipeline:{pipeline_id}:reassess:{current_id}",
        )

    def _resume(
        self,
        context: WorkspaceContext,
        pipeline_id: UUID,
        *,
        stop_after: str,
        progress: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        if stop_after not in {"baseline", "application", "verification", "export"}:
            raise InvalidInputError("Unknown pipeline checkpoint.")
        with self.database.transaction(context) as conn:
            pipeline = verification_row(conn, context, "patch_pipelines", pipeline_id)
            episode = lock_episode(conn, context, pipeline["episode_id"])
            if episode["active_contract_id"] != pipeline["contract_revision_id"]:
                raise StateMismatchError(
                    "The pipeline contract changed; create a new handoff and verification profile."
                )
            export = self._own_export(conn, context, pipeline_id)
            if export is not None:
                completed_export = dict(json_record(dict(export)))
            else:
                completed_export = None
        if completed_export is not None:
            return self._finished(context, pipeline, completed_export)
        key = f"pipeline:{pipeline_id}"
        report: dict[str, Any] = {
            "pipeline_id": str(pipeline_id),
            "task_id": str(pipeline["episode_id"]),
        }
        if progress:
            progress("baseline")
        baseline = self.verification.start(
            context,
            pipeline["profile_id"],
            pipeline["base_state_id"],
            "baseline",
            key + ":baseline",
        )
        report["baseline"] = self.verification.run_all(context, UUID(baseline["id"]))
        if stop_after == "baseline":
            return {**report, "checkpoint": "baseline"}
        task = self.tasks.inspect(context, pipeline["episode_id"])
        scope = ReuseScope(
            pipeline["episode_id"], UUID(task["repository_id"]), context.workspace_id
        )
        plan = plan_reuse_search(
            scope,
            task["current_contract"]["content"]["objective"],
            ("Python", "scripted supplied patch"),
        )
        searches = tuple(
            SearchEvidence(
                intent,
                SearchStatus.UNAVAILABLE,
                f"artifact:{pipeline['receipt_artifact_id']}",
                reason="This scripted fixture has no live discovery adapter; supplied receipt records that limitation.",
            )
            for intent in plan.intents
        )
        review = review_reuse_plan(
            plan,
            searches,
            ReuseDecision(
                scope,
                ReuseAction.BUILD,
                "Apply the supplied bounded fixture repair; retain the explicit discovery limitations.",
            ),
        )
        profile = self.verification.get_profile(context, pipeline["profile_id"])
        spec = ImplementationSpec(
            tuple(task["current_contract"]["content"]["change_scope"]),
            tuple(
                item["acceptance_criterion"]
                for item in task["current_contract"]["content"]["requirements"]
            ),
            tuple(
                (item["check_id"], item["definition_digest"])
                for item in profile["content"]["checks"]
                if item["mandatory"]
            ),
            review,
            (pipeline["receipt_artifact_id"],),
        )
        handoff = CodingStore(self.database, self.artifacts).create_contract(
            context,
            pipeline["episode_id"],
            pipeline["contract_revision_id"],
            pipeline["base_state_id"],
            spec,
            key + ":handoff",
        )
        if progress:
            progress("scripted coding")
        coding = ScriptedCodingProducer(self.database, self.artifacts).produce(
            context,
            ScriptedCodingResponse(
                UUID(handoff["id"]),
                pipeline["base_state_id"],
                patch=self.artifacts.read(context, pipeline["patch_artifact_id"]),
            ),
            key + ":coding",
        )
        if progress:
            progress("patch application")
        application = PatchApplicationService(
            self.database, self.artifacts, self.candidates, self.gateway
        ).apply(context, UUID(coding["id"]), key + ":application")
        report.update({"coding": coding, "application": application})
        if application["status"] != "applied":
            return {**report, "checkpoint": "application", "outcome": "blocked"}
        if stop_after == "application":
            return {**report, "checkpoint": "application"}
        if progress:
            progress("candidate verification")
        verification = self.verification.start(
            context,
            pipeline["profile_id"],
            UUID(application["result_state_id"]),
            "candidate",
            key + ":verification",
            application_id=UUID(application["id"]),
        )
        report["verification"] = self.verification.run_all(context, UUID(verification["id"]))
        assessment = self.verification.assess(
            context,
            UUID(application["id"]),
            UUID(baseline["id"]),
            UUID(verification["id"]),
            key + ":assessment",
        )
        report["assessment"] = assessment
        if stop_after == "verification":
            return {
                **report,
                "checkpoint": "verification",
                "outcome": assessment["content"]["outcome"],
            }
        if progress:
            progress("export")
        export = self.exports.create(context, UUID(assessment["id"]), key + ":export")
        return self._finished(context, pipeline, export)

    def _finished(
        self, context: WorkspaceContext, pipeline: dict[str, Any], export: dict[str, Any]
    ) -> dict[str, Any]:
        inspected = self.exports.inspect(context, UUID(export["id"]))
        task = self.tasks.inspect(context, pipeline["episode_id"])
        manifest = inspected["manifest"]
        if (
            not inspected["current_contract_matches"]
            or task["current_contract"]["id"] != str(pipeline["contract_revision_id"])
            or manifest["contract"]["id"] != str(pipeline["contract_revision_id"])
            or manifest["episode_id"] != str(pipeline["episode_id"])
            or manifest["base_state"]["id"] != str(pipeline["base_state_id"])
            or manifest["profile"]["id"] != str(pipeline["profile_id"])
            or manifest["patch"]["artifact_id"] != str(pipeline["patch_artifact_id"])
        ):
            raise StateMismatchError(
                "The selected export no longer satisfies this pipeline's active contract and exact inputs."
            )
        # A later revision increments update_token and is fenced by transition.
        outcome = manifest["outcome"]
        if task["lifecycle"] == "running":
            target = TaskStatus.VERIFIED if outcome == "verified" else TaskStatus.PARTIAL
            task = self.tasks.transition(
                context,
                pipeline["episode_id"],
                task["update_token"],
                target,
                "Scripted patch export finalized with explicit criterion statuses.",
                idempotency_key=f"pipeline:{pipeline['id']}:finalize",
            )
        return {
            "pipeline_id": str(pipeline["id"]),
            "task_id": str(pipeline["episode_id"]),
            "checkpoint": "export",
            "outcome": outcome,
            "export": inspected,
            "task": task,
        }
