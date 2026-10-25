"""Live local backend HTTP projection; no vault, no DB persistence in fixtures."""
from __future__ import annotations

import contextlib
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.api.ui_operator_s13 import register_operator_routes
from potbot.tools.operator_attachment_s13 import OperatorAttachmentServer

IMAGE = "a" * 64
QUAL = "b" * 64

class FakeDB:
    def __init__(self):
        self.calls = []
    @contextlib.contextmanager
    def transaction(self, actor):
        self.calls.append(actor)
        yield None


def test_operator_status_is_workspace_scoped_and_has_no_sensitive_fields(monkeypatch, tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    sock = directory / "operator.sock"
    ws, principal = uuid4(), uuid4()
    monkeypatch.setenv("POTBOT_OPERATOR_SOCKET", str(sock))
    monkeypatch.setenv("POTBOT_OPERATOR_WORKSPACE_ID", str(ws))
    monkeypatch.setenv("POTBOT_OPERATOR_IMAGE_DIGEST", IMAGE)
    monkeypatch.setenv("POTBOT_OPERATOR_QUALIFICATION_SHA256", QUAL)
    monkeypatch.setenv("POTBOT_OPERATOR_MOUNTS_SHA256", "0" * 64)
    audit = FakeDB()
    app = FastAPI()
    app.add_exception_handler(ScopeDeniedError,
                              lambda req, err: JSONResponse({"error": "denied"}, status_code=403))
    user = WorkspaceContext(ws, principal)
    def authorize(header):
        if header != "Bearer local-token":
            raise ScopeDeniedError("unknown bearer")
        return user
    register_operator_routes(app, audit, authorize, ("http://localhost:3100",), ws)
    with OperatorAttachmentServer(socket_path=sock, workspace_id=ws, image_digest=IMAGE,
                                   qualification_sha256=QUAL,
                                   status=lambda: {"running":True,"history_healthy":True,
                                                   "workspace_id":str(ws),"image_digest":IMAGE,
                                                   "supervised_runs":3,"pending_release_gates":1}):
        with TestClient(app) as client:
            headers={"Authorization":"Bearer local-token","Origin":"http://localhost:3100"}
            res = client.get('/v1/ui/operator',headers=headers)
            assert res.status_code == 200
            assert res.json()["healthy"] is True
            assert res.json()["pendingReleases"] == 1
            assert "socket" not in res.text and "token" not in res.text
            assert len(audit.calls) == 1
            assert client.get('/v1/ui/operator').status_code == 403
            assert client.get('/v1/ui/operator',headers={**headers,"Origin":"http://evil.test"}).status_code == 403
            assert client.get('/v1/ui/operator?secret=x',headers=headers).status_code == 403
    with TestClient(app) as client:
        res = client.get('/v1/ui/operator',headers=headers)
        assert res.status_code == 200 and res.json()["healthy"] is False
