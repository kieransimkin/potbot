"""Runtime Syn integration preserves scope, stable receipts and existing event authority."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.api.server import JobOperations, create_app
from potbot.api.syn_service_cli import main
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.cli import main as cli_main
from potbot.controller.execution_events import ExecutionEventService
from potbot.controller.syn_service import configured_client, sync_execution_events
from potbot.controller.tasks import TaskService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError


def test_client_is_optional_and_validates_transport_before_use():
    assert configured_client({}) is None
    scope = {
        "POTBOT_INSTALLATION_ID": str(uuid4()),
        "POTBOT_SYN_WORKSPACE_ID": str(uuid4()),
    }
    for url in ("http://remote.example", "https://user:password@remote.example"):
        with pytest.raises(InvalidInputError):
            configured_client({**scope, "POTBOT_SYN_URL": url, "POTBOT_SYN_TOKEN": "x" * 40})
    with pytest.raises(InvalidInputError):
        configured_client({"POTBOT_SYN_URL": "http://127.0.0.1:8000"})


def test_event_export_uses_committed_public_projection_and_original_identity():
    actor = WorkspaceContext(uuid4(), uuid4())
    identifier = str(uuid4())
    event = {
        "id": identifier,
        "event_ref": "syn://event/" + identifier,
        "sequence": 8,
        "stage": "started",
    }
    source = Mock(spec=ExecutionEventService)
    source.events.return_value = {"items": [event], "next_cursor": 8, "has_more": False}
    client = Mock()
    client.send.return_value = {"sequence": 2, "hash": "a" * 64}
    result = sync_execution_events(client, source, actor, after=3)
    source.events.assert_called_once_with(actor, after=3, limit=50)
    client.send.assert_called_once_with(
        str(actor.workspace_id),
        message_id=identifier,
        channel="potbot.execution-events",
        sender="potbot",
        payload=event,
    )
    assert result["next_cursor"] == 8 and result["items"][0]["source_sequence"] == 8
    # A lost acknowledgement never advances a retained source cursor.
    client.send.side_effect = RuntimeError("synthetic interrupted acknowledgement")
    with pytest.raises(RuntimeError):
        sync_execution_events(client, source, actor, after=3)


def application(enabled=True):
    actor = WorkspaceContext(uuid4(), uuid4())
    token = "synthetic-api-token-" + "x" * 40
    service = Mock()
    service.head.return_value = {
        "workspace_id": str(actor.workspace_id),
        "sequence": 0,
        "hash": "0" * 64,
    }
    service.messages.return_value = {"items": [], "next_cursor": 0, "has_more": False}
    service.send.return_value = {"sequence": 1, "hash": "a" * 64}
    app = create_app(
        task_service=cast(TaskService, object()),
        job_service=cast(JobOperations, object()),
        authenticate=DevelopmentBearerAuth({token: actor}),
        syn_service=service if enabled else None,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = "Bearer " + token
    return app, client, service, actor


def test_rest_forwarding_uses_authenticated_scope_and_sender():
    _app, http, service, actor = application()
    identifier = str(uuid4())
    body = {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "idempotency_key": identifier,
        "payload": {"channel": "agents.progress", "payload": {"state": "observed"}},
    }
    assert http.get("/v1/syn/head").status_code == 200
    service.head.assert_called_once_with(str(actor.workspace_id))
    assert http.get("/v1/syn/messages?after=2&limit=5").status_code == 200
    service.messages.assert_called_once_with(str(actor.workspace_id), after=2, limit=5)
    assert http.post("/v1/syn/messages", json=body).status_code == 200
    service.send.assert_called_once_with(
        str(actor.workspace_id),
        message_id=identifier,
        channel="agents.progress",
        sender="principal:" + str(actor.principal_id),
        payload={"state": "observed"},
    )
    body["payload"]["workspace_id"] = str(uuid4())
    assert http.post("/v1/syn/messages", json=body).status_code == 400
    assert http.get("/v1/syn/head", headers={"Authorization": ""}).status_code == 404
    assert (
        http.get("/v1/syn/head", headers={"Origin": "https://untrusted.example"}).status_code == 404
    )


def test_disabled_service_does_not_add_routes_and_errors_are_sanitized():
    app, http, _service, _actor = application(False)
    assert "/v1/syn/messages" not in app.openapi()["paths"]
    assert http.get("/v1/syn/head").status_code != 200
    _app, http, service, _actor = application()
    service.head.side_effect = RuntimeError("private-token-must-never-leak")
    response = http.get("/v1/syn/head")
    assert response.status_code >= 400
    assert "private-token-must-never-leak" not in response.text


def test_remote_message_conflict_preserves_http_conflict():
    from syn_client import MessageConflictError

    _app, http, service, _actor = application()
    service.send.side_effect = MessageConflictError("synthetic private conflict detail")
    body = {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "payload": {"channel": "agents.progress", "payload": {}},
    }
    response = http.post("/v1/syn/messages", json=body)
    assert response.status_code == 409
    assert "synthetic private conflict detail" not in response.text


def test_cli_help_is_lazy_and_missing_configuration_is_safe(monkeypatch, capsys):
    monkeypatch.delenv("POTBOT_SYN_URL", raising=False)
    with pytest.raises(SystemExit) as result:
        cli_main(["syn-service", "--help"])
    assert result.value.code == 0
    assert main(["--workspace-id", str(uuid4()), "head"]) == 1
    assert "Syn operation failed" in capsys.readouterr().err
