"""Typed caller for the existing bound Cog registry, with no transport access."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from potbot.cogs.contracts import CogOutcome, CogRunContext, TypedCogValue
from potbot.cogs.debugger import cog_ref, signature
from potbot.cogs.registry import BoundCogRegistry
from potbot.debugging.cog_runtime import DebuggerCogRuntime


class DebuggerCogClient:
    def __init__(self, registry: BoundCogRegistry, controller: DebuggerCogRuntime) -> None:
        self.registry, self.controller = registry, controller
        self.session_ref: str | None = None
        self.history: list[CogOutcome] = []

    async def call(self, operation: str, **parameters: Any) -> dict[str, Any]:
        selected = signature(operation)
        request = {"schema_version": 1, **parameters}
        if self.session_ref is not None:
            request["session_ref"] = self.session_ref
        context = CogRunContext(
            self.registry.snapshot.fingerprint,
            self.controller.task_revision,
            self.controller.repository_state,
            uuid4(),
            self.history[-1].receipt.fingerprint if self.history else None,
        )
        outcome = await self.registry.invoke(
            cog_ref(operation),
            TypedCogValue(selected.input_type_ref, request),
            context,
            controller=self.controller,
        )
        self.history.append(outcome)
        self.session_ref = outcome.value.value["session_ref"]
        return dict(outcome.value.value["data"])
