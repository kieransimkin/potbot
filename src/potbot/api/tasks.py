"""Framework-independent v1 task API and an explicitly configured development WSGI app."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

from potbot.api.wire import (
    APIResponse,
    OperationSemantics,
    RequestEnvelope,
    error_response,
    success_response,
)
from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, SchemaViolationError, ScopeDeniedError
from potbot.domain.tasks import ContractSpec

Authenticator = Callable[[str | None], WorkspaceContext]


def _fields(payload: dict[str, Any], required: set[str]) -> None:
    if set(payload) != required:
        raise SchemaViolationError("payload fields do not match this operation")


def _uuid(value: object) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        identifier = UUID(value)
        if str(identifier) != value:
            raise ValueError
        return identifier
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("expected a canonical UUID") from None


class DevelopmentBearerAuth:
    """Explicit fixture authenticator; provisioned database identity remains authoritative.

    A production identity provider can implement the same Authenticator boundary.
    Tokens are never accepted from task payloads or persisted in task configuration.
    """

    def __init__(self, tokens: Mapping[str, WorkspaceContext]) -> None:
        if not tokens or any(not isinstance(token, str) or len(token) < 32 for token in tokens):
            raise InvalidInputError("development bearer tokens need at least 32 characters")
        self._bindings = tuple(
            (hashlib.sha256(token.encode()).digest(), context) for token, context in tokens.items()
        )

    def __call__(self, authorization: str | None) -> WorkspaceContext:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise ScopeDeniedError("authenticated workspace context is required")
        supplied = hashlib.sha256(authorization[7:].encode()).digest()
        for expected, context in self._bindings:
            if hmac.compare_digest(supplied, expected):
                return context
        raise ScopeDeniedError("authenticated workspace context is required")


class TaskAPI:
    def __init__(self, service: TaskService, authenticate: Authenticator) -> None:
        self.service = service
        self.authenticate = authenticate

    def handle(
        self,
        method: str,
        path: str,
        request: object,
        *,
        authorization: str | None = None,
    ) -> APIResponse:
        try:
            context = self.authenticate(authorization)
        except Exception as error:
            return error_response(error)
        return self.dispatch(context, method, path, request)

    def dispatch(
        self,
        context: WorkspaceContext,
        method: str,
        path: str,
        request: object,
    ) -> APIResponse:
        """Trusted in-process boundary used by the CLI after explicit dev auth setup."""
        envelope: RequestEnvelope | None = None
        semantics = OperationSemantics(read_only=method == "GET")
        try:
            envelope = RequestEnvelope.from_dict(request)
            semantics = OperationSemantics(
                read_only=method == "GET", idempotency_key=envelope.idempotency_key is not None
            )
            if method not in {"GET", "POST"}:
                raise InvalidInputError("unsupported HTTP method")
            if method == "POST" and envelope.idempotency_key is None:
                raise SchemaViolationError("mutating requests require an idempotency_key")
            payload = envelope.payload
            key = envelope.idempotency_key or ""
            if path == "/v1/tasks" and method == "POST":
                _fields(payload, {"repository_id", "contract"})
                result = self.service.submit(
                    context,
                    _uuid(payload["repository_id"]),
                    ContractSpec.from_dict(payload["contract"]),
                    key,
                )
                return success_response(envelope.request_id, result, status=202)
            parts = path.split("/")
            if len(parts) > 1 and parts[1] != "v1":
                raise SchemaViolationError("unsupported API version")
            if len(parts) not in {4, 5} or parts[:3] != ["", "v1", "tasks"]:
                raise InvalidInputError("unsupported operation")
            episode_id = _uuid(parts[3])
            suffix = parts[4] if len(parts) == 5 else ""
            if method == "GET":
                _fields(payload, set())
                if suffix == "":
                    data: object = self.service.inspect(context, episode_id)
                elif suffix == "contracts":
                    data = self.service.history(context, episode_id)
                elif suffix == "budget":
                    data = BudgetLedger(self.service.database).inspect(context, episode_id)
                else:
                    raise InvalidInputError("unsupported operation")
            elif suffix == "contracts":
                _fields(payload, {"expected_contract_id", "contract"})
                data = self.service.revise(
                    context,
                    episode_id,
                    _uuid(payload["expected_contract_id"]),
                    ContractSpec.from_dict(payload["contract"]),
                    key,
                )
            elif suffix == "control":
                _fields(payload, {"expected_token", "action", "reason"})
                data = self.service.control(
                    context,
                    episode_id,
                    payload["expected_token"],
                    payload["action"],
                    payload["reason"],
                    idempotency_key=key,
                )
            else:
                raise InvalidInputError("unsupported operation")
            return success_response(envelope.request_id, data)
        except Exception as error:
            return error_response(
                error, request_id=envelope.request_id if envelope else None, semantics=semantics
            )


class TaskWSGI:
    """A small local HTTP adapter; deployment supplies transport security and auth."""

    def __init__(self, api: TaskAPI) -> None:
        self.api = api

    def __call__(
        self,
        environ: Mapping[str, Any],
        start_response: Callable[[str, list[tuple[str, str]]], Any],
    ) -> list[bytes]:
        try:
            method = str(environ.get("REQUEST_METHOD", ""))
            if method == "GET":
                request: object = {
                    "schema_version": 1,
                    "request_id": str(uuid4()),
                    "idempotency_key": None,
                    "payload": {},
                }
            else:
                length = int(environ.get("CONTENT_LENGTH", "0") or "0")
                if not 0 < length <= 1024 * 1024:
                    raise SchemaViolationError("body must be a bounded JSON envelope")
                if str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0] != "application/json":
                    raise SchemaViolationError("content type must be application/json")
                request = json.loads(environ["wsgi.input"].read(length))
            response = self.api.handle(
                method,
                str(environ.get("PATH_INFO", "")),
                request,
                authorization=environ.get("HTTP_AUTHORIZATION"),
            )
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            response = error_response(SchemaViolationError("invalid request body"))
        except Exception as error:
            response = error_response(error)
        body = json.dumps(response.body, sort_keys=True, separators=(",", ":")).encode()
        start_response(
            f"{response.status} {HTTPStatus(response.status).phrase}",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]
