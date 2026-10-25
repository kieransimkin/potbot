"""FastAPI contract smoke: routes exist and use bounded scoped identity."""
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from potbot.api.ui_resources import UiResourceStore, register_ui_routes
from potbot.domain.auth import WorkspaceContext


def test_ui_routes_use_authenticated_principal(monkeypatch):
    app = FastAPI()
    identity = WorkspaceContext(uuid4(), uuid4())
    class NoDatabase:
        pass

    def authenticate(header):
        if header == "Bearer admitted-token":
            return identity
        raise RuntimeError("invalid principal")

    register_ui_routes(app, NoDatabase(), authenticate, ("http://127.0.0.1:3100",))
    monkeypatch.setattr(UiResourceStore, "preferences", lambda self, ctx: {
        "revision": 4, "settings": {"theme": "dark"},
    } if ctx == identity else {})
    monkeypatch.setattr(UiResourceStore, "workspace", lambda self, ctx: {
        "id": str(ctx.workspace_id), "name": "Workspace A",
    })
    with TestClient(app) as client:
        headers = {"authorization": "Bearer admitted-token"}
        assert client.get("/v1/ui/preferences", headers=headers).json() == {
            "revision": 4, "settings": {"theme": "dark"}}
        assert client.get("/v1/ui/workspace", headers=headers).json() == {
            "workspace": {"id": str(identity.workspace_id), "name": "Workspace A"},
            "available": True,
        }
