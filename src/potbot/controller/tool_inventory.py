"""Read-only source inventory through the existing sealed Cog and work services."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot import __version__
from potbot.cogs.contracts import (
    CogOutcome,
    CogReceipt,
    CogRunContext,
    CogSignature,
    TypedCogValue,
    check_cog_output,
)
from potbot.cogs.inventory import SIGNATURE, execute
from potbot.controller.cog_work import CogWorkMonitor
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.tools.inventory import PROFILES, build_inventory
from potbot.tools.repository_snapshot import RepositorySnapshot

if TYPE_CHECKING:
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.artifacts import ArtifactStore
    from potbot.persistence.database import Database

Admission = Callable[[CogSignature, TypedCogValue, CogRunContext], None]


class ToolInventoryRuntime:
    def __init__(
        self,
        monitor: CogWorkMonitor,
        load: Callable[[], RepositorySnapshot],
        *,
        task_revision_id: UUID,
        repository_state_id: UUID,
        expected_digest: str,
        catalog_hash: str,
        admit: Admission,
    ) -> None:
        if (
            not callable(admit)
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise ScopeDeniedError("Inventory requires host-bound source and admission")
        self.monitor, self.load, self.admit = monitor, load, admit
        self.task_revision, self.repository_state = task_revision_id, repository_state_id
        self.expected_digest, self.catalog_hash = expected_digest, catalog_hash
        self._seen: set[UUID] = set()
        self._lock = asyncio.Lock()
        self._active: asyncio.Task[Any] | None = None
        self._progress: Callable[[str], None] = lambda _: None
        self._poisoned = False

    @classmethod
    def from_workspace(
        cls,
        *,
        database: Database,
        artifacts: ArtifactStore,
        actor: WorkspaceContext,
        snapshot_id: UUID,
        task_revision_id: UUID,
        repository_state_id: UUID,
        expected_digest: str,
        emitter_run_id: UUID,
        admit: Admission,
    ) -> ToolInventoryRuntime:
        from potbot.cogs.registry import BoundCogRegistry
        from potbot.controller.execution_events import ExecutionEventService
        from potbot.domain.syn import SynSnapshot
        from potbot.semantics.service import SynService
        from potbot.tools.repository import RepositoryStateStore

        expected = BoundCogRegistry.tool_inventory().snapshot.fingerprint
        with database.transaction(actor) as connection:
            imported = SynService._snapshot(connection, actor, snapshot_id)
        source = json.loads(artifacts.read(actor, imported["artifact_id"]))["source"]
        if (
            imported["source_hash"] != expected
            or SynSnapshot.from_dict(source).fingerprint != expected
        ):
            raise ScopeDeniedError("Workspace lacks exact installed inventory Cog snapshot")
        states = RepositoryStateStore(database, artifacts)
        return cls(
            CogWorkMonitor(
                ExecutionEventService(database),
                artifacts,
                actor,
                emitter_run_id=emitter_run_id,
                event_type_ref="synny://type/workflow-execution",
            ),
            lambda: states.snapshot(actor, repository_state_id),
            task_revision_id=task_revision_id,
            repository_state_id=repository_state_id,
            expected_digest=expected_digest,
            catalog_hash=expected,
            admit=admit,
        )

    async def invoke(
        self, signature: CogSignature, source: TypedCogValue, context: CogRunContext
    ) -> CogOutcome:
        async with self._lock:
            if (
                self._poisoned
                or signature != SIGNATURE
                or context.cog_snapshot_hash != self.catalog_hash
                or context.task_revision_id != self.task_revision
                or context.repository_state_id != self.repository_state
                or context.invocation_id in self._seen
                or len(self._seen) >= 4096
            ):
                raise ScopeDeniedError("Inventory scope, snapshot or invocation is invalid")
            value = source.value
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", "expected_content_digest", "profile"}
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or not isinstance(value["profile"], str)
                or value["profile"] not in PROFILES
                or value["expected_content_digest"] != self.expected_digest
            ):
                raise InvalidInputError(
                    "Inventory input/profile/source does not match admitted scope"
                )
            frozen_hash = source.fingerprint
            try:
                self.admit(signature, source, context)
            except ScopeDeniedError:
                self.monitor.denied(signature, source, context)
                raise
            if source.fingerprint != frozen_hash:
                raise StateMismatchError("Admission changed inventory input")
            self._seen.add(context.invocation_id)
            work = self.monitor.begin(
                signature,
                source,
                context,
                implementation={
                    "potbot_version": __version__,
                    "adapter": "immutable-source-inventory-v1",
                },
            )
            try:
                work.progress("admitted_dispatch")
                self.admit(signature, source, context)
                if source.fingerprint != frozen_hash:
                    raise StateMismatchError("Inventory input changed before dispatch")
                self._active = asyncio.current_task()
                self._progress = work.progress
                data = await execute(self, source)
                self._active = None
                output = TypedCogValue(signature.output_type_ref, data)
                check_cog_output(signature, output)
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
                    source.source_artifact_id,
                    context.upstream_receipt_hash,
                    content_hash(
                        {
                            "workspace": str(self.monitor.actor.workspace_id),
                            "principal": str(self.monitor.actor.principal_id),
                            "repository_state": str(self.repository_state),
                            "content_digest": self.expected_digest,
                        }
                    ),
                )
                outcome = CogOutcome(output, receipt)
                work.complete(outcome)
                return outcome
            except BaseException as error:
                uncertain = not isinstance(
                    error, (InvalidInputError, ScopeDeniedError, StateMismatchError)
                )
                self._poisoned |= uncertain
                try:
                    if not work.terminal:
                        work.fail(uncertain=uncertain, reason=type(error).__name__)
                except Exception:
                    self._poisoned = True
                    raise IndeterminateExecutionError(
                        "Inventory work storage failed; reconcile history"
                    ) from error
                raise
            finally:
                self._active = None
                self._progress = lambda _: None

    async def dispatch(self, source: TypedCogValue) -> dict[str, Any]:
        if self._active is None or asyncio.current_task() is not self._active:
            raise ScopeDeniedError("Direct inventory handler dispatch is forbidden")
        snapshot = self.load()
        if snapshot.content_digest != self.expected_digest:
            raise StateMismatchError("Inventory source drifted from the admitted repository state")
        return build_inventory(snapshot, profile=source.value["profile"], progress=self._progress)
