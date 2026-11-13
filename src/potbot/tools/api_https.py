"""Controller-side egress for installed REST and GraphQL reads.

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
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
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


def graphql_read_configuration_hash(
    actor: WorkspaceContext, origin: str, operation: ApiOperation
) -> str:
    """Pin the fixed GraphQL document, schemas and route to one principal."""
    return content_hash(
        {
            "profile": "potbot.pinned-graphql-read/1",
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
        return await _call(self.operation, self.origin, url, frozen, scope)


@dataclass(frozen=True, slots=True)
class InstalledGraphqlRead:
    """Operator-owned egress for one named GraphQL query at a fixed HTTPS path."""

    actor: WorkspaceContext
    origin: str
    operation: ApiOperation
    implementation: ToolImplementationDescriptor
    installation: ToolInstallationDescriptor

    def __post_init__(self) -> None:
        op = self.operation
        if (
            self.origin != validated_service_origin(self.origin)
            or op.protocol != "graphql"
            or op.effect_class != "read_only"
            or "{" in op.target
            or any(part in {".", ".."} for part in op.target.split("/"))
            or self.actor.workspace_id != self.installation.workspace_id
            or self.installation.configuration_hash
            != graphql_read_configuration_hash(self.actor, self.origin, op)
        ):
            raise ScopeDeniedError("GraphQL query differs from the pinned installation")
        canonical_https_host(self.origin + op.target)
        self.implementation.assert_capability(op.to_capability())
        self.installation.assert_implementation(self.implementation)

        try:
            from graphql import GraphQLError, OperationDefinitionNode, OperationType, parse
        except ImportError as exc:
            raise CapabilityUnavailableError(
                "GraphQL installation requires the providers extra"
            ) from exc
        assert op.graphql_document is not None
        try:
            document = parse(op.graphql_document, max_tokens=2048)
        except (GraphQLError, RecursionError):
            raise InvalidInputError("Invalid or oversized installed GraphQL document") from None
        if len(document.definitions) != 1:
            raise ScopeDeniedError("GraphQL installation requires one query definition")
        query = document.definitions[0]
        if (
            not isinstance(query, OperationDefinitionNode)
            or query.operation is not OperationType.QUERY
            or query.name is None
            or query.name.value != op.name.replace("-", "_").replace(".", "_")
        ):
            raise ScopeDeniedError("GraphQL installation requires the selected named query")

    async def call(
        self, actor: WorkspaceContext, arguments: Mapping[str, Any], scope: ApiCallScope
    ) -> ApiCallResult:
        if actor != self.actor or scope.installation != self.installation:
            raise ScopeDeniedError("GraphQL query is unavailable to this principal or installation")
        if scope.egress is not None:
            raise ScopeDeniedError("Installed GraphQL queries own their egress policy")
        frozen = json.loads(canonical_json(dict(arguments)))
        if set(frozen) - {"variables"} or not isinstance(frozen.get("variables", {}), dict):
            raise InvalidInputError("GraphQL query accepts only a variables object")
        url = self.origin + self.operation.target
        return await _call(self.operation, self.origin, url, frozen, scope)


async def _call(
    operation: ApiOperation, origin: str, url: str, frozen: dict[str, Any], scope: ApiCallScope
) -> ApiCallResult:
    def admit_url(candidate: str) -> None:
        if candidate != url:
            raise ScopeDeniedError("API read URL changed after admission")

    # One hard deadline covers DNS and the entire streamed HTTP response.
    async with (
        asyncio.timeout(operation.timeout_ms / 1000),
        httpx.AsyncClient(
            transport=PinnedHttpsTransport(
                operation.method, url, allow_query=operation.protocol == "rest"
            ),
            trust_env=False,
        ) as client,
    ):
        adapter = ApprovedHttpApiAdapter(origin=origin, client=client, retry_reads=False)
        enforced = ApiCallScope(
            scope.installation, scope.intent, scope.operation_hash, scope.authorize, admit_url
        )
        return await adapter.call(operation, frozen, enforced)
