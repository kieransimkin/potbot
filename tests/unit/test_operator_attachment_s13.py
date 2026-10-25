"""Offline Linux integration with real AF_UNIX/SO_PEERCRED, no fake wire transport."""
from __future__ import annotations

import os
import socket
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.tools.operator_attachment_s13 import (
    OperatorAttachmentClient, OperatorAttachmentServer, runtime_attachment,
)

IMAGE = "a" * 64
QUAL = "b" * 64


@pytest.fixture
def fixture():
    with tempfile.TemporaryDirectory(prefix="s13-") as name:
        path = Path(name)
        path.chmod(0o700)
        state = {"running": True, "history_healthy": True}
        ws = uuid4()
        def status():
            return {**state, "workspace_id": str(ws), "image_digest": IMAGE,
                    "supervised_runs": 2}
        yield path / "operator.sock", ws, state, status


def client(path, ws, image=IMAGE):
    return OperatorAttachmentClient(socket_path=path, workspace_id=ws,
                                    image_digest=image, qualification_sha256=QUAL)


def server(path, ws, status):
    return OperatorAttachmentServer(socket_path=path, workspace_id=ws,
                                    image_digest=IMAGE, qualification_sha256=QUAL,
                                    status=status)


def test_real_socket_healthy_and_latched_epoch(fixture):
    path, ws, state, status = fixture
    with server(path, ws, status) as owner:
        checked = client(path, ws)
        response = checked.require_healthy()
        assert response["epoch"] == owner.epoch
        assert response["history_healthy"] is True
        assert response["supervised_runs"] == 2
        assert checked.healthy()
        assert path.stat().st_mode & 0o077 == 0
    assert not path.exists()


def test_epoch_change_requires_worker_restart(fixture):
    path, ws, state, status = fixture
    consumer = client(path, ws)
    with server(path, ws, status):
        consumer.require_healthy()
    with server(path, ws, status):
        # More than the 150ms bounded positive cache.
        import time
        time.sleep(0.17)
        with pytest.raises(CapabilityUnavailableError):
            consumer.require_healthy()
        assert client(path, ws).healthy()


def test_fail_closed_when_monitor_faults(fixture):
    path, ws, state, status = fixture
    with server(path, ws, status):
        consumer = client(path, ws)
        assert consumer.healthy()
        state["history_healthy"] = False
        import time
        time.sleep(0.17)
        assert not consumer.healthy()
        with pytest.raises(CapabilityUnavailableError):
            consumer.require_healthy()


def test_wrong_workspace_or_image_never_produces_healthy_reply(fixture):
    path, ws, state, status = fixture
    with server(path, ws, status):
        for bad in [client(path, uuid4()), client(path, ws, image="c" * 64)]:
            assert not bad.healthy()


def test_private_directory_and_socket_required(fixture):
    path, ws, state, status = fixture
    path.parent.chmod(0o755)
    with pytest.raises(ScopeDeniedError):
        server(path, ws, status).start()
    path.parent.chmod(0o700)
    with server(path, ws, status):
        path.chmod(0o666)
        with pytest.raises(ScopeDeniedError):
            client(path, ws).require_healthy()


def test_preexisting_symlink_denied(fixture):
    path, ws, state, status = fixture
    path.symlink_to("/dev/null")
    with pytest.raises(ScopeDeniedError):
        server(path, ws, status).start()
    with pytest.raises(ScopeDeniedError):
        client(path, ws).require_healthy()


def test_stale_socket_is_recovered_only_in_private_directory(fixture):
    path, ws, state, status = fixture
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(path))
    assert path.is_socket()
    with server(path, ws, status):
        assert client(path, ws).healthy()


def test_live_operator_cannot_be_displaced(fixture):
    path, ws, state, status = fixture
    with server(path, ws, status):
        with pytest.raises(ScopeDeniedError):
            server(path, ws, status).start()
        assert client(path, ws).healthy()


def test_invalid_operator_status_fails_closed(fixture):
    path, ws, state, status = fixture
    with server(path, ws, lambda: {"running": True, "history_healthy": True,
                                    "workspace_id": str(ws), "image_digest": "f"*64}):
        assert not client(path, ws).healthy()


def test_concurrent_clients_with_real_peer_credentials(fixture):
    path, ws, state, status = fixture
    results = []
    with server(path, ws, status):
        threads = [threading.Thread(target=lambda: results.append(client(path, ws).healthy()))
                   for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        assert results == [True] * 8


def test_required_runtime_attachment_cannot_be_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("POTBOT_OPERATOR_REQUIRED", "1")
    monkeypatch.delenv("POTBOT_OPERATOR_SOCKET", raising=False)
    runtime = type("Runtime", (), {"home":tmp_path / "home", "rootfs":tmp_path / "root"})()
    with pytest.raises(CapabilityUnavailableError):
        runtime_attachment(runtime)


def test_runtime_socket_inside_guest_mount_is_rejected(monkeypatch, tmp_path):
    home = tmp_path / "home"; rootfs = tmp_path / "root"
    home.mkdir(); rootfs.mkdir()
    monkeypatch.setenv("POTBOT_OPERATOR_SOCKET", str(home / "operator.sock"))
    monkeypatch.setenv("POTBOT_OPERATOR_WORKSPACE_ID", str(uuid4()))
    runtime = type("Runtime", (), {"home":home, "rootfs":rootfs,
                    "image_digest":IMAGE,"qualification_sha256":QUAL})()
    with pytest.raises(ScopeDeniedError):
        runtime_attachment(runtime)


def test_operator_mount_binding_cannot_be_substituted(fixture):
    from potbot.tools.operator_attachment_s13 import mount_fingerprint
    path, ws, state, status = fixture
    config, history = path.parent / "config", path.parent / "history"
    config.mkdir(); history.mkdir()
    pinned = mount_fingerprint(config, history)
    assert pinned != "0" * 64
    with OperatorAttachmentServer(socket_path=path, workspace_id=ws,
                                  image_digest=IMAGE, qualification_sha256=QUAL,
                                  mounts_sha256=pinned, status=status):
        trusted = OperatorAttachmentClient(socket_path=path, workspace_id=ws,
                                           image_digest=IMAGE, qualification_sha256=QUAL,
                                           mounts_sha256=pinned)
        assert trusted.healthy()
        assert not client(path, ws).healthy()
