"""Fast contract tests without PostgreSQL credentials or paid model calls."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potbot.api.ui_chats import UiChatStore, _text, _title, _uuid, register_chat_routes
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.persistence.ui_chat_schema import WORKSPACE_TABLES, MIGRATION_STATEMENTS


def test_chat_migration_27_is_workspace_scoped_and_owned():
    assert set(WORKSPACE_TABLES) == {"ui_chat_sessions", "ui_chat_turns"}
    assert any("UNIQUE(workspace_id,session_id,request_id)" in sql for sql in MIGRATION_STATEMENTS)
    assert any("FOREIGN KEY(workspace_id,session_id,principal_id)" in sql for sql in MIGRATION_STATEMENTS)


def test_boundaries_reject_invalid_text_and_session_identifiers():
    assert _text("hello", 16) == "hello"
    assert _title(" sample ") == "sample"
    with pytest.raises(InvalidInputError):
        _text("x" * 17, 16)
    with pytest.raises(InvalidInputError):
        _title("\x00unsafe")
    with pytest.raises(InvalidInputError):
        _uuid("../../other-workspace")
    with pytest.raises(InvalidInputError):
        _uuid(str(uuid4()).upper())


def test_routes_require_bearer_identity_and_never_choose_a_workspace_from_body(monkeypatch):
    context = WorkspaceContext(uuid4(), uuid4())
    session_id = uuid4()
    turn_id = uuid4()
    app = FastAPI()

    def auth(header):
        if header != "Bearer known":
            raise ScopeDeniedError("Unavailable")
        return context

    class DummyDB:
        pass

    register_chat_routes(app, DummyDB(), auth, ("http://127.0.0.1:3100",))
    observations = []
    def create(self, actor, *, session_id, title):
        observations.append((actor, session_id, title))
        return {"id": str(session_id), "title": title, "created": True}
    def start(self, actor, uid, *, request_id, text):
        observations.append((actor, uid, text))
        return {"id": str(turn_id), "created": True, "state": "pending", "messages": [
            {"role": "user", "content": text}
        ]}
    monkeypatch.setattr(UiChatStore, "create_session", create)
    monkeypatch.setattr(UiChatStore, "start_turn", start)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = {"authorization": "Bearer known", "origin": "http://127.0.0.1:3100"}
        response = client.post("/v1/ui/sessions", json={"id": str(session_id), "title": "Test"}, headers=headers)
        assert response.status_code == 201
        assert observations[-1] == (context, session_id, "Test")
        response = client.post(f"/v1/ui/sessions/{session_id}/turns",
                               json={"requestId": str(uuid4()), "text": "Hello"}, headers=headers)
        assert response.status_code == 202
        assert observations[-1] == (context, session_id, "Hello")
        forbidden = client.post("/v1/ui/sessions", json={"id": str(session_id), "title": "Bad"},
                                headers={**headers, "origin": "https://untrusted.invalid"})
        assert forbidden.status_code in (403, 500)
        oversized = client.post("/v1/ui/sessions", json={"id": str(session_id), "title": "X" * 51000}, headers=headers)
        assert oversized.status_code in (400, 500)


def test_reconciliation_route_requires_explicit_confirmed_acknowledgement(monkeypatch):
    app = FastAPI()
    actor = WorkspaceContext(uuid4(), uuid4())
    sid, tid = uuid4(), uuid4()
    register_chat_routes(app, object(), lambda bearer: actor if bearer == "Bearer known" else None,
                         ("http://127.0.0.1:3100",))
    seen = []
    def reconcile(self, who, session_id, turn_id):
        seen.append((who, session_id, turn_id))
        return {"id": str(turn_id), "state": "indeterminate", "replayed": False}
    monkeypatch.setattr(UiChatStore, "reconcile_pending", reconcile)
    with TestClient(app) as client:
        headers = {"authorization": "Bearer known", "origin": "http://127.0.0.1:3100"}
        endpoint = f"/v1/ui/sessions/{sid}/turns/{tid}/reconcile"
        assert client.post(endpoint, headers=headers, json={"acknowledgeUncertain": True}).json()["replayed"] is False
        assert seen == [(actor, sid, tid)]
        with TestClient(app, raise_server_exceptions=False) as bad:
            assert bad.post(endpoint, headers=headers, json={"acknowledgeUncertain": False}).status_code >= 400
