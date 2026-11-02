"""Operator installation and typed ports for the admitted local queue worker."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.queue_transport_worker import EXECUTABLE, REQUEST, RESULT, operation_ref
from potbot.tools.sandbox_cog_worker import SandboxedCogOperation


def schemas() -> dict[str, dict[str, Any]]:
    from potbot.tools.semantic_contracts import HASH, UUID_TEXT, obj

    return {
        REQUEST: obj(
            {
                "effect_id": UUID_TEXT,
                "message": {"type": "string", "minLength": 1, "maxLength": 512},
            }
        ),
        RESULT: obj(
            {
                "target_id": UUID_TEXT,
                "effect_id": UUID_TEXT,
                "request_sha256": HASH,
                "outcome": {"enum": ["applied", "cancelled"]},
            }
        ),
    }


def install_queue_transport(
    target_id: UUID, executable_sha256: str, policy: ExecutionPolicy
) -> SandboxedCogOperation:
    target = str(target_id)
    return SandboxedCogOperation(
        WorkBinding(
            "api",
            operation_ref(target),
            REQUEST,
            RESULT,
            executable_sha256,
            "idempotent_write",
            policy,
        ),
        (EXECUTABLE, "apply", target),
        executable_sha256,
    )


def queue_target(operation: SandboxedCogOperation) -> str:
    if len(operation.argv) != 3:
        raise ScopeDeniedError("Queue requires an operator-pinned installation")
    try:
        target = UUID(operation.argv[2])
    except ValueError as exc:
        raise ScopeDeniedError("Queue requires a pinned target identity") from exc
    if operation != install_queue_transport(
        target, operation.executable_sha256, operation.binding.policy
    ):
        raise ScopeDeniedError("Queue requires an operator-pinned installation")
    return str(target)


def resolution_worker(operation: SandboxedCogOperation) -> SandboxedCogOperation:
    target = queue_target(operation)
    return replace(
        operation,
        binding=replace(operation.binding, operation_ref=operation_ref(target, "resolve")),
        argv=(EXECUTABLE, "resolve", target),
    )
