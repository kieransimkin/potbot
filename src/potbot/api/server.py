"""Authenticated REST and replayable WebSocket adapters for the durable job service.

The server does not run jobs in request handlers. Workers and PostgreSQL remain
authoritative, including when several ASGI processes serve the same workspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import time
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from potbot import __version__
from potbot.api.dashboard import DASHBOARD_CSS, DASHBOARD_HTML, DASHBOARD_JS
from potbot.api.openai_compat import (
    CompatibilityError,
    OpenAICompatibility,
    chat_document,
    error_document,
    parse_chat,
    parse_responses,
    response_document,
    sse,
)
from potbot.api.tasks import Authenticator, TaskAPI
from potbot.api.wire import (
    APIResponse,
    OperationSemantics,
    RequestEnvelope,
    error_response,
    success_response,
)
from potbot.controller.tasks import TaskService, json_record
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    SchemaViolationError,
    ScopeDeniedError,
)
from potbot.models.profiles import default_model_profiles


class JobOperations(Protocol):
    """Synchronous transaction boundary implemented by the PostgreSQL JobService."""

    def submit(
        self, context: WorkspaceContext, payload: dict[str, Any], key: str
    ) -> dict[str, Any]: ...

    def list(
        self,
        context: WorkspaceContext,
        *,
        state: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]: ...

    def inspect(self, context: WorkspaceContext, job_id: UUID) -> dict[str, Any]: ...

    def cancel(self, context: WorkspaceContext, job_id: UUID, key: str) -> dict[str, Any]: ...

    def queue(self, context: WorkspaceContext) -> dict[str, Any]: ...

    def events(
        self,
        context: WorkspaceContext,
        job_id: UUID | None = None,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]: ...


class EventReadOperations(Protocol):
    """Minimal event-source protocol shared by jobs and SYN execution streams."""

    def events(
        self,
        context: WorkspaceContext,
        job_id: UUID | None = None,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]: ...


class ExecutionEventOperations(Protocol):
    """Existing workspace-scoped SYN ledger, with no execution authority."""

    def events(
        self,
        context: WorkspaceContext,
        *,
        after: int = 0,
        run_id: UUID | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...


class _ExecutionEventReadAdapter:
    """Adapt the generic WebSocket cursor protocol to a run-scoped ledger."""

    def __init__(self, service: ExecutionEventOperations) -> None:
        self.service = service

    def events(
        self,
        context: WorkspaceContext,
        job_id: UUID | None = None,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self.service.events(context, after=after, run_id=job_id, limit=limit)


CatalogReader = Callable[[WorkspaceContext], dict[str, Any]]
CatalogDiscovery = Callable[[WorkspaceContext, str], dict[str, Any]]
InvocationReader = Callable[[WorkspaceContext, UUID], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ServerSettings:
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    )
    max_body_bytes: int = 1_048_576
    max_frame_bytes: int = 524_288
    event_batch_size: int = 50
    body_timeout_seconds: float = 10.0
    authentication_timeout_seconds: float = 5.0
    send_timeout_seconds: float = 5.0
    poll_interval_seconds: float = 0.5
    heartbeat_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_origins, tuple) or not self.allowed_origins:
            raise InvalidInputError("at least one exact browser origin is required")
        for origin in self.allowed_origins:
            if not isinstance(origin, str):
                raise InvalidInputError("browser origins must be exact HTTP origins")
            try:
                parsed = urlsplit(origin)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.path == parsed.query == parsed.fragment == ""
                    and parsed.port != 0
                    and "*" not in origin
                )
            except ValueError:
                valid = False
            if not valid:
                raise InvalidInputError("browser origins must be exact HTTP origins")
        for name, upper in (
            ("max_body_bytes", 4_194_304),
            ("max_frame_bytes", 1_048_576),
            ("event_batch_size", 200),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise InvalidInputError("server limits must be positive and bounded")
        for name in (
            "body_timeout_seconds",
            "authentication_timeout_seconds",
            "send_timeout_seconds",
            "poll_interval_seconds",
            "heartbeat_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise InvalidInputError("server deadlines must be finite and bounded")
            if not math.isfinite(value) or not 0 < value <= 120:
                raise InvalidInputError("server deadlines must be finite and bounded")


_DEFAULT_SETTINGS = ServerSettings()
_LOGGER = logging.getLogger(__name__)
_JSON_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_DASHBOARD_HEADERS = {
    **_JSON_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
}
_BODY_DOCUMENTATION = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["schema_version", "request_id", "idempotency_key", "payload"],
                    "properties": {
                        "schema_version": {"type": "integer", "const": 1},
                        "request_id": {"type": "string", "format": "uuid"},
                        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
                        "payload": {"type": "object"},
                    },
                }
            }
        },
    },
    "security": [{"BearerAuth": []}],
}


def _response(value: APIResponse) -> JSONResponse:
    return JSONResponse(json_record(value.body), status_code=value.status, headers=_JSON_HEADERS)


def _json(value: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = item
        return result

    def constant(_value: str) -> None:
        raise ValueError("nonfinite JSON")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        raise SchemaViolationError("invalid bounded JSON") from None


def _uuid(value: str) -> UUID:
    try:
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (ValueError, AttributeError):
        raise InvalidInputError("expected a canonical UUID") from None


def _integer(value: str | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not value.isascii() or not value.isdecimal() or len(value) > 16:
        raise InvalidInputError("invalid integer query parameter")
    result = int(value)
    if not 0 <= result <= maximum:
        raise InvalidInputError("integer query parameter is out of range")
    return result


def _queries(request: Request, allowed: set[str]) -> None:
    pairs = request.query_params.multi_items()
    if any(key not in allowed for key, _ in pairs) or len(pairs) != len(dict(pairs)):
        raise InvalidInputError("unsupported or repeated query parameter")


async def _body(request: Request, settings: ServerSettings) -> RequestEnvelope:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise SchemaViolationError("expected an application/json envelope")
    data = bytearray()
    try:
        async with asyncio.timeout(settings.body_timeout_seconds):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > settings.max_body_bytes:
                    raise SchemaViolationError("request body is too large")
        envelope = RequestEnvelope.from_dict(_json(data.decode("utf-8")))
    except (TimeoutError, UnicodeDecodeError):
        raise SchemaViolationError("invalid request body") from None
    request.state.request_id = envelope.request_id
    request.state.idempotency_key = envelope.idempotency_key
    if envelope.idempotency_key is None:
        raise SchemaViolationError("mutating requests require an idempotency key")
    return envelope


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    # Existing PostgreSQL services are synchronous; avoid blocking every socket
    # while one request waits on the database or a bounded catalogue request.
    return await run_in_threadpool(partial(function, *args, **kwargs))


class _SanitizedErrors:
    """Keep unexpected exception text out of the ASGI transport's traceback log.

    Starlette's outer ServerErrorMiddleware deliberately re-raises even after an
    error handler sends a safe response. Catch within that boundary instead.
    Expected domain errors continue through the normal exception handlers.
    """

    def __init__(
        self,
        app: ASGIApp,
        on_error: Callable[[Request, Exception], Awaitable[JSONResponse]],
    ) -> None:
        self.app, self.on_error = app, on_error

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as error:
            # Do not attach exception objects, tracebacks, request URLs, headers
            # or body data. Operators still receive an error-count signal.
            _LOGGER.error("Unexpected HTTP operation failure; diagnostic payload omitted.")
            if not started:
                response = await self.on_error(Request(scope), error)
                await response(scope, receive, send)
            # After a partial response the transport will close the incomplete
            # response. Never re-raise the secret-bearing original exception.


def create_app(
    *,
    task_service: TaskService,
    job_service: JobOperations,
    authenticate: Authenticator,
    model_catalog: CatalogReader | None = None,
    discover_models: CatalogDiscovery | None = None,
    invocation_reader: InvocationReader | None = None,
    invocation_list_reader: InvocationReader | None = None,
    execution_events: ExecutionEventOperations | None = None,
    openai_compat: OpenAICompatibility | None = None,
    reasoning_store: Any | None = None,
    settings: ServerSettings = _DEFAULT_SETTINGS,
) -> FastAPI:
    """Create a server with explicitly supplied identity and application services."""
    app = FastAPI(
        title="Potbot job and task API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    task_api = TaskAPI(task_service, authenticate)

    def context(request: Request) -> WorkspaceContext:
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            raise ScopeDeniedError("browser origin is unavailable")
        return authenticate(request.headers.get("authorization"))

    @app.exception_handler(Exception)
    @app.exception_handler(DomainError)
    async def unknown_error(request: Request, error: Exception) -> JSONResponse:
        return _response(
            error_response(
                error,
                request_id=getattr(request.state, "request_id", None),
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=getattr(request.state, "idempotency_key", None) is not None,
                ),
            )
        )

    app.add_middleware(_SanitizedErrors, on_error=unknown_error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return _response(error_response(SchemaViolationError("request validation failed")))

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        kind = ScopeDeniedError if error.status_code == 404 else InvalidInputError
        return _response(error_response(kind("unsupported HTTP operation")))

    @app.get("/", include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML, headers=_DASHBOARD_HEADERS)

    @app.get("/assets/dashboard.js", include_in_schema=False)
    async def dashboard_script() -> Response:
        return Response(DASHBOARD_JS, media_type="text/javascript", headers=_JSON_HEADERS)

    @app.get("/assets/dashboard.css", include_in_schema=False)
    async def dashboard_style() -> Response:
        return Response(DASHBOARD_CSS, media_type="text/css", headers=_JSON_HEADERS)

    @app.get("/health")
    async def health() -> JSONResponse:
        # Liveness only; it deliberately makes no assertion about DB/worker health.
        return _response(success_response(uuid4(), {"status": "listening", "version": __version__}))

    @app.get("/v1/queue")
    async def queue(request: Request) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        return _response(success_response(uuid4(), await _call(job_service.queue, actor)))

    @app.get("/v1/jobs")
    async def jobs(request: Request) -> JSONResponse:
        actor = context(request)
        _queries(request, {"state", "after", "limit"})
        limit = _integer(request.query_params.get("limit"), 50, 200)
        if not limit:
            raise InvalidInputError("page size must be positive")
        after = request.query_params.get("after")
        if after is not None and (not after or len(after) > 512):
            raise InvalidInputError("invalid page cursor")
        result = await _call(
            job_service.list,
            actor,
            state=request.query_params.get("state"),
            after=after,
            limit=limit,
        )
        return _response(success_response(uuid4(), result))

    @app.post("/v1/jobs", status_code=202, openapi_extra=_BODY_DOCUMENTATION)
    async def submit(request: Request) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        envelope = await _body(request, settings)
        assert envelope.idempotency_key is not None
        try:
            result = await _call(
                job_service.submit, actor, envelope.payload, envelope.idempotency_key
            )
            return _response(success_response(envelope.request_id, result, status=202))
        except Exception as error:
            return _response(
                error_response(
                    error,
                    request_id=envelope.request_id,
                    semantics=OperationSemantics(idempotency_key=True),
                )
            )

    @app.get("/v1/jobs/events")
    async def workspace_events(request: Request) -> JSONResponse:
        return await event_page(request, None)

    async def event_page(request: Request, job_id: UUID | None) -> JSONResponse:
        actor = context(request)
        _queries(request, {"after", "limit"})
        after = _integer(request.query_params.get("after"), 0, (1 << 53) - 1)
        limit = _integer(request.query_params.get("limit"), 100, 200)
        if not limit:
            raise InvalidInputError("page size must be positive")
        result = await _call(job_service.events, actor, job_id, after=after, limit=limit)
        return _response(success_response(uuid4(), result))

    @app.get("/v1/jobs/{job_id}")
    async def inspect_job(request: Request, job_id: str) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        result = await _call(job_service.inspect, actor, _uuid(job_id))
        return _response(success_response(uuid4(), result))

    @app.get("/v1/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str) -> JSONResponse:
        return await event_page(request, _uuid(job_id))

    if execution_events is not None:
        # Event history is read-only here. Publishing events remains a trusted
        # controller operation, never a public HTTP/WebSocket mutation.
        async def syn_page(request: Request, run_id: UUID | None) -> JSONResponse:
            actor = context(request)
            _queries(request, {"after", "limit"})
            after = _integer(request.query_params.get("after"), 0, (1 << 53) - 1)
            limit = _integer(request.query_params.get("limit"), 50, 200)
            if limit == 0:
                raise InvalidInputError("page size must be positive")
            page = await _call(
                execution_events.events, actor, after=after, run_id=run_id, limit=limit
            )
            return _response(success_response(uuid4(), page))

        @app.get("/v1/executions/events")
        async def all_execution_events(request: Request) -> JSONResponse:
            return await syn_page(request, None)

        @app.get("/v1/executions/{run_id}/events")
        async def run_execution_events(request: Request, run_id: str) -> JSONResponse:
            return await syn_page(request, _uuid(run_id))

    @app.get("/v1/jobs/{job_id}/invocations")
    async def job_invocations(request: Request, job_id: str) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        identifier = _uuid(job_id)
        if invocation_list_reader is None:
            raise CapabilityUnavailableError("hosted invocation listing is not configured")
        result = await _call(invocation_list_reader, actor, identifier)
        return _response(success_response(uuid4(), result))

    @app.post("/v1/jobs/{job_id}/cancel", openapi_extra=_BODY_DOCUMENTATION)
    async def cancel_job(request: Request, job_id: str) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        envelope = await _body(request, settings)
        if envelope.payload:
            raise SchemaViolationError("cancellation payload must be empty")
        assert envelope.idempotency_key is not None
        try:
            result = await _call(job_service.cancel, actor, _uuid(job_id), envelope.idempotency_key)
            return _response(success_response(envelope.request_id, result))
        except Exception as error:
            return _response(
                error_response(
                    error,
                    request_id=envelope.request_id,
                    semantics=OperationSemantics(idempotency_key=True),
                )
            )

    @app.get("/v1/potbot/models")
    async def models(request: Request) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        result = (
            await _call(model_catalog, actor)
            if model_catalog is not None
            else {"local": [profile.to_dict() for profile in default_model_profiles()]}
        )
        return _response(success_response(uuid4(), result))

    # The public OpenAI routes do not use Potbot's v1 command envelope. Preserve
    # the native catalogue at /v1/potbot/models for the dashboard and older tools.
    def openai_error(error: Exception) -> JSONResponse:
        from potbot.models.providers.contracts import ProviderAPIError
        if isinstance(error, CompatibilityError):
            failure = error
        elif isinstance(error, ProviderAPIError):
            status = 429 if error.status_code == 429 else 502
            failure = CompatibilityError("Hosted provider request failed.", status=status,
                                         code="provider_error")
        elif isinstance(error, (InvalidInputError, SchemaViolationError)):
            failure = CompatibilityError("Invalid or unsupported request.")
        elif isinstance(error, ScopeDeniedError):
            failure = CompatibilityError("The resource is unavailable.", status=404,
                                         code="model_not_found")
        elif isinstance(error, CapabilityUnavailableError):
            failure = CompatibilityError("The requested model is unavailable.", status=403,
                                         code="model_unavailable")
        else:
            failure = CompatibilityError("Inference failed.", status=500,
                                         code="internal_error")
        return JSONResponse(error_document(failure), status_code=failure.status,
                            headers=_JSON_HEADERS)

    async def openai_body(request: Request) -> Any:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise CompatibilityError("Expected application/json.")
        data = bytearray()
        try:
            async with asyncio.timeout(settings.body_timeout_seconds):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > settings.max_body_bytes:
                        raise CompatibilityError("Request body is too large.")
            return _json(data.decode("utf-8"))
        except (TimeoutError, UnicodeError, SchemaViolationError):
            raise CompatibilityError("Invalid bounded JSON request.") from None

    @app.get("/v1/models")
    async def openai_models(request: Request) -> JSONResponse:
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None:
                raise CompatibilityError("Inference gateway is not configured.", status=503)
            data = await _call(openai_compat.models, actor)
            return JSONResponse(data, headers=_JSON_HEADERS)
        except Exception as error:
            return openai_error(error)

    @app.get("/v1/models/{model_id:path}")
    async def openai_model(request: Request, model_id: str) -> JSONResponse:
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None:
                raise CompatibilityError("Inference gateway is not configured.", status=503)
            return JSONResponse(await _call(openai_compat.model, actor, model_id),
                                headers=_JSON_HEADERS)
        except Exception as error:
            return openai_error(error)

    @app.get("/v1/reasoning/{run_id}")
    async def reasoning_state(request: Request, run_id: str) -> JSONResponse:
        try:
            actor = context(request)
            _queries(request, set())
            if reasoning_store is None:
                raise CompatibilityError("Reasoning state is not configured.", status=503)
            return JSONResponse(await _call(reasoning_store.inspect, actor, run_id),
                                headers=_JSON_HEADERS)
        except ScopeDeniedError:
            return openai_error(CompatibilityError("Reasoning run not found.", status=404))
        except Exception as error:
            return openai_error(error)

    @app.get("/v1/reasoning/{run_id}/events")
    async def reasoning_events_page(request: Request, run_id: str) -> JSONResponse:
        try:
            actor = context(request)
            _queries(request, {"after", "limit"})
            if reasoning_store is None:
                raise CompatibilityError("Reasoning state is not configured.", status=503)
            after = _integer(request.query_params.get("after"), 0, (1 << 53) - 1)
            limit = _integer(request.query_params.get("limit"), 100, 200)
            if not limit:
                raise CompatibilityError("Page size must be positive.")
            events = await _call(reasoning_store.events, actor, run_id, after=after,
                                 limit=limit)
            return JSONResponse({"object": "list", "data": events}, headers=_JSON_HEADERS)
        except ScopeDeniedError:
            return openai_error(CompatibilityError("Reasoning run not found.", status=404))
        except Exception as error:
            return openai_error(error)

    @app.post("/v1/reasoning/{run_id}/steer", status_code=202)
    async def steer_reasoning(request: Request, run_id: str) -> JSONResponse:
        """Accept a new prompt while the tree owner continues independently."""
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None or openai_compat.reasoning_investigator is None:
                raise CompatibilityError("Live reasoning steering is not configured.", status=503)
            body = await openai_body(request)
            if not isinstance(body, dict) or set(body) != {"request_id", "prompt"}:
                raise CompatibilityError("Expected request_id and prompt only.")
            inbox = openai_compat.reasoning_investigator.steering_inbox
            result = await _call(inbox.submit,
                                 actor if getattr(inbox, "uses_context", False) else actor.workspace_id, run_id,
                                 request_id=body["request_id"], prompt=body["prompt"])
            return JSONResponse(result, status_code=202 if result["status"] == "queued" else 200,
                                headers=_JSON_HEADERS)
        except ScopeDeniedError:
            return openai_error(CompatibilityError("Reasoning run not found.", status=404))
        except InvalidInputError:
            return openai_error(CompatibilityError("Invalid steering prompt or run state."))
        except Exception as error:
            return openai_error(error)

    @app.get("/v1/reasoning/{run_id}/steering/{request_id}")
    async def inspect_steering(request: Request, run_id: str, request_id: str) -> JSONResponse:
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None or openai_compat.reasoning_investigator is None:
                raise CompatibilityError("Live reasoning steering is not configured.", status=503)
            inbox = openai_compat.reasoning_investigator.steering_inbox
            result = await _call(inbox.get,
                                 actor if getattr(inbox, "uses_context", False) else actor.workspace_id,
                                 run_id, request_id)
            return JSONResponse(result, headers=_JSON_HEADERS)
        except ScopeDeniedError:
            return openai_error(CompatibilityError("Steering prompt not found.", status=404))
        except InvalidInputError:
            return openai_error(CompatibilityError("Invalid reasoning or prompt identity."))
        except Exception as error:
            return openai_error(error)

    @app.get("/v1/reasoning/{run_id}/steering")
    async def list_steering(request: Request, run_id: str) -> JSONResponse:
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None or openai_compat.reasoning_investigator is None:
                raise CompatibilityError("Live reasoning steering is not configured.", status=503)
            inbox = openai_compat.reasoning_investigator.steering_inbox
            result = await _call(inbox.history,
                                 actor if getattr(inbox, "uses_context", False) else actor.workspace_id,
                                 run_id)
            return JSONResponse({"object": "list", "data": result}, headers=_JSON_HEADERS)
        except ScopeDeniedError:
            return openai_error(CompatibilityError("Reasoning run not found.", status=404))
        except InvalidInputError:
            return openai_error(CompatibilityError("Invalid reasoning identity."))
        except Exception as error:
            return openai_error(error)

    async def inference(request: Request, *, responses: bool) -> Response:
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None:
                raise CompatibilityError("Inference gateway is not configured.", status=503)
            options = (parse_responses if responses else parse_chat)(await openai_body(request))
            # Check admission before beginning the stream so a bad model is a
            # normal OpenAI JSON error, never a half-open SSE response.
            await _call(openai_compat._route, actor, options["model"])
            identifier = ("resp_" if responses else "chatcmpl-") + uuid4().hex
            created = int(time())
            run_id = uuid4().hex
            if not options["stream"]:
                result = await _call(openai_compat.stream, actor, options, lambda _: None, run_id)
                data = (response_document(identifier, created, options["model"], result)
                        if responses else chat_document(identifier, created, options["model"], result))
                return JSONResponse(data, headers={**_JSON_HEADERS,
                                                   "X-Potbot-Run-Id": run_id})

            def events() -> Any:
                # A bounded producer queue keeps the provider's blocking HTTP
                # work out of the ASGI loop and allows disconnect cancellation.
                queue: Queue[tuple[str, Any]] = Queue(maxsize=64)
                stopped = Event()

                def send(kind: str, value: Any) -> None:
                    while not stopped.is_set():
                        try:
                            queue.put((kind, value), timeout=0.2)
                            return
                        except Full:
                            continue

                def emit(delta: str) -> None:
                    send("delta", delta)

                def work() -> None:
                    try:
                        result = openai_compat.stream(actor, options, emit, run_id,
                                                      cancelled=stopped.is_set)
                        send("done", result)
                    except Exception as error:
                        send("error", error)

                worker = Thread(target=work, name="potbot-openai-" + run_id[:8], daemon=True)
                worker.start()
                try:
                    if responses:
                        yield sse({"type": "response.created", "response": {
                            "id": identifier, "object": "response", "model": options["model"],
                            "status": "in_progress", "created_at": created}}, "response.created")
                    else:
                        yield sse({"id": identifier, "object": "chat.completion.chunk",
                                   "created": created, "model": options["model"],
                                   "choices": [{"index": 0, "delta": {"role": "assistant"},
                                                "finish_reason": None}]})
                    while True:
                        try:
                            kind, value = queue.get(timeout=0.5)
                        except Empty:
                            if not worker.is_alive():
                                break
                            continue
                        if kind == "delta":
                            if responses:
                                yield sse({"type": "response.output_text.delta", "delta": value,
                                           "output_index": 0, "content_index": 0},
                                          "response.output_text.delta")
                            else:
                                yield sse({"id": identifier, "object": "chat.completion.chunk",
                                           "created": created, "model": options["model"],
                                           "choices": [{"index": 0, "delta": {"content": value},
                                                        "finish_reason": None}]})
                        elif kind == "done":
                            if responses:
                                yield sse({"type": "response.completed", "response":
                                           response_document(identifier, created, options["model"], value)},
                                          "response.completed")
                            else:
                                yield sse({"id": identifier, "object": "chat.completion.chunk",
                                           "created": created, "model": options["model"],
                                           "choices": [{"index": 0, "delta": {},
                                                        "finish_reason": value.finish_reason}]})
                                if options["include_usage"]:
                                    yield sse({"id": identifier, "object": "chat.completion.chunk",
                                               "created": created, "model": options["model"],
                                               "choices": [], "usage": value.usage})
                            yield sse("[DONE]")
                            break
                        elif kind == "error":
                            yield sse(error_document(CompatibilityError("Inference failed.",
                                                                           status=502, code="provider_error")), "error")
                            yield sse("[DONE]")
                            break
                finally:
                    stopped.set()
                    worker.join(timeout=0.2)

            return StreamingResponse(events(), media_type="text/event-stream", headers={
                **_JSON_HEADERS, "X-Accel-Buffering": "no", "X-Potbot-Run-Id": run_id})
        except Exception as error:
            return openai_error(error)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await inference(request, responses=False)

    @app.post("/v1/responses")
    async def responses_create(request: Request) -> Response:
        return await inference(request, responses=True)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        try:
            actor = context(request)
            _queries(request, set())
            if openai_compat is None:
                raise CompatibilityError("Inference gateway is not configured.", status=503)
            body = await openai_body(request)
            result = await _call(openai_compat.embeddings, actor, body, uuid4().hex)
            return JSONResponse(result, headers=_JSON_HEADERS)
        except Exception as error:
            return openai_error(error)

    @app.post("/v1/providers/{provider}/models/discover", openapi_extra=_BODY_DOCUMENTATION)
    async def discover(request: Request, provider: str) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        envelope = await _body(request, settings)
        if envelope.payload:
            raise SchemaViolationError("catalogue discovery payload must be empty")
        if discover_models is None:
            raise CapabilityUnavailableError("catalogue discovery is not configured")
        result = await _call(discover_models, actor, provider)
        return _response(success_response(envelope.request_id, result))

    @app.get("/v1/invocations/{invocation_id}")
    async def inspect_invocation(request: Request, invocation_id: str) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        identifier = _uuid(invocation_id)
        if invocation_reader is None:
            raise CapabilityUnavailableError("hosted invocation inspection is not configured")
        result = await _call(invocation_reader, actor, identifier)
        return _response(success_response(uuid4(), result))

    async def task_operation(request: Request) -> JSONResponse:
        actor = context(request)
        _queries(request, set())
        if request.method == "GET":
            body: dict[str, Any] = {
                "schema_version": 1,
                "request_id": str(uuid4()),
                "idempotency_key": None,
                "payload": {},
            }
        else:
            envelope = await _body(request, settings)
            body = {
                "schema_version": 1,
                "request_id": str(envelope.request_id),
                "idempotency_key": envelope.idempotency_key,
                "payload": envelope.payload,
            }
        result = await _call(task_api.dispatch, actor, request.method, request.url.path, body)
        return _response(result)

    @app.post("/v1/tasks", status_code=202, openapi_extra=_BODY_DOCUMENTATION)
    async def submit_task(request: Request) -> JSONResponse:
        return await task_operation(request)

    @app.get("/v1/tasks/{episode_id}")
    async def inspect_task(request: Request, episode_id: str) -> JSONResponse:
        return await task_operation(request)

    @app.get("/v1/tasks/{episode_id}/contracts")
    async def task_contracts(request: Request, episode_id: str) -> JSONResponse:
        return await task_operation(request)

    @app.get("/v1/tasks/{episode_id}/budget")
    async def task_budget(request: Request, episode_id: str) -> JSONResponse:
        return await task_operation(request)

    @app.post("/v1/tasks/{episode_id}/contracts", openapi_extra=_BODY_DOCUMENTATION)
    async def revise_task(request: Request, episode_id: str) -> JSONResponse:
        return await task_operation(request)

    @app.post("/v1/tasks/{episode_id}/control", openapi_extra=_BODY_DOCUMENTATION)
    async def control_task(request: Request, episode_id: str) -> JSONResponse:
        return await task_operation(request)

    @app.post("/v1/tasks/{episode_id}/start-generation", openapi_extra=_BODY_DOCUMENTATION)
    async def start_task_generation(request: Request, episode_id: str) -> JSONResponse:
        return await task_operation(request)

    @app.websocket("/v1/jobs/stream")
    async def stream_workspace(websocket: WebSocket) -> None:
        await _stream(websocket, None, job_service, authenticate, settings)

    @app.websocket("/v1/jobs/{job_id}/stream")
    async def stream_job(websocket: WebSocket, job_id: str) -> None:
        try:
            identifier = _uuid(job_id)
        except InvalidInputError:
            await websocket.close(code=1008)
            return
        await _stream(websocket, identifier, job_service, authenticate, settings)

    if execution_events is not None:
        # Reuse authenticated bounded cursor replay, no in-memory fanout.
        event_reader = _ExecutionEventReadAdapter(execution_events)

        @app.websocket("/v1/executions/stream")
        async def stream_execution_events(websocket: WebSocket) -> None:
            await _stream(websocket, None, event_reader, authenticate, settings)

        @app.websocket("/v1/executions/{run_id}/stream")
        async def stream_execution_run(websocket: WebSocket, run_id: str) -> None:
            try:
                identifier = _uuid(run_id)
            except InvalidInputError:
                await websocket.close(code=1008)
                return
            await _stream(websocket, identifier, event_reader, authenticate, settings)

    schema = app.openapi()
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }
    for path, operations in schema["paths"].items():
        if path.startswith("/v1/"):
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [{"BearerAuth": []}]
    # These parameters are parsed by the bounded adapter rather than FastAPI's
    # automatic body parser, so declare their public contract explicitly.
    job_query = [
        {
            "name": "state",
            "in": "query",
            "schema": {
                "type": "string",
                "enum": [
                    "queued",
                    "running",
                    "cancelling",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "indeterminate",
                ],
            },
        },
        {
            "name": "after",
            "in": "query",
            "description": "Opaque next_cursor from the preceding jobs page.",
            "schema": {"type": "string", "maxLength": 512},
        },
        {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
    ]
    schema["paths"]["/v1/jobs"]["get"]["parameters"] = job_query
    for path in ("/v1/jobs/events", "/v1/jobs/{job_id}/events"):
        operation = schema["paths"][path]["get"]
        operation.setdefault("parameters", []).extend(
            [
                {
                    "name": "after",
                    "in": "query",
                    "description": "Last received durable event sequence; replay is exclusive.",
                    "schema": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": (1 << 53) - 1,
                        "default": 0,
                    },
                },
                {
                    "name": "limit",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                },
            ]
        )
    if execution_events is not None:
        for path in ("/v1/executions/events", "/v1/executions/{run_id}/events"):
            schema["paths"][path]["get"].setdefault("parameters", []).extend(
                [
                    {
                        "name": "after",
                        "in": "query",
                        "description": "Exclusive, commit-ordered workspace event cursor.",
                        "schema": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": (1 << 53) - 1,
                            "default": 0,
                        },
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                    },
                ]
            )
    return app


async def _stream(
    websocket: WebSocket,
    job_id: UUID | None,
    service: EventReadOperations,
    authenticate: Authenticator,
    settings: ServerSettings,
) -> None:
    origin = websocket.headers.get("origin")
    if websocket.query_params or (origin is not None and origin not in settings.allowed_origins):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send(message: dict[str, Any]) -> None:
        encoded = json.dumps(json_record(message), separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > settings.max_frame_bytes:
            raise SchemaViolationError("event exceeds stream frame limit")
        async with send_lock:
            async with asyncio.timeout(settings.send_timeout_seconds):
                await websocket.send_text(encoded)

    async def text_frame() -> str:
        try:
            return await websocket.receive_text()
        except KeyError:
            raise SchemaViolationError("stream messages must be text frames") from None

    try:
        async with asyncio.timeout(settings.authentication_timeout_seconds):
            raw = await text_frame()
        if len(raw.encode("utf-8")) > 4096:
            raise SchemaViolationError("authentication frame is too large")
        auth = _json(raw)
        if not isinstance(auth, dict) or set(auth) != {"type", "token", "after"}:
            raise SchemaViolationError("expected a stream authentication frame")
        if auth["type"] != "authenticate" or not isinstance(auth["token"], str):
            raise SchemaViolationError("invalid stream authentication")
        cursor = auth["after"]
        if type(cursor) is not int or not 0 <= cursor <= (1 << 53) - 1:
            raise InvalidInputError("invalid stream cursor")
        actor = authenticate("Bearer " + auth["token"])
        # Recheck database membership and resource scope before acknowledging auth.
        initial = await _call(
            service.events, actor, job_id, after=cursor, limit=settings.event_batch_size
        )
        await send({"schema_version": 1, "type": "ready", "cursor": cursor})
        # Do not retain a raw token in the long-lived stream coroutine.
        del auth, raw

        async def produce() -> None:
            nonlocal cursor
            page: dict[str, Any] | None = initial
            last_sent = asyncio.get_running_loop().time()
            while True:
                if page is None:
                    page = await _call(
                        service.events, actor, job_id, after=cursor, limit=settings.event_batch_size
                    )
                for event in page["items"]:
                    sequence = event["sequence"]
                    if type(sequence) is not int or sequence <= cursor:
                        raise SchemaViolationError("durable stream cursor is not increasing")
                    await send(
                        {"schema_version": 1, "type": "event", "cursor": sequence, "event": event}
                    )
                    cursor = sequence
                    last_sent = asyncio.get_running_loop().time()
                more = page["has_more"]
                page = None
                if not more:
                    now = asyncio.get_running_loop().time()
                    if now - last_sent >= settings.heartbeat_seconds:
                        await send({"schema_version": 1, "type": "heartbeat", "cursor": cursor})
                        last_sent = now
                    await asyncio.sleep(settings.poll_interval_seconds)

        async def receive() -> None:
            while True:
                text = await text_frame()
                if len(text.encode("utf-8")) > 64 or _json(text) != {"type": "ping"}:
                    raise SchemaViolationError("unsupported stream control frame")
                await send({"schema_version": 1, "type": "pong", "cursor": cursor})

        pending = [asyncio.create_task(produce()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        # The ASGI server can cancel a connection scope during shutdown or
        # TestClient teardown after the last event has been delivered. This is
        # a normal lifecycle transition, not an application/protocol error.
        # Do not attempt a final WebSocket send on a cancelled connection.
        return
    except WebSocketDisconnect:
        return
    except TimeoutError:
        await websocket.close(code=1013, reason="Reconnect with your last received cursor.")
    except Exception as error:
        try:
            await send(
                {"schema_version": 1, "type": "error", "error": error_response(error).body["error"]}
            )
            code = (
                1008
                if isinstance(error, ScopeDeniedError | InvalidInputError | SchemaViolationError)
                else 1011
            )
            await websocket.close(code=code)
        except (WebSocketDisconnect, RuntimeError, TimeoutError):
            pass
