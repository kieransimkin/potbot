"""Bounded HTTP exchange tests with an in-memory transport (no external egress)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import ApiCallScope, ApiOperation
from potbot.tools.api_http import ApiReadCache, ApprovedHttpApiAdapter
from potbot.tools.capability_contracts import ToolInstallationDescriptor, ToolInvocationIntent


def operation(**changes: object) -> ApiOperation:
    values = dict(
        name="get_items",
        protocol="rest",
        effect_class="read_only",
        target="/items/{id}",
        method="GET",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            }
        ),
        output_schema_json=canonical_json(
            {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            }
        ),
        source_hash="a" * 64,
    )
    values.update(changes)
    return ApiOperation(**values)


def scope_for(
    op: ApiOperation, args: dict[str, object], *, approve=None, egress=None
) -> ApiCallScope:
    w = uuid4()
    installation = ToolInstallationDescriptor(
        installation_ref="syn://thing/test-api-installation",
        workspace_id=w,
        implementation_hash="b" * 64,
        configuration_hash="c" * 64,
        policy_revision_hash="d" * 64,
        permitted_scopes=("read",),
    )
    intent = ToolInvocationIntent(
        invocation_id=uuid4(),
        workspace_id=w,
        task_revision_id=uuid4(),
        installation_hash=installation.fingerprint,
        input_type_ref=op.input_type_ref,
        input_hash=content_hash(args),
        effect_class=op.effect_class,
        policy_revision_hash=installation.policy_revision_hash,
        max_wall_time_ms=op.timeout_ms,
        max_output_bytes=op.max_output_bytes,
        idempotency_key="api-test-001",
    )
    return ApiCallScope(
        installation,
        intent,
        operation_hash=op.fingerprint,
        authorize=approve or (lambda *_: None),
        egress=egress or (lambda *_: None),
    )


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def test_rest_exact_path_query_and_response() -> None:
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url)))
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"value": 23})

    op = operation(
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                    "query": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer"}},
                        "additionalProperties": False,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            }
        )
    )
    args = {"path": {"id": "abc_1"}, "query": {"limit": 2}}
    scope = scope_for(op, args)

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope
            )

    result = run(check())
    assert result.status == "succeeded" and result.value == {"value": 23}
    assert seen == [("GET", "https://api.example.org/items/abc_1?limit=2")]


def test_redirects_not_followed() -> None:
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    op = operation()
    args = {"path": {"id": "1"}}

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "failed" and result.error_code == "redirect_denied"
    assert len(urls) == 1


def test_unqualified_egress_and_failed_authorization_block_before_request() -> None:
    calls = []
    op = operation()
    args = {"path": {"id": "1"}}

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"value": 1})

    async def check():
        async with client_for(handler) as c:
            adapter = ApprovedHttpApiAdapter(origin="https://api.example.org", client=c)
            with pytest.raises(ScopeDeniedError):
                await adapter.call(op, args, replace(scope_for(op, args), egress=None))
            with pytest.raises(ScopeDeniedError):
                await adapter.call(op, args, scope_for(op, args, approve=lambda *_: False))
            with pytest.raises(ScopeDeniedError):
                await adapter.call(op, args, scope_for(op, args, egress=lambda *_: False))

    run(check())
    assert not calls


def test_rest_path_escapes_are_blocked() -> None:
    op = operation()
    args = {"path": {"id": "../admin"}}

    async def check():
        async with client_for(lambda req: httpx.Response(200, json={"value": 1})) as c:
            with pytest.raises(InvalidInputError):
                await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                    op, args, scope_for(op, args)
                )

    run(check())


def test_oversized_decompressed_response_and_duplicate_json_keys() -> None:
    op = operation(max_output_bytes=32)
    args = {"path": {"id": "1"}}

    async def call(handler):
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    huge = run(
        call(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "application/json"}, content=b" " * 33
            )
        )
    )
    bad = run(
        call(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "application/json"}, content=b'{"value":1,"value":2}'
            )
        )
    )
    assert huge.error_code == "response_too_large"
    assert bad.error_code == "response_schema_invalid"


def test_non_idempotent_write_is_never_retried_on_server_error() -> None:
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(
            503, headers={"Content-Type": "application/json"}, json={"error": True}
        )

    op = operation(
        name="save_item",
        method="POST",
        target="/items",
        effect_class="non_idempotent_write",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"body": {"type": "object"}},
                "required": ["body"],
                "additionalProperties": False,
            }
        ),
    )
    args = {"body": {"value": 3}}

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "indeterminate"
    assert result.attempts == 1 and len(requests) == 1


def test_read_only_retry_rate_limit_is_bounded() -> None:
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(429)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"value": 8})

    op = operation()
    args = {"path": {"id": "2"}}

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "succeeded" and result.attempts == 2 and count == 2


def test_write_idempotency_header_not_an_automatic_retry() -> None:
    seen = []

    def handler(req):
        seen.append(req.headers)
        return httpx.Response(429)

    op = operation(
        name="update_item",
        method="PUT",
        target="/items",
        effect_class="idempotent_write",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"body": {"type": "object"}},
                "required": ["body"],
                "additionalProperties": False,
            }
        ),
    )
    args = {"body": {"value": 2}}

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "indeterminate" and result.attempts == 1
    assert len(seen) == 1 and seen[0]["idempotency-key"] == "api-test-001"


def test_read_cache_is_opt_in_scoped_and_only_successful() -> None:
    hits = 0

    def handler(req):
        nonlocal hits
        hits += 1
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json={"value": hits}
        )

    op = operation()
    args = {"path": {"id": "1"}}
    s1 = scope_for(op, args)
    s2 = scope_for(op, args)

    async def check():
        async with client_for(handler) as c:
            adapter = ApprovedHttpApiAdapter(
                origin="https://api.example.org",
                client=c,
                cache=ApiReadCache(),
                cache_partition="tenant-a-user-1",
            )
            a = await adapter.call(op, args, s1)
            b = await adapter.call(op, args, s1)
            d = await adapter.call(op, args, s2)
            return a, b, d

    a, b, d = run(check())
    assert hits == 2 and a.value == b.value == {"value": 1} and d.value == {"value": 2}


def test_graphql_document_is_fixed_and_errors_are_not_success() -> None:
    requests = []

    def handler(req):
        body = json.loads(req.content)
        requests.append(body)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/graphql-response+json"},
            json={"data": {"lookup": {"id": "1"}}, "errors": [{"message": "denied"}]},
        )

    op = operation(
        name="lookup",
        protocol="graphql",
        target="/graphql",
        method="POST",
        graphql_document="query lookup($id: ID!) { lookup(id:$id) { id } }",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {
                    "variables": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    }
                },
                "required": ["variables"],
                "additionalProperties": False,
            }
        ),
    )
    args = {"variables": {"id": "1"}}

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "failed" and result.error_code == "graphql_errors"
    assert requests[0]["query"] == op.graphql_document
    assert requests[0]["variables"] == {"id": "1"}


def test_read_pagination_rejects_cycles_and_limits() -> None:
    op = operation(
        target="/items",
        max_pages=2,
        page_cursor_parameter="cursor",
        next_cursor_field="next",
        input_schema_json=canonical_json({"type": "object", "additionalProperties": False}),
        output_schema_json=canonical_json(
            {
                "type": "object",
                "required": ["value", "next"],
                "properties": {"value": {"type": "integer"}, "next": {"type": ["string", "null"]}},
                "additionalProperties": False,
            }
        ),
    )

    async def execute(handler):
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, {}, scope_for(op, {})
            )

    def loop_handler(req):
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json={"value": 1, "next": "x"}
        )

    bad = run(execute(loop_handler))
    assert bad.status == "failed" and bad.error_code == "pagination_cursor_invalid"

    def continue_handler(req):
        cursor = req.url.params.get("cursor")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"value": 1, "next": "a" if cursor is None else "b"},
        )

    limited = run(execute(continue_handler))
    assert limited.error_code == "pagination_limit"

    def done_handler(req):
        cursor = req.url.params.get("cursor")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"value": 1 if cursor is None else 2, "next": "a" if cursor is None else None},
        )

    good = run(execute(done_handler))
    assert good.value == [{"value": 1, "next": "a"}, {"value": 2, "next": None}]


def test_graphql_mutation_is_never_retried() -> None:
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        return httpx.Response(
            503,
            headers={"Content-Type": "application/graphql-response+json"},
            json={"errors": [{"message": "unavailable"}]},
        )

    op = operation(
        name="mutate",
        protocol="graphql",
        target="/graphql",
        method="POST",
        effect_class="non_idempotent_write",
        graphql_document="mutation mutate { update { ok } }",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"variables": {"type": "object"}},
                "additionalProperties": False,
            }
        ),
    )

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, {}, scope_for(op, {})
            )

    result = run(check())
    assert result.status == "indeterminate" and result.attempts == 1 and count == 1


def test_response_cannot_echo_trusted_bearer_token() -> None:
    observed = []

    def handler(req):
        observed.append(req.headers.get("authorization"))
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"value":1,"echo":"super-secret-token"}',
        )

    op = operation(
        output_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}, "echo": {"type": "string"}},
                "required": ["value"],
            }
        )
    )
    args = {"path": {"id": "1"}}

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(
                origin="https://api.example.org",
                client=c,
                bearer_supplier=lambda: "super-secret-token",
            ).call(op, args, scope_for(op, args))

    result = run(check())
    assert result.error_code == "response_credential_exposure"
    assert observed == ["Bearer super-secret-token"]


def test_pagination_deadline_is_global_not_per_page() -> None:
    import time

    op = operation(
        target="/items",
        max_pages=2,
        page_cursor_parameter="cursor",
        next_cursor_field="next",
        timeout_ms=20,
        input_schema_json=canonical_json({"type": "object", "additionalProperties": False}),
        output_schema_json=canonical_json(
            {
                "type": "object",
                "required": ["next"],
                "properties": {"next": {"type": ["string", "null"]}},
            }
        ),
    )
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        time.sleep(0.04)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"next": "x"})

    async def check():
        async with client_for(handler) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, {}, scope_for(op, {})
            )

    result = run(check())
    assert result.error_code == "api_deadline_exceeded" and count == 1


def test_write_malformed_200_is_indeterminate_not_rejected_write() -> None:
    op = operation(
        name="save_item",
        method="POST",
        target="/items",
        effect_class="non_idempotent_write",
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"body": {"type": "object"}},
                "required": ["body"],
                "additionalProperties": False,
            }
        ),
    )
    args = {"body": {"value": 3}}

    async def check():
        async with client_for(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "application/json"}, content=b'{"value":"wrong"}'
            )
        ) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "indeterminate" and result.error_code == "response_schema_invalid"


def test_arguments_are_frozen_before_egress_or_authorization_mutation() -> None:
    op = operation()
    args = {"path": {"id": "original"}}
    urls = []

    def egress(url):
        args["path"]["id"] = "changed"
        urls.append(url)

    async def check():
        async with client_for(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "application/json"}, json={"value": 1}
            )
        ) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args, egress=egress)
            )

    result = run(check())
    assert result.status == "succeeded" and urls == ["https://api.example.org/items/original"]


def test_cache_returns_detached_value_not_shared_mutable_instance() -> None:
    op = operation(
        output_schema_json=canonical_json(
            {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
        )
    )
    args = {"path": {"id": "1"}}
    scope = scope_for(op, args)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"value": 7})

    async def check():
        async with client_for(handler) as c:
            a = ApprovedHttpApiAdapter(
                origin="https://api.example.org",
                client=c,
                cache=ApiReadCache(),
                cache_partition="trusted-partition",
            )
            first = await a.call(op, args, scope)
            first.value["value"] = 100
            second = await a.call(op, args, scope)
            return second

    second = run(check())
    assert second.value == {"value": 7} and len(calls) == 1


def test_rest_rejects_undeclared_success_status_even_with_valid_json() -> None:
    op = operation()
    args = {"path": {"id": "1"}}

    async def check():
        async with client_for(
            lambda req: httpx.Response(
                202, headers={"Content-Type": "application/json"}, json={"value": 1}
            )
        ) as c:
            return await ApprovedHttpApiAdapter(origin="https://api.example.org", client=c).call(
                op, args, scope_for(op, args)
            )

    result = run(check())
    assert result.status == "failed" and result.error_code == "unexpected_success_status"
