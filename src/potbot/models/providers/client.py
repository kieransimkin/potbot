"""Bounded HTTPS/SSE clients with no redirects, implicit proxies or paid-call retries."""

from __future__ import annotations

import codecs
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.models.providers.contracts import (
    CATALOG_ORIGINS,
    CatalogModel,
    ProviderAPIError,
    ProviderCompleted,
    ProviderEvent,
    ProviderRequest,
    ProviderTextDelta,
    default_protocol,
    is_known_non_text_model,
    model_name,
    provider_name,
)
from potbot.models.providers.protocols import (
    StreamDecoder,
    decode_document,
    parse_json,
    request_body,
)

if TYPE_CHECKING:
    import httpx

MAX_EVENT_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_MODELS = 10_000


def _sse(chunks: Iterator[bytes], max_bytes: int) -> Iterator[tuple[str, str]]:
    """Parse UTF-8 SSE across arbitrary transport chunk and CR/LF boundaries."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    data: list[str] = []
    name = ""
    used = event_bytes = event_count = 0
    first = True

    def line(value: str) -> tuple[str, str] | None:
        nonlocal name, event_bytes, event_count
        if value == "":
            if not data:
                name, event_bytes = "", 0
                return None
            event_count += 1
            if event_count > MAX_EVENTS:
                raise ProviderAPIError("provider_event_limit", dispatched=True)
            result = (name, "\n".join(data))
            data.clear()
            name, event_bytes = "", 0
            return result
        event_bytes += len(value.encode("utf-8"))
        if event_bytes > MAX_EVENT_BYTES:
            raise ProviderAPIError("provider_event_too_large", dispatched=True)
        if value.startswith(":"):
            return None
        field, _, content = value.partition(":")
        if content.startswith(" "):
            content = content[1:]
        if field == "data":
            data.append(content)
        elif field == "event":
            name = content
        return None

    try:
        for chunk in chunks:
            used += len(chunk)
            if used > max_bytes:
                raise ProviderAPIError("provider_response_too_large", dispatched=True)
            text = decoder.decode(chunk)
            if first and text:
                text = text.removeprefix("\ufeff")
                first = False
            pending += text
            while True:
                positions = [p for p in (pending.find("\n"), pending.find("\r")) if p >= 0]
                if not positions:
                    break
                index = min(positions)
                if pending[index] == "\r" and index == len(pending) - 1:
                    break
                size = 2 if pending[index : index + 2] == "\r\n" else 1
                result = line(pending[:index])
                pending = pending[index + size :]
                if result is not None:
                    yield result
            if len(pending.encode("utf-8")) > MAX_EVENT_BYTES:
                raise ProviderAPIError("provider_event_too_large", dispatched=True)
        pending += decoder.decode(b"", final=True)
    except UnicodeError:
        raise ProviderAPIError("invalid_provider_utf8", dispatched=True) from None
    if pending.endswith("\r"):
        result = line(pending[:-1])
        pending = ""
        if result is not None:
            yield result
    # An SSE event requires a blank-line terminator. Retain no truncated event.
    if pending or data:
        raise ProviderAPIError("incomplete_provider_event", dispatched=True)


def _identifier(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable():
        return value
    return None


def _limit(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2**53 - 1 else None


def _catalog_model(provider: str, record: object) -> CatalogModel:
    if not isinstance(record, dict):
        raise ProviderAPIError("invalid_model_catalog", dispatched=True)
    identity = record.get("name") if provider == "gemini" else record.get("id")
    try:
        model = model_name(identity)
    except InvalidInputError:
        raise ProviderAPIError("invalid_model_catalog", dispatched=True) from None
    if provider == "gemini":
        model = model.removeprefix("models/")
    title = record.get("displayName", record.get("display_name", model))
    if not isinstance(title, str) or not title.isprintable() or len(title) > 256:
        title = model
    modalities: tuple[str, ...] = ()
    raw_modalities = record.get("output_modalities", record.get("modalities"))
    if isinstance(raw_modalities, list):
        modalities = tuple(
            sorted(
                {
                    v
                    for v in raw_modalities
                    if isinstance(v, str) and v in {"text", "image", "audio", "video", "embedding"}
                }
            )
        )
    eligible: bool | None = None
    protocols: tuple[str, ...] = ()
    methods = record.get("supportedGenerationMethods")
    if provider == "anthropic":
        eligible, protocols = True, ("messages",)
    elif provider == "gemini" and isinstance(methods, list):
        eligible = "generateContent" in methods
        if eligible:
            protocols = ("gemini",)
    capability = record.get("capabilities")
    if isinstance(capability, dict) and type(capability.get("text_generation")) is bool:
        eligible = capability["text_generation"]
    model_type = record.get("type", record.get("model_type"))
    if isinstance(model_type, str) and model_type in {
        "image",
        "video",
        "audio",
        "embedding",
        "moderation",
        "image_generation",
        "video_generation",
        "audio_generation",
    }:
        eligible = False
    elif isinstance(model_type, str) and model_type in {
        "text",
        "chat",
        "language",
        "text_generation",
    }:
        eligible = True
    if modalities and "output_modalities" in record:
        eligible = "text" in modalities
    # A minimal OpenAI-compatible catalogue supplies names, not capabilities.
    # Recognize obvious non-text product families; leave all other cases unknown.
    if is_known_non_text_model(model):
        eligible = False
    if eligible is True and not protocols:
        protocols = (default_protocol(provider, model),)
    if eligible is False:
        protocols = ()
    return CatalogModel(
        provider,
        model,
        title,
        eligible,
        protocols,
        modalities,
        _limit(record.get("inputTokenLimit", record.get("max_input_tokens"))),
        _limit(record.get("outputTokenLimit", record.get("max_tokens"))),
    )


class HostedProviderClient:
    """Production requests require an injected controller disclosure authorizer.

    The caller owns budget reservation, durable dispatch journaling and terminal
    accounting. This transport never retries a request or treats a closed local
    stream as confirmation that the remote model stopped or was not billed.
    """

    def __init__(
        self,
        secret_resolver: Callable[[str], str],
        *,
        http_client: httpx.Client | None = None,
        authorize: Callable[[ProviderRequest], None] | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError:
            raise CapabilityUnavailableError(
                "Install Potbot's providers extra to use hosted APIs."
            ) from None
        self._httpx = httpx
        self._client = http_client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            transport=httpx.HTTPTransport(
                retries=0,
                trust_env=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            ),
        )
        self._owns_client = http_client is None
        self._secret_resolver = secret_resolver
        self._authorize = authorize
        self._lock = threading.Lock()
        self._active: dict[UUID, tuple[threading.Event, httpx.Response | None]] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self, provider: str, *, sse: bool = False) -> dict[str, str]:
        try:
            key = self._secret_resolver("secret://providers/" + provider)
        except Exception:
            raise ProviderAPIError("credential_unavailable", dispatched=False) from None
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 8192
            or any(not 33 <= ord(c) <= 126 for c in key)
        ):
            raise ProviderAPIError("credential_unavailable", dispatched=False)
        headers = {"Accept": "text/event-stream" if sse else "application/json"}
        if provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif provider == "gemini":
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
        return headers

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code != 200:
            code = (
                "provider_redirect_denied"
                if 300 <= response.status_code < 400
                else (
                    "provider_rate_limited"
                    if response.status_code == 429
                    else "provider_http_error"
                )
            )
            raise ProviderAPIError(
                code,
                dispatched=True,
                retryable=response.status_code in {429, 500, 502, 503, 504},
                status_code=response.status_code,
            )

    def stream(
        self,
        request: ProviderRequest,
        operation_id: UUID,
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[ProviderEvent]:
        if not isinstance(request, ProviderRequest) or not isinstance(operation_id, UUID):
            raise InvalidInputError("Hosted dispatch requires a typed request and operation UUID.")
        if self._authorize is None:
            raise ScopeDeniedError("Hosted generation requires a controller disclosure policy.")
        self._authorize(request)
        signal = threading.Event()
        with self._lock:
            if operation_id in self._active:
                raise InvalidInputError("This hosted operation is already active.")
            self._active[operation_id] = (signal, None)
        started = time.monotonic()
        dispatched = False

        def check() -> None:
            if signal.is_set() or (cancelled is not None and cancelled()):
                raise ProviderAPIError("cancelled", dispatched=dispatched)
            if time.monotonic() - started >= request.timeout_seconds:
                raise ProviderAPIError("deadline_exceeded", dispatched=dispatched)

        try:
            check()
            path, body = request_body(request)
            headers = self._headers(request.selection.provider, sse=True)
            if request.selection.protocol == "messages":
                headers["anthropic-version"] = "2023-06-01"
            check()
            timeout = self._httpx.Timeout(
                request.timeout_seconds,
                connect=min(10, request.timeout_seconds),
                read=min(30, request.timeout_seconds),
                write=min(10, request.timeout_seconds),
            )
            dispatched = True
            with self._client.stream(
                "POST",
                request.selection.origin + path,
                headers=headers,
                json=body,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                with self._lock:
                    self._active[operation_id] = (signal, response)
                check()
                self._check_status(response)
                request_id = _identifier(
                    response.headers.get("x-request-id", response.headers.get("request-id"))
                )
                wire_bound = request.max_response_bytes * 4 + 262144

                def chunks() -> Iterator[bytes]:
                    total = 0
                    for chunk in response.iter_bytes():
                        check()
                        total += len(chunk)
                        if total > wire_bound:
                            raise ProviderAPIError("provider_response_too_large", dispatched=True)
                        yield chunk
                    check()

                protocol = request.selection.protocol
                assert protocol is not None
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                if content_type == "application/json":
                    events = decode_document(protocol, parse_json(b"".join(chunks())), request_id)
                elif content_type == "text/event-stream":
                    decoder = StreamDecoder(protocol, request_id)

                    def streamed() -> Iterator[ProviderEvent]:
                        for name, data in _sse(chunks(), wire_bound):
                            yield from decoder.feed(name, data)
                        yield from decoder.finish()

                    events = streamed()
                else:
                    raise ProviderAPIError("unexpected_provider_content_type", dispatched=True)
                output_bytes = 0
                completed = False
                for event in events:
                    check()
                    if completed:
                        raise ProviderAPIError("event_after_terminal", dispatched=True)
                    if isinstance(event, ProviderTextDelta):
                        output_bytes += len(event.text.encode("utf-8"))
                        if output_bytes > request.max_response_bytes:
                            raise ProviderAPIError("provider_output_limit", dispatched=True)
                    elif isinstance(event, ProviderCompleted):
                        if (
                            event.usage is not None
                            and event.usage.output_tokens == 0
                            and output_bytes
                        ):
                            raise ProviderAPIError("invalid_provider_usage", dispatched=True)
                        completed = True
                    yield event
                    if completed:
                        # A protocol terminal is sufficient. Do not wait for the
                        # server to close a keep-alive stream after completion.
                        return
                if not completed:
                    raise ProviderAPIError("incomplete_provider_stream", dispatched=True)
        except ProviderAPIError:
            raise
        except self._httpx.TimeoutException:
            raise ProviderAPIError(
                "provider_timeout", dispatched=dispatched, retryable=True
            ) from None
        except (self._httpx.HTTPError, self._httpx.StreamError):
            if signal.is_set():
                raise ProviderAPIError("cancelled", dispatched=dispatched) from None
            raise ProviderAPIError(
                "provider_connection_error", dispatched=dispatched, retryable=True
            ) from None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ProviderAPIError("invalid_provider_response", dispatched=dispatched) from None
        except Exception:
            raise ProviderAPIError("provider_transport_error", dispatched=dispatched) from None
        finally:
            with self._lock:
                self._active.pop(operation_id, None)

    def stop(self, operation_id: UUID) -> bool:
        with self._lock:
            active = self._active.get(operation_id)
            if active is not None:
                active[0].set()
        if active is not None and active[1] is not None:
            active[1].close()
        return False

    def list_models(self, provider: str) -> list[CatalogModel]:
        """Read all bounded pages, preserving unknown capabilities as unknown."""
        provider_name(provider)
        deadline = time.monotonic() + 60
        headers = self._headers(provider)
        path = "/v1beta/models" if provider == "gemini" else "/v1/models"
        params: dict[str, str | int] = (
            {"pageSize": 1000}
            if provider == "gemini"
            else ({"limit": 1000} if provider == "anthropic" else {})
        )
        result: dict[str, CatalogModel] = {}
        cursors: set[str] = set()
        try:
            for _ in range(100):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderAPIError("model_catalog_deadline_exceeded", dispatched=True)
                with self._client.stream(
                    "GET",
                    CATALOG_ORIGINS[provider] + path,
                    headers=headers,
                    params=params,
                    timeout=min(15, remaining),
                    follow_redirects=False,
                ) as response:
                    self._check_status(response)
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise ProviderAPIError(
                                "model_catalog_deadline_exceeded", dispatched=True
                            )
                        data.extend(chunk)
                        if len(data) > MAX_CATALOG_BYTES:
                            raise ProviderAPIError("model_catalog_too_large", dispatched=True)
                    value = parse_json(bytes(data))
                if time.monotonic() >= deadline:
                    raise ProviderAPIError("model_catalog_deadline_exceeded", dispatched=True)
                records = value.get("models" if provider == "gemini" else "data")
                if not isinstance(records, list):
                    raise ProviderAPIError("invalid_model_catalog", dispatched=True)
                for record in records:
                    model = _catalog_model(provider, record)
                    result[model.model] = model
                if len(result) > MAX_MODELS:
                    raise ProviderAPIError("model_catalog_too_large", dispatched=True)
                cursor = (
                    value.get("nextPageToken")
                    if provider == "gemini"
                    else (value.get("last_id") if value.get("has_more") is True else None)
                )
                if not cursor:
                    if value.get("has_more") is True:
                        raise ProviderAPIError("invalid_catalog_pagination", dispatched=True)
                    return sorted(result.values(), key=lambda model: model.model)
                if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in cursors:
                    raise ProviderAPIError("invalid_catalog_pagination", dispatched=True)
                cursors.add(cursor)
                params["pageToken" if provider == "gemini" else "after_id"] = cursor
            raise ProviderAPIError("model_catalog_page_limit", dispatched=True)
        except ProviderAPIError:
            raise
        except self._httpx.HTTPError:
            raise ProviderAPIError(
                "model_catalog_unavailable", dispatched=True, retryable=True
            ) from None
        except Exception:
            raise ProviderAPIError("invalid_model_catalog", dispatched=True) from None
