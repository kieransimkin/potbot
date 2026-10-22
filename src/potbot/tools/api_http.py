"""T02.4 bounded REST and pinned-document GraphQL-over-HTTP adapters.

The HTTP client must be created by the controller with a qualified egress path.
This module validates syntax and calls a required enforcement hook but does NOT
implement DNS/IP pinning itself. It is NOT independently safe as a network proxy.
There is no automatic write retry, arbitrary URL, or model-supplied GraphQL text.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlencode

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import (
    ApiCallResult,
    ApiCallScope,
    ApiOperation,
    validate_json,
    validated_service_origin,
)

_PATH_TOKEN = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SAFE_CURSOR = re.compile(r"[A-Za-z0-9_=:.-]{1,256}\Z")
_RETRY_HTTP = frozenset({429, 503})
_JSON_TYPES = frozenset({"application/json", "application/graphql-response+json"})


def _safe_response_json(body: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise ValueError("duplicate")
            output[key] = value
        return output
    def invalid(_: str) -> None:
        raise ValueError("nonfinite")
    try:
        return json.loads(body.decode("utf-8", "strict"), object_pairs_hook=pairs,
                          parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidInputError("API response is not valid bounded JSON") from error


def _query_scalar(value: Any) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return str(value)
    if isinstance(value, str) and len(value) <= 512 and not any(ord(x) < 32 for x in value):
        return value
    raise InvalidInputError("API query arguments must be bounded scalar values")


def _rest_url(origin: str, operation: ApiOperation, args: Mapping[str, Any], cursor: str | None) -> str:
    path = args.get("path", {})
    query = args.get("query", {})
    if not isinstance(path, dict) or not isinstance(query, dict):
        raise InvalidInputError("REST path/query arguments must be objects")
    def replace(match: re.Match[str]) -> str:
        v = path.get(match.group(1))
        if not isinstance(v, str) or not _SAFE_SEGMENT.fullmatch(v):
            raise InvalidInputError("REST path variable is unsafe or unsupported")
        return quote(v, safe="")
    routed = _PATH_TOKEN.sub(replace, operation.target)
    values = dict(query)
    if cursor is not None:
        if operation.page_cursor_parameter is None or operation.page_cursor_parameter in values:
            raise InvalidInputError("Caller cannot override a trusted pagination cursor")
        values[operation.page_cursor_parameter] = cursor
    if len(values) > 32:
        raise InvalidInputError("Too many API query arguments")
    encoded = urlencode(sorted((k, _query_scalar(v)) for k, v in values.items()), safe="")
    result = origin + routed + ("?" + encoded if encoded else "")
    if len(result) > 2048:
        raise InvalidInputError("API target URL exceeds its limit")
    return result


class ApiReadCache:
    """Bounded in-process read cache, never shared across controller partitions."""

    def __init__(self, *, max_entries: int = 128, ttl_s: float = 30.0) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 1024 or not 0 < ttl_s <= 3600:
            raise InvalidInputError("Invalid API read cache bounds")
        self.max_entries, self.ttl_s = max_entries, ttl_s
        self._values: dict[str, tuple[float, ApiCallResult]] = {}

    def get(self, key: str) -> ApiCallResult | None:
        value = self._values.get(key)
        if value is None:
            return None
        if time.monotonic() - value[0] >= self.ttl_s:
            self._values.pop(key, None)
            return None
        return replace(value[1], value=copy.deepcopy(value[1].value))

    def put(self, key: str, result: ApiCallResult) -> None:
        if result.status != "succeeded":
            return
        if len(self._values) >= self.max_entries:
            self._values.pop(next(iter(self._values)))
        self._values[key] = (time.monotonic(), replace(result, value=copy.deepcopy(result.value)))


class ApprovedHttpApiAdapter:
    """Requires externally enforced egress and independently admitted API calls."""

    def __init__(self, *, origin: str, client: Any, test_loopback: bool = False,
                 cache: ApiReadCache | None = None, cache_partition: str | None = None,
                 bearer_supplier: Any | None = None) -> None:
        self.origin = validated_service_origin(origin, test_loopback=test_loopback)
        if client is None or not callable(getattr(client, "stream", None)):
            raise InvalidInputError("HTTP API requires controller-owned streaming HTTP client")
        self.client = client
        self.cache = cache
        if cache is not None and (not isinstance(cache_partition, str) or len(cache_partition) < 8):
            raise ScopeDeniedError("API cache requires an explicit controller-owned scope partition")
        self.cache_partition = cache_partition
        self.bearer_supplier = bearer_supplier

    async def call(self, operation: ApiOperation, arguments: Mapping[str, Any],
                   scope: ApiCallScope) -> ApiCallResult:
        if operation.protocol not in {"rest", "graphql"}:
            raise InvalidInputError("Selected API operation is not HTTP")
        # Freeze the mutable caller input *before* any authorization or await.
        approved_arguments = json.loads(canonical_json(dict(arguments)))
        scope.check(operation, approved_arguments)
        if not callable(scope.egress):
            raise ScopeDeniedError("API call requires qualified network egress admission")
        if operation.protocol == "graphql":
            if set(approved_arguments) - {"variables"}:
                raise InvalidInputError("GraphQL operation cannot override pinned document")
            url = self.origin + operation.target
            request_payload: Any = {"query": operation.graphql_document,
                                    "operationName": operation.name.replace("-", "_").replace(".", "_"),
                                    "variables": dict(approved_arguments.get("variables", {}))}
        else:
            request_payload = approved_arguments.get("body")
            url = _rest_url(self.origin, operation, approved_arguments, cursor=None)
        if len(url) > 2048:
            raise InvalidInputError("API target URL too long")
        use_cache = (operation.effect_class == "read_only" and self.cache is not None
                     and self.cache_partition is not None and self.bearer_supplier is None)
        cache_key = content_hash({
            "partition": self.cache_partition, "workspace": str(scope.intent.workspace_id),
            "policy": scope.intent.policy_revision_hash, "task": str(scope.intent.task_revision_id),
            "installation": scope.installation.fingerprint,
            "operation": operation.fingerprint, "arguments": approved_arguments, "origin": self.origin,
        }) if use_cache else None
        if cache_key is not None:
            cached = self.cache.get(cache_key) if self.cache is not None else None
            if cached is not None:
                # An admission check has already happened on this invocation.
                return cached
        deadline_at = time.monotonic() + scope.intent.max_wall_time_ms / 1000
        pages: list[Any] = []
        seen_cursors: set[str] = set()
        current_cursor: str | None = None
        attempts = 0
        total_bytes = 0
        while True:
            if operation.protocol == "rest":
                url = _rest_url(self.origin, operation, approved_arguments, current_cursor)
            if scope.egress(url) is not None:
                raise ScopeDeniedError("Egress authorization must approve by returning None")
            if current_cursor is not None and scope.authorize(operation, {**approved_arguments, "_approved_cursor": current_cursor}, scope.intent) is not None:
                raise ScopeDeniedError("Paginated API call was not admitted")
            headers = {"Accept": "application/graphql-response+json, application/json" if operation.protocol == "graphql" else "application/json"}
            if operation.method != "GET":
                headers["Content-Type"] = "application/json"
            bearer: str | None = None
            if self.bearer_supplier is not None:
                bearer = self.bearer_supplier()
                if not isinstance(bearer, str) or not 1 <= len(bearer) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in bearer):
                    raise ScopeDeniedError("Credential broker returned invalid bearer token")
                headers["Authorization"] = "Bearer " + bearer
            if operation.effect_class == "idempotent_write":
                headers["Idempotency-Key"] = scope.intent.idempotency_key
            is_read = operation.effect_class == "read_only"
            attempt_limit = 2 if is_read else 1
            last_status: int | None = None
            current_body: bytes | None = None
            for attempt in range(attempt_limit):
                remaining_s = deadline_at - time.monotonic()
                if remaining_s <= 0:
                    return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                         None, "api_deadline_exceeded", last_status, attempts, len(pages))
                attempts += 1
                try:
                    async with self.client.stream(
                        operation.method, url, headers=headers,
                        json=request_payload if operation.method != "GET" else None,
                        timeout=min(operation.timeout_ms / 1000, remaining_s), follow_redirects=False,
                    ) as response:
                        last_status = response.status_code
                        if 300 <= last_status < 400:
                            return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                                 None, "redirect_denied", last_status, attempts, len(pages))
                        if last_status in _RETRY_HTTP and is_read and attempt + 1 < attempt_limit:
                            delay = 0.02
                            if last_status == 429:
                                raw_after = response.headers.get("Retry-After", "")
                                if raw_after.isdigit():
                                    delay = min(float(raw_after), 2.0)
                            if time.monotonic() + delay >= deadline_at:
                                return ApiCallResult(operation.fingerprint, "failed", None,
                                                     "rate_limited", last_status, attempts, len(pages))
                            await asyncio.sleep(delay)
                            continue
                        if last_status < 200 or last_status >= 300:
                            status = "failed" if is_read else "indeterminate"
                            return ApiCallResult(operation.fingerprint, status, None,
                                                 "rate_limited" if last_status == 429 else "upstream_http_error",
                                                 last_status, attempts, len(pages))
                        if last_status not in operation.success_statuses:
                            return ApiCallResult(operation.fingerprint,
                                                 "failed" if is_read else "indeterminate",
                                                 None, "unexpected_success_status", last_status, attempts, len(pages))
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type not in _JSON_TYPES:
                            return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                                 None, "unexpected_content_type", last_status, attempts, len(pages))
                        chunks: list[bytes] = []
                        used = 0
                        async for chunk in response.aiter_bytes():
                            used += len(chunk)
                            if used + total_bytes > operation.max_output_bytes:
                                return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                                     None, "response_too_large", last_status, attempts, len(pages))
                            chunks.append(chunk)
                        current_body = b"".join(chunks)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception:
                    if attempt + 1 < attempt_limit:
                        await asyncio.sleep(0.01)
                        continue
                    return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                         None, "transport_unavailable", last_status, attempts, len(pages))
                if current_body is not None:
                    break
            if current_body is None:
                return ApiCallResult(operation.fingerprint, "failed", None, "transport_unavailable", last_status, attempts, len(pages))
            total_bytes += len(current_body)
            if bearer is not None and bearer.encode("utf-8") in current_body:
                return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                     None, "response_credential_exposure", last_status, attempts, len(pages))
            try:
                parsed = _safe_response_json(current_body)
                if operation.protocol == "graphql":
                    if not isinstance(parsed, dict) or set(parsed) - {"data", "errors", "extensions"}:
                        raise InvalidInputError("Invalid GraphQL response envelope")
                    if parsed.get("errors"):
                        return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                             None, "graphql_errors", last_status, attempts, len(pages))
                    parsed = parsed.get("data")
                validate_json(operation.output_schema_json, parsed, label="API response")
            except InvalidInputError:
                return ApiCallResult(operation.fingerprint, "failed" if is_read else "indeterminate",
                                     None, "response_schema_invalid", last_status, attempts, len(pages))
            pages.append(parsed)
            if not operation.next_cursor_field:
                result = ApiCallResult(operation.fingerprint, "succeeded", parsed, None, last_status, attempts, len(pages))
                break
            if not isinstance(parsed, dict):
                return ApiCallResult(operation.fingerprint, "failed", None, "pagination_shape_invalid", last_status, attempts, len(pages))
            next_cursor = parsed.get(operation.next_cursor_field)
            if next_cursor is None:
                result = ApiCallResult(operation.fingerprint, "succeeded", pages, None, last_status, attempts, len(pages))
                break
            if not isinstance(next_cursor, str) or not _SAFE_CURSOR.fullmatch(next_cursor) or next_cursor in seen_cursors:
                return ApiCallResult(operation.fingerprint, "failed", None, "pagination_cursor_invalid", last_status, attempts, len(pages))
            if len(pages) >= operation.max_pages:
                return ApiCallResult(operation.fingerprint, "failed", None, "pagination_limit", last_status, attempts, len(pages))
            seen_cursors.add(next_cursor)
            current_cursor = next_cursor
        if cache_key is not None and self.cache is not None:
            self.cache.put(cache_key, result)
        return result
