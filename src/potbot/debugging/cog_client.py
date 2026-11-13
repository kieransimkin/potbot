"""Typed caller for the existing bound Cog registry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.cogs.contracts import CogOutcome, CogRunContext, TypedCogValue
from potbot.cogs.debugger import cog_ref, signature
from potbot.cogs.registry import BoundCogRegistry
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.domain.errors import IndeterminateExecutionError

if TYPE_CHECKING:
    from potbot.debugging.syn_forwarding import SynDebuggerRoute


class DebuggerCogClient:
    def __init__(
        self,
        registry: BoundCogRegistry,
        controller: DebuggerCogRuntime | None = None,
        *,
        route: SynDebuggerRoute | None = None,
        task_revision: UUID | None = None,
        repository_state: UUID | None = None,
    ) -> None:
        self.registry = registry
        self.controller = controller
        self._invoke: Callable[[str, TypedCogValue, CogRunContext], Awaitable[CogOutcome]]
        if controller is not None and route is None:
            self.task_revision = controller.task_revision
            self.repository_state = controller.repository_state

            async def invoke_local(
                reference: str, source: TypedCogValue, context: CogRunContext
            ) -> CogOutcome:
                return await registry.invoke(reference, source, context, controller=controller)

            self._invoke = invoke_local
        elif controller is None and route is not None and task_revision and repository_state:
            self.task_revision = task_revision
            self.repository_state = repository_state
            self._invoke = route.invoke
        else:
            raise ValueError("Debugger needs one local or one pinned Syn route")
        self.session_ref: str | None = None
        self.history: list[CogOutcome] = []

    @classmethod
    def forwarded(
        cls,
        registry: BoundCogRegistry,
        route: SynDebuggerRoute,
        *,
        task_revision: UUID,
        repository_state: UUID,
    ) -> DebuggerCogClient:
        return cls(
            registry, route=route, task_revision=task_revision, repository_state=repository_state
        )

    async def call(self, operation: str, **parameters: Any) -> dict[str, Any]:
        selected = signature(operation)
        request = {"schema_version": 1, **parameters}
        if self.session_ref is not None:
            request["session_ref"] = self.session_ref
        context = CogRunContext(
            self.registry.snapshot.fingerprint,
            self.task_revision,
            self.repository_state,
            uuid4(),
            self.history[-1].receipt.fingerprint if self.history else None,
        )
        source = TypedCogValue(selected.input_type_ref, request)
        try:
            outcome = await self._invoke(cog_ref(operation), source, context)
        except IndeterminateExecutionError:
            self.session_ref = None
            raise
        self.history.append(outcome)
        self.session_ref = outcome.value.value["session_ref"]
        return dict(outcome.value.value["data"])
