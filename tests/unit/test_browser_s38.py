"""S38 security and durability fixtures; offline, no host browser/network needed."""
from __future__ import annotations

import hashlib
import os
import socket
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.browser_session_leases import BrowserSessionCoordinator
from potbot.tools.browser_connect_gateway import ConnectPolicy, BrowserConnectGateway
from potbot.tools.browser_gallery import BrowserEvidenceGallery
from potbot.tools.browser_evidence import BrowserEvidenceRecorder, BrowserEvidenceScope


class FakeArtifacts:
    def __init__(self): self.storage = {}
    def put(self, actor, raw, mime):
        uid = uuid4()
        self.storage[(actor.workspace_id, uid)] = raw
        return {"id": str(uid), "digest": hashlib.sha256(raw).hexdigest()}
    def read(self, actor, uid):
        return self.storage[(actor.workspace_id, uid)]


def gallery_setup(tmp_path: Path):
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    sandbox = tmp_path / "guest"
    sandbox.mkdir()
    actor = WorkspaceContext(uuid4(), uuid4())
    store = FakeArtifacts()
    gallery = BrowserEvidenceGallery(private / "gallery.sqlite3", store, forbidden_roots=(sandbox,))
    session = "a" * 32
    scope = BrowserEvidenceScope(actor.workspace_id, actor.principal_id, session, uuid4(), "f" * 64)
    jpeg = b"\xff\xd8\xff" + b"sample-image" + b"\xff\xd9"
    raw = {"frame": {"mime": "image/jpeg", "sha256": hashlib.sha256(jpeg).hexdigest(),
                     "bytes": len(jpeg), "base64": __import__("base64").b64encode(jpeg).decode()},
           "aria": "- button: Open"}
    receipt = BrowserEvidenceRecorder(store, actor).record(
        scope=scope, command={"op": "sample", "image": True}, result=raw,
        kind="capture", require_image=True)
    return gallery, actor, store, scope, receipt, jpeg, sandbox


def test_gallery_is_private_persistent_and_checksum_verified(tmp_path):
    gallery, actor, store, scope, receipt, jpeg, _ = gallery_setup(tmp_path)
    gallery.append(actor, scope, receipt)
    page = gallery.list(actor)
    assert page["items"][0]["id"] == str(scope.invocation_id)
    assert page["items"][0]["hasImage"] is True
    assert gallery.read(actor, str(scope.invocation_id), "image") == jpeg
    assert b"aria" in gallery.read(actor, str(scope.invocation_id), "observation")
    with pytest.raises(StateMismatchError):
        gallery.append(actor, scope, receipt)
    other = WorkspaceContext(actor.workspace_id, uuid4())
    assert gallery.list(other)["items"] == []
    with pytest.raises(ScopeDeniedError):
        gallery.read(other, str(scope.invocation_id), "image")
    # Same on-disk index, new operator process.
    new = BrowserEvidenceGallery(gallery.path, store, forbidden_roots=(tmp_path / "guest",))
    assert new.list(actor)["items"][0]["id"] == str(scope.invocation_id)
    store.storage[(actor.workspace_id, __import__("uuid").UUID(receipt["image_artifact_id"]))] = b"tampered"
    with pytest.raises(StateMismatchError):
        new.read(actor, str(scope.invocation_id), "image")


def test_gallery_refuses_guest_location_and_bad_cursors(tmp_path):
    gallery, actor, store, scope, receipt, _, sandbox = gallery_setup(tmp_path)
    sandbox.chmod(0o700)
    with pytest.raises(ScopeDeniedError):
        BrowserEvidenceGallery(sandbox / "guest.sqlite3", store, forbidden_roots=(sandbox,))
    for cursor in (-1, 0, True, 2**55, "1; DROP TABLE"):
        with pytest.raises(InvalidInputError):
            gallery.list(actor, before=cursor)
    with pytest.raises(ScopeDeniedError):
        gallery.append(WorkspaceContext(uuid4(), actor.principal_id), scope, receipt)


def test_exclusive_reservations_expire_fenced_and_are_not_transferred():
    now = [100.0]
    workspace, principal, agent = uuid4(), uuid4(), uuid4()
    broker = BrowserSessionCoordinator(workspace, principal, clock=lambda: now[0])
    first, second = "a" * 32, "b" * 32
    token = broker.reserve(first, owner_kind="agent", owner_id=agent, ttl_seconds=20)
    assert token.generation == 1
    assert broker.reserve(second, owner_kind="operator", owner_id=principal).session_id == second
    with pytest.raises(StateMismatchError):
        broker.reserve(first, owner_kind="agent", owner_id=uuid4())
    with pytest.raises(ScopeDeniedError):
        with broker.operation(first, "wrong-token"):
            pass
    with broker.operation(first, token.token, owner_kind="agent", owner_id=agent):
        with pytest.raises(StateMismatchError):
            with broker.operation(first, token.token): pass
        with pytest.raises(StateMismatchError):
            broker.release(first, token.token)
    assert broker.renew(first, token.token).generation == 1
    now[0] += 61
    assert broker.projection(first)["fenced"] is True
    with pytest.raises(StateMismatchError):
        broker.reserve(first, owner_kind="agent", owner_id=agent)
    with pytest.raises(StateMismatchError):
        broker.renew(first, token.token)
    with pytest.raises(StateMismatchError):
        broker.retired(first, process_stopped=False)
    broker.retired(first, process_stopped=True)
    assert broker.reserve(first, owner_kind="agent", owner_id=agent).generation == 2


def test_cross_workspace_owner_and_release_boundaries():
    ws, operator = uuid4(), uuid4()
    coord = BrowserSessionCoordinator(ws, operator)
    session = "c" * 32
    with pytest.raises(ScopeDeniedError):
        coord.reserve(session, owner_kind="operator", owner_id=uuid4())
    lease = coord.reserve(session, owner_kind="operator", owner_id=operator)
    with pytest.raises(ScopeDeniedError):
        with coord.operation("d" * 32, lease.token): pass
    coord.release(session, lease.token)
    assert coord.projection(session)["reserved"] is False



def test_release_does_not_transfer_live_profile_between_agents():
    ws, op, alice, bob = uuid4(), uuid4(), uuid4(), uuid4()
    coordinator = BrowserSessionCoordinator(ws, op)
    session = "f" * 32
    first = coordinator.reserve(session, owner_kind="agent", owner_id=alice)
    coordinator.release(session, first.token, owner_kind="agent", owner_id=alice)
    with pytest.raises(ScopeDeniedError):
        coordinator.reserve(session, owner_kind="agent", owner_id=bob)
    with pytest.raises(ScopeDeniedError):
        coordinator.reserve(session, owner_kind="operator", owner_id=op)
    # A previous holder may explicitly reacquire before the session is retired.
    reopened = coordinator.reserve(session, owner_kind="agent", owner_id=alice)
    assert reopened.generation == first.generation + 1
    coordinator.retired(session, process_stopped=True)
    assert coordinator.reserve(session, owner_kind="agent", owner_id=bob).owner_id == bob

def fake_dns(ip: str):
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return resolve


@pytest.mark.parametrize("raw", ["127.0.0.1", "internal", "localhost", "faß.example.org", "xn--fa-hia.example.org", "EXAMPLE.ORG", "a..com", "good.com:443", "x" * 300])
def test_connect_host_validation(raw):
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        ConnectPolicy((raw,))


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.2.3.4", "192.168.1.1", "100.64.0.1", "0.0.0.0"])
def test_connect_dns_rejects_all_nonglobal_addresses(address):
    policy = ConnectPolicy(("docs.example.org",))
    with pytest.raises(ScopeDeniedError):
        policy.resolve("docs.example.org:443", resolver=fake_dns(address))



def test_connect_mixed_dns_replies_are_rejected_not_fallback_to_public_ip():
    def mixed(host, port, **_):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in ("8.8.8.8", "169.254.169.254")]
    with pytest.raises(ScopeDeniedError):
        ConnectPolicy(("docs.example.org",)).resolve("docs.example.org:443", resolver=mixed)


def test_connect_exact_origin_dns_pinning_and_hostonly_socket(tmp_path):
    policy = ConnectPolicy(("docs.example.org",))
    assert policy.resolve("docs.example.org:443", resolver=fake_dns("8.8.8.8")) == "8.8.8.8"
    for denied in ("docs.example.org:80", "www.docs.example.org:443", "docs.example.org:444", "evil.org:443"):
        with pytest.raises((ScopeDeniedError, InvalidInputError)):
            policy.resolve(denied, resolver=fake_dns("8.8.8.8"))
    private = tmp_path / "private"; private.mkdir(mode=0o700); private.chmod(0o700)
    proxy = BrowserConnectGateway(private / "proxy.sock", policy=policy, token="t" * 48,
                                  resolver=fake_dns("8.8.8.8"))
    proxy.start()
    try:
        assert (private / "proxy.sock").is_socket()
        assert os.stat(private / "proxy.sock").st_mode & 0o777 == 0o600
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with sock:
            sock.settimeout(3)
            sock.connect(str(private / "proxy.sock"))
            sock.sendall(b"CONNECT docs.example.org:443 HTTP/1.1\r\nHost: docs.example.org:443\r\n\r\n")
            assert b"407" in sock.recv(200)
    finally:
        proxy.stop()
    assert not (private / "proxy.sock").exists()


def test_gallery_http_uses_authenticated_actor_and_bounded_binary(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from potbot.api.browser_gallery import install_gallery_routes

    gallery, actor, _, scope, receipt, picture, _ = gallery_setup(tmp_path)
    gallery.append(actor, scope, receipt)
    app = FastAPI()
    def authenticate(request: Request) -> WorkspaceContext:
        if request.headers.get("authorization") != "Bearer correct-test-session":
            raise ScopeDeniedError("unauthorized")
        return actor
    install_gallery_routes(app, authenticate, gallery)
    client = TestClient(app)
    headers = {"Authorization": "Bearer correct-test-session"}
    response = client.get("/v1/browser/evidence?limit=20", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["id"] == str(scope.invocation_id)
    assert response.headers["cache-control"] == "private, no-store"
    response = client.get(f"/v1/browser/evidence/{scope.invocation_id}/image", headers=headers)
    assert response.status_code == 200
    assert response.content == picture
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["x-content-type-options"] == "nosniff"
