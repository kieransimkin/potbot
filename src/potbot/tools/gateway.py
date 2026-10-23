"""Controller-owned admission, isolated execution and conservative recovery."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.controller.budgets import BudgetLedger
from potbot.controller.execution import ExecutionStore, execution_inputs, scoped_row
from potbot.controller.tasks import lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database
from potbot.persistence.evidence import append_event
from potbot.tools.isolation import runtime_roots
from potbot.tools.receipts import RunReceiptStore
from potbot.tools.repository import (
    CandidateWorkspaceManager,
    Lease,
    assert_lease,
    workspace_fingerprint,
)
from potbot.tools.secrets import CaptureRedactionError, CaptureRedactor, SecretReferenceProvider
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor

if TYPE_CHECKING:
    from potbot.workspace_sandbox import WorkspaceSandbox


class ToolGateway:
    def __init__(
        self,
        database: Database,
        artifacts: ArtifactStore,
        candidates: CandidateWorkspaceManager,
        registry: ToolRegistry,
        capabilities: CapabilityContext,
        receipts: RunReceiptStore,
        *,
        supervisor: ProcessSupervisor | None = None,
        secrets: SecretReferenceProvider | None = None,
        read_only_roots: tuple[Path, ...] = (),
        workspace_sandbox: WorkspaceSandbox | None = None,
    ) -> None:
        self.database, self.artifacts, self.candidates = database, artifacts, candidates
        self.registry, self.capabilities, self.receipts = registry, capabilities, receipts
        self.supervisor = supervisor or ProcessSupervisor()
        self.secrets = secrets or SecretReferenceProvider({})
        self.read_only_roots = read_only_roots
        self.workspace_sandbox = workspace_sandbox
        self.instance_id = uuid4()
        self.store = ExecutionStore(database, registry)
        self.budgets = BudgetLedger(database)

    def _fingerprint(
        self,
        context: WorkspaceContext,
        candidate: dict[str, Any],
        state: dict[str, Any] | None = None,
    ) -> str:
        if state is None:
            state = self.candidates.states.get(context, candidate["state_id"])
        return workspace_fingerprint(
            Path(candidate["path"]),
            excluded_paths=tuple(state["manifest"]["profile"]["excluded_paths"]),
        )

    def _paths(self, root: Path, proposal: ActionProposal) -> Path:
        for relative in (*proposal.input_paths, *proposal.output_paths, proposal.working_directory):
            path = root / relative
            if not path.resolve().is_relative_to(root.resolve()):
                raise ScopeDeniedError("A requested path escapes the candidate workspace.")
        cwd = root / proposal.working_directory
        if not cwd.is_dir():
            raise InvalidInputError("The requested working directory is unavailable.")
        for private in (self.receipts.root, getattr(self.artifacts.backend, "root", None)):
            if isinstance(private, Path):
                if private.resolve().is_relative_to(
                    root.resolve()
                ) or root.resolve().is_relative_to(private.resolve()):
                    raise ScopeDeniedError(
                        "Controller storage and candidate content must be separate."
                    )
                if any(
                    private.resolve().is_relative_to(path.resolve())
                    or path.resolve().is_relative_to(private.resolve())
                    for path in (*runtime_roots(), *self.read_only_roots)
                ):
                    raise ScopeDeniedError("A runtime mount exposes private controller storage.")
        return cwd

    def _admit(
        self, context: WorkspaceContext, action_id: UUID, lease: Lease, reservation_id: UUID
    ) -> tuple[dict[str, Any], ToolDefinition, ActionProposal, dict[str, Any]]:
        candidate = self.candidates.check_lease(context, lease)
        actual = self._fingerprint(context, candidate)
        with self.database.transaction(context) as conn:
            action, node, view = execution_inputs(conn, context, action_id)
            episode = lock_episode(conn, context, action["episode_id"])
            current = assert_lease(conn, context, lease)
            if current["state_id"] != candidate["state_id"]:
                raise StateMismatchError("Candidate changed during execution admission.")
            existing = conn.execute(
                "SELECT * FROM action_runs WHERE workspace_id=%s AND action_id=%s",
                (context.workspace_id, action_id),
            ).fetchone()
            if existing is not None:
                raise IndeterminateExecutionError(
                    "An action attempt already exists; inspect or reconcile it."
                )
            if (
                episode["status"] != "running"
                or episode["active_contract_id"] != view["contract_revision_id"]
            ):
                raise StateMismatchError("Task is suspended or its active contract changed.")
            if (
                candidate["repository_id"] != episode["repository_id"]
                or candidate["state_id"] != view["repository_state_id"]
            ):
                raise StateMismatchError("Candidate does not match the action input state.")
            state = scoped_row(conn, context, "repository_states", candidate["state_id"])
            if actual != state["content_digest"]:
                raise StateMismatchError("Candidate contents differ from the expected input state.")
            contract = scoped_row(conn, context, "contract_revisions", view["contract_revision_id"])
            definition = self.registry.get(action["kind"])
            proposal = ActionProposal.from_dict(action["proposal"])
            if definition.fingerprint != action["tool_definition_digest"]:
                raise StateMismatchError(
                    "Installed tool definition changed after proposal creation."
                )
            definition.admit(proposal)
            if action.get("stdin_artifact_id") is not None and not definition.accepts_stdin:
                raise ScopeDeniedError("The installed tool cannot consume this input artifact.")
            self.capabilities.authorize(definition, contract["content"])
            self._paths(Path(candidate["path"]), proposal)
            reservation = scoped_row(conn, context, "budget_reservations", reservation_id)
            if (
                reservation["episode_id"] != action["episode_id"]
                or reservation["status"] != "reserved"
            ):
                raise StateMismatchError("A live task reservation is required before dispatch.")
            amounts = reservation["amounts"]
            if amounts.get("calls", 0) < 1 or amounts.get("tool_ms", 0) < definition.timeout_ms:
                raise StateMismatchError("Reservation does not cover the tool's call and deadline.")
            if not set(amounts).issubset({"calls", "tool_ms", "concurrency"}):
                raise InvalidInputError(
                    "A local tool reservation accepts calls, tool_ms and concurrency."
                )
            incomplete = conn.execute(
                "SELECT 1 FROM action_dependencies d WHERE d.workspace_id=%s AND d.action_id=%s "
                "AND NOT EXISTS (SELECT 1 FROM action_runs r WHERE r.workspace_id=d.workspace_id "
                "AND r.action_id=d.dependency_id AND r.status='completed') LIMIT 1",
                (context.workspace_id, action_id),
            ).fetchone()
            if incomplete:
                raise StateMismatchError("An action dependency has not completed successfully.")
            busy = conn.execute(
                "SELECT id FROM action_runs WHERE workspace_id=%s AND candidate_id=%s "
                "AND status IN ('proposed','running','indeterminate')",
                (context.workspace_id, lease.candidate_id),
            ).fetchone()
            if busy:
                raise StateMismatchError("Candidate has an active or unresolved operation.")
            run_id = uuid4()
            row = conn.execute(
                "INSERT INTO action_runs(workspace_id,id,episode_id,action_id,candidate_id,contract_revision_id,"
                "input_state_id,reservation_id,lease_owner,lease_generation,input_fingerprint,owner_instance,status,started_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',clock_timestamp()) RETURNING *",
                (
                    context.workspace_id,
                    run_id,
                    action["episode_id"],
                    action_id,
                    lease.candidate_id,
                    view["contract_revision_id"],
                    state["id"],
                    reservation_id,
                    lease.owner,
                    lease.generation,
                    actual,
                    self.instance_id,
                ),
            ).fetchone()
            append_event(
                conn,
                context,
                "tool_dispatch_intent",
                episode_id=action["episode_id"],
                run_id=run_id,
                node_id=node["id"],
                details={
                    "action_id": str(action_id),
                    "reservation_id": str(reservation_id),
                    "lease_generation": lease.generation,
                },
            )
            return dict(row), definition, proposal, candidate

    def _cancelled(self, context: WorkspaceContext, run: dict[str, Any], lease: Lease) -> bool:
        try:
            with self.database.transaction(context) as conn:
                task = conn.execute(
                    "SELECT status,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, run["episode_id"]),
                ).fetchone()
                if (
                    task is None
                    or task["status"] != "running"
                    or task["active_contract_id"] != run["contract_revision_id"]
                ):
                    return True
                assert_lease(conn, context, lease)
                current = scoped_row(conn, context, "action_runs", run["id"])
                return bool(
                    current["status"] != "running" or current["owner_instance"] != self.instance_id
                )
        except DomainError:
            return True

    @staticmethod
    def _check_secret_files(root: Path, redactor: CaptureRedactor) -> None:
        if not redactor.secrets and not redactor.reject_patterns:
            return
        total, entries = 0, 0
        for directory, directories, names in os.walk(root, followlinks=False):
            for name in (*directories, *names):
                path = Path(directory) / name
                entries += 1
                if entries > 20_000:
                    raise CaptureRedactionError("Secret-bearing workspace has too many entries.")
                metadata = str(path.relative_to(root)).encode()
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    metadata += b"\x00" + os.readlink(path).encode()
                if redactor.redact_bytes(metadata) != metadata:
                    raise CaptureRedactionError(
                        "Result path metadata contains supplied secret material."
                    )
                if stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise CaptureRedactionError("Result contains an unsupported special file.")
                total += info.st_size
                if total > 64 * 1024**2:
                    raise CaptureRedactionError(
                        "Secret-bearing workspace exceeds publication scan limit."
                    )
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise CaptureRedactionError("Result changed during secret scan.")
                    data = stream.read(info.st_size + 1)
                if len(data) != info.st_size:
                    raise CaptureRedactionError("Result changed during secret scan.")
                if redactor.redact_bytes(data) != data:
                    raise CaptureRedactionError("A result file contains supplied secret material.")

    def execute(
        self,
        context: WorkspaceContext,
        action_id: UUID,
        lease: Lease,
        reservation_id: UUID,
        *,
        after_receipt: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        # A completed action is a durable replay, not a second process invocation.
        prior = self.store.inspect(context, action_id)
        if prior["run"] is not None:
            if prior["run"]["reservation_id"] != str(reservation_id) or prior["run"][
                "candidate_id"
            ] != str(lease.candidate_id):
                raise StateMismatchError("Replay inputs differ from the original action run.")
            if prior["completion"] is not None:
                self._settle_receipt(context, UUID(prior["run"]["id"]))
                return self.store.inspect(context, action_id)
            raise IndeterminateExecutionError(
                "An action attempt exists without a confirmed completion."
            )
        definition = self.registry.get(prior["action"]["kind"])
        stdin_id = prior["action"].get("stdin_artifact_id")
        stdin = b""
        if stdin_id is not None:
            if not definition.accepts_stdin:
                raise ScopeDeniedError("The installed tool does not permit standard input.")
            stdin = self.artifacts.read(context, UUID(stdin_id))
            if len(stdin) > 16_777_216:
                raise InvalidInputError("Tool input exceeds its bounded input limit.")
        # Secret material is resolved only from installed definitions and controller grants.
        material = self.secrets.resolve(
            dict(definition.secret_refs),
            permitted_references=frozenset(self.capabilities.allowed_secret_refs),
            permitted_environment=frozenset(name for name, _ in definition.secret_refs),
        )
        run, definition, proposal, candidate = self._admit(
            context, action_id, lease, reservation_id
        )
        try:
            root = Path(candidate["path"])
            sandbox_environment: dict[str, str] = {}
            writable_roots: tuple[Path, ...] = ()
            read_only_roots = self.read_only_roots
            sandbox_root = root
            if self.workspace_sandbox is not None:
                sandbox = self.workspace_sandbox
                sandbox.confine(root, area="candidates", must_exist=True)
                sandbox_root = sandbox.root
                sandbox_environment = sandbox.runtime_environment()
                writable_roots = tuple(
                    sandbox.path(name)
                    for name in (
                        "tmp",
                        "cache",
                        "environments",
                        "builds",
                        "tests",
                        "tools",
                        "dependencies",
                    )
                )
                read_only_roots = (
                    *read_only_roots,
                    sandbox.path("models"),
                )
            request = ProcessRequest(
                argv=(*definition.argv_prefix, *proposal.arguments),
                cwd=self._paths(root, proposal),
                workspace_root=root,
                sandbox_root=sandbox_root,
                timeout_ms=definition.timeout_ms,
                max_output_bytes=definition.max_output_bytes,
                environment={
                    **sandbox_environment,
                    **dict(definition.environment),
                    **material.environment,
                },
                read_only_roots=read_only_roots,
                writable_roots=writable_roots,
                stdin=stdin,
                memory_bytes=definition.memory_bytes or 4 * 1024**3,
                cpu_seconds=definition.cpu_seconds or 60,
            )
            result = self.supervisor.run(
                request,
                cancelled=lambda: self._cancelled(context, run, lease),
                redactor=material.redactor,
            )
            if not result.processes_stopped:
                raise IndeterminateExecutionError(
                    "Process termination is unconfirmed; completion cannot be published."
                )
            # Metadata and both output streams have passed redaction before any durable publication.
            receipt = {
                "schema_version": 1,
                "workspace_id": str(context.workspace_id),
                "run_id": str(run["id"]),
                "action_id": str(action_id),
                "candidate_id": str(lease.candidate_id),
                "lease_owner": lease.owner,
                "lease_generation": lease.generation,
                "input_fingerprint": run["input_fingerprint"],
                "result_fingerprint": self._fingerprint(context, candidate),
                "outcome": result.outcome,
                "exit_code": result.exit_code,
                "elapsed_ms": result.elapsed_ms,
                "stdout": base64.b64encode(result.stdout).decode(),
                "stderr": base64.b64encode(result.stderr).decode(),
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "dispatched": result.dispatched,
                "isolation_identity": result.isolation_identity,
                "error_code": result.error_code,
                "retryable": result.retryable,
                "processes_stopped": result.processes_stopped,
            }
            if stdin_id is not None:
                receipt["stdin_artifact_id"] = stdin_id
                receipt["stdin_digest"] = hashlib.sha256(stdin).hexdigest()
            self._check_secret_files(root, material.redactor)
            self.receipts.write(context.workspace_id, run["id"], receipt)
            if after_receipt is not None:
                after_receipt()
            self._complete(context, run, lease, receipt)
            return self.store.inspect(context, action_id)
        except BaseException:
            # A committed dispatch intent is never treated as proof of no side effects.
            self._uncertain(context, run, lease)
            raise

    def _uncertain(self, context: WorkspaceContext, run: dict[str, Any], lease: Lease) -> None:
        """Fence publication even when a controller fails between durable boundaries."""
        try:
            with self.database.transaction(context) as conn:
                lock_episode(conn, context, run["episode_id"])
                current = conn.execute(
                    "SELECT * FROM action_runs WHERE workspace_id=%s AND id=%s FOR UPDATE",
                    (context.workspace_id, run["id"]),
                ).fetchone()
                if current is None:
                    raise ScopeDeniedError("Execution run is unavailable.")
                completion = conn.execute(
                    "SELECT id FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                    (context.workspace_id, run["id"]),
                ).fetchone()
                if completion is not None:
                    return
                if current["status"] not in {"running", "indeterminate"}:
                    return
                conn.execute(
                    "UPDATE action_runs SET status='indeterminate' WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, run["id"]),
                )
                append_event(
                    conn,
                    context,
                    "tool_execution_indeterminate",
                    episode_id=run["episode_id"],
                    run_id=run["id"],
                    details={"reconciliation_required": True},
                )
            self.candidates.quarantine(context, lease)
            self.budgets.mark_indeterminate(
                context, run["reservation_id"], "Dispatched operation requires reconciliation."
            )
        except DomainError:
            # The already committed running intent and expiring lease remain fail-closed.
            # Recovery can inspect them when the database is reachable again.
            return

    def _validate_receipt(
        self,
        context: WorkspaceContext,
        run: dict[str, Any],
        receipt: dict[str, Any],
        candidate: dict[str, Any],
    ) -> None:
        checks = {
            "schema_version": 1,
            "workspace_id": str(context.workspace_id),
            "run_id": str(run["id"]),
            "action_id": str(run["action_id"]),
            "candidate_id": str(run["candidate_id"]),
            "lease_owner": run["lease_owner"],
            "lease_generation": run["lease_generation"],
            "input_fingerprint": run["input_fingerprint"],
            "processes_stopped": True,
        }
        if any(receipt.get(key) != value for key, value in checks.items()):
            raise StateMismatchError(
                "Receipt does not identify the original operation and stopped processes."
            )
        with self.database.transaction(context) as conn:
            action = scoped_row(conn, context, "actions", run["action_id"])
            stdin_id = action.get("stdin_artifact_id")
            if stdin_id is not None:
                artifact = scoped_row(conn, context, "artifacts", stdin_id)
                if (
                    receipt.get("stdin_artifact_id") != str(stdin_id)
                    or receipt.get("stdin_digest") != artifact["digest"]
                ):
                    raise StateMismatchError(
                        "Receipt does not match the frozen standard-input artifact."
                    )
        if self._fingerprint(context, candidate) != receipt.get("result_fingerprint"):
            raise StateMismatchError("Candidate contents do not match the observed completion.")
        if receipt.get("outcome") not in {
            "completed",
            "nonzero_exit",
            "timeout",
            "cancelled",
            "infrastructure_error",
            "inconclusive",
        }:
            raise InvalidInputError("Receipt has an unsupported result classification.")

    def _complete(
        self,
        context: WorkspaceContext,
        run: dict[str, Any],
        lease: Lease,
        receipt: dict[str, Any],
        *,
        recovered: bool = False,
    ) -> None:
        from psycopg.types.json import Jsonb

        candidate = self.candidates.check_lease(context, lease)
        self._validate_receipt(context, run, receipt, candidate)
        # Preserve the actual resulting bytes and state before calling a run complete.
        result_state = self.candidates.publish_state(context, lease)
        if result_state["content_digest"] != receipt["result_fingerprint"]:
            raise StateMismatchError("Result contents changed during completion publication.")
        stdout = base64.b64decode(receipt["stdout"], validate=True)
        stderr = base64.b64decode(receipt["stderr"], validate=True)
        with self.database.transaction(context) as conn:
            lock_episode(conn, context, run["episode_id"])
            assert_lease(conn, context, lease)
            current = conn.execute(
                "SELECT * FROM action_runs WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, run["id"]),
            ).fetchone()
            if current is None:
                raise ScopeDeniedError("Execution run is unavailable.")
            existing = conn.execute(
                "SELECT receipt_digest FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                (context.workspace_id, run["id"]),
            ).fetchone()
            if existing is not None:
                if existing["receipt_digest"] != content_hash(receipt):
                    raise StateMismatchError("Completion has a different recorded receipt.")
                return
            if (
                current["status"] not in {"running", "indeterminate"}
                or current["candidate_id"] != lease.candidate_id
            ):
                raise StateMismatchError("Execution publication was fenced.")
            if not recovered and (
                current["lease_generation"] != lease.generation
                or current["owner_instance"] != self.instance_id
            ):
                raise StateMismatchError(
                    "Execution publication belongs to another controller or generation."
                )
            if recovered:
                claim = conn.execute(
                    "SELECT 1 FROM run_recovery_claims WHERE workspace_id=%s AND run_id=%s "
                    "AND candidate_id=%s AND generation=%s AND owner=%s AND receipt_digest=%s",
                    (
                        context.workspace_id,
                        run["id"],
                        lease.candidate_id,
                        lease.generation,
                        lease.owner,
                        content_hash(receipt),
                    ),
                ).fetchone()
                if claim is None:
                    raise StateMismatchError(
                        "Recovery publication requires its durable fenced claim."
                    )
            stdout_record = self.artifacts.put_in_transaction(
                conn, context, stdout, "text/plain; charset=utf-8"
            )
            stderr_record = self.artifacts.put_in_transaction(
                conn, context, stderr, "text/plain; charset=utf-8"
            )
            # Recheck after filesystem publication; an expired lease cannot publish a DB completion.
            assert_lease(conn, context, lease)
            details = {
                key: receipt[key]
                for key in (
                    "stdout_truncated",
                    "stderr_truncated",
                    "dispatched",
                    "isolation_identity",
                    "error_code",
                    "retryable",
                )
            }
            details.update(
                {"recovered": recovered, "publication_lease_generation": lease.generation}
            )
            conn.execute(
                "INSERT INTO action_completions(workspace_id,id,episode_id,run_id,outcome,exit_code,elapsed_ms,"
                "stdout_artifact_id,stderr_artifact_id,result_state_id,result_fingerprint,receipt_digest,details) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    uuid4(),
                    run["episode_id"],
                    run["id"],
                    receipt["outcome"],
                    receipt["exit_code"],
                    receipt["elapsed_ms"],
                    stdout_record["id"],
                    stderr_record["id"],
                    result_state["id"],
                    receipt["result_fingerprint"],
                    content_hash(receipt),
                    Jsonb(details),
                ),
            )
            conn.execute(
                "UPDATE action_runs SET status=%s,finished_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (receipt["outcome"], context.workspace_id, run["id"]),
            )
            action = scoped_row(conn, context, "actions", run["action_id"])
            append_event(
                conn,
                context,
                "tool_observation",
                episode_id=run["episode_id"],
                run_id=run["id"],
                node_id=action["node_id"],
                producer_kind="tool",
                producer_id="potbot.local-tool-gateway",
                payload_id=stdout_record["id"],
                details={
                    **details,
                    "outcome": receipt["outcome"],
                    "exit_code": receipt["exit_code"],
                    "stderr_artifact_id": str(stderr_record["id"]),
                    "actual_input_state_id": str(run["input_state_id"]),
                    "result_state_id": str(result_state["id"]),
                },
            )
            if recovered:
                conn.execute(
                    "INSERT INTO run_reconciliations(workspace_id,id,episode_id,run_id,decision,observation) "
                    "VALUES(%s,%s,%s,%s,'recognized_completion',%s)",
                    (
                        context.workspace_id,
                        uuid4(),
                        run["episode_id"],
                        run["id"],
                        Jsonb(
                            {
                                "receipt_digest": content_hash(receipt),
                                "result_fingerprint": receipt["result_fingerprint"],
                                "original_generation": run["lease_generation"],
                                "recovery_generation": lease.generation,
                            }
                        ),
                    ),
                )
        self._settle_receipt(context, run["id"])

    def _settle_receipt(self, context: WorkspaceContext, run_id: UUID) -> None:
        with self.database.transaction(context) as conn:
            run = scoped_row(conn, context, "action_runs", run_id)
            completion = conn.execute(
                "SELECT * FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                (context.workspace_id, run_id),
            ).fetchone()
            reservation = scoped_row(conn, context, "budget_reservations", run["reservation_id"])
        if completion is None or reservation["status"] in {"settled", "released"}:
            return
        if (
            not completion["details"]["dispatched"]
            and completion["elapsed_ms"] == 0
            and reservation["status"] == "reserved"
        ):
            self.budgets.release(
                context,
                run["reservation_id"],
                "Confirmed tool process was never started.",
                confirmed_not_started=True,
            )
            return
        actual = {
            key: value
            for key, value in {
                "calls": int(completion["details"]["dispatched"]),
                "tool_ms": completion["elapsed_ms"],
            }.items()
            if key in reservation["amounts"]
        }
        self.budgets.settle(context, run["reservation_id"], actual, "measured")

    def _claim_recovery(
        self, context: WorkspaceContext, run: dict[str, Any], receipt: dict[str, Any]
    ) -> Lease:
        """Retire the proven stopped generation and claim publication atomically.

        A recovery claim never authorizes another command. A crashed claimant can
        be replaced only after its lease expires or it explicitly quarantines.
        This keeps every crash point restartable without creating an unowned gap.
        """
        candidate = self.candidates.get(context, run["candidate_id"])
        owner = f"recovery:{self.instance_id}:{uuid4()}"
        digest = content_hash(receipt)
        with self.database.transaction(context) as conn:
            lock_episode(conn, context, run["episode_id"])
            current_candidate = conn.execute(
                "SELECT * FROM candidate_workspaces WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, run["candidate_id"]),
            ).fetchone()
            prior = conn.execute(
                "SELECT *,expires_at>clock_timestamp() AS live FROM workspace_leases "
                "WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
                (context.workspace_id, run["candidate_id"]),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM action_runs WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, run["id"]),
            ).fetchone()
            if (
                current_candidate is None
                or current_candidate["status"] == "removed"
                or prior is None
                or current is None
                or current["status"] not in {"running", "indeterminate"}
            ):
                raise StateMismatchError("Operation is no longer eligible for recovery.")
            candidate["state_id"] = current_candidate["state_id"]
            self._validate_receipt(context, run, receipt, candidate)
            original = (
                prior["generation"] == run["lease_generation"]
                and prior["owner"] == run["lease_owner"]
            )
            if not original:
                previous_claim = conn.execute(
                    "SELECT 1 FROM run_recovery_claims WHERE workspace_id=%s AND run_id=%s "
                    "AND candidate_id=%s AND generation=%s AND owner=%s AND receipt_digest=%s",
                    (
                        context.workspace_id,
                        run["id"],
                        run["candidate_id"],
                        prior["generation"],
                        prior["owner"],
                        digest,
                    ),
                ).fetchone()
                if previous_claim is None:
                    raise StateMismatchError("Candidate belongs to an unrelated lease generation.")
                if prior["status"] == "active" and prior["live"]:
                    raise StateMismatchError("A recovery publisher still holds an active lease.")
            generation = prior["generation"] + 1
            conn.execute(
                "INSERT INTO workspace_reconciliations(workspace_id,id,candidate_id,generation,"
                "principal_id,observed_digest,reason,processes_stopped) VALUES(%s,%s,%s,%s,%s,%s,%s,true)",
                (
                    context.workspace_id,
                    uuid4(),
                    run["candidate_id"],
                    prior["generation"],
                    context.principal_id,
                    receipt["result_fingerprint"],
                    "Private supervisor receipt confirms stopped execution; atomic recovery handoff.",
                ),
            )
            conn.execute(
                "INSERT INTO run_recovery_claims(workspace_id,id,episode_id,run_id,candidate_id,owner,"
                "from_generation,generation,receipt_digest,result_fingerprint) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    uuid4(),
                    run["episode_id"],
                    run["id"],
                    run["candidate_id"],
                    owner,
                    prior["generation"],
                    generation,
                    digest,
                    receipt["result_fingerprint"],
                ),
            )
            saved = conn.execute(
                "UPDATE workspace_leases SET owner=%s,generation=%s,status='active',"
                "expires_at=clock_timestamp()+interval '300 seconds',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND candidate_id=%s RETURNING expires_at",
                (owner, generation, context.workspace_id, run["candidate_id"]),
            ).fetchone()
            conn.execute(
                "UPDATE candidate_workspaces SET status='leased',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, run["candidate_id"]),
            )
            append_event(
                conn,
                context,
                "tool_recovery_claimed",
                episode_id=run["episode_id"],
                run_id=run["id"],
                details={"generation": generation, "receipt_digest": digest},
            )
            return Lease(run["candidate_id"], owner, generation, saved["expires_at"])

    def _release_completed_lease(self, context: WorkspaceContext, run: dict[str, Any]) -> None:
        """Finish a crash after completion commit without releasing a newer operation."""
        with self.database.transaction(context) as conn:
            lock_episode(conn, context, run["episode_id"])
            candidate = conn.execute(
                "SELECT * FROM candidate_workspaces WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, run["candidate_id"]),
            ).fetchone()
            lease = conn.execute(
                "SELECT * FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s FOR UPDATE",
                (context.workspace_id, run["candidate_id"]),
            ).fetchone()
            completion = conn.execute(
                "SELECT * FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                (context.workspace_id, run["id"]),
            ).fetchone()
            if (
                candidate is None
                or lease is None
                or completion is None
                or lease["status"] == "released"
                or candidate["state_id"] != completion["result_state_id"]
            ):
                return
            own = (
                lease["generation"] == run["lease_generation"]
                and lease["owner"] == run["lease_owner"]
            )
            if not own:
                own = (
                    conn.execute(
                        "SELECT 1 FROM run_recovery_claims WHERE workspace_id=%s AND run_id=%s "
                        "AND candidate_id=%s AND generation=%s AND owner=%s AND receipt_digest=%s",
                        (
                            context.workspace_id,
                            run["id"],
                            run["candidate_id"],
                            lease["generation"],
                            lease["owner"],
                            completion["receipt_digest"],
                        ),
                    ).fetchone()
                    is not None
                )
            busy = conn.execute(
                "SELECT 1 FROM action_runs WHERE workspace_id=%s AND candidate_id=%s "
                "AND status IN ('proposed','running','indeterminate') LIMIT 1",
                (context.workspace_id, run["candidate_id"]),
            ).fetchone()
            if not own or busy:
                return
            actual_candidate = self.candidates.get(context, run["candidate_id"])
            if self._fingerprint(context, actual_candidate) != completion["result_fingerprint"]:
                return
            conn.execute(
                "UPDATE workspace_leases SET status='released',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, run["candidate_id"]),
            )
            conn.execute(
                "UPDATE candidate_workspaces SET status='ready',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, run["candidate_id"]),
            )

    def reconcile(
        self,
        context: WorkspaceContext,
        run_id: UUID,
        *,
        after_claim: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Inspect a private completion receipt and current bytes; never rerun a command."""
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            run = scoped_row(conn, context, "action_runs", run_id)
            complete = conn.execute(
                "SELECT id FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                (context.workspace_id, run_id),
            ).fetchone()
        if complete:
            self._settle_receipt(context, run_id)
            self._release_completed_lease(context, run)
            return self.store.inspect(context, run["action_id"])
        candidate = self.candidates.get(context, run["candidate_id"])
        receipt = self.receipts.read(context.workspace_id, run_id)
        recognized = False
        if receipt is not None:
            try:
                self._validate_receipt(context, run, receipt, candidate)
                recognized = True
            except DomainError:
                pass
        with self.database.transaction(context) as conn:
            lease_row = conn.execute(
                "SELECT * FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, run["candidate_id"]),
            ).fetchone()
        if lease_row is None:
            raise StateMismatchError("Original workspace lease is unavailable.")
        original = Lease(
            run["candidate_id"],
            run["lease_owner"],
            run["lease_generation"],
            lease_row["expires_at"],
        )
        if not recognized:
            self._uncertain(context, run, original)
            with self.database.transaction(context) as conn:
                conn.execute(
                    "INSERT INTO run_reconciliations(workspace_id,id,episode_id,run_id,decision,observation) "
                    "VALUES(%s,%s,%s,%s,'unresolved',%s)",
                    (
                        context.workspace_id,
                        uuid4(),
                        run["episode_id"],
                        run_id,
                        Jsonb(
                            {
                                "receipt_present": receipt is not None,
                                "automatic_retry_permitted": False,
                            }
                        ),
                    ),
                )
            return {
                "run_id": str(run_id),
                "status": "indeterminate",
                "reconciliation": "unresolved",
                "automatic_retry_permitted": False,
            }
        assert receipt is not None
        recovered_lease = self._claim_recovery(context, run, receipt)
        try:
            if after_claim is not None:
                after_claim()
            self._complete(context, run, recovered_lease, receipt, recovered=True)
            self.candidates.release(context, recovered_lease)
        except BaseException:
            self._uncertain(context, run, recovered_lease)
            raise
        return self.store.inspect(context, run["action_id"])

    def resume(self, context: WorkspaceContext, episode_id: UUID) -> dict[str, Any]:
        """Reconstruct accounting and unresolved operations without scheduling a replay."""
        with self.database.transaction(context) as conn:
            lock_episode(conn, context, episode_id)
            rows = conn.execute(
                "SELECT id,status FROM action_runs WHERE workspace_id=%s AND episode_id=%s ORDER BY created_at,id",
                (context.workspace_id, episode_id),
            ).fetchall()
        unresolved: list[str] = []
        for row in rows:
            if row["status"] in {"running", "indeterminate"}:
                result = self.reconcile(context, row["id"])
                if result.get("reconciliation") == "unresolved":
                    unresolved.append(str(row["id"]))
            else:
                self._settle_receipt(context, row["id"])
                with self.database.transaction(context) as conn:
                    run = scoped_row(conn, context, "action_runs", row["id"])
                self._release_completed_lease(context, run)
        from potbot.semantics.releases import read_task_pin

        semantic_ready = True
        with self.database.transaction(context) as conn:
            task = lock_episode(conn, context, episode_id)
            try:
                read_task_pin(conn, context, episode_id, required=True)
            except CapabilityUnavailableError:
                semantic_ready = False
            pending = conn.execute(
                "SELECT id FROM action_runs WHERE workspace_id=%s AND episode_id=%s "
                "AND status IN ('proposed','running','indeterminate') ORDER BY created_at,id",
                (context.workspace_id, episode_id),
            ).fetchall()
            unresolved = [str(row["id"]) for row in pending]
        return {
            "episode_id": str(episode_id),
            "lifecycle": task["status"],
            "unresolved_runs": unresolved,
            "may_schedule": task["status"] == "running" and not unresolved and semantic_ready,
            "semantic_compatible": semantic_ready,
            "budget": self.budgets.inspect(context, episode_id),
        }
