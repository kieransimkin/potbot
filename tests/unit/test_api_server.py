"""Actual ASGI requests cover authentication, bounded input and durable cursor replay."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from potbot.api.server import ServerSettings, create_app
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.controller.tasks import TaskService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

TOKEN = "workspace-token-" + "b" * 40
ORIGIN = "http://testserver"


class Jobs:
    def __init__(self, context: WorkspaceContext) -> None:
        self.context = context
        self.identifier = uuid4()
        self.record: dict[str, Any] = {"id": str(self.identifier), "state": "queued"}
        self.calls: list[tuple[Any, ...]] = []
        self.items = [
            {"sequence": 1, "job_id": str(self.identifier), "event_type": "queued", "data": {}},
            {"sequence": 2, "job_id": str(self.identifier), "event_type": "running", "data": {}},
            {
                "sequence": 3,
                "job_id": str(self.identifier),
                "event_type": "output",
                "data": {"text": "safe output"},
            },
        ]
        self.failure: Exception | None = None

    def check(self, context: WorkspaceContext, job_id: UUID | None = None) -> None:
        if context != self.context or (job_id is not None and job_id != self.identifier):
            raise ScopeDeniedError("private-other-workspace-marker")
        if self.failure is not None:
            raise self.failure

    def submit(
        self, context: WorkspaceContext, payload: dict[str, Any], key: str
    ) -> dict[str, Any]:
        self.check(context)
        self.calls.append(("submit", context, payload, key))
        return self.record.copy()

    def list(self, context: WorkspaceContext, *, state=None, limit=50, after=None):
        self.check(context)
        self.calls.append(("list", context, state, limit, after))
        return {"items": [self.record.copy()], "next_cursor": None, "event_cursor": 3}

    def inspect(self, context: WorkspaceContext, job_id: UUID) -> dict[str, Any]:
        self.check(context, job_id)
        self.calls.append(("inspect", context, job_id))
        return self.record.copy()

    def cancel(self, context: WorkspaceContext, job_id: UUID, key: str) -> dict[str, Any]:
        self.check(context, job_id)
        self.calls.append(("cancel", context, job_id, key))
        self.record["state"] = "cancelled"
        return self.record.copy()

    def queue(self, context: WorkspaceContext) -> dict[str, Any]:
        self.check(context)
        return {"counts": {"queued": 1}, "event_cursor": 3}

    def events(self, context: WorkspaceContext, job_id=None, *, after=0, limit=100):
        self.check(context, job_id)
        self.calls.append(("events", context, job_id, after, limit))
        matching = [
            item
            for item in self.items
            if item["sequence"] > after and (job_id is None or item["job_id"] == str(job_id))
        ]
        page = matching[:limit]
        return {
            "items": page,
            "next_cursor": page[-1]["sequence"] if page else after,
            "has_more": len(matching) > limit,
        }


class Tasks:
    def inspect(self, context: WorkspaceContext, episode_id: UUID) -> dict[str, Any]:
        return {"task_id": str(episode_id), "workspace": str(context.workspace_id)}

    def start_generation(self, context, episode_id, expected_token, reason, *, idempotency_key):
        return {
            "task_id": str(episode_id),
            "workspace": str(context.workspace_id),
            "expected_token": expected_token,
            "reason": reason,
            "key": idempotency_key,
            "lifecycle": "running",
        }


def envelope(payload: dict[str, Any], key: str | None = "test-command") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "idempotency_key": key,
        "payload": payload,
    }


def setup(*, invocation_reader=None, invocation_list_reader=None, **settings: Any):
    context = WorkspaceContext(uuid4(), uuid4())
    service = Jobs(context)
    auth = DevelopmentBearerAuth({TOKEN: context})
    app = create_app(
        task_service=cast(TaskService, Tasks()),
        job_service=service,
        authenticate=auth,
        settings=ServerSettings(allowed_origins=(ORIGIN,), poll_interval_seconds=0.01, **settings),
        model_catalog=lambda _context: {
            "providers": [{"id": "openai", "configured": True}],
            "models": [],
        },
        discover_models=lambda actor, provider: {
            "provider": provider,
            "models": [],
            "workspace": str(actor.workspace_id),
        },
        invocation_reader=invocation_reader,
        invocation_list_reader=invocation_list_reader,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = "Bearer " + TOKEN
    return app, client, service, context


def test_queue_job_and_task_routes_use_authenticated_context() -> None:
    _app, client, service, context = setup()
    with client:
        response = client.get("/v1/jobs?state=running&limit=7&after=opaque-cursor")
        assert response.status_code == 200
        assert service.calls[-1] == ("list", context, "running", 7, "opaque-cursor")
        assert client.get("/v1/queue").json()["data"]["counts"] == {"queued": 1}
        assert client.get(f"/v1/jobs/{service.identifier}").json()["data"]["id"] == str(
            service.identifier
        )
        task_id = uuid4()
        task = client.get(f"/v1/tasks/{task_id}")
        assert task.status_code == 200
        assert task.json()["data"] == {
            "task_id": str(task_id),
            "workspace": str(context.workspace_id),
        }
        assert response.headers["cache-control"] == "no-store"


def test_mutations_preserve_request_identity_and_idempotency_key() -> None:
    _app, client, service, context = setup()
    body = envelope({"job_type": "model.generate", "input": {"prompt": "hello"}})
    with client:
        response = client.post("/v1/jobs", json=body)
        assert response.status_code == 202
        assert response.json()["request_id"] == body["request_id"]
        assert service.calls[-1] == ("submit", context, body["payload"], "test-command")
        cancelled = client.post(f"/v1/jobs/{service.identifier}/cancel", json=envelope({}))
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["state"] == "cancelled"
        assert service.calls[-1] == ("cancel", context, service.identifier, "test-command")


def test_start_generation_routes_an_explicit_versioned_task_command() -> None:
    _app, client, _service, context = setup()
    task_id = uuid4()
    body = envelope({"expected_token": 3, "reason": "Start budgeted generation."}, "start-key")
    with client:
        response = client.post(f"/v1/tasks/{task_id}/start-generation", json=body)
        assert response.status_code == 200
        assert response.json()["request_id"] == body["request_id"]
        assert response.json()["data"] == {
            "task_id": str(task_id),
            "workspace": str(context.workspace_id),
            "expected_token": 3,
            "reason": "Start budgeted generation.",
            "key": "start-key",
            "lifecycle": "running",
        }


@pytest.mark.parametrize("path", ["/v1/jobs", "/v1/queue", "/v1/models", "/v1/jobs/events"])
def test_bearer_header_is_required_and_query_tokens_do_not_authenticate(path: str) -> None:
    _app, client, service, _context = setup()
    with client:
        client.headers.pop("Authorization")
        response = client.get(path + "?token=" + TOKEN)
        assert response.status_code == 404
        assert TOKEN not in response.text
        assert not service.calls


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "limit=-1",
        "limit=1&limit=2",
        "token=private",
        "workspace_id=other",
        "after=",
    ],
)
def test_job_query_bounds_and_unknown_fields_fail_before_service(query: str) -> None:
    _app, client, service, _context = setup()
    with client:
        assert client.get("/v1/jobs?" + query).status_code == 400
        assert not service.calls


@pytest.mark.parametrize(
    "body",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b"\xff",
        b'{"payload":{}}',
    ],
)
def test_invalid_json_is_bounded_and_sanitized(body: bytes) -> None:
    _app, client, service, _context = setup(max_body_bytes=512)
    with client:
        result = client.post("/v1/jobs", content=body, headers={"Content-Type": "application/json"})
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "schema_violation"
        assert not service.calls
        assert (
            client.post(
                "/v1/jobs", content=b"x" * 513, headers={"Content-Type": "application/json"}
            ).status_code
            == 400
        )


def test_no_key_wrong_content_type_and_nonempty_cancel_payload_are_rejected() -> None:
    _app, client, service, _context = setup()
    with client:
        assert client.post("/v1/jobs", json=envelope({}, None)).status_code == 400
        assert client.post("/v1/jobs", content=json.dumps(envelope({}))).status_code == 400
        assert (
            client.post(
                f"/v1/jobs/{service.identifier}/cancel", json=envelope({"force": True})
            ).status_code
            == 400
        )
        assert not service.calls


def test_origin_scope_and_exception_messages_cannot_disclose_records_or_credentials() -> None:
    _app, client, service, _context = setup()
    with client:
        assert (
            client.get("/v1/jobs", headers={"Origin": "https://untrusted.example"}).status_code
            == 404
        )
        missing = client.get(f"/v1/jobs/{uuid4()}")
        assert missing.status_code == 404
        assert "private-other-workspace-marker" not in missing.text
        service.failure = RuntimeError(
            "postgresql://user:private-password@example.test provider-key-secret"
        )
        failed = client.get("/v1/queue")
        assert failed.status_code == 500
        assert "private-password" not in failed.text and "provider-key-secret" not in failed.text


def test_unexpected_error_cannot_escape_to_transport_traceback_logging(caplog, capsys) -> None:
    app, _client, service, _context = setup()
    secret = "secret-in-an-unexpected-provider-exception"
    service.failure = RuntimeError(secret)
    # The default raising transport would re-raise a ServerErrorMiddleware
    # exception even when its response handler returned a sanitized 500.
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.get("/v1/queue", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    output = capsys.readouterr()
    assert secret not in response.text + caplog.text + output.out + output.err
    assert "Unexpected HTTP operation failure" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_binary_stream_authentication_is_a_sanitized_protocol_error() -> None:
    _app, client, _service, _context = setup()
    with client, client.websocket_connect("/v1/jobs/stream") as socket:
        socket.send_bytes(b"binary credential canary")
        response = socket.receive_json()
        assert response["error"]["code"] == "schema_violation"
        assert "canary" not in json.dumps(response)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1008


def test_catalogue_discovery_is_explicit_and_scoped() -> None:
    _app, client, _service, context = setup()
    with client:
        assert client.get("/v1/potbot/models").json()["data"]["providers"][0]["id"] == "openai"
        result = client.post("/v1/providers/gemini/models/discover", json=envelope({}))
        assert result.status_code == 200
        assert result.json()["data"] == {
            "provider": "gemini",
            "models": [],
            "workspace": str(context.workspace_id),
        }


def test_invocation_inspection_is_scoped_and_requires_an_installed_reader() -> None:
    calls = []

    def reader(context, identifier):
        calls.append((context, identifier))
        return {
            "id": str(identifier),
            "state": "completed",
            "usage": {"calls": 1},
            "policy_hash": "a" * 64,
        }

    _app, client, _service, context = setup(invocation_reader=reader)
    identifier = uuid4()
    with client:
        result = client.get(f"/v1/invocations/{identifier}")
        assert result.status_code == 200
        assert result.json()["data"]["usage"] == {"calls": 1}
        assert calls == [(context, identifier)]
        assert client.get("/v1/invocations/invalid-id").status_code == 400
        assert client.get(f"/v1/invocations/{identifier}?token=forbidden").status_code == 400
        client.headers.pop("Authorization")
        assert client.get(f"/v1/invocations/{identifier}").status_code == 404
        assert calls == [(context, identifier)]
    _app, client, _service, _context = setup()
    with client:
        assert client.get(f"/v1/invocations/{identifier}").status_code == 503


def test_public_dashboard_and_api_schema_have_no_workspace_data_or_inline_scripts() -> None:
    _app, client, _service, _context = setup()
    with client:
        client.headers.pop("Authorization")
        page = client.get("/")
        assert page.status_code == 200
        assert "script-src 'self'" in page.headers["content-security-policy"]
        script = client.get("/assets/dashboard.js")
        assert script.status_code == 200
        for unsafe in ("innerHTML", "localStorage", "sessionStorage", "document.cookie", TOKEN):
            assert unsafe not in script.text
        assert "textContent" in script.text
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        assert schema["paths"]["/v1/jobs"]["get"]["security"] == [{"BearerAuth": []}]
        assert (
            schema["paths"]["/v1/tasks/{episode_id}"]["get"]["parameters"][0]["name"]
            == "episode_id"
        )
        assert client.get("/health").json()["data"]["status"] == "listening"


def test_websocket_replays_from_cursor_across_batches_and_reconnects() -> None:
    _app, client, service, context = setup(event_batch_size=1)
    with client:
        with client.websocket_connect(
            f"/v1/jobs/{service.identifier}/stream", headers={"Origin": ORIGIN}
        ) as ws:
            ws.send_json({"type": "authenticate", "token": TOKEN, "after": 1})
            assert ws.receive_json() == {"schema_version": 1, "type": "ready", "cursor": 1}
            first = ws.receive_json()
            second = ws.receive_json()
            assert [first["cursor"], second["cursor"]] == [2, 3]
            assert all(call[1] == context for call in service.calls)
        with client.websocket_connect("/v1/jobs/stream") as ws:
            ws.send_json({"type": "authenticate", "token": TOKEN, "after": 2})
            assert ws.receive_json()["type"] == "ready"
            assert ws.receive_json()["cursor"] == 3
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_websocket_live_output_is_seen_without_server_local_broadcasts() -> None:
    _app, client, service, _context = setup()
    with client:
        with client.websocket_connect("/v1/jobs/stream") as ws:
            ws.send_json({"type": "authenticate", "token": TOKEN, "after": 3})
            assert ws.receive_json()["type"] == "ready"
            service.items.append(
                {
                    "sequence": 4,
                    "job_id": str(service.identifier),
                    "event_type": "output",
                    "data": {"text": "new worker output"},
                }
            )
            event = ws.receive_json()
            assert event["cursor"] == 4 and event["event"]["data"]["text"] == "new worker output"


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "authenticate", "token": "wrong", "after": 0},
        {"type": "authenticate", "token": TOKEN, "after": True},
        {"type": "authenticate", "token": TOKEN, "after": -1},
        {"type": "authenticate", "token": TOKEN, "after": 0, "workspace": "other"},
    ],
)
def test_websocket_rejects_invalid_authentication_or_cursor(auth: dict[str, Any]) -> None:
    _app, client, service, _context = setup()
    with client:
        with client.websocket_connect("/v1/jobs/stream") as ws:
            ws.send_json(auth)
            error = ws.receive_json()
            assert error["type"] == "error"
            assert TOKEN not in json.dumps(error)
            with pytest.raises(WebSocketDisconnect) as disconnected:
                ws.receive_json()
            assert disconnected.value.code == 1008
        assert not service.calls


def test_websocket_query_tokens_and_untrusted_origins_are_rejected() -> None:
    _app, client, _service, _context = setup()
    with client:
        for path, headers in (
            ("/v1/jobs/stream?token=" + TOKEN, {}),
            ("/v1/jobs/stream", {"Origin": "https://untrusted.example"}),
        ):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(path, headers=headers):
                    pytest.fail("unexpected socket acceptance")


def test_websocket_scope_is_checked_before_ready() -> None:
    _app, client, _service, _context = setup()
    with client:
        with client.websocket_connect(f"/v1/jobs/{uuid4()}/stream") as ws:
            ws.send_json({"type": "authenticate", "token": TOKEN, "after": 0})
            response = ws.receive_json()
            assert response["type"] == "error"
            assert "private-other-workspace-marker" not in json.dumps(response)


def test_websocket_slow_send_has_deadline_and_reconnect_code() -> None:
    app, _client, _service, _context = setup(send_timeout_seconds=0.01)
    messages = []

    async def scenario() -> None:
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "authenticate", "token": TOKEN, "after": 0}),
            }
        )

        async def receive():
            return await incoming.get()

        async def send(message):
            messages.append(message)
            if message["type"] == "websocket.send":
                await asyncio.Event().wait()

        scope = {
            "type": "websocket",
            "path": "/v1/jobs/stream",
            "raw_path": b"/v1/jobs/stream",
            "root_path": "",
            "scheme": "ws",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 1),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=1)

    asyncio.run(scenario())
    assert messages[-1]["type"] == "websocket.close"
    assert messages[-1]["code"] == 1013


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_origins": ("*",)},
        {"allowed_origins": ("https://example.test/path",)},
        {"max_frame_bytes": True},
        {"event_batch_size": 201},
        {"send_timeout_seconds": float("nan")},
    ],
)
def test_server_configuration_cannot_disable_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidInputError):
        ServerSettings(**kwargs)


def test_job_invocation_listing_is_scoped_bounded_and_optional() -> None:
    identifier = uuid4()
    _app, client, _service, _context = setup()
    with client:
        unavailable = client.get(f"/v1/jobs/{identifier}/invocations")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "capability_unavailable"

    calls = []

    def reader(actor, job_id):
        calls.append((actor, job_id))
        if job_id != identifier:
            raise ScopeDeniedError("private-other-job-marker")
        return {"job_id": str(job_id), "items": [{"id": str(uuid4()), "status": "indeterminate"}]}

    _app, client, _service, context = setup(invocation_list_reader=reader)
    with client:
        response = client.get(f"/v1/jobs/{identifier}/invocations")
        assert response.status_code == 200
        assert response.json()["data"]["items"][0]["status"] == "indeterminate"
        assert calls == [(context, identifier)]
        assert client.get("/v1/jobs/not-a-uuid/invocations").status_code == 400
        assert client.get(f"/v1/jobs/{identifier}/invocations?workspace=other").status_code == 400
        assert calls == [(context, identifier)]
        denied = client.get(f"/v1/jobs/{uuid4()}/invocations")
        assert denied.status_code == 404 and "private-other-job-marker" not in denied.text
        count = len(calls)
        client.headers.pop("Authorization")
        assert client.get(f"/v1/jobs/{identifier}/invocations").status_code == 404
        assert len(calls) == count
