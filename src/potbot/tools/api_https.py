"""Controller-side egress for one installed REST read.

The caller retains authority over task admission and the worker lifecycle.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import (
    ApiCallResult,
    ApiCallScope,
    ApiOperation,
    validated_service_origin,
)
from potbot.tools.api_http import ApprovedHttpApiAdapter, rest_url
from potbot.tools.capability_contracts import (
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
)
from potbot.tools.pinned_https import PinnedHttpsTransport, canonical_https_host


def rest_read_configuration_hash(
    actor: WorkspaceContext, origin: str, operation: ApiOperation
) -> str:
    """Bind the operation and origin to the authenticated workspace and principal."""
    return content_hash(
        {
            "profile": "potbot.pinned-rest-read/1",
            "workspace_id": str(actor.workspace_id),
            "principal_id": str(actor.principal_id),
            "origin": origin,
            "operation": operation.fingerprint,
        }
    )


@dataclass(frozen=True, slots=True)
class InstalledRestRead:
    """Operator-owned egress selection for a single public HTTPS GET operation."""

    actor: WorkspaceContext
    origin: str
    operation: ApiOperation
    implementation: ToolImplementationDescriptor
    installation: ToolInstallationDescriptor

    def __post_init__(self) -> None:
        if (
            self.origin != validated_service_origin(self.origin)
            or self.operation.protocol != "rest"
            or self.operation.method != "GET"
            or self.operation.effect_class != "read_only"
            or self.operation.max_pages != 1
            or self.operation.page_cursor_parameter is not None
            or self.operation.next_cursor_field is not None
            or self.actor.workspace_id != self.installation.workspace_id
            or self.installation.configuration_hash
            != rest_read_configuration_hash(self.actor, self.origin, self.operation)
        ):
            raise ScopeDeniedError("REST read differs from the pinned installation")
        canonical_https_host(self.origin)
        self.implementation.assert_capability(self.operation.to_capability())
        self.installation.assert_implementation(self.implementation)

    async def call(
        self, actor: WorkspaceContext, arguments: Mapping[str, Any], scope: ApiCallScope
    ) -> ApiCallResult:
        if actor != self.actor or scope.installation != self.installation:
            raise ScopeDeniedError("REST read is unavailable to this principal or installation")
        if scope.egress is not None:
            raise ScopeDeniedError("Installed REST reads own their egress policy")
        frozen = json.loads(canonical_json(dict(arguments)))

        url = rest_url(self.origin, self.operation, frozen, cursor=None)

        def admit_url(candidate: str) -> None:
            if candidate != url:
                raise ScopeDeniedError("REST read URL changed after admission")

        # One hard deadline covers DNS and the entire streamed HTTP response.
        async with (
            asyncio.timeout(self.operation.timeout_ms / 1000),
            httpx.AsyncClient(
                transport=PinnedHttpsTransport("GET", url, allow_query=True),
                trust_env=False,
            ) as client,
        ):
            adapter = ApprovedHttpApiAdapter(origin=self.origin, client=client, retry_reads=False)
            enforced = ApiCallScope(
                self.installation, scope.intent, scope.operation_hash, scope.authorize, admit_url
            )
            return await adapter.call(self.operation, frozen, enforced)
