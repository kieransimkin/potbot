"""Syn owns communication and receipts; Potbot owns admission and execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError

if TYPE_CHECKING:
    from syn_client import SynClient

    from potbot.controller.execution_events import ExecutionEventService


def configured_client(environment: Mapping[str, str] | None = None) -> SynClient | None:
    environment = os.environ if environment is None else environment
    url = environment.get("POTBOT_SYN_URL")
    if not url:
        return None
    try:
        from syn_client import SynClient
    except ImportError:
        raise CapabilityUnavailableError(
            "Initialize vendor/syn and install the syn extra."
        ) from None
    token = environment.get("POTBOT_SYN_TOKEN", "")
    try:
        return SynClient(url, token)
    except ValueError:
        raise InvalidInputError(
            "Configure a secure POTBOT_SYN_URL and workspace POTBOT_SYN_TOKEN."
        ) from None


def sync_execution_events(
    client: SynClient,
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
