#!/usr/bin/env python3
"""Offline mocked OpenAPI call with a real Potbot ToolObservation hash chain."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from potbot.domain.serialization import content_hash
from potbot.tools.api_contracts import ApiCallScope
from potbot.tools.api_http import ApprovedHttpApiAdapter
from potbot.tools.api_openapi import compile_openapi
from potbot.tools.capability_contracts import ToolInstallationDescriptor, ToolInvocationIntent


def fixture_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Mock search", "version": "v1"},
        "paths": {
            "/search": {
                "get": {
                    "operationId": "search",
                    "parameters": [
                        {"in": "query", "name": "q", "schema": {"type": "string"}, "required": True}
                    ],
                    "responses": {
                        "200": {
                            "description": "Search result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["results"],
                                        "properties": {
                                            "results": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


async def main() -> None:
    import httpx

    (operation,) = compile_openapi(fixture_spec(), selected_effects={"search": "read_only"})
    args = {"query": {"q": "Potbot"}}
    workspace = uuid4()
    installation = ToolInstallationDescriptor(
        installation_ref="syn://thing/example-api",
        workspace_id=workspace,
        implementation_hash="a" * 64,
        configuration_hash=operation.fingerprint,
        policy_revision_hash="b" * 64,
        permitted_scopes=("read",),
    )
    intent = ToolInvocationIntent(
        invocation_id=uuid4(),
        workspace_id=workspace,
        task_revision_id=uuid4(),
        installation_hash=installation.fingerprint,
        input_type_ref=operation.input_type_ref,
        input_hash=content_hash(args),
        effect_class=operation.effect_class,
        policy_revision_hash=installation.policy_revision_hash,
        max_wall_time_ms=operation.timeout_ms,
        max_output_bytes=operation.max_output_bytes,
        idempotency_key="example-query",
    )
    scope = ApiCallScope(
        installation=installation,
        intent=intent,
        operation_hash=operation.fingerprint,
        authorize=lambda *_: None,
        egress=lambda *_: None,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"results": ["A pinned search result"]},
            )
        ),
        trust_env=False,
    ) as client:
        result = await ApprovedHttpApiAdapter(origin="https://api.example.org", client=client).call(
            operation,
            args,
            scope,
        )
    observation = result.to_observation(intent, operation)
    print(
        json.dumps(
            {
                "operation_hash": operation.fingerprint,
                "result_status": result.status,
                "observation_hash": observation.fingerprint,
                "value": result.value,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
