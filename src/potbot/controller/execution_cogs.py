"""Host-owned work dispatch through pinned SYN Cogs and the shared work monitor."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogOutcome, CogReceipt, CogRunContext, CogSignature, TypedCogValue
from potbot.cogs.execution import EXECUTION_COGS, SEAL, WORK_COGS, execute
from potbot.controller.cog_work import CogWorkMonitor, CogWorkRun
from potbot.controller.execution_journal import PinnedExecutionJournal
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json
from potbot.tools.execution_contracts import EVENT, WORK_KINDS, ExecutionPolicy, WorkBinding, fields
from potbot.tools.semantic_contracts import ToolSemanticCatalog


@dataclass(frozen=True, slots=True)
class HostOperation:
    """In-process installation, supplied by trusted host code, never deserialized."""

    binding: WorkBinding
    handler: Callable[[ExecutionCall, TypedCogValue], Awaitable[TypedCogValue]]

    def __post_init__(self) -> None:
        if not callable(self.handler) or self.binding.kind not in WORK_KINDS:
            raise ScopeDeniedError("Work requires an installed host handler")
        if self.binding.parent_event_id or self.binding.links:
            raise ScopeDeniedError("Causation belongs to an invocation, not an installation")


class ExecutionCall:
    """Trusted callback context; child work returns through the same Cog boundary."""

    def __init__(self, runtime: ExecutionCogRuntime, work: CogWorkRun, depth: int) -> None:
        self.runtime, self.work, self.depth = runtime, work, depth
        self.owner = asyncio.current_task()

    def _check(self) -> None:
        if (
            self.work.terminal
            or asyncio.current_task() is not self.owner
            or self.runtime._live.get(self.work.context.invocation_id) is not self
        ):
            raise ScopeDeniedError("Work callbacks are confined to their admitted live task")

    def progress(self, phase: str) -> None:
        self._check()
        if (
            not isinstance(phase, str)
            or not 1 <= len(phase) <= 64
            or any(ord(c) < 33 for c in phase)
        ):
            raise InvalidInputError("Progress phase must be a bounded identifier")
        self.work.progress(phase)

    async def child(
        self, kind: str, operation_ref: str, value: TypedCogValue, *, links: tuple[UUID, ...] = ()
    ) -> CogOutcome:
        self._check()
        if self.depth >= 16 or len(self.work.child_runs) >= 32 or kind not in WORK_KINDS:
            raise ScopeDeniedError("Nested work exceeds its bounded installed scope")
        signature = WORK_COGS[WORK_KINDS.index(kind)]
        source = TypedCogValue(
            signature.input_type_ref,
            {
                "schema_version": 1,
                "operation_ref": operation_ref,
                "input": {"type_ref": value.type_ref, "value": value.value},
            },
        )
        return await self._child(signature, source, links)

    async def child_cog(
        self, cog_ref: str, source: TypedCogValue, *, links: tuple[UUID, ...] = ()
    ) -> CogOutcome:
        """Invoke a fixed installed tool through the same parent/admission boundary."""
        self._check()
        signature = next(
            (s for s in self.runtime._direct_operations if str(s.cog_ref) == cog_ref), None
        )
        if signature is None:
            raise ScopeDeniedError("Child Cog is not installed in this host profile")
        return await self._child(signature, source, links)

    async def _child(
        self, signature: CogSignature, source: TypedCogValue, links: tuple[UUID, ...]
    ) -> CogOutcome:
        self._check()
        if self.depth >= 16 or len(self.work.child_runs) >= 32:
            raise ScopeDeniedError("Nested work exceeds its bounded installed scope")
        self.progress("child_dispatch")
        assert self.work.previous is not None
        context = replace(self.work.context, invocation_id=uuid4(), upstream_receipt_hash=None)
        outcome = await self.runtime._invoke(
            signature,
            source,
            context,
            parent_event_id=UUID(self.work.previous["id"]),
            links=links,
            depth=self.depth + 1,
        )
        self.work.child_runs.append(
            {
                "run_id": str(context.invocation_id),
                "status": "completed",
                "receipt_hash": outcome.receipt.fingerprint,
            }
        )
        self.progress("child_completed")
        return outcome


class ExecutionCogRuntime:
    def __init__(
        self,
        *,
        database: Any,
        artifacts: Any,
        actor: Any,
        snapshot_id: UUID,
        task_revision_id: UUID,
        repository_state_id: UUID,
        emitter_run_id: UUID,
        policy: ExecutionPolicy,
        operations: tuple[HostOperation, ...],
        admit: Callable[[CogSignature, TypedCogValue, CogRunContext], None],
        receipts: Any = None,
        artifact_ranges: Any = None,
        artifact_uploads: Any = None,
    ) -> None:
        from potbot.cogs.execution import execution_ember_bundle
        from potbot.cogs.registry import BoundCogRegistry
        from potbot.domain.syn import SynSnapshot
        from potbot.semantics.ember_service import EmberContractService
        from potbot.semantics.service import SynService
        from potbot.tools.repository import RepositoryStateStore

        if not callable(admit) or not isinstance(operations, tuple) or len(operations) > 128:
            raise ScopeDeniedError(
                "Execution needs independent host admission and bounded installations"
            )
        if artifact_uploads is not None and artifact_ranges is None:
            raise ScopeDeniedError("Upload profile requires its matching range service")
        if artifact_ranges is not None:
            from potbot.cogs.artifact_ranges import (
                artifact_range_ember_bundle,
                installed_operations,
            )
            from potbot.controller.artifact_ranges import ArtifactRangeService

            if (
                not isinstance(artifact_ranges, ArtifactRangeService)
                or artifact_ranges.actor != actor
                or artifact_ranges.policy != policy
                or artifact_ranges.db is not database
                or artifact_ranges.artifacts is not artifacts
            ):
                raise ScopeDeniedError("Artifact service belongs to another host scope")
            self.registry = BoundCogRegistry.artifact_ranges()
            ember_bundle = artifact_range_ember_bundle
            self._direct_operations = installed_operations(artifact_ranges, policy)
            if artifact_uploads is not None:
                from potbot.cogs.artifact_uploads import artifact_upload_ember_bundle
                from potbot.cogs.artifact_uploads import installed_operations as upload_operations
                from potbot.controller.artifact_uploads import ArtifactUploadService

                if (
                    not isinstance(artifact_uploads, ArtifactUploadService)
                    or artifact_uploads.actor != actor
                    or artifact_uploads.policy != policy
                    or artifact_uploads.db is not database
                    or artifact_uploads.artifacts is not artifacts
                ):
                    raise ScopeDeniedError("Upload service belongs to another host scope")
                self.registry = BoundCogRegistry.artifact_uploads()
                ember_bundle = artifact_upload_ember_bundle
                self._direct_operations.update(upload_operations(artifact_uploads, policy))
        else:
            self.registry = BoundCogRegistry.provenance()
            ember_bundle = execution_ember_bundle
            self._direct_operations = {}
        with database.transaction(actor) as connection:
            sealed = SynService._snapshot(connection, actor, snapshot_id)
        imported = SynSnapshot.from_dict(
            json.loads(artifacts.read(actor, sealed["artifact_id"]))["source"]
        )
        state = RepositoryStateStore(database, artifacts).get(actor, repository_state_id)
        if (
            imported.fingerprint != self.registry.snapshot.fingerprint
            or sealed["source_hash"] != imported.fingerprint
            or state["content_digest"] != policy.source_digest
        ):
            raise ScopeDeniedError(
                "Execution requires its exact sealed catalog and repository revision"
            )
        EmberContractService(database).import_bundle(
            actor,
            snapshot_id,
            ember_bundle(
                workspace_id=actor.workspace_id,
                snapshot_id=snapshot_id,
                namespace=sealed["namespace"],
            ),
        )
        self.db, self.artifacts, self.actor, self.snapshot_id = (
            database,
            artifacts,
            actor,
            snapshot_id,
        )
        self.task_revision, self.repository_state, self.emitter = (
            task_revision_id,
            repository_state_id,
            emitter_run_id,
        )
        self.policy, self.admit, self.receipts = policy, admit, receipts
        if receipts is not None and (receipts.actor != actor or receipts.policy != policy):
            raise ScopeDeniedError("Receipt service belongs to another actor or policy revision")
        self.catalog = ToolSemanticCatalog(imported)
        self.operations = {(op.binding.kind, op.binding.operation_ref): op for op in operations}
        if len(self.operations) != len(operations) or any(
            op.binding.policy != policy for op in operations
        ):
            raise ScopeDeniedError("Duplicate or differently pinned host installations")
        for op in (*operations, *self._direct_operations.values()):
            self.catalog.schema_hash(op.binding.input_type_ref)
            self.catalog.schema_hash(op.binding.output_type_ref)
        self._seen: set[UUID] = set()
        self._live: dict[UUID, ExecutionCall] = {}
        self._poisoned = False

    def _admission(
        self, signature: CogSignature, source: TypedCogValue, context: CogRunContext
    ) -> None:
        checked = TypedCogValue(
            source.type_ref, json.loads(canonical_json(source.value)), source.source_artifact_id
        )
        self.admit(signature, checked, context)
        if checked.fingerprint != source.fingerprint:
            raise StateMismatchError("Admission changed the supplied work input")

    async def invoke(
        self, signature: CogSignature, source: TypedCogValue, context: CogRunContext
    ) -> CogOutcome:
        return await self._invoke(signature, source, context)

    async def _invoke(
        self,
        signature: CogSignature,
        source: TypedCogValue,
        context: CogRunContext,
        *,
        parent_event_id: UUID | None = None,
        links: tuple[UUID, ...] = (),
        depth: int = 0,
    ) -> CogOutcome:
        if (
            signature not in (*EXECUTION_COGS, *self._direct_operations)
            or (self._poisoned and signature in (*WORK_COGS, *self._direct_operations))
            or context.cog_snapshot_hash != self.registry.snapshot.fingerprint
            or context.task_revision_id != self.task_revision
            or context.repository_state_id != self.repository_state
            or context.invocation_id in self._seen
            or len(self._seen) >= 4096
            or source.type_ref != signature.input_type_ref
        ):
            raise ScopeDeniedError("Execution differs from the admitted one-use Cog scope")
        source = TypedCogValue(
            source.type_ref, json.loads(canonical_json(source.value)), source.source_artifact_id
        )
        self.catalog.validate(source)
        op = None
        if signature in WORK_COGS:
            request = fields(source.value, {"schema_version", "operation_ref", "input"})
            kind = WORK_KINDS[WORK_COGS.index(signature)]
            if not isinstance(request["operation_ref"], str):
                raise InvalidInputError("Operation requires its installed native identity")
            op = self.operations.get((kind, request["operation_ref"]))
            if op is None:
                raise ScopeDeniedError("No host installation for this kind and operation")
            payload = request["input"]
            if (
                not isinstance(payload, dict)
                or set(payload) != {"type_ref", "value"}
                or payload["type_ref"] != op.binding.input_type_ref
            ):
                raise InvalidInputError("Work input differs from the installed semantic port")
            typed = TypedCogValue(payload["type_ref"], json.loads(canonical_json(payload["value"])))
            typed_hash = typed.fingerprint
            self.catalog.validate(typed)
            binding = replace(op.binding, parent_event_id=parent_event_id, links=links)
        elif signature in self._direct_operations:
            op = self._direct_operations[signature]
            kind, typed, typed_hash = op.binding.kind, source, source.fingerprint
            binding = replace(op.binding, parent_event_id=parent_event_id, links=links)
        else:
            if self.receipts is None or parent_event_id is not None:
                raise ScopeDeniedError(
                    "Receipt Cogs need their host-owned service and independent root"
                )
            kind = "seal" if signature == SEAL else "replay"
            binding = WorkBinding(
                kind,
                f"syn://thing/potbot-work-{kind}-v1",
                signature.input_type_ref,
                signature.output_type_ref,
                signature.binding_hash,
                "read_only",
                self.policy,
            )
        journal = PinnedExecutionJournal(
            self.db,
            self.artifacts,
            self.actor,
            snapshot_id=self.snapshot_id,
            signature=signature,
            source=source,
            context=context,
            binding=binding,
            emitter=self.emitter,
        )
        monitor = CogWorkMonitor(
            journal, self.artifacts, self.actor, emitter_run_id=self.emitter, event_type_ref=EVENT
        )
        original_hash = source.fingerprint
        self._seen.add(context.invocation_id)
        try:
            self._admission(signature, source, context)
        except ScopeDeniedError:
            try:
                monitor.denied(signature, source, context)
            except Exception:
                self._poisoned = True
                raise
            raise
        if source.fingerprint != original_hash:
            raise StateMismatchError("Admission changed the frozen work input")
        try:
            work = monitor.begin(signature, source, context, implementation=binding.to_dict())
        except Exception:
            self._poisoned = True
            raise
        dispatched = False
        try:
            work.progress("admitted_dispatch")
            self._admission(signature, source, context)
            if source.fingerprint != original_hash:
                raise StateMismatchError("Work input changed before dispatch")
            dispatched = True
            call = ExecutionCall(self, work, depth)
            if op:
                # The timeout task owns callbacks; no detached task inherits its grant.
                async def run() -> TypedCogValue:
                    call.owner = asyncio.current_task()
                    self._live[context.invocation_id] = call
                    try:
                        return await execute(call, op.handler, typed)
                    finally:
                        self._live.pop(context.invocation_id, None)

                result = await asyncio.wait_for(run(), timeout=self.policy.max_wall_ms / 1000)
                if typed.fingerprint != typed_hash:
                    raise StateMismatchError("Handler mutated its pinned semantic input")
                if (
                    not isinstance(result, TypedCogValue)
                    or result.type_ref != op.binding.output_type_ref
                ):
                    raise StateMismatchError("Handler returned a different semantic port")
                self.catalog.validate(result)
                value = (
                    result.value
                    if signature in self._direct_operations
                    else {
                        "schema_version": 1,
                        "operation_ref": op.binding.operation_ref,
                        "result": {"type_ref": result.type_ref, "value": result.value},
                        "work_run_id": str(context.invocation_id),
                        "status": "completed",
                    }
                )
            else:

                async def inspect(call: ExecutionCall, request: TypedCogValue) -> TypedCogValue:
                    call.progress("receipt_inspection")
                    value = (
                        self.receipts.seal(request.value)
                        if signature == SEAL
                        else self.receipts.replay(request.value)
                    )
                    return TypedCogValue(signature.output_type_ref, value)

                self._live[context.invocation_id] = call
                try:
                    value = (await execute(call, inspect, source)).value
                finally:
                    self._live.pop(context.invocation_id, None)
            if len(canonical_json(value).encode("utf-8")) > self.policy.max_output_bytes:
                raise InvalidInputError("Inline work output exceeds the admitted byte limit")
            work.progress("typed_result_validated")
            output = TypedCogValue(signature.output_type_ref, value)
            self.catalog.validate(output)
            receipt = CogReceipt(
                str(signature.cog_ref),
                signature.cog_type_ref,
                signature.binding_hash,
                context.cog_snapshot_hash,
                context.task_revision_id,
                context.repository_state_id,
                context.invocation_id,
                source.type_ref,
                source.fingerprint,
                output.type_ref,
                output.fingerprint,
                UUID(str(work.input["id"])),
                context.upstream_receipt_hash,
                journal.scope_hash,
            )
            outcome = CogOutcome(output, receipt)
            work.complete(outcome)
            return outcome
        except BaseException as error:
            uncertain = dispatched and (
                binding.effect_class != "read_only"
                or isinstance(error, IndeterminateExecutionError)
            )
            self._poisoned |= uncertain
            try:
                if not work.terminal:
                    work.fail(
                        uncertain=uncertain,
                        cancelled=isinstance(error, asyncio.CancelledError) and not uncertain,
                        reason=type(error).__name__,
                    )
            except Exception:
                self._poisoned = True
                raise IndeterminateExecutionError(
                    "Work monitoring failed; reconcile the durable invocation"
                ) from error
            raise
