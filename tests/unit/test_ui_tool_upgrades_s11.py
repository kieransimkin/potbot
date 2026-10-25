"""S11 frontend/API boundary tests; real PostgreSQL RLS is a separate CI gate."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.api import ui_tool_upgrades as api


@pytest.fixture
def routes(monkeypatch):
    workspace, actor = uuid4(), uuid4()
    fake_context = SimpleNamespace(workspace_id=workspace, principal_id=actor)
    items = {"id": str(uuid4()), "slug": "stemlab", "state": "awaiting_approval",
             "oldCommit": "a" * 40, "newCommit": "b" * 40,
             "evidenceSha256": "e" * 64, "suiteSha256": "f" * 64, "revision": 0, "tests": []}
    called = []
    class FakeStore:
        def __init__(self, _db): pass
        def list(self, context):
            called.append("list")
            return {"schema": "potbot-tool-upgrades-s11/v1", "items": []}
        def inspect(self, context, id):
            return items
        def decide(self, context, operation, *, expected_revision, evidence_sha256, approve, setup_token=None):
            called.append((operation, expected_revision, evidence_sha256, approve))
            if evidence_sha256 != "e" * 64 or expected_revision != 0:
                raise RevisionConflictError("outdated")
            return {**items, "state": "approved" if approve else "rejected"}
    monkeypatch.setattr(api, "UiToolUpgradeStore", FakeStore)
    setup_calls = []
    def setup(token):
        setup_calls.append(token)
        if token != "approved": raise ScopeDeniedError("not authorized")
    def authenticate(value):
        if value != "Bearer valid": raise ScopeDeniedError("invalid bearer")
        return fake_context
    application = FastAPI()
    api.register_tool_upgrade_routes(application, object(), authenticate,
                                     ("http://127.0.0.1:3100",), setup)
    return TestClient(application), workspace, items, called, setup_calls


def test_read_only_listing_uses_workspace_bearer(routes):
    client, _, _, called, setup = routes
    r = client.get("/v1/ui/tools/upgrades", headers={"Authorization": "Bearer valid"})
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert called == ["list"] and not setup


def test_approve_requires_separate_setup_token_and_origin(routes):
    client, _, item, called, setups = routes
    url = f"/v1/ui/tools/upgrades/{item['id']}/approve"
    body = {"revision": 0, "evidenceSha256": "e" * 64, "confirm": True}
    with pytest.raises(ScopeDeniedError):
        client.post(url, json=body, headers={"Authorization": "Bearer valid"})
    with pytest.raises(ScopeDeniedError):
        client.post(url, json=body, headers={"Authorization": "Bearer valid",
                                             "Origin": "https://evil.invalid"})
    with pytest.raises(ScopeDeniedError):
        client.post(url, json=body, headers={"Authorization": "Bearer valid",
                                             "Origin": "http://127.0.0.1:3100", "X-Potbot-Setup-Token": "wrong"})
    assert not called
    result = client.post(url, json=body, headers={"Authorization": "Bearer valid",
                                                 "Origin": "http://127.0.0.1:3100", "X-Potbot-Setup-Token": "approved"})
    assert result.status_code == 200
    assert result.json()["state"] == "approved"
    assert len(called) == 1 and called[0][3] is True


def test_reject_validates_revision_hash_and_confirmation(routes):
    client, _, item, called, _ = routes
    headers = {"Authorization": "Bearer valid", "Origin": "http://127.0.0.1:3100",
               "X-Potbot-Setup-Token": "approved"}
    url = f"/v1/ui/tools/upgrades/{item['id']}/reject"
    with pytest.raises(InvalidInputError):
        client.post(url, json={"revision": 0, "evidenceSha256": "e" * 64,
                               "confirm": False}, headers=headers)
    with pytest.raises(RevisionConflictError):
        client.post(url, json={"revision": 0, "evidenceSha256": "0" * 64,
                               "confirm": True}, headers=headers)
    assert not called or all(x[3] is False for x in called)


def test_ambiguous_duplicate_fields_rejected(routes):
    client, _, item, _, _ = routes
    with pytest.raises(InvalidInputError):
        client.post(f"/v1/ui/tools/upgrades/{item['id']}/approve",
                    content='{"revision":0,"revision":1,"evidenceSha256":"' + "a"*64 + '","confirm":true}',
                    headers={"Authorization": "Bearer valid", "Origin": "http://127.0.0.1:3100",
                             "X-Potbot-Setup-Token": "approved", "Content-Type": "application/json"})
