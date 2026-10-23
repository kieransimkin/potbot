"""Syn owns communication and receipts; Potbot owns admission and execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.syn_credentials import SynCredentialScope, service_origin
from potbot.service_credentials import ServiceCredentials

if TYPE_CHECKING:
    from syn_client import SynClient

    from potbot.controller.execution_events import ExecutionEventService


class ScopedSynClient:
    """One installation/workspace/recipient credential, checked before every call."""

    def __init__(self, scope: SynCredentialScope, client: SynClient) -> None:
        self.scope, self._client = scope, client

    def _workspace(self, workspace_id: str) -> str:
        if workspace_id != str(self.scope.workspace_id):
            raise ScopeDeniedError("Syn communication is unavailable for this workspace.")
        return workspace_id

    def head(self, workspace_id: str) -> dict[str, Any]:
        return self._client.head(self._workspace(workspace_id))

    def messages(self, workspace_id: str, *, after: int = 0, limit: int = 50) -> dict[str, Any]:
        return self._client.messages(self._workspace(workspace_id), after=after, limit=limit)

    def send(
        self,
        workspace_id: str,
        *,
        message_id: str,
        channel: str,
        sender: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._client.send(
            self._workspace(workspace_id),
            message_id=message_id,
            channel=channel,
            sender=sender,
            payload=payload,
        )

    def verify(
        self, workspace_id: str, *, expected_head: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._client.verify(self._workspace(workspace_id), expected_head=expected_head)


def configured_client(
    environment: Mapping[str, str] | None = None,
    *,
    workspace_id: UUID | None = None,
    credentials: ServiceCredentials | None = None,
) -> ScopedSynClient | None:
    environment = os.environ if environment is None else environment
    url = environment.get("POTBOT_SYN_URL")
    if not url:
        return None
    scope = SynCredentialScope.from_environment(environment)
    if workspace_id is not None and workspace_id != scope.workspace_id:
        raise ScopeDeniedError("Configured Syn workspace differs from the authenticated workspace.")
    try:
        from syn_client import SynClient
    except ImportError:
        raise CapabilityUnavailableError(
            "Initialize vendor/syn and install the syn extra."
        ) from None
    credentials = credentials or ServiceCredentials(environment=environment)
    token = credentials.resolve(scope.reference)
    try:
        return ScopedSynClient(scope, SynClient(service_origin(url), token))
    except ValueError:
        raise InvalidInputError(
            "Configure a secure POTBOT_SYN_URL and workspace POTBOT_SYN_TOKEN."
        ) from None


def sync_execution_events(
    client: SynClient | ScopedSynClient,
    source: ExecutionEventService,
    context: WorkspaceContext,
    *,
    after: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Replay committed public projections using stable IDs for crash retries.

    A receipt records delivery and chain integrity, never a successful job outcome.
    Stop on failed acknowledgement; the caller may repeat the whole page.
    """
    page = source.events(context, after=after, limit=limit)
    receipts = []
    for event in page["items"]:
        receipt = client.send(
            str(context.workspace_id),
            message_id=str(UUID(event["id"])),
            channel="potbot.execution-events",
            sender="potbot",
            payload=event,
        )
        receipts.append({"source_sequence": event["sequence"], "receipt": receipt})
    return {"items": receipts, "next_cursor": page["next_cursor"], "has_more": page["has_more"]}
