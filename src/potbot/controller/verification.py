"""Baseline and candidate checks through the durable, isolated tool gateway.

Every check gets a fresh materialization of exactly the recorded target state.
Check definitions, environment and task revision are part of evidence identity;
an edited test or an old contract cannot supply an unqualified passing result.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.controller.execution import scoped_row
from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    BudgetUnavailableError,
    CapabilityUnavailableError,
    DomainError,
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal
from potbot.domain.serialization import content_hash
from potbot.domain.verification import VerificationProfile, aggregate_status, comparison
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.tools.gateway import ToolGateway
from potbot.tools.repository import CandidateWorkspaceManager, Lease, RepositoryStateStore

_TABLES = frozenset(
    {
        "verification_profiles",
        "verification_runs",
        "verification_attempts",
        "verification_observations",
        "criterion_assessments",
        "patch_exports",
        "patch_pipelines",
    }
)


def verification_row(
    conn: Any, context: WorkspaceContext, table: str, identity: UUID
) -> dict[str, Any]:
    if table not in _TABLES:
        raise InvalidInputError("Unknown verification resource.")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE workspace_id=%s AND id=%s", (context.workspace_id, identity)
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Verification resource is unavailable in this workspace.")
    result = dict(row)
    if "content_digest" in result and content_hash(result["content"]) != result["content_digest"]:
        raise StateMismatchError("Verification record integrity failed.")
    return result


def _definition(
    check: dict[str, Any], files: list[dict[str, Any]], tool_digest: str
) -> dict[str, Any]:
    by_path = {item["path"]: item for item in files}
    return {
        "check": check,
        "tool_definition_digest": tool_digest,
        "definition_files": {path: by_path.get(path) for path in check["definition_paths"]},
    }


class VerificationService:
    def __init__(
        self,
        database: Database,
        artifacts: ArtifactStore,
        candidates: CandidateWorkspaceManager,
        gateway: ToolGateway,
    ) -> None:
        self.database, self.artifacts = database, artifacts
        self.candidates, self.gateway = candidates, gateway
        self.states = RepositoryStateStore(database, artifacts)

    def create_profile(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        contract_revision_id: UUID,
        base_state_id: UUID,
        profile: VerificationProfile,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(profile, VerificationProfile):
            raise InvalidInputError("A validated verification profile is required.")
        state = self.states.get(context, base_state_id)
        snapshot = self.states.snapshot(context, base_state_id)
        checks = []
        for check in profile.checks:
            definition = _definition(
                check.to_dict(),
                snapshot.manifest["files"],
                self.gateway.registry.get(check.tool_name).fingerprint,
            )
            checks.append(
                {
                    **check.to_dict(),
                    "definition": definition,
                    "definition_digest": content_hash(definition),
                }
            )
        content = {**profile.to_dict(), "checks": checks}
        request = {
            "episode_id": str(episode_id),
            "contract_revision_id": str(contract_revision_id),
            "base_state_id": str(base_state_id),
            "content": content,
        }
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                episode = lock_episode(conn, context, episode_id)
                if episode["active_contract_id"] != contract_revision_id:
                    raise StateMismatchError("Verification profile requires the current contract.")
                if state["repository_id"] != episode["repository_id"]:
                    raise ScopeDeniedError("Verification base belongs to another repository.")
                contract = scoped_row(conn, context, "contract_revisions", contract_revision_id)
                known = {item["id"] for item in contract["content"]["requirements"]}
                if any(not set(check.requirement_ids).issubset(known) for check in profile.checks):
                    raise InvalidInputError("A check references an unknown acceptance criterion.")
                identity = uuid4()
                conn.execute(
                    "INSERT INTO verification_profiles(workspace_id,id,episode_id,contract_revision_id,base_state_id,environment_id,content,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        episode_id,
                        contract_revision_id,
                        base_state_id,
                        state["environment_id"],
                        Jsonb(content),
                        content_hash(content),
                    ),
                )
                return dict(
                    json_record(verification_row(conn, context, "verification_profiles", identity))
                )

            return idempotent_command(
                conn, context, "create_verification_profile", idempotency_key, request, create
            )

    def get_profile(self, context: WorkspaceContext, identity: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            return dict(
                json_record(verification_row(conn, context, "verification_profiles", identity))
            )

    def start(
        self,
        context: WorkspaceContext,
        profile_id: UUID,
        target_state_id: UUID,
        purpose: str,
        idempotency_key: str,
        *,
        application_id: UUID | None = None,
    ) -> dict[str, Any]:
        if purpose not in {"baseline", "candidate", "import"} or (
            purpose == "baseline" and application_id is not None
        ):
            raise InvalidInputError("Unknown verification purpose or baseline application.")
        request = {
            "profile_id": str(profile_id),
            "target_state_id": str(target_state_id),
            "purpose": purpose,
            "application_id": str(application_id) if application_id else None,
        }
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                profile = verification_row(conn, context, "verification_profiles", profile_id)
                episode = lock_episode(conn, context, profile["episode_id"])
                if (
                    episode["active_contract_id"] != profile["contract_revision_id"]
                    or episode["status"] != "running"
                ):
                    raise StateMismatchError(
                        "Verification requires a running task and current profile."
                    )
                state = scoped_row(conn, context, "repository_states", target_state_id)
                if state["repository_id"] != episode["repository_id"]:
                    raise ScopeDeniedError("Verification target belongs to another repository.")
                if purpose == "baseline" and target_state_id != profile["base_state_id"]:
                    raise StateMismatchError("Baseline checks require the exact profile base.")
                if application_id is not None:
                    application = conn.execute(
                        "SELECT a.*,i.episode_id,i.imported_from FROM patch_applications a JOIN patch_application_intents i USING(workspace_id,id) WHERE a.workspace_id=%s AND a.id=%s",
                        (context.workspace_id, application_id),
                    ).fetchone()
                    if application is None:
                        raise ScopeDeniedError("Patch application is unavailable.")
                    if (
                        application["episode_id"] != episode["id"]
                        or application["result_state_id"] != target_state_id
                        or application["status"] != "applied"
                        or (application["imported_from"] is not None) != (purpose == "import")
                    ):
                        raise StateMismatchError(
                            "Checks must bind the exact selected application and import provenance."
                        )
                identity = uuid4()
                conn.execute(
                    "INSERT INTO verification_runs(workspace_id,id,episode_id,profile_id,contract_revision_id,target_state_id,environment_id,purpose,application_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        profile["episode_id"],
                        profile_id,
                        profile["contract_revision_id"],
                        target_state_id,
                        state["environment_id"],
                        purpose,
                        application_id,
                    ),
                )
                return dict(
                    json_record(verification_row(conn, context, "verification_runs", identity))
                )

            return idempotent_command(
                conn, context, "start_verification", idempotency_key, request, create
            )

    def inspect(self, context: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            run = verification_row(conn, context, "verification_runs", run_id)
            profile = verification_row(conn, context, "verification_profiles", run["profile_id"])
            observations = {
                row["check_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM verification_observations WHERE workspace_id=%s AND verification_run_id=%s",
                    (context.workspace_id, run_id),
                ).fetchall()
            }
            attempts = {
                row["check_id"]: dict(row)
                for row in conn.execute(
                    "SELECT v.*,r.id AS execution_run_id,r.status AS execution_status FROM verification_attempts v LEFT JOIN action_runs r ON r.workspace_id=v.workspace_id AND r.action_id=v.action_id WHERE v.workspace_id=%s AND v.verification_run_id=%s",
                    (context.workspace_id, run_id),
                ).fetchall()
            }
            checks = []
            for check in profile["content"]["checks"]:
                observation = observations.get(check["check_id"])
                if observation is None:
                    attempt = attempts.get(check["check_id"])
                    started = attempt is not None and attempt["execution_run_id"] is not None
                    observation = {
                        "check_id": check["check_id"],
                        "status": "inconclusive" if started else "not_run",
                        "reason": "Execution requires completion or reconciliation."
                        if started
                        else "No completed observation exists.",
                        "expected_definition_digest": check["definition_digest"],
                        "actual_definition_digest": None,
                        "execution_run_id": attempt["execution_run_id"]
                        if started and attempt
                        else None,
                        "details": {},
                        "persisted": False,
                    }
                checks.append({"definition": check, "observation": observation})
            return dict(json_record({**run, "profile": profile, "checks": checks}))

    def run_all(self, context: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        for check in self.inspect(context, run_id)["profile"]["content"]["checks"]:
            self.run_check(context, run_id, check["check_id"])
        return self.inspect(context, run_id)

    def run_check(self, context: WorkspaceContext, run_id: UUID, check_id: str) -> dict[str, Any]:
        # Serialize just this check across controllers. The lock holds no episode
        # or artifact rows, so gateway transactions and cancellation remain free.
        lock = int(content_hash([str(context.workspace_id), str(run_id), check_id])[:15], 16)
        with self.database.transaction(context) as conn:
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock,))
            return self._run_check(context, run_id, check_id)

    def _run_check(self, context: WorkspaceContext, run_id: UUID, check_id: str) -> dict[str, Any]:
        inspected = self.inspect(context, run_id)
        matches = [
            entry for entry in inspected["checks"] if entry["definition"]["check_id"] == check_id
        ]
        if not matches:
            raise InvalidInputError("Check is not in the frozen verification profile.")
        current = matches[0]
        if current["observation"].get("id") is not None:
            return dict(current["observation"])
        check = current["definition"]
        episode_id = UUID(inspected["episode_id"])
        contract_id = UUID(inspected["contract_revision_id"])
        state_id = UUID(inspected["target_state_id"])
        with self.database.transaction(context) as conn:
            episode = lock_episode(conn, context, episode_id)
            if episode["active_contract_id"] != contract_id or episode["status"] != "running":
                raise StateMismatchError("A stale or inactive task cannot dispatch verification.")
            attempt = conn.execute(
                "SELECT * FROM verification_attempts WHERE workspace_id=%s AND verification_run_id=%s AND check_id=%s",
                (context.workspace_id, run_id, check_id),
            ).fetchone()
        plain = check["definition"]["check"]
        try:
            tool = self.gateway.registry.get(check["tool_name"])
        except (CapabilityUnavailableError, InvalidInputError):
            return self._observe(
                context,
                inspected,
                check,
                "infrastructure_error",
                "Registered check tool is unavailable.",
                check["definition_digest"],
            )
        actual = _definition(
            plain, self.states.snapshot(context, state_id).manifest["files"], tool.fingerprint
        )
        actual_digest = content_hash(actual)
        missing = any(
            entry is None or entry["kind"] != "file"
            for entry in actual["definition_files"].values()
        )
        if missing:
            return self._observe(
                context,
                inspected,
                check,
                "blocked",
                "A required check definition is missing or is not a regular file.",
                actual_digest,
            )
        if actual_digest != check["definition_digest"]:
            return self._observe(
                context,
                inspected,
                check,
                "inconclusive",
                "Check definition changed; the baseline result is not comparable.",
                actual_digest,
            )
        if inspected["environment_id"] != inspected["profile"]["environment_id"]:
            return self._observe(
                context,
                inspected,
                check,
                "inconclusive",
                "Execution environment differs from the frozen baseline profile.",
                actual_digest,
            )
        key = f"verify:{run_id}:{check_id}"
        # Hash internal command keys so a maximum-length external check ID still
        # fits the shared 128-character idempotency-key bound.
        key = "verify:" + content_hash(key)
        if attempt is None:
            inputs = self.gateway.store.create_inputs(
                context, episode_id, contract_id, state_id, key + ":inputs"
            )
            action = self.gateway.store.propose(
                context,
                UUID(inputs["node_id"]),
                ActionProposal(check["tool_name"]),
                key + ":action",
            )
            try:
                reservation = self.gateway.budgets.reserve(
                    context, episode_id, {"calls": 1, "tool_ms": tool.timeout_ms}, key + ":budget"
                )
            except BudgetUnavailableError:
                return self._observe(
                    context,
                    inspected,
                    check,
                    "blocked",
                    "Verification resource budget is unavailable.",
                    actual_digest,
                )
            try:
                candidate = self.candidates.allocate(context, state_id)
                with self.database.transaction(context) as conn:
                    identity = uuid4()
                    conn.execute(
                        "INSERT INTO verification_attempts(workspace_id,id,episode_id,verification_run_id,check_id,candidate_id,action_id,reservation_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            identity,
                            episode_id,
                            run_id,
                            check_id,
                            candidate["id"],
                            UUID(action["id"]),
                            UUID(reservation["id"]),
                        ),
                    )
                    attempt = verification_row(conn, context, "verification_attempts", identity)
            except IndeterminateExecutionError:
                # A committed attempt may reference this reservation. Preserve
                # both identities until a retry reads the durable outcome.
                return {
                    "check_id": check_id,
                    "status": "inconclusive",
                    "reason": "Verification preparation requires reconciliation; no process was dispatched.",
                }
            except (DomainError, OSError) as error:
                self.gateway.budgets.release(
                    context,
                    UUID(reservation["id"]),
                    "Verification preparation failed before process dispatch.",
                    confirmed_not_started=True,
                )
                status = (
                    "infrastructure_error"
                    if isinstance(error, (CapabilityUnavailableError, OSError))
                    else "blocked"
                )
                return self._observe(
                    context,
                    inspected,
                    check,
                    status,
                    "Verification workspace preparation failed before process dispatch.",
                    actual_digest,
                )
        action_id = attempt["action_id"]
        prior = self.gateway.store.inspect(context, action_id)
        if prior["run"] is not None:
            result = self.gateway.reconcile(context, UUID(prior["run"]["id"]))
            if result.get("completion") is None:
                # Uncertain work remains resumable; do not freeze a failure that
                # would suppress a later recovered, independently validated receipt.
                return {
                    "check_id": check_id,
                    "status": "inconclusive",
                    "reason": "Execution has no confirmed completion; it was not repeated.",
                }
        else:
            lease: Lease | None = None
            try:
                with self.database.transaction(context) as conn:
                    prior_lease = conn.execute(
                        "SELECT * FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s",
                        (context.workspace_id, attempt["candidate_id"]),
                    ).fetchone()
                owner = key
                if (
                    prior_lease is not None
                    and prior_lease["status"] == "active"
                    and prior_lease["owner"] == owner
                ):
                    lease = Lease(
                        attempt["candidate_id"],
                        owner,
                        prior_lease["generation"],
                        prior_lease["expires_at"],
                    )
                    self.candidates.check_lease(context, lease)
                else:
                    lease = self.candidates.claim(
                        context,
                        attempt["candidate_id"],
                        owner,
                        min(3600, max(120, tool.timeout_ms // 1000 + 60)),
                    )
                result = self.gateway.execute(context, action_id, lease, attempt["reservation_id"])
            except DomainError as error:
                prior = self.gateway.store.inspect(context, action_id)
                if prior["run"] is not None:
                    return {
                        "check_id": check_id,
                        "status": "inconclusive",
                        "reason": "Execution requires reconciliation; it was not repeated.",
                    }
                self.gateway.budgets.release(
                    context,
                    attempt["reservation_id"],
                    "Verification admission did not dispatch a process.",
                    confirmed_not_started=True,
                )
                if lease is not None:
                    try:
                        self.candidates.release(context, lease)
                    except StateMismatchError:
                        pass  # Preserve quarantined or expired ownership.
                status = (
                    "infrastructure_error"
                    if isinstance(error, CapabilityUnavailableError)
                    else "blocked"
                )
                return self._observe(
                    context,
                    inspected,
                    check,
                    status,
                    "Verification admission failed: " + error.code.value,
                    actual_digest,
                    attempt=attempt,
                )
            else:
                assert lease is not None
                self.candidates.release(context, lease)
        completion = result["completion"]
        execution = result["run"]
        if (
            execution["input_state_id"] != str(state_id)
            or execution["contract_revision_id"] != str(contract_id)
            or result["action"]["tool_definition_digest"] != tool.fingerprint
        ):
            raise StateMismatchError("Check execution does not match the frozen evidence identity.")
        outcome = completion["outcome"]
        status = {
            "completed": "passed",
            "nonzero_exit": "failed",
            "infrastructure_error": "infrastructure_error",
            "cancelled": "blocked",
            "timeout": "inconclusive",
            "inconclusive": "inconclusive",
        }.get(outcome, "inconclusive")
        if completion["exit_code"] in check["infrastructure_exit_codes"]:
            status = "infrastructure_error"
        return self._observe(
            context,
            inspected,
            check,
            status,
            "Observed isolated check completion: " + outcome,
            actual_digest,
            attempt=attempt,
            execution_run_id=UUID(execution["id"]),
            details={
                "execution": execution,
                "completion": completion,
                "target_state_id": str(state_id),
                "environment_id": inspected["environment_id"],
                "contract_revision_id": str(contract_id),
            },
        )

    def _observe(
        self,
        context: WorkspaceContext,
        run: dict[str, Any],
        check: dict[str, Any],
        status: str,
        reason: str,
        actual_digest: str,
        *,
        attempt: Any = None,
        execution_run_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            identity = uuid4()
            conn.execute(
                "INSERT INTO verification_observations(workspace_id,id,episode_id,verification_run_id,check_id,attempt_id,execution_run_id,status,expected_definition_digest,actual_definition_digest,reason,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    identity,
                    UUID(run["episode_id"]),
                    UUID(run["id"]),
                    check["check_id"],
                    attempt["id"] if attempt else None,
                    execution_run_id,
                    status,
                    check["definition_digest"],
                    actual_digest,
                    reason,
                    Jsonb(details or {}),
                ),
            )
            return dict(
                json_record(verification_row(conn, context, "verification_observations", identity))
            )

    def assess(
        self,
        context: WorkspaceContext,
        application_id: UUID,
        baseline_run_id: UUID,
        candidate_run_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        baseline, candidate = (
            self.inspect(context, baseline_run_id),
            self.inspect(context, candidate_run_id),
        )
        request = {
            "application_id": str(application_id),
            "baseline_run_id": str(baseline_run_id),
            "candidate_run_id": str(candidate_run_id),
            "baseline_evidence": content_hash(baseline),
            "candidate_evidence": content_hash(candidate),
        }
        with self.database.transaction(context) as conn:
            # Current-contract authority is checked even for an idempotent
            # replay. A historical assessment remains inspectable but cannot be
            # returned here as evidence for a superseded task contract.
            application = conn.execute(
                "SELECT a.*,i.base_state_id,i.imported_from,i.implementation_id,i.episode_id FROM patch_applications a JOIN patch_application_intents i USING(workspace_id,id) WHERE a.workspace_id=%s AND a.id=%s",
                (context.workspace_id, application_id),
            ).fetchone()
            if application is None:
                raise ScopeDeniedError("Patch application is unavailable.")
            episode = lock_episode(conn, context, application["episode_id"])
            contract_id = episode["active_contract_id"]
            if (
                str(contract_id) != candidate["contract_revision_id"]
                or str(contract_id) != baseline["contract_revision_id"]
            ):
                raise StateMismatchError("Superseded evidence cannot satisfy the active contract.")

            def assess_current() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                if (
                    baseline["profile_id"] != candidate["profile_id"]
                    or baseline["purpose"] != "baseline"
                    or baseline["target_state_id"] != str(application["base_state_id"])
                    or candidate["target_state_id"] != str(application["result_state_id"])
                    or candidate["application_id"] != str(application_id)
                    or application["status"] != "applied"
                    or (application["imported_from"] is not None)
                    != (candidate["purpose"] == "import")
                ):
                    raise StateMismatchError(
                        "Assessment requires fresh checks of this exact application and baseline."
                    )
                handoff = conn.execute(
                    "SELECT content FROM implementation_contracts WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, application["implementation_id"]),
                ).fetchone()
                configured = {
                    entry["definition"]["check_id"]: entry["definition"]["definition_digest"]
                    for entry in candidate["checks"]
                }
                if any(
                    configured.get(item["check_id"]) != item["definition_digest"]
                    for item in handoff["content"]["spec"]["required_checks"]
                ):
                    raise StateMismatchError(
                        "Verification profile omits or changes an implementation-contract check."
                    )
                contract = scoped_row(conn, context, "contract_revisions", contract_id)
                before = {entry["definition"]["check_id"]: entry for entry in baseline["checks"]}
                checks = []
                for entry in candidate["checks"]:
                    check, observation = entry["definition"], entry["observation"]
                    prior = before[check["check_id"]]["observation"]
                    comparable = (
                        baseline["environment_id"] == candidate["environment_id"]
                        and observation["actual_definition_digest"] == check["definition_digest"]
                        and prior["actual_definition_digest"] == check["definition_digest"]
                    )
                    status = observation["status"]
                    # A candidate pass alone does not establish whether a missing
                    # baseline was already failing or whether it regressed.
                    if status == "passed" and (
                        not comparable or prior["status"] not in {"passed", "failed"}
                    ):
                        status = "inconclusive"
                    checks.append(
                        {
                            "check_id": check["check_id"],
                            "behavior_id": check["behavior_id"],
                            "mandatory": check["mandatory"],
                            "requirement_ids": check["requirement_ids"],
                            "authored_by": check["authored_by"],
                            "status": status,
                            "baseline_status": prior["status"],
                            "candidate_status": observation["status"],
                            "comparison": comparison(
                                prior["status"], observation["status"], comparable=comparable
                            ),
                            "definition_digest": check["definition_digest"],
                            "observation_id": observation.get("id"),
                            "execution_run_id": observation["execution_run_id"],
                        }
                    )
                criteria = []
                for requirement in contract["content"]["requirements"]:
                    supporting = [
                        check for check in checks if requirement["id"] in check["requirement_ids"]
                    ]
                    status = aggregate_status([check["status"] for check in supporting])
                    if status == "passed" and not any(
                        check["authored_by"] != "agent" for check in supporting
                    ):
                        status = "inconclusive"
                    criteria.append(
                        {
                            **requirement,
                            "status": status,
                            "check_ids": [check["check_id"] for check in supporting],
                        }
                    )
                overall = aggregate_status(
                    [item["status"] for item in criteria]
                    + [check["status"] for check in checks if check["mandatory"]]
                )
                outcome = (
                    "verified"
                    if overall == "passed"
                    else (
                        "blocked"
                        if overall in {"blocked", "infrastructure_error", "not_run"}
                        else "partial"
                    )
                )
                limitations = [
                    f"{check['check_id']}: {check['status']} ({check['comparison']})"
                    for check in checks
                    if check["status"] != "passed"
                ]
                if any(check["authored_by"] == "agent" for check in checks):
                    limitations.append(
                        "Agent-authored checks alone cannot establish an acceptance criterion."
                    )
                content = {
                    "schema_version": 1,
                    "outcome": outcome,
                    "criteria": criteria,
                    "checks": checks,
                    "limitations": limitations,
                    "active_contract_id": str(contract_id),
                    "base_state_id": baseline["target_state_id"],
                    "result_state_id": candidate["target_state_id"],
                    "environment_id": candidate["environment_id"],
                    "application_id": str(application_id),
                    "baseline_run_id": str(baseline_run_id),
                    "candidate_run_id": str(candidate_run_id),
                }
                identity = uuid4()
                conn.execute(
                    "INSERT INTO criterion_assessments(workspace_id,id,episode_id,contract_revision_id,application_id,baseline_run_id,candidate_run_id,content,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        episode["id"],
                        contract_id,
                        application_id,
                        baseline_run_id,
                        candidate_run_id,
                        Jsonb(content),
                        content_hash(content),
                    ),
                )
                return dict(
                    json_record(verification_row(conn, context, "criterion_assessments", identity))
                )

            return idempotent_command(
                conn, context, "assess_patch", idempotency_key, request, assess_current
            )
