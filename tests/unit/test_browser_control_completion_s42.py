"""S42: adversarial one-use agent Xpra ticket and display boundaries.

Real AF_UNIX socket inodes are used, but no network browser or unqualified Xpra
instance is started. Host acceptance remains a separate, mandatory gate.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, CapabilityUnavailableError
from potbot.tools.agent_xpra_streams import AgentXpraTicketAuthority


class FakeServer:
    readonly_mode = True
    def __init__(self, workspace): self.workspace_id = workspace
    def environment(self): return {"DISPLAY": ":411"}


class FakeApps:
    def __init__(self, workspace):
        self.workspace_id = workspace
        self.agent_displays = object()
        self.bindings = {}
    def agent_stream_socket(self, agent, session):
        if (agent, session) not in self.bindings:
            raise ScopeDeniedError("session inaccessible")
        return self.bindings[agent,session]
    def agent_display_inventory(self):
        return [{"id": sid, "agentId":str(agent), "state":"running", "title":"Browser",
                 "viewOnly":True,"startedAt":1} for agent,sid in self.bindings]


@pytest.fixture
def pair(tmp_path):
    workspace, agent1, agent2 = uuid4(), uuid4(), uuid4()
    s1, s2 = 'a'*32, 'b'*32
    sockets, paths = [], []
    for name in ('a','b'):
        p = tmp_path / f'{name}.sock'
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(p)); sock.listen()
        sockets.append(sock); paths.append(p)
    app=FakeApps(workspace)
    app.bindings[agent1,s1]=paths[0];app.bindings[agent2,s2]=paths[1]
    try: yield FakeServer(workspace),app,(agent1,s1,paths[0]),(agent2,s2,paths[1]),sockets
    finally:
        for s in sockets: s.close()


def test_agent_streams_are_not_interchangeable(pair):
    server,apps,left,right,_=pair
    authority=AgentXpraTicketAuthority(server,apps)
    with pytest.raises(ScopeDeniedError): authority.issue(left[0],right[1])
    with pytest.raises(ScopeDeniedError): authority.issue(right[0],left[1])
    first=authority.issue(left[0],left[1]); second=authority.issue(right[0],right[1])
    assert first['ticket'] != second['ticket']
    assert authority.consume(first['ticket']).agent_id == left[0]
    assert authority.consume(second['ticket']).agent_id == right[0]
    with pytest.raises(ScopeDeniedError): authority.consume(first['ticket'])


def test_expired_and_malformed_tickets_always_fail_closed(pair):
    server,apps,left,right,_=pair
    clock=[100.0]
    authority=AgentXpraTicketAuthority(server,apps,clock=lambda:clock[0],lifetime=2)
    doc=authority.issue(left[0],left[1]); clock[0]=103
    with pytest.raises(ScopeDeniedError): authority.consume(doc['ticket'])
    for bad in ('',doc['ticket']+'=',doc['ticket']+'%00','?' + doc['ticket']):
        with pytest.raises(ScopeDeniedError): authority.consume(bad)


def test_live_binding_changes_on_socket_replacement(pair):
    server,apps,left,right,sockets=pair
    authority=AgentXpraTicketAuthority(server,apps)
    doc=authority.issue(left[0],left[1]); binding=authority.consume(doc['ticket'])
    assert authority.still_live(binding)
    sockets[0].close();left[2].unlink()
    rebound=socket.socket(socket.AF_UNIX)
    try:
        rebound.bind(str(left[2]));rebound.listen()
        assert not authority.still_live(binding)
        # The previously issued credential cannot attach to the new listener.
        with pytest.raises(ScopeDeniedError): authority.consume(doc['ticket'])
    finally: rebound.close()


def test_session_exit_disables_active_stream(pair):
    server,apps,left,right,_=pair
    authority=AgentXpraTicketAuthority(server,apps)
    token=authority.issue(right[0],right[1]);binding=authority.consume(token['ticket'])
    assert authority.still_live(binding)
    del apps.bindings[right[0],right[1]]
    assert not authority.still_live(binding)


def test_pending_capacity_and_expiry(pair):
    server,apps,left,right,_=pair
    clock=[500.0]
    authority=AgentXpraTicketAuthority(server,apps,clock=lambda:clock[0],lifetime=1,maximum=1)
    first=authority.issue(left[0],left[1])
    with pytest.raises(CapabilityUnavailableError): authority.issue(right[0],right[1])
    clock[0]+=2
    second=authority.issue(right[0],right[1])
    assert second['ticket'] != first['ticket']


def test_socket_symlinks_are_not_capabilities(pair,tmp_path):
    server,apps,left,right,_=pair
    link=tmp_path/'untrusted.sock'
    link.symlink_to(left[2])
    apps.bindings[left[0],left[1]]=link
    with pytest.raises(ScopeDeniedError): AgentXpraTicketAuthority(server,apps).issue(left[0],left[1])
