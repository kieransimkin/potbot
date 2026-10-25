"""S2: brokered tool and PTY contracts (fake launch is NOT isolation evidence)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError, StateMismatchError
from potbot.tools.gateway import ToolGateway
from potbot.tools.isolation import IsolationLaunch
from potbot.tools.shared_bash_sessions import SharedBashSessionSupervisor
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessResult
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource, WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import (
    WorkspaceExecutorIdentity,
    WorkspaceLeaseGuard,
    admission_digest,
    assert_shared_execution_allowed,
)

DIGEST = "a" * 64
QUALIFIED = "f" * 64


@pytest.fixture
def identity(tmp_path: Path):
    home = tmp_path / "workspaces" / "home"
    home.mkdir(parents=True)
    (home / ".tmp").mkdir()
    candidate = home / "project"
    candidate.mkdir()
    image = tmp_path / "images" / "revision"
    image.mkdir(parents=True)
    private = tmp_path / "controller"
    private.mkdir()
    ledger = WorkspaceLeaseLedger(private / "leases.db", workspace_home=home)
    workspace_id, actor_id = uuid4(), uuid4()
    ledger.pin_image(
        workspace_id, digest=DIGEST, image_ref="ghcr.io/example/root@sha256:" + DIGEST,
        build_commit="c" * 40, platform="linux/amd64",
        rootfs_path=image, qualification_sha256=QUALIFIED,
    )
    model_staging = tmp_path / "sandbox-ai"
    model_staging.mkdir()
    library = tmp_path / "shared-models"
    library.mkdir()
    tools = tmp_path / "shared-tools"
    tools.mkdir()
    runtime = SharedWorkspaceRuntime(DIGEST, image, home, QUALIFIED,
                                     model_staging=model_staging, shared_models=library,
                                     shared_tools=tools)
    return SimpleNamespace(
        home=home, candidate=candidate, image=image, ledger=ledger,
        workspace_id=workspace_id, actor_id=actor_id, runtime=runtime,
        ctx=WorkspaceContext(workspace_id, actor_id),
    )


class _TestOnlyUnconfinedBackend(SharedWorkspaceBubblewrapBackend):
    """Only for unit testing interface lifecycle; NEVER a real OS sandbox."""

    def prepare(self, *, status_fd: int, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return IsolationLaunch(
            ("/bin/bash", "--noprofile", "--norc", "-i"), (status_fd,),
            "UNCONFINED_TEST_BACKEND_NOT_FOR_PRODUCTION",
        )


class _DeniedBackend(SharedWorkspaceBubblewrapBackend):
    def prepare(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        raise CapabilityUnavailableError("isolation unavailable")


def _second_write(identity):  # type: ignore[no-untyped-def]
    return identity.ledger.claim(AccessRequest(
        identity.workspace_id, uuid4(), "file.write", uuid4().hex, "unknown",
        (AccessResource("workspace", "*", "write"),), DIGEST,
    ))


def test_guard_renews_and_blocks_other_agents(identity) -> None:
    guard = WorkspaceLeaseGuard.exclusive_home(
        identity.ledger, WorkspaceExecutorIdentity(identity.workspace_id, identity.actor_id, DIGEST),
        operation="tool.execute", idempotency_key="guard-test",
        ttl_seconds=1, interval_seconds=0.1,
    )
    guard.start()
    time.sleep(1.2)
    guard.ensure_live()
    with pytest.raises(StateMismatchError):
        _second_write(identity)
    guard.finish(evidence_sha256=admission_digest("finished"))
    second = _second_write(identity)
    identity.ledger.finish(second, result_sha256=QUALIFIED)
    with pytest.raises(StateMismatchError):
        guard.finish(evidence_sha256=QUALIFIED)


def test_runtime_pin_and_home_guard(identity, tmp_path: Path) -> None:
    assert_shared_execution_allowed(
        runtime=identity.runtime, ledger=identity.ledger,
        workspace_id=identity.workspace_id, candidate_path=str(identity.candidate),
    )
    with pytest.raises(ScopeDeniedError):
        assert_shared_execution_allowed(
            runtime=identity.runtime, ledger=identity.ledger,
            workspace_id=identity.workspace_id, candidate_path=str(tmp_path / "controller"),
        )
    invalid = SharedWorkspaceRuntime("b" * 64, identity.image, identity.home, QUALIFIED)
    with pytest.raises(StateMismatchError):
        assert_shared_execution_allowed(
            runtime=invalid, ledger=identity.ledger,
            workspace_id=identity.workspace_id, candidate_path=str(identity.candidate),
        )


def test_gateway_fails_closed_without_broker(identity) -> None:
    fake = SimpleNamespace(backend=object())
    common = dict(
        database=Mock(), artifacts=Mock(), candidates=Mock(), registry=Mock(),
        capabilities=Mock(), receipts=Mock(),
    )
    with pytest.raises((ScopeDeniedError, StateMismatchError)):
        ToolGateway(**common, supervisor=fake, shared_runtime=identity.runtime,
                    shared_leases=identity.ledger)
    with pytest.raises(Exception):
        ToolGateway(**common, shared_runtime=identity.runtime)
    with pytest.raises(ScopeDeniedError):
        ToolGateway(**common, shared_runtime=identity.runtime,
                    shared_leases=identity.ledger, read_only_roots=(identity.image,))


def _fake_gateway(identity, *, outcome: str = "success"):  # type: ignore[no-untyped-def]
    """Mock the existing PostgreSQL admission, not the new lease broker."""
    gateway = ToolGateway.__new__(ToolGateway)
    action_id, candidate_id, reservation_id = uuid4(), uuid4(), uuid4()
    candidate = {"path": str(identity.candidate), "state_id": uuid4()}
    lease = SimpleNamespace(candidate_id=candidate_id, owner="agent", generation=1)
    gateway.shared_runtime = identity.runtime
    gateway.shared_leases = identity.ledger
    gateway.workspace_sandbox = None
    gateway.read_only_roots = ()
    gateway.secrets = SimpleNamespace(resolve=lambda *a, **k: SimpleNamespace(
        environment={}, redactor=SimpleNamespace(secrets=[], reject_patterns=[])))
    gateway.capabilities = SimpleNamespace(allowed_secret_refs=())
    gateway.registry = SimpleNamespace(get=lambda _: SimpleNamespace(
        secret_refs=(), accepts_stdin=False, argv_prefix=("/bin/bash", "-c", "pwd"),
        environment=(), timeout_ms=30000, max_output_bytes=10000,
        memory_bytes=None, cpu_seconds=None,
    ))
    gateway.candidates = SimpleNamespace(check_lease=lambda *args: candidate)
    inspected = []
    gateway.store = SimpleNamespace(inspect=lambda *args: inspected.append(1) or {
        "action": {"kind": "bash", "stdin_artifact_id": None},
        "run": None, "completion": None,
    })
    run = {"id": uuid4(), "input_fingerprint": "x" * 64}
    gateway._admit = lambda *args: (run, gateway.registry.get("bash"),
        SimpleNamespace(arguments=(), working_directory="."), candidate)
    gateway._paths = lambda root, proposal: root
    gateway._fingerprint = lambda *a, **k: "e" * 64
    gateway._check_secret_files = lambda *args: None
    completed = []
    uncertain = []
    gateway._complete = lambda *args: completed.append(1)
    gateway._uncertain = lambda *args: uncertain.append(1)
    gateway.receipts = SimpleNamespace(root=identity.home.parent.parent / "receipts",
        write=lambda *args: None)
    gateway._cancelled = lambda *args: False

    def run_process(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert args[0].sandbox_root == identity.home
        assert len(identity.ledger.active(identity.workspace_id)) == 1
        with pytest.raises(StateMismatchError):
            _second_write(identity)
        if outcome == "error":
            raise OSError("dispatched command lost")
        return ProcessResult("completed", 0, b"ok", b"", 1, False, False,
                             True, "fake-isolation", processes_stopped=True)

    gateway.supervisor = SimpleNamespace(run=run_process)
    return gateway, action_id, reservation_id, lease, completed, uncertain


def test_gateway_acquires_exclusive_home_until_receipt(identity) -> None:
    gateway, action_id, reservation_id, lease, completed, uncertain = _fake_gateway(identity)
    response = gateway.execute(identity.ctx, action_id, lease, reservation_id)
    assert response["run"] is None  # fixture returns a synthetic inspect view
    assert completed == [1] and not uncertain
    assert identity.ledger.active(identity.workspace_id) == []


def test_gateway_dispatched_exception_blocks_until_reconciliation(identity) -> None:
    gateway, action_id, reservation_id, lease, completed, uncertain = _fake_gateway(
        identity, outcome="error"
    )
    with pytest.raises(OSError):
        gateway.execute(identity.ctx, action_id, lease, reservation_id)
    assert uncertain == [1] and not completed
    assert identity.ledger.active(identity.workspace_id)[0]["state"] == "indeterminate"
    with pytest.raises(StateMismatchError):
        _second_write(identity)


def test_bash_detach_reconnect_resize_and_terminal_exit(identity) -> None:
    backend = _TestOnlyUnconfinedBackend(identity.runtime)
    broker = SharedBashSessionSupervisor(
        ledger=identity.ledger, backend=backend,
        authorize=lambda ws, actor: ws == identity.workspace_id and actor == identity.actor_id,
    )
    session = broker.open(workspace_id=identity.workspace_id, actor_id=identity.actor_id)
    try:
        with pytest.raises(StateMismatchError):
            _second_write(identity)
        broker.detach(session_id=session.id, workspace_id=identity.workspace_id,
                      actor_id=identity.actor_id)
        assert broker.attach(session_id=session.id, workspace_id=identity.workspace_id,
                             actor_id=identity.actor_id).running
        broker.resize(session_id=session.id, workspace_id=identity.workspace_id,
                      actor_id=identity.actor_id, columns=120, rows=32)
        broker.write(session_id=session.id, workspace_id=identity.workspace_id,
                     actor_id=identity.actor_id, data=b"echo POTBOT_BASH_S2_TEST\n")
        observed = bytearray()
        for _ in range(30):
            observed.extend(broker.read(session_id=session.id, workspace_id=identity.workspace_id,
                                        actor_id=identity.actor_id, timeout=0.1))
            if b"POTBOT_BASH_S2_TEST" in observed:
                break
        assert b"POTBOT_BASH_S2_TEST" in observed
        with pytest.raises(ScopeDeniedError):
            broker.attach(session_id=session.id, workspace_id=identity.workspace_id,
                          actor_id=uuid4())
    finally:
        broker.terminate(session_id=session.id, workspace_id=identity.workspace_id,
                         actor_id=identity.actor_id)
    assert not broker.list_sessions(workspace_id=identity.workspace_id,
                                    actor_id=identity.actor_id)[0].running
    assert identity.ledger.active(identity.workspace_id) == []


def test_bash_fails_closed_when_guest_isolation_unavailable(identity) -> None:
    broker = SharedBashSessionSupervisor(
        ledger=identity.ledger, backend=_DeniedBackend(identity.runtime),
        authorize=lambda ws, actor: True,
    )
    with pytest.raises(CapabilityUnavailableError):
        broker.open(workspace_id=identity.workspace_id, actor_id=identity.actor_id)
    assert identity.ledger.active(identity.workspace_id) == []


def test_bash_authorization_and_runtime_change(identity) -> None:
    broker = SharedBashSessionSupervisor(
        ledger=identity.ledger, backend=_TestOnlyUnconfinedBackend(identity.runtime),
        authorize=lambda ws, actor: False,
    )
    with pytest.raises(ScopeDeniedError):
        broker.open(workspace_id=identity.workspace_id, actor_id=identity.actor_id)
    assert identity.ledger.active(identity.workspace_id) == []


def test_named_agent_terminal_reports_logical_identity_not_kernel_attestation(identity) -> None:
    from potbot.tools.agent_homes import AgentHomeRegistry

    registry = AgentHomeRegistry(identity.ledger, workspace_name="DanceFlow Workspace")
    agent_home = registry.provision(identity.workspace_id, identity.actor_id)
    observed: dict[str, object] = {}

    class _CapturingTestOnlyBackend(_TestOnlyUnconfinedBackend):
        def prepare(self, *, status_fd: int, **kwargs):  # type: ignore[no-untyped-def]
            observed.update(kwargs)
            return super().prepare(status_fd=status_fd, **kwargs)

    broker = SharedBashSessionSupervisor(
        ledger=identity.ledger,
        backend=_CapturingTestOnlyBackend(identity.runtime),
        authorize=lambda ws, actor: ws == identity.workspace_id and actor == identity.actor_id,
        agent_homes=registry,
    )
    session = broker.open(workspace_id=identity.workspace_id, actor_id=identity.actor_id)
    try:
        assert session.guest_home == agent_home.guest_path
        assert session.simulated_uid == 420
        assert session.simulated_gid == 69
        assert session.kernel_identity_qualified is False
        assert observed["cwd"] == identity.home / agent_home.directory
        assert observed["guest_uid"] == 420
        assert observed["guest_gid"] == 69
        with pytest.raises(StateMismatchError):
            registry.rename(identity.workspace_id, identity.actor_id,
                            "new-name", expected_revision=1)
    finally:
        broker.terminate(session_id=session.id, workspace_id=identity.workspace_id,
                         actor_id=identity.actor_id)
    renamed = registry.rename(identity.workspace_id, identity.actor_id,
                              "new-name", expected_revision=1)
    assert renamed.directory == "new-name" and renamed.uid == 420
