"""Forward admitted debugger Cogs over one private Syn pub/sub scope."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.cogs.contracts import CogOutcome, CogReceipt, CogRunContext, CogSignature, TypedCogValue
from potbot.cogs.debugger import OPERATIONS, cog_ref, signature
from potbot.cogs.registry import BoundCogRegistry
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.domain.errors import (
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.syn_release.contracts import canonical, digest, document, fields
from potbot.syn_release.store import _directory, _sync_directory, _write_new

TOPIC = "syn://type/potbot-debugger-forwarding-v1"
# Syn limits canonical publication payloads to 8192 bytes. Keep headroom for
# its typed wrapper. Ask for smaller pages when returning large inspections.
MAX_BYTES = 7500
_CLAIM_LIMIT = 65_536
_REFERENCES = {
    cog_ref(operation): signature(operation)
    for operation in OPERATIONS
    if operation not in {"launch", "evaluate", "terminate"}
}
_REFUSALS = {
    error.code.value: error for error in (InvalidInputError, ScopeDeniedError, StateMismatchError)
}


def debugger_group_id(operator: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"syn://operator/{digest(operator)}/debugger/1")


class DebuggerInvocationClaims:
    """Protected claims shared by every reconnect for an admitted workspace."""

    def __init__(self, root: Path, workspace_id: UUID) -> None:
        _directory(root)
        if type(workspace_id) is not UUID or not workspace_id.int:
            raise InvalidInputError("Debugger claim store requires a workspace")
        self.root = root / workspace_id.hex
        _directory(self.root, create=True)
        _sync_directory(root)
        self.workspace_id = workspace_id

    def claim(self, invocation: UUID) -> bool:
        if type(invocation) is not UUID or not invocation.int:
            raise InvalidInputError("Debugger claim requires a nonzero invocation ID")
        if (self.root / invocation.hex).exists():
            return False
        for index, _ in enumerate(self.root.iterdir(), 1):
            if index >= _CLAIM_LIMIT:
                raise ScopeDeniedError("Debugger invocation claim capacity reached")
        try:
            _write_new(self.root / invocation.hex, b"claimed\n")
        except FileExistsError:
            return False
        _sync_directory(self.root)
        return True


def _value(source: TypedCogValue) -> dict[str, Any]:
    return {
        "type_ref": source.type_ref,
        "value": source.value,
        "source_artifact_id": str(source.source_artifact_id) if source.source_artifact_id else None,
    }


def _read_value(raw: object) -> TypedCogValue:
    source = fields(raw, {"type_ref", "value", "source_artifact_id"})
    artifact = source["source_artifact_id"]
    return TypedCogValue(source["type_ref"], source["value"], UUID(artifact) if artifact else None)


def _context(raw: object) -> CogRunContext:
    value = fields(
        raw,
        {
            "cog_snapshot_hash",
            "task_revision_id",
            "repository_state_id",
            "invocation_id",
            "upstream_receipt_hash",
        },
    )
    return CogRunContext(
        value["cog_snapshot_hash"],
        UUID(value["task_revision_id"]),
        UUID(value["repository_state_id"]),
        UUID(value["invocation_id"]),
        value["upstream_receipt_hash"],
    )


class DebuggerEndpoint(Protocol):
    @property
    def node(self) -> str: ...

    @property
    def scope(self) -> dict[str, Any]: ...

    def call(self, operation: str, payload: object = None) -> dict[str, Any]: ...


class SynDebuggerRoute:
    """One controller-owned consumer for a dedicated two-Agent debugger group."""

    def __init__(self, endpoint: DebuggerEndpoint, *, operator: str, peer: str, peer_boot: str):
        scope = endpoint.scope
        self.workspace_id = UUID(scope["workspace_id"])
        self.session = scope["session_id"]
        self.endpoint = endpoint
        self.peer = digest(peer)
        self.peer_boot = peer_boot
        self._closed = False
        self._busy = asyncio.Lock()
        if (
            endpoint.node == peer
            or scope["operator"] != operator
            or scope["group_id"] != str(debugger_group_id(operator))
            or len(scope["members"]) != 2
            or {row["body"]["node_public_key_hash"] for row in scope["members"]}
            != {endpoint.node, peer}
        ):
            raise ScopeDeniedError(
                "Debugger route requires the operator's dedicated two-Agent group"
            )

    def close(self) -> None:
        self._closed = True

    async def _call(self, operation: str, payload: object = None) -> dict[str, Any]:
        if self._closed:
            raise IndeterminateExecutionError("Syn debugger scope is closed")
        return await asyncio.to_thread(self.endpoint.call, operation, payload)

    def _publication(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        message = {
            "topic": TOPIC,
            "value": {
                "kind": kind,
                "session": self.session,
                "to": self.peer,
                **payload,
            },
        }
        if len(canonical(message)) > MAX_BYTES:
            raise InvalidInputError("Debugger publication is too large for this Syn group")
        return message

    async def _messages(self, kind: str) -> AsyncIterator[dict[str, Any]]:
        while True:
            result = await self._call("receive")
            for item in result["messages"]:
                header = item["envelope"]["header"]
                message = item["payload"]
                if (
                    header["topic"] == TOPIC
                    and header["node"] == self.peer
                    and header["boot_id"] == self.peer_boot
                    and header["session_id"] == self.session
                    and header["workspace_id"] == str(self.workspace_id)
                    and message.get("kind") == kind
                    and message.get("to") == self.endpoint.node
                    and message.get("session") == self.session
                ):
                    yield message
            await asyncio.sleep(0.08)

    async def invoke(
        self,
        reference: str,
        source: TypedCogValue,
        context: CogRunContext,
    ) -> CogOutcome:
        selected = _REFERENCES.get(reference)
        if selected is None:
            raise ScopeDeniedError("Syn debugger only accepts installed debugger Cogs")
        if source.type_ref != selected.input_type_ref:
            raise InvalidInputError("Debugger input type does not match the installed Cog")
        # Keep one immutable snapshot across the publication and receipt checks.
        source = _read_value(document(canonical(_value(source)), canonical_only=True))
        request = self._publication(
            "request",
            {
                "cog": reference,
                "source": _value(source),
                "context": {
                    key: str(value) if isinstance(value, UUID) else value
                    for key, value in asdict(context).items()
                },
            },
        )
        async with self._busy:
            refusal = None
            try:
                async with asyncio.timeout(45):
                    accepted = await self._call("publish", request)
                    if accepted.get("relay_accepted") is not True:
                        raise IndeterminateExecutionError("Syn relay did not confirm submission")
                    async for result in self._messages("reply"):
                        if result.get("invocation_id") != str(context.invocation_id):
                            continue
                        if set(result) == {"kind", "session", "to", "invocation_id", "error"}:
                            refusal = _REFUSALS.get(result["error"])
                            if refusal is not None:
                                break
                        return self._outcome(result, selected, source, context)
            except asyncio.CancelledError:
                self.close()
                raise
            except Exception as error:
                self.close()
                raise IndeterminateExecutionError(
                    "Syn debugger result could not be verified. Reconcile the target before reconnecting."
                ) from error
            if refusal is not None:
                raise refusal("The admitted debugger rejected this invocation")
        raise AssertionError("Syn debugger reply loop exited")

    @staticmethod
    def _outcome(
        result: dict[str, Any],
        selected: CogSignature,
        source: TypedCogValue,
        context: CogRunContext,
    ) -> CogOutcome:
        fields(result, {"kind", "session", "to", "invocation_id", "value", "receipt"})
        value = _read_value(result["value"])
        receipt_data = dict(result["receipt"])
        if receipt_data.pop("schema_version") != 1:
            raise InvalidInputError("Unknown debugger receipt version")
        for key in (
            "task_revision_id",
            "repository_state_id",
            "invocation_id",
            "input_artifact_id",
        ):
            if receipt_data[key] is not None:
                receipt_data[key] = UUID(receipt_data[key])
        receipt = CogReceipt(**receipt_data)
        if (
            (receipt.cog_ref, receipt.cog_type_ref, receipt.binding_hash)
            != (str(selected.cog_ref), selected.cog_type_ref, selected.binding_hash)
            or (
                receipt.cog_snapshot_hash,
                receipt.task_revision_id,
                receipt.repository_state_id,
                receipt.invocation_id,
                receipt.upstream_receipt_hash,
            )
            != (
                context.cog_snapshot_hash,
                context.task_revision_id,
                context.repository_state_id,
                context.invocation_id,
                context.upstream_receipt_hash,
            )
            or (receipt.input_type_ref, receipt.input_hash, receipt.input_artifact_id)
            != (source.type_ref, source.fingerprint, source.source_artifact_id)
            or (receipt.output_type_ref, receipt.output_hash, value.type_ref)
            != (selected.output_type_ref, value.fingerprint, selected.output_type_ref)
        ):
            raise ScopeDeniedError("Syn debugger result does not match the admitted invocation")
        return CogOutcome(value, receipt)

    async def serve(
        self,
        registry: BoundCogRegistry,
        controller: DebuggerCogRuntime,
        *,
        claims: DebuggerInvocationClaims,
    ) -> None:
        """The target owner supplies the workspace runtime and a durable one-use claim."""
        if (
            controller.session.identity.workspace_id != self.workspace_id
            or claims.workspace_id != self.workspace_id
            or controller.session.target_process_id is not None
        ):
            raise ScopeDeniedError("Debugger runtime and Syn workspace differ")
        controller.session.require_owned_attach_target()
        async with self._busy:
            try:
                async for message in self._messages("request"):
                    await self._dispatch(registry, controller, claims, message)
            finally:
                self.close()

    async def _dispatch(
        self,
        registry: BoundCogRegistry,
        controller: DebuggerCogRuntime,
        claims: DebuggerInvocationClaims,
        message: dict[str, Any],
    ) -> None:
        try:
            fields(message, {"kind", "session", "to", "cog", "source", "context"})
            reference = message["cog"]
            if reference not in _REFERENCES:
                return
            source = _read_value(message["source"])
            context = _context(message["context"])
            if (
                context.cog_snapshot_hash != registry.snapshot.fingerprint
                or context.task_revision_id != controller.task_revision
                or context.repository_state_id != controller.repository_state
            ):
                return
        except (KeyError, TypeError, ValueError):
            return
        # Claim before dispatch. A failed publication, restart or new scope
        # does not release the claim or resend the command.
        if not await asyncio.to_thread(claims.claim, context.invocation_id):
            return
        response: dict[str, Any] = {"invocation_id": str(context.invocation_id)}
        try:
            outcome = await registry.invoke(reference, source, context, controller=controller)
        except (InvalidInputError, ScopeDeniedError, StateMismatchError) as error:
            response["error"] = error.code.value
        except Exception:
            response["error"] = "outcome_unavailable"
        else:
            response.update(value=_value(outcome.value), receipt=outcome.receipt.to_dict())
        try:
            payload = self._publication("reply", response)
        except InvalidInputError:
            payload = self._publication(
                "reply",
                {
                    "invocation_id": str(context.invocation_id),
                    "error": "outcome_unavailable",
                },
            )
        await self._call("publish", payload)
