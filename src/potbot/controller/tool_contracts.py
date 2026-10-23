"""Controller-owned canonical contract Cogs on the existing work ledger.

Search operates only on the bounded corpus supplied by the authenticated host.
Neither a serialized descriptor nor a successful contract check installs a tool.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from potbot import __version__
from potbot.cogs.contracts import (
    CogOutcome,
    CogReceipt,
    CogRunContext,
    CogSignature,
    TypedCogValue,
    check_cog_input,
)
from potbot.cogs.tool_contracts import CONTRACT_COGS, CONVERT, SEARCH, VALIDATE, execute
from potbot.controller.cog_work import CogWorkMonitor
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.semantic_contracts import ToolSemanticCatalog

Admission = Callable[[CogSignature, TypedCogValue, CogRunContext], None]


class ToolContractRuntime:
    def __init__(
        self,
        monitor: CogWorkMonitor,
        *,
        catalog: ToolSemanticCatalog,
        task_revision_id: UUID,
        repository_state_id: UUID,
        source_digest: str,
        corpus: tuple[dict[str, str], ...],
        admit: Admission,
    ) -> None:
        if not callable(admit) or not isinstance(corpus, tuple) or len(corpus) > 128:
            raise ScopeDeniedError("Contract runtime requires host admission and a bounded corpus")
        if len(source_digest) != 64 or any(c not in "0123456789abcdef" for c in source_digest):
            raise InvalidInputError("Contract runtime requires a pinned source digest")
        for row in corpus:
            if (
                set(row) != {"id", "text", "source_sha256"}
                or any(not isinstance(v, str) or not 1 <= len(v) <= 4096 for v in row.values())
                or len(row["source_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in row["source_sha256"])
            ):
                raise InvalidInputError("Search corpus entries must be source-bound text")
        if len({r["id"] for r in corpus}) != len(corpus):
            raise InvalidInputError("Search corpus identifiers must be unique")
        self.monitor, self.catalog, self.admit = monitor, catalog, admit
        self.task_revision, self.repository_state, self.source_digest = (
            task_revision_id,
            repository_state_id,
            source_digest,
        )
        self._corpus = canonical_json(sorted(corpus, key=lambda r: r["id"]))
        self._seen: set[UUID] = set()
        self._lock = asyncio.Lock()
        self._active: asyncio.Task[Any] | None = None
        self._progress: Callable[[str], None] = lambda _: None
        self._poisoned = False

    @classmethod
    def from_workspace(
        cls,
        *,
        database: Any,
        artifacts: Any,
        actor: Any,
        snapshot_id: UUID,
        task_revision_id: UUID,
        repository_state_id: UUID,
        emitter_run_id: UUID,
        corpus: tuple[dict[str, str], ...],
        admit: Admission,
    ) -> ToolContractRuntime:
        from potbot.cogs.registry import BoundCogRegistry
        from potbot.cogs.tool_contracts import contract_ember_bundle
        from potbot.controller.execution_events import ExecutionEventService
        from potbot.domain.syn import SynSnapshot
        from potbot.semantics.ember_service import EmberContractService
        from potbot.semantics.service import SynService
        from potbot.tools.repository import RepositoryStateStore

        installed = BoundCogRegistry.tool_contracts().snapshot
        with database.transaction(actor) as connection:
            imported = SynService._snapshot(connection, actor, snapshot_id)
        source = json.loads(artifacts.read(actor, imported["artifact_id"]))["source"]
        if (
            imported["source_hash"] != installed.fingerprint
            or SynSnapshot.from_dict(source).fingerprint != installed.fingerprint
        ):
            raise ScopeDeniedError(
                "Workspace lacks the exact installed canonical contract snapshot"
            )
        bundle = contract_ember_bundle(
            workspace_id=actor.workspace_id,
            snapshot_id=snapshot_id,
            namespace=imported["namespace"],
        )
        EmberContractService(database).import_bundle(actor, snapshot_id, bundle)
        state = RepositoryStateStore(database, artifacts).get(actor, repository_state_id)
        return cls(
            CogWorkMonitor(
                ExecutionEventService(database),
                artifacts,
                actor,
                emitter_run_id=emitter_run_id,
                event_type_ref="synny://type/workflow-execution",
            ),
            catalog=ToolSemanticCatalog(installed),
            task_revision_id=task_revision_id,
            repository_state_id=repository_state_id,
            source_digest=state["content_digest"],
            corpus=corpus,
            admit=admit,
        )

    async def invoke(
        self, signature: CogSignature, source: TypedCogValue, context: CogRunContext
    ) -> CogOutcome:
        async with self._lock:
            if (
                self._poisoned
                or signature not in CONTRACT_COGS
                or (
                    context.cog_snapshot_hash != self.catalog.snapshot.fingerprint
                    or context.task_revision_id != self.task_revision
                    or context.repository_state_id != self.repository_state
                    or context.invocation_id in self._seen
                    or len(self._seen) >= 4096
                )
            ):
                raise ScopeDeniedError(
                    "Canonical tool invocation differs from admitted installed scope"
                )
            check_cog_input(signature, source)
            self.catalog.validate(source)
            frozen_hash = source.fingerprint
            try:
                self.admit(signature, source, context)
            except ScopeDeniedError:
                self.monitor.denied(signature, source, context)
                raise
            if source.fingerprint != frozen_hash:
                raise StateMismatchError("Admission changed canonical input")
            self._seen.add(context.invocation_id)
            work = self.monitor.begin(
                signature,
                source,
                context,
                implementation={
                    "potbot_version": __version__,
                    "source_digest": self.source_digest,
                    "input_schema_hash": self.catalog.schema_hash(source.type_ref),
                    "output_schema_hash": self.catalog.schema_hash(signature.output_type_ref),
                    "adapter": "local-canonical-contracts-v1",
                },
            )
            try:
                work.progress("admitted_dispatch")
                self.admit(signature, source, context)
                if source.fingerprint != frozen_hash:
                    raise StateMismatchError("Canonical input changed before dispatch")
                self._active, self._progress = asyncio.current_task(), work.progress
                result = await execute(self, signature, source)
                self._active = None
                output = TypedCogValue(signature.output_type_ref, result)
                self.catalog.validate(output)
                work.progress("typed_result_validated")
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
                            "source": self.source_digest,
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
                        "Contract work storage failed; reconcile history"
                    ) from error
                raise
            finally:
                self._active = None
                self._progress = lambda _: None

    async def dispatch(self, signature: CogSignature, source: TypedCogValue) -> dict[str, Any]:
        if self._active is None or asyncio.current_task() is not self._active:
            raise ScopeDeniedError("Raw canonical handler dispatch requires an active admitted Cog")
        value = source.value
        if signature == SEARCH:
            corpus = json.loads(self._corpus)
            items = [r for r in corpus if value["text"].casefold() in r["text"].casefold()]
            self._progress("corpus_searched")
            return {
                "schema_version": 1,
                "query_hash": source.fingerprint,
                "corpus_hash": content_hash(corpus),
                "items": items[: value["limit"]],
                "complete": len(items) <= value["limit"],
            }
        if signature == VALIDATE:
            checked = self.catalog.decode(
                canonical_json({k: value[k] for k in ("type_ref", "schema_hash", "value")})
            )
            encoded = self.catalog.encode(checked)
            self._progress("contract_round_tripped")
            return {
                "schema_version": 1,
                "type_ref": checked.type_ref,
                "schema_hash": value["schema_hash"],
                "value_hash": checked.fingerprint,
                "canonical_json": encoded,
            }
        if signature != CONVERT:
            raise ScopeDeniedError("Canonical operation is not installed")
        if "loss" in value or any(r["source_start"] >= r["source_end"] for r in value["rows"]):
            raise InvalidInputError("Conversion needs original values and nonempty source spans")
        rows, losses = [], []
        with localcontext() as decimal_context:
            decimal_context.prec = 40
            factor = (
                Decimal(1)
                if value["unit"] == value["target_unit"]
                else Decimal(1000)
                if value["unit"] == "s"
                else Decimal("0.001")
            )
            for index, row in enumerate(value["rows"]):
                exact = Decimal(row["value"]) * factor
                rounded = exact.quantize(Decimal(1).scaleb(-value["precision"]))
                loss = abs(exact - rounded)
                if loss > Decimal(value["tolerance"]):
                    raise InvalidInputError("Conversion precision loss exceeds declared tolerance")
                rows.append({**row, "value": format(rounded, "f")})
                if loss:
                    losses.append({"row": index, "absolute": format(loss, "f")})
        self._progress("unit_table_converted")
        return {**value, "unit": value["target_unit"], "rows": rows, "loss": losses}
