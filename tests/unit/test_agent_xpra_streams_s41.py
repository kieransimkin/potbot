"""S41 ticket tests use genuine AF_UNIX inodes but no X server/process spawn."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.agent_xpra_streams import AgentXpraTicketAuthority


class FakeServer:
    readonly_mode = True
    def __init__(self, wid):
        self.workspace_id = wid
    def environment(self):
        return {"DISPLAY": ":411"}


class FakeApps:
    def __init__(self, wid, agent, session, path):
        self.workspace_id, self.agent, self.session, self.path = wid, agent, session, path
        self.agent_displays = object()
        self.running = True
    def agent_stream_socket(self, agent, session):
        if agent != self.agent or session != self.session or not self.running:
            raise ScopeDeniedError("not this agent browser")
        return self.path
    def agent_display_inventory(self):
        return [{"agentId": str(self.agent), "id": self.session, "title": "Agent browser",
                 "state": "running", "startedAt": 1000, "viewOnly": True}]


@pytest.fixture
def fixture(tmp_path):
    wid, agent, session = uuid4(), uuid4(), "a"*32
    path = tmp_path / "xpra.sock"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path)); sock.listen()
    apps = FakeApps(wid, agent, session, path)
    try:
        yield FakeServer(wid), apps, sock
    finally:
        sock.close()


def test_one_use_ticket_for_exact_private_socket(fixture):
    server, apps, sock = fixture
    auth = AgentXpraTicketAuthority(server, apps)
    doc = auth.issue(apps.agent, apps.session)
    assert doc["expiresInSeconds"] == 30 and doc["viewOnly"] is True
    assert "socket" not in doc and "agentId" not in doc
    selected = auth.consume(doc["ticket"])
    assert selected.agent_id == apps.agent and selected.socket == apps.path
    with pytest.raises(ScopeDeniedError): auth.consume(doc["ticket"])
    assert auth.inventory()["sessions"][0]["id"] == apps.session


def test_foreign_agent_and_session_denied(fixture):
    server, apps, sock = fixture
    auth = AgentXpraTicketAuthority(server, apps)
    with pytest.raises(ScopeDeniedError): auth.issue(uuid4(), apps.session)
    with pytest.raises(ScopeDeniedError): auth.issue(apps.agent, "b"*32)


def test_expiration_is_not_revived(fixture):
    server, apps, sock = fixture
    timer = [100.0]
    auth = AgentXpraTicketAuthority(server, apps, clock=lambda: timer[0], lifetime=3)
    # lifetime >= 1 by design
    doc = auth.issue(apps.agent, apps.session)
    timer[0] += 4
    with pytest.raises(ScopeDeniedError): auth.consume(doc["ticket"])
    with pytest.raises(ScopeDeniedError): auth.consume(doc["ticket"])


def test_socket_restart_fences_ticket(fixture):
    server, apps, sock = fixture
    auth = AgentXpraTicketAuthority(server, apps)
    doc = auth.issue(apps.agent, apps.session)
    sock.close()
    apps.path.unlink()
    replacement = socket.socket(socket.AF_UNIX)
    try:
        replacement.bind(str(apps.path)); replacement.listen()
        with pytest.raises(ScopeDeniedError): auth.consume(doc["ticket"])
    finally:
        replacement.close()


def test_process_termination_fences_ticket(fixture):
    server, apps, sock = fixture
    auth = AgentXpraTicketAuthority(server, apps)
    doc = auth.issue(apps.agent, apps.session)
    apps.running = False
    with pytest.raises(ScopeDeniedError): auth.consume(doc["ticket"])


def test_symlinked_socket_rejected(fixture, tmp_path):
    server, apps, sock = fixture
    aliased = tmp_path / "socket-alias"
    aliased.symlink_to(apps.path)
    apps.path = aliased
    with pytest.raises(ScopeDeniedError): AgentXpraTicketAuthority(server, apps).issue(apps.agent, apps.session)
