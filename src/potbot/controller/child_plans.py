"""Operator-installed finite child-Cog plans for sandbox-only work.

A plan contains immutable references to *already installed* sandbox Cogs. It
cannot be submitted in a model input, compiled from page data, or executed by
the sandbox child. The controller invokes each planned child through the normal
Cog admission/journal/supervisor path after the parent worker exits. In
particular, a callback never runs guest code in the operator process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.execution_contracts import WORK_KINDS
from potbot.tools.sandbox_cog_worker import SandboxedCogOperation

if TYPE_CHECKING:
    from collections.abc import Mapping

    from potbot.cogs.contracts import TypedCogValue
    from potbot.controller.execution_cogs import ExecutionCall

_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_EFFECTS = {"read_only": 0, "idempotent_write": 1, "non_idempotent_write": 2, "destructive": 3}


@dataclass(frozen=True, slots=True)
class ChildWorkStep:
    """A trusted child reference; links name earlier completed steps only."""

    name: str
    kind: str
    operation_ref: str
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _ID.fullmatch(self.name)
            or self.kind not in WORK_KINDS
            or not isinstance(self.operation_ref, str)
            or len(self.operation_ref) > 256
            or not self.operation_ref.startswith("syn://thing/")
            or not isinstance(self.links, tuple)
            or len(self.links) > 16
            or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in self.links)
            or len(set(self.links)) != len(self.links)
        ):
            raise ScopeDeniedError("Child work step is not a bounded installed reference")


@dataclass(frozen=True, slots=True)
class InstalledChildPlan:
    parent_kind: str
    parent_operation_ref: str
    steps: tuple[ChildWorkStep, ...]

    def __post_init__(self) -> None:
        if (
            self.parent_kind not in WORK_KINDS
            or not isinstance(self.parent_operation_ref, str)
            or len(self.parent_operation_ref) > 256
            or not self.parent_operation_ref.startswith("syn://thing/")
            or not isinstance(self.steps, tuple)
            or not 1 <= len(self.steps) <= 32
            or any(not isinstance(step, ChildWorkStep) for step in self.steps)
        ):
            raise ScopeDeniedError("Child work plan must be an installed bounded tuple")
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen or any(link not in seen for link in step.links):
                raise ScopeDeniedError("Child plan has duplicate steps or noncausal links")
            seen.add(step.name)

    @property
    def key(self) -> tuple[str, str]:
        return self.parent_kind, self.parent_operation_ref


def compile_child_plans(
    operations: tuple[SandboxedCogOperation, ...],
    plans: tuple[InstalledChildPlan, ...],
) -> Mapping[tuple[str, str], InstalledChildPlan]:
    """Resolve exact installed identities and reject cycles/authority escalation.

    No plan declares a guest executable, a role, a principal, policy override,
    workspace, or new side-effect grant. Each child receives the parent's
    independently admitted *typed input*, and gets its own one-use invocation.
    """
    if (
        not isinstance(operations, tuple)
        or not isinstance(plans, tuple)
        or len(plans) > 64
        or any(not isinstance(p, InstalledChildPlan) for p in plans)
    ):
        raise ScopeDeniedError("Child plans require operator-installed bounded descriptors")
    installed = {(op.binding.kind, op.binding.operation_ref): op for op in operations}
    if len(installed) != len(operations):
        raise ScopeDeniedError("Duplicate installed work identity")
    result: dict[tuple[str, str], InstalledChildPlan] = {}
    for plan in plans:
        if plan.key in result or plan.key not in installed:
            raise ScopeDeniedError("Duplicate child plan or unknown parent installation")
        parent = installed[plan.key].binding
        for step in plan.steps:
            child = installed.get((step.kind, step.operation_ref))
            if child is None:
                raise ScopeDeniedError("Planned child is not an installed sandbox Cog")
            binding = child.binding
            if (
                binding.input_type_ref != parent.input_type_ref
                or _EFFECTS[binding.effect_class] > _EFFECTS[parent.effect_class]
            ):
                raise ScopeDeniedError("Child input port or effect exceeds parent authority")
            # The child cannot inherit a separate, weaker or stale policy grant.
            if binding.policy != parent.policy:
                raise ScopeDeniedError("Child work changes the parent policy revision")
        result[plan.key] = plan

    def count_family(key: tuple[str, str], ancestors: tuple[tuple[str, str], ...]) -> int:
        if key in ancestors or len(ancestors) >= 16:
            raise ScopeDeniedError("Installed child plans cycle or exceed maximum depth")
        total = 1
        plan = result.get(key)
        for step in plan.steps if plan else ():
            total += count_family((step.kind, step.operation_ref), (*ancestors, key))
            if total > 256:
                raise ScopeDeniedError("Expanded child plan exceeds trace run bound")
        return total

    for key in result:
        count_family(key, ())
    return MappingProxyType(result)


async def execute_child_plan(
    call: ExecutionCall,
    value: TypedCogValue,
    plan: InstalledChildPlan,
) -> None:
    """No guest-supplied child scheduling; all links are DB-verified receipts."""
    from potbot.controller.execution_events import ExecutionEventService

    children: dict[str, Any] = {}
    for step in plan.steps:
        links: list[UUID] = []
        for name in step.links:
            receipt = children[name].receipt
            history = ExecutionEventService(call.runtime.db).events(
                call.runtime.actor,
                run_id=receipt.invocation_id,
                limit=200,
            )
            if history.get("has_more") or not history.get("items"):
                raise StateMismatchError("Linked child has no bounded durable event history")
            terminal = history["items"][-1]
            if terminal.get("stage") != "completed" or terminal.get("run_id") != str(
                receipt.invocation_id
            ):
                raise StateMismatchError("Child link has no verified terminal completion")
            links.append(UUID(terminal["id"]))
        children[step.name] = await call.child(
            step.kind,
            step.operation_ref,
            value,
            links=tuple(links),
        )
