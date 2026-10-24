"""Controller-mediated execution of installed debugger Cogs; transports stay private."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot import __version__
from potbot.cogs.contracts import (
    CogOutcome,
    CogReceipt,
    CogRunContext,
    CogSignature,
    TypedCogValue,
    check_cog_output,
)
from potbot.cogs.debugger import DEBUGGER_COGS, OPERATIONS, execute
from potbot.controller.cog_work import CogWorkMonitor
from potbot.debugging.contracts import Frame, Stop, ValueReference
from potbot.debugging.session import DebugSession
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
    ToolFailureError,
)
from potbot.domain.serialization import content_hash

Admission = Callable[[CogSignature, TypedCogValue, CogRunContext], None]

if TYPE_CHECKING:
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.artifacts import ArtifactStore
    from potbot.persistence.database import Database


class DebuggerCogRuntime:
    """Trusted host composition, never deserialized from a model/tool request.

    The host supplies already confined adapter pipes, authenticated work storage
    and a mandatory per-operation admission callback after task/lease/budget checks.
    Constructing a request or knowing a session reference does not authorize it.
    A live session is not resumable after controller loss: reconcile/close it,
    never restore OS/debugger objects from an artifact or replay a control effect.
    """

    def __init__(
        self,
        session: DebugSession,
        monitor: CogWorkMonitor,
        *,
        task_revision_id: UUID,
        repository_state_id: UUID,
        catalog_hash: str,
        admit: Admission,
    ) -> None:
        if not callable(admit) or monitor.actor.workspace_id != session.identity.workspace_id:
            raise ScopeDeniedError(
                "Debugger Cog requires matching authenticated workspace/admission"
            )
        self.session, self.monitor, self._admit = session, monitor, admit
        self.task_revision, self.repository_state = task_revision_id, repository_state_id
        self.catalog_hash = catalog_hash
        self.session_ref = "debug-session:" + uuid4().hex
        self._handles: dict[str, Stop | Frame | ValueReference] = {}
        self._seen: set[UUID] = set()
        self._lock = asyncio.Lock()
        self._active: asyncio.Task[Any] | None = None
        self._poisoned = False

    @classmethod
    def from_workspace(
        cls,
        session: DebugSession,
        *,
        database: Database,
        artifacts: ArtifactStore,
        actor: WorkspaceContext,
        snapshot_id: UUID,
        task_revision_id: UUID,
        repository_state_id: UUID,
        admit: Admission,
    ) -> DebuggerCogRuntime:
        """Bind real workspace storage and an imported immutable semantic snapshot.

        Database transactions authenticate actor; importing definitions never
        grants the task/target/lease rights checked by admit on every operation.
        """
        import json

        from potbot.cogs.registry import BoundCogRegistry
        from potbot.controller.execution_events import ExecutionEventService
        from potbot.domain.syn import SynSnapshot
        from potbot.semantics.service import SynService
        from potbot.workspace_sandbox import load_workspace_sandbox

        expected = BoundCogRegistry.debugger().snapshot.fingerprint
        sandbox = load_workspace_sandbox(database, actor)
        if (
            not session.identity.source_root.is_relative_to(sandbox.root)
            or session.isolation_identity is None
            or len(session.isolation_identity) != 64
        ):
            raise ScopeDeniedError(
                "Production debugger Cogs require the qualified workspace supervisor"
            )
        with database.transaction(actor) as connection:
            snapshot = SynService._snapshot(connection, actor, snapshot_id)
        source = json.loads(artifacts.read(actor, snapshot["artifact_id"]))["source"]
        if (
            snapshot["source_hash"] != expected
            or SynSnapshot.from_dict(source).fingerprint != expected
        ):
            raise ScopeDeniedError("Workspace lacks the exact installed debugger Cog snapshot")
        return cls(
            session,
            CogWorkMonitor(
                ExecutionEventService(database),
                artifacts,
                actor,
                emitter_run_id=session.identity.emitter_run_id,
                event_type_ref=session.identity.event_type_ref,
                event_type_version=session.identity.event_type_version,
            ),
            task_revision_id=task_revision_id,
            repository_state_id=repository_state_id,
            catalog_hash=expected,
            admit=admit,
        )

    def _issue(self, value: Stop | Frame | ValueReference) -> str:
        if len(self._handles) >= 4096:
            raise InvalidInputError("Debugger Cog handle budget exhausted")
        token = "debug-handle:" + uuid4().hex
        self._handles[token] = value
        return token

    def _handle(self, token: Any, expected: type[Any]) -> Any:
        if not isinstance(token, str) or len(token) > 128:
            raise InvalidInputError("Debugger handle must be an opaque bounded reference")
        value = self._handles.get(token)
        if not isinstance(value, expected):
            raise StateMismatchError("Debugger handle is foreign, forged or expired")
        return value

    def _stop(self, stop: Stop) -> dict[str, Any]:
        return {
            "stop_ref": self._issue(stop),
            "revision": stop.revision,
            "thread_id": stop.thread_id,
            "reason": stop.reason,
            "all_threads_stopped": stop.all_threads_stopped,
        }

    async def invoke(
        self, signature: CogSignature, source: TypedCogValue, context: CogRunContext
    ) -> CogOutcome:
        async with self._lock:
            if (
                self._poisoned
                or context.cog_snapshot_hash != self.catalog_hash
                or context.task_revision_id != self.task_revision
                or context.repository_state_id != self.repository_state
                or context.invocation_id in self._seen
                or len(self._seen) >= 4096
                or signature not in DEBUGGER_COGS
                or not signature.requires_controller_scope
            ):
                raise ScopeDeniedError("Debugger controller scope/revision/attempt is invalid")
            operation = OPERATIONS[DEBUGGER_COGS.index(signature)]
            request = source.value
            self._validate_request(operation, request)
            self.session.identity.verify_sources()
            # Callback consumes controller-loaded rights, never authority from source.value.
            admitted_hash = source.fingerprint
            try:
                self._admit(signature, source, context)
            except ScopeDeniedError:
                self.monitor.denied(signature, source, context)
                raise
            if source.fingerprint != admitted_hash:
                raise StateMismatchError("Admission cannot mutate typed Cog input")
            self._seen.add(context.invocation_id)
            work = self.monitor.begin(
                signature,
                source,
                context,
                implementation={
                    "adapter": self.session.identity.adapter,
                    "adapter_version": self.session.identity.adapter_version,
                    "potbot_version": __version__,
                    "isolation_identity": self.session.isolation_identity,
                    "runtime": self.session.identity.runtime,
                    "source_inventory_sha256": content_hash(
                        dict(self.session.identity.source_sha256)
                    ),
                },
            )
            task: asyncio.Task[dict[str, Any]] | None = None
            try:
                work.progress("admitted_dispatch")  # persisted before effect dispatch
                # Recheck revocation/fences after durable monitoring IO and before
                # dispatch. Admission must be idempotent for this invocation ID.
                self._admit(signature, source, context)
                if source.fingerprint != admitted_hash:
                    raise StateMismatchError("Admitted Cog input was mutated before dispatch")
                task = asyncio.create_task(execute(self, operation, source))
                self._active = task
                while not task.done():
                    await asyncio.wait({task}, timeout=1)
                    if not task.done():
                        work.progress("waiting_for_debugger")
                data = await task
                self._active = None
                output = TypedCogValue(
                    signature.output_type_ref,
                    {
                        "schema_version": 1,
                        "session_ref": self.session_ref,
                        "state": self.session.state.value,
                        "data": data,
                    },
                )
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
                            "session": self.session_ref,
                            "operation": operation,
                        }
                    ),
                )
                outcome = CogOutcome(output, receipt)
                work.implementation["target_process_id"] = self.session.target_process_id
                work.implementation["session_state"] = self.session.state.value
                work.complete(outcome)
                return outcome
            except BaseException as error:
                self._active = None
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                # Validation/known peer rejection can be retried as a *new* proposal.
                # Lost acknowledgement/storage/cancellation poisons this control lease.
                uncertain = not isinstance(
                    error,
                    (InvalidInputError, ScopeDeniedError, StateMismatchError, ToolFailureError),
                )
                self._poisoned = self._poisoned or uncertain
                try:
                    if not work.terminal:
                        work.fail(uncertain=uncertain, reason=type(error).__name__)
                except Exception:
                    self._poisoned = True
                    raise IndeterminateExecutionError(
                        "Cog work storage failed; reconcile target"
                    ) from error
                raise

    def _validate_request(self, operation: str, value: Any) -> None:
        optional = {"schema_version", "session_ref"}
        fields = {
            "initialize": set(),
            "launch": {"configuration"},
            "attach": {"configuration"},
            "set_breakpoints": {"source", "lines"},
            "configure_exceptions": {"filters"},
            "configuration_done": set(),
            "wait_stopped": {"after_revision"},
            "threads": set(),
            "stack": {"stop_ref", "start", "limit"},
            "scopes": {"frame_ref"},
            "variables": {"value_ref", "start", "limit"},
            "step_over": {"stop_ref"},
            "step_into": {"stop_ref"},
            "step_out": {"stop_ref"},
            "continue": {"stop_ref"},
            "pause": {"thread_id"},
            "exception_info": {"stop_ref"},
            "wait_terminated": set(),
            "detach": set(),
            "terminate": set(),
            "evaluate": {"frame_ref", "expression"},
        }
        if not isinstance(value, dict) or set(value) - (optional | fields[operation]):
            raise InvalidInputError("Debugger request has undeclared fields")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise InvalidInputError("Debugger request version must be 1")
        if operation != "initialize" and value.get("session_ref") != self.session_ref:
            raise ScopeDeniedError("Debugger request has no current workspace session")
        if operation in {"launch", "attach"} and not isinstance(value.get("configuration"), dict):
            raise InvalidInputError("Debugger target needs an admitted typed configuration")
        required = {
            "launch": {"configuration"},
            "attach": {"configuration"},
            "set_breakpoints": {"source", "lines"},
            "configure_exceptions": {"filters"},
            "stack": {"stop_ref"},
            "scopes": {"frame_ref"},
            "variables": {"value_ref"},
            "step_over": {"stop_ref"},
            "step_into": {"stop_ref"},
            "step_out": {"stop_ref"},
            "continue": {"stop_ref"},
            "pause": {"thread_id"},
            "exception_info": {"stop_ref"},
            "evaluate": {"frame_ref", "expression"},
        }.get(operation, set())
        if not required <= set(value):
            raise InvalidInputError("Debugger request is missing required fields")
        if "lines" in value and (
            not isinstance(value["lines"], list)
            or len(value["lines"]) > 128
            or any(type(x) is not int or not 1 <= x <= 2**31 - 1 for x in value["lines"])
        ):
            raise InvalidInputError("Debugger line breakpoints exceed their typed bounds")
        if "filters" in value and (
            not isinstance(value["filters"], list)
            or len(value["filters"]) > 16
            or any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in value["filters"])
        ):
            raise InvalidInputError("Debugger exception filters exceed their typed bounds")
        for key in ("source", "stop_ref", "frame_ref", "value_ref", "expression"):
            if key in value and (
                not isinstance(value[key], str) or not 1 <= len(value[key]) <= 4096
            ):
                raise InvalidInputError("Debugger text/reference field is invalid")
        for key in ("after_revision", "start", "limit", "thread_id"):
            maximum = 2**31 - 1 if key in {"thread_id", "after_revision"} else 4096
            if key in value and (type(value[key]) is not int or not 0 <= value[key] <= maximum):
                raise InvalidInputError("Debugger numeric field is invalid")

    async def dispatch(self, operation: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if asyncio.current_task() is not self._active or self._poisoned:
            raise ScopeDeniedError("Raw debugger handler dispatch requires an active admitted Cog")
        session = self.session
        if operation == "initialize":
            return {"operations": sorted(await session.initialize())}
        if operation in {"launch", "attach"}:
            await session.begin_target(value["configuration"], attach=operation == "attach")
            return {"status": "configuring", "target_process_id": session.target_process_id}
        if operation == "set_breakpoints":
            points = await session.set_breakpoints(value["source"], tuple(value["lines"]))
            return {
                "breakpoints": [
                    {
                        "requested_line": b.requested_line,
                        "verified": b.verified,
                        "actual_line": b.actual_line,
                    }
                    for b in points
                ]
            }
        if operation == "configure_exceptions":
            await session.configure_exceptions(tuple(value["filters"]))
            return {"configured": True}
        if operation == "configuration_done":
            await session.finish_configuration()
            return {"configured": True}
        if operation == "wait_stopped":
            return self._stop(
                await session.wait_stopped(after_revision=value.get("after_revision", 0))
            )
        if operation == "threads":
            return {
                "threads": [
                    {"id": identity, "name": name} for identity, name in await session.threads()
                ]
            }
        if operation == "stack":
            frames = await session.stack(
                self._handle(value["stop_ref"], Stop),
                start=value.get("start", 0),
                limit=value.get("limit", 32),
            )
            return {
                "frames": [
                    {
                        "frame_ref": self._issue(f),
                        "name": f.name,
                        "source": f.source,
                        "line": f.line,
                        "column": f.column,
                    }
                    for f in frames
                ]
            }
        if operation == "scopes":
            scopes = await session.scopes(self._handle(value["frame_ref"], Frame))
            return {
                "scopes": [
                    {
                        "name": s.name,
                        "value_ref": self._issue(s.reference),
                        "expensive": s.expensive,
                    }
                    for s in scopes
                ]
            }
        if operation == "variables":
            variables = await session.variables(
                self._handle(value["value_ref"], ValueReference),
                start=value.get("start", 0),
                limit=value.get("limit", 64),
            )
            return {
                "variables": [
                    {
                        "name": v.name,
                        "value": v.value,
                        "type": v.type_name,
                        "value_ref": self._issue(v.reference) if v.reference else None,
                    }
                    for v in variables
                ]
            }
        if operation in {"step_over", "step_into", "step_out", "continue"}:
            stop = self._handle(value["stop_ref"], Stop)
            self._handles.clear()
            if operation == "continue":
                await session.resume(stop)
                return {"status": "running"}
            return self._stop(await session.step(stop, operation))
        if operation == "pause":
            return self._stop(await session.pause(value["thread_id"]))
        if operation == "exception_info":
            return await session.exception_info(self._handle(value["stop_ref"], Stop))
        if operation == "wait_terminated":
            return {"exit_code": await session.wait_terminated()}
        if operation == "evaluate":
            return await session.evaluate(
                self._handle(value["frame_ref"], Frame), value["expression"]
            )
        if operation in {"detach", "terminate"}:
            self._handles.clear()
            await session.close(terminate_launched=operation == "terminate")
            return {
                "closed": True,
                "target_process_id": session.target_process_id,
                "target_exit_code": session.exit_code,
            }
        raise ScopeDeniedError("Debugger operation is not installed")
