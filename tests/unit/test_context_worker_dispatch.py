"""Host dispatch orchestration with fake processes; not sandbox qualification."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context import worker_dispatch as module
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError


@pytest.fixture
def rig(tmp_path, monkeypatch):
    principal, agent_id = WorkspaceContext(uuid4(), uuid4()), uuid4()
    root = tmp_path / "home"
    folder = root / "alice"
    folder.mkdir(parents=True)
    events = []
    ledger = SimpleNamespace(home=root)
    home = SimpleNamespace(
        workspace_id=principal.workspace_id, agent_id=agent_id, directory="alice", uid=420, gid=69
    )
    registry = SimpleNamespace(ledger=ledger, lookup=lambda *args: home)
    request = SimpleNamespace(
        telemetry_agent_id=agent_id,
        telemetry_workspace_id=principal.workspace_id,
        telemetry_run_id=uuid4(),
        telemetry_identity_kind="agent",
        guest_uid=420,
        guest_gid=69,
        stdin=b"",
        ipc_profile="none",
        read_only_roots=(),
        writable_roots=(),
        workspace_root=root,
        sandbox_root=root,
        cwd=folder,
    )

    class Guard:
        lease = None
        fenced = False

        def start(self):
            events.append("claim")
            self.lease = object()

        def finish(self, *, evidence_sha256):
            assert len(evidence_sha256) == 64
            events.append("finish")

        def abandon(self):
            events.append("abandon")

    guard = Guard()
    guard.ledger = ledger
    guard.request = SimpleNamespace(
        workspace_id=principal.workspace_id, actor_id=agent_id, image_digest="a" * 64
    )
    operator = SimpleNamespace(
        _lifecycle=threading.RLock(),
        _require_live=lambda: None,
        _context_manager=SimpleNamespace(
            principal=principal, service=SimpleNamespace(database=None)
        ),
        _project_agent_context=True,
        workspace_id=principal.workspace_id,
        ledger=ledger,
        runtime=SimpleNamespace(
            image_digest="a" * 64,
            agent_context_projections=[SimpleNamespace(agent_id=agent_id, directory="alice")],
        ),
        supervisor=SimpleNamespace(_closing=threading.Event()),
        backend=object(),
        monitor=SimpleNamespace(require_active=lambda: None, is_healthy=lambda: True),
    )

    class Process:
        async def close(self, *, force=False):
            events.append("stop")
            return SimpleNamespace(processes_stopped=True)

    process = Process()

    class Supervisor:
        def __init__(self, backend):
            assert backend is operator.backend

        async def open(self, req, *, cancelled):
            assert req is request and not cancelled()
            events.append("spawn")
            return process

    monkeypatch.setitem(
        sys.modules,
        "potbot.tools.interactive_supervisor",
        SimpleNamespace(InteractiveProcessSupervisor=Supervisor),
    )
    return SimpleNamespace(**locals())


def test_dispatch_validates_identity_and_approval_before_any_spawn(rig):
    supervisor = module._prepare(
        rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
    )
    assert isinstance(supervisor, rig.Supervisor)
    assert rig.events == ["claim"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("stdin", b"other protocol"),
        ("guest_uid", 421),
        ("guest_gid", 70),
        ("telemetry_identity_kind", "principal"),
        ("ipc_profile", "private-loopback-v1"),
        ("telemetry_agent_id", None),
        ("telemetry_run_id", None),
    ],
)
def test_invalid_request_never_starts_process_lease(rig, field, value):
    setattr(rig.request, field, value)
    with pytest.raises(ScopeDeniedError):
        module._prepare(
            rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
        )
    assert rig.events == []


def test_other_agent_home_is_rejected_before_spawn(rig, tmp_path):
    other = rig.root / "bob"
    other.mkdir()
    rig.request.cwd = other
    with pytest.raises(ScopeDeniedError):
        module._prepare(
            rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
        )
    assert rig.events == []


def test_callback_false_is_not_approval(rig):
    with pytest.raises(ScopeDeniedError):
        module._prepare(
            rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: False
        )
    assert rig.events == []


def test_shutdown_gate_blocks_new_context_launch(rig):
    rig.operator.supervisor._closing.set()
    with pytest.raises(ScopeDeniedError):
        module._prepare(
            rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
        )
    assert rig.events == []


def test_dispatch_releases_claim_only_after_channel_confirmed_stop(rig, monkeypatch):
    class Channel:
        def __init__(self, **kwargs):
            assert kwargs["agent_id"] == rig.agent_id and kwargs["principal"] == rig.principal

        async def serve(self, process):
            assert process is rig.process
            rig.events.append("channel")
            await process.close()
            return SimpleNamespace(
                session_id=uuid4(),
                exit=SimpleNamespace(return_code=0, isolation_identity="b" * 64),
                channel=SimpleNamespace(requests=2, errors=0),
            )

    monkeypatch.setattr(module, "ContextWorkerChannel", Channel)
    result = asyncio.run(
        module.dispatch_context_worker(
            rig.operator,
            rig.registry,
            rig.principal,
            rig.request,
            rig.guard,
            approve=lambda _: None,
        )
    )
    assert result.channel.requests == 2
    assert rig.events == ["claim", "spawn", "channel", "stop", "finish"]


def test_cancel_during_spawn_settles_launch_then_stops_before_abandon(rig, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def opening(self, req, *, cancelled):
            rig.events.append("spawn-start")
            entered.set()
            await release.wait()
            rig.events.append("spawn-return")
            return rig.process

        monkeypatch.setattr(rig.Supervisor, "open", opening)
        task = asyncio.create_task(
            module.dispatch_context_worker(
                rig.operator,
                rig.registry,
                rig.principal,
                rig.request,
                rig.guard,
                approve=lambda _: None,
            )
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert rig.events == ["claim", "spawn-start", "spawn-return", "stop", "abandon"]


def test_image_mismatch_is_rejected_before_admission(rig):
    rig.guard.request.image_digest = "b" * 64
    with pytest.raises(ScopeDeniedError):
        module._prepare(
            rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
        )
    assert rig.events == []


def test_missing_registered_projection_is_reconciled_before_claim(rig, monkeypatch):
    rig.operator.runtime.agent_context_projections = []

    class Lifecycle:
        def __init__(self, service, registry):
            assert registry is rig.registry

        def reconcile_existing(self, identity):
            assert identity == (rig.principal, rig.agent_id)
            assert rig.guard.lease is None
            rig.events.append("reconcile")

    def refresh():
        assert rig.guard.lease is None
        rig.events.append("refresh")
        rig.operator.runtime.agent_context_projections = [
            SimpleNamespace(agent_id=rig.agent_id, directory="alice")
        ]

    rig.operator.refresh_context = refresh
    monkeypatch.setitem(
        sys.modules,
        "potbot.context.agent",
        SimpleNamespace(AgentContextLifecycle=Lifecycle, ContextIdentity=lambda p, a: (p, a)),
    )
    module._prepare(
        rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
    )
    assert rig.events == ["reconcile", "refresh", "claim"]


def test_missing_registry_identity_is_not_adopted_from_the_filesystem(rig):
    rig.registry.lookup = lambda *args: None
    with pytest.raises(ScopeDeniedError):
        module._prepare(
            rig.operator, rig.registry, rig.principal, rig.request, rig.guard, lambda _: None
        )
    assert rig.events == []
