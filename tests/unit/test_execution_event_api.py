"""Read-only SYN execution streams; the legacy job API remains available."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from potbot.api.server import JobOperations, ServerSettings, create_app
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.controller.tasks import TaskService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError

_TOKEN = "synthetic-api-token-" + "x" * 40


class FixtureEvents:
    def __init__(self, context: WorkspaceContext) -> None:
        self.context = context
        self.run = uuid4()
        self.calls: list[tuple[Any, ...]] = []
        self.items: list[dict[str, Any]] = [
            {
                "sequence": 2,
                "event_ref": "syn://event/" + str(uuid4()),
                "run_id": str(self.run),
                "stage": "started",
                "trigger_kind": "api_request",
                "trigger_ref": "request:fixture",
            },
            {
                "sequence": 3,
                "event_ref": "syn://event/" + str(uuid4()),
                "run_id": str(self.run),
                "stage": "completed",
                "trigger_kind": "parent_event",
                "trigger_ref": "event:fixture",
            },
        ]

    def events(
        self,
        context: WorkspaceContext,
        *,
        after: int = 0,
        run_id: UUID | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if context != self.context:
            raise ScopeDeniedError("private-cross-workspace-marker")
        self.calls.append((context, after, run_id, limit))
        matches = [
            item
            for item in self.items
            if item["sequence"] > after and (run_id is None or item["run_id"] == str(run_id))
        ]
        selected = matches[:limit]
        return {
            "items": selected,
            "next_cursor": selected[-1]["sequence"] if selected else after,
            "has_more": len(matches) > limit,
        }


def application(*, enabled: bool = True):
    context = WorkspaceContext(uuid4(), uuid4())
    events = FixtureEvents(context)
    app = create_app(
        task_service=cast(TaskService, object()),
        job_service=cast(JobOperations, object()),
        authenticate=DevelopmentBearerAuth({_TOKEN: context}),
        execution_events=events if enabled else None,
        settings=ServerSettings(
            allowed_origins=("http://testserver",),
            poll_interval_seconds=0.01,
            heartbeat_seconds=1.0,
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = "Bearer " + _TOKEN
    return app, client, events, context


def test_syn_event_pagination_scope_and_validation() -> None:
    _app, client, events, actor = application()
    with client:
        result = client.get("/v1/executions/events?after=0&limit=1")
        assert result.status_code == 200
        assert [item["stage"] for item in result.json()["data"]["items"]] == ["started"]
        assert result.json()["data"]["has_more"] is True
        assert events.calls[-1] == (actor, 0, None, 1)
        selected = client.get(f"/v1/executions/{events.run}/events?after=2")
        assert selected.status_code == 200
        assert selected.json()["data"]["items"][0]["stage"] == "completed"
        assert events.calls[-1] == (actor, 2, events.run, 50)
        assert client.get("/v1/executions/events?limit=0").status_code == 400
        assert client.get("/v1/executions/events?limit=2&limit=5").status_code == 400
        assert client.get("/v1/executions/events?unsupported=1").status_code == 400
        assert client.get("/v1/executions/not-a-uuid/events").status_code == 400
        denied = client.get("/v1/executions/events", headers={"Authorization": ""})
        assert denied.status_code in (401, 403)


def test_syn_event_websocket_replay_and_run_filter() -> None:
    _app, client, events, _actor = application()
    with client:
        with client.websocket_connect("/v1/executions/stream") as session:
            session.send_json({"type": "authenticate", "token": _TOKEN, "after": 0})
            assert session.receive_json()["type"] == "ready"
            first = session.receive_json()
            assert first["type"] == "event" and first["cursor"] == 2
            second = session.receive_json()
            assert second["type"] == "event" and second["cursor"] == 3
        with client.websocket_connect(f"/v1/executions/{events.run}/stream") as session:
            session.send_json({"type": "authenticate", "token": _TOKEN, "after": 2})
            assert session.receive_json()["type"] == "ready"
            assert session.receive_json()["event"]["stage"] == "completed"


def test_disabled_execution_monitor_does_not_change_job_api() -> None:
    app, client, _events, _actor = application(enabled=False)
    with client:
        assert client.get("/v1/executions/events").status_code != 200
    schema = app.openapi()
    assert "/v1/executions/events" not in schema["paths"]
    assert "/v1/jobs/events" in schema["paths"]
