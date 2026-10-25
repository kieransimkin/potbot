"""S7 operator lifecycle; uses real inotify, mocks child execution only."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource, WorkspaceLeaseLedger

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify requires Linux")


@pytest.fixture
def owner(tmp_path: Path):
    home, image, config, past, private = (tmp_path / part for part in (
        "home", "rootfs", "config", "ai-past", "private"
    ))
    for path in (home, image, config, past, private):
        path.mkdir()
    workspace, actor = uuid4(), uuid4()
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    digest, qualification = "a" * 64, "b" * 64
    ledger.pin_image(workspace, digest=digest,
                     image_ref="ghcr.io/test/image@sha256:" + digest,
                     build_commit="c" * 40, platform="linux/amd64",
                     rootfs_path=image, qualification_sha256=qualification)
    runtime = SharedWorkspaceRuntime(digest, image, home, qualification,
                                     ai_config_dir=config, ai_history_dir=past)
    return AiWorkspaceOperator(workspace_id=workspace, ledger=ledger,
                               runtime=runtime, monitor_interval_seconds=5), actor, config, past


def test_operator_starts_watcher_before_admitting_gateway(owner) -> None:
    op, _, config, past = owner
    with pytest.raises(CapabilityUnavailableError):
        _ = op.supervisor
    with op:
        assert op.monitor.is_healthy()
        assert op.backend.ai_monitor is op.monitor
        params = op.gateway_parameters()
        assert params["supervisor"] is op.supervisor
        assert params["ai_monitor"] is op.monitor
        assert params["shared_leases"] is op.ledger
        (config / "settings.toml").write_text("x = 1\n")
        assert op.monitor.poll_once() is not None
        assert op.status()["history_healthy"] is True
    assert op.status()["running"] is False
    with pytest.raises(CapabilityUnavailableError):
        _ = op.backend
    assert len(list(past.glob("*.reverse.json"))) == 1


def test_operator_refuses_to_stop_with_outstanding_workspace_lease(owner) -> None:
    op, actor, _, _ = owner
    op.start()
    image = op.runtime.image_digest
    lease = op.ledger.claim(AccessRequest(
        op.workspace_id, actor, "bash.session", "active-terminal", "unknown",
        (AccessResource("workspace", "*", "write"),), image,
    ))
    try:
        with pytest.raises(StateMismatchError):
            op.close(timeout_seconds=1)
        assert op.monitor.is_healthy()  # Never stop journaling while a PTY lives.
    finally:
        op.ledger.finish(lease, result_sha256="f" * 64)
        op.close()


def test_operator_supervisor_cancels_active_runs_before_shutdown(
    owner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    op, _, _, _ = owner
    started = threading.Event()
    stopped = threading.Event()
    observations: list[object] = []

    def fake_supervisor_run(self: ProcessSupervisor, request: object, *,
                            cancelled: object = None, redactor: object = None) -> str:
        del self, request, redactor
        assert callable(cancelled)
        started.set()
        for _ in range(400):
            if cancelled():
                stopped.set()
                return "cancelled"
            time.sleep(0.005)
        raise AssertionError("monitor-aware cancellation did not fire")

    monkeypatch.setattr(ProcessSupervisor, "run", fake_supervisor_run)
    with op:
        worker = threading.Thread(target=lambda: observations.append(op.supervisor.run(None)))  # type: ignore[arg-type]
        worker.start()
        assert started.wait(2)
        assert op.supervisor.active_runs == 1
        op.close(timeout_seconds=3)
        worker.join(timeout=3)
        assert stopped.is_set()
        assert observations == ["cancelled"]
        assert op.status()["supervised_runs"] == 0


def test_operator_rejects_duplicate_start(owner) -> None:
    op, _, _, _ = owner
    with op:
        with pytest.raises(StateMismatchError):
            op.start()
    with pytest.raises(StateMismatchError):
        op.start()


def test_operator_faulted_watcher_releases_lock_only_after_workers_end(owner) -> None:
    op, _, config, past = owner
    op.start()
    monitor = op.monitor
    # Test a fatal journal inconsistency by writing a disallowed symlink.
    (config / "unsafe").symlink_to(config.parent)
    with pytest.raises(ScopeDeniedError):
        monitor.poll_once()
    assert monitor.is_healthy() is False
    with pytest.raises(CapabilityUnavailableError):
        _ = op.backend
    with pytest.raises(CapabilityUnavailableError):
        op.close()
    assert op.status()["running"] is False
    (config / "unsafe").unlink()
    (config / "offline.txt").write_text("unobserved change")
    # An unobserved change cannot be silently accepted as a new baseline.
    with pytest.raises(StateMismatchError):
        AiConfigHistoryMonitor(config_dir=config, past_dir=past).start()
