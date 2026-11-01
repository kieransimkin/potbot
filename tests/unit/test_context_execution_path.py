"""Exercise the ordinary execution integration with explicitly fake process/DB boundaries."""
from __future__ import annotations
import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from potbot.context import runtime_integration as integration
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError, StateMismatchError

@pytest.fixture
def rig(tmp_path, monkeypatch):
    actor, agent, run = (WorkspaceContext(uuid4(), uuid4()), uuid4(), uuid4())
    home = tmp_path / 'home'
    (home / 'alice').mkdir(parents=True)
    events = []

    class Guard:
        lease = None
        _finished = False
        fenced = False

        def start(self):
            self.lease = object()
            events.append('claim')

        def ensure_live(self):
            events.append('live')

        def finish(self, **kw):
            self._finished = True
            events.append('finish')

        def abandon(self):
            events.append('abandon')
    guard = Guard()
    registry = SimpleNamespace(lookup=lambda *a: SimpleNamespace(directory='alice', uid=420, gid=69))

    class Binding:
        database = object()

        def check(self, *a):
            events.append('check')

        def prepare(self, *a):
            events.append('prepare')
            return projection
    projection = SimpleNamespace(backend=object(), close=lambda: events.append('drop-projection'))
    executor = SimpleNamespace(_context_binding=Binding(), homes=registry, runtime=SimpleNamespace(home=home, image_digest='a' * 64), ledger=object(), supervisor=SimpleNamespace(monitor_config=object()), _executable=lambda op: events.append('binary'))
    op = SimpleNamespace(argv=('/bin/worker',), binding=SimpleNamespace(operation_ref='syn://thing/work', output_type_ref='syn://type/out', effect_class='workspace_write', policy=SimpleNamespace(max_wall_ms=1000, max_output_bytes=4096)))
    source = SimpleNamespace(type_ref='syn://type/in', value={'hello': 1})
    cog = SimpleNamespace(invocation_id=run)

    @classmethod
    def exclusive(cls, *a, **kw):
        return guard

    class LeaseGuard:
        exclusive_home = exclusive
    monkeypatch.setitem(sys.modules, 'potbot.tools.workspace_supervision', SimpleNamespace(WorkspaceExecutorIdentity=lambda *a: object(), WorkspaceLeaseGuard=LeaseGuard, assert_shared_execution_allowed=lambda **kw: events.append('pin')))

    class Typed:

        def __init__(self, type_ref, value):
            self.type_ref, self.value, self.fingerprint = (type_ref, value, 'b' * 64)
    monkeypatch.setitem(sys.modules, 'potbot.cogs.contracts', SimpleNamespace(TypedCogValue=Typed))
    monkeypatch.setitem(sys.modules, 'potbot.tools.supervisor', SimpleNamespace(ProcessRequest=lambda **kw: SimpleNamespace(**kw)))
    receipt = SimpleNamespace(processes_stopped=True, return_code=0, isolation_identity='c' * 64)

    class Process:

        async def close(self, *, force=False):
            events.append('stop')
            return receipt
    process = Process()

    class Supervisor:

        def __init__(self, backend):
            assert backend is projection.backend

        async def open(self, request, *, cancelled):
            assert request.stdin == b'' and request.telemetry_run_id == run
            assert not cancelled()
            events.append('spawn')
            return process
    monkeypatch.setitem(sys.modules, 'potbot.tools.interactive_supervisor', SimpleNamespace(InteractiveProcessSupervisor=Supervisor))

    class Channel:

        def __init__(self, **kw):
            self.kw = kw
            events.append('channel-init')

        async def serve(self, child):
            assert child is process
            events.append('rpc')
            await child.close()
            return SimpleNamespace(exit=receipt, session_id=uuid4(), output=dict(schema_version=1, type_ref='syn://type/out', value={'ok': True}))
    monkeypatch.setattr(integration, 'ContextWorkerChannel', Channel)

    async def execute():
        return await integration.execute_context_operation(executor, op, source, cog, actor, agent, threading.Event(), threading.Event())
    return SimpleNamespace(**locals())

def test_normal_context_work_releases_only_after_confirmed_result_and_shutdown(rig):
    result = asyncio.run(rig.execute())
    assert result.value == {'ok': True}
    assert rig.events.index('prepare') < rig.events.index('claim') < rig.events.index('spawn')
    assert rig.events.index('stop') < rig.events.index('finish') < rig.events.index('drop-projection')
    assert 'abandon' not in rig.events

def test_nonzero_exit_does_not_make_result_receipt_a_success(rig):
    rig.receipt.return_code = 2
    with pytest.raises(StateMismatchError):
        asyncio.run(rig.execute())
    assert 'finish' not in rig.events and 'abandon' in rig.events and ('drop-projection' in rig.events)

def test_unconfirmed_shutdown_retains_projection_and_claim(rig):
    rig.receipt.processes_stopped = False
    with pytest.raises(IndeterminateExecutionError):
        asyncio.run(rig.execute())
    assert 'finish' not in rig.events and 'abandon' in rig.events and ('drop-projection' not in rig.events)

def test_unbound_executor_cannot_run_context_protocol(rig):
    rig.executor._context_binding = None
    with pytest.raises(ScopeDeniedError):
        asyncio.run(rig.execute())
    assert 'claim' not in rig.events and 'spawn' not in rig.events

def test_failed_preparation_never_launches_worker(rig):

    def prepare(*a):
        raise ScopeDeniedError('unsafe mount')
    rig.executor._context_binding.prepare = prepare
    with pytest.raises(ScopeDeniedError):
        asyncio.run(rig.execute())
    assert 'claim' not in rig.events and 'spawn' not in rig.events

def test_cancel_during_launch_settles_child_before_disposing_snapshot(rig, monkeypatch):

    async def exercise():
        entered, release = (asyncio.Event(), asyncio.Event())

        class Supervisor:

            def __init__(self, *a):
                pass

            async def open(self, *a, **kw):
                entered.set()
                await release.wait()
                rig.events.append('spawn')
                return rig.process
        monkeypatch.setitem(sys.modules, 'potbot.tools.interactive_supervisor', SimpleNamespace(InteractiveProcessSupervisor=Supervisor))
        task = asyncio.create_task(rig.execute())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert rig.events.index('stop') < rig.events.index('drop-projection')
    assert 'finish' not in rig.events and 'abandon' in rig.events

def test_cancel_during_preparation_does_not_leak_projection_or_start_lease(rig):
    entered, release = (threading.Event(), threading.Event())
    original = rig.executor._context_binding.prepare

    def prepare(*a):
        entered.set()
        assert release.wait(3)
        return original(*a)
    rig.executor._context_binding.prepare = prepare

    async def exercise():
        task = asyncio.create_task(rig.execute())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert 'drop-projection' in rig.events and 'spawn' not in rig.events and ('claim' not in rig.events)

def test_existing_controller_calls_context_binding_not_an_unused_helper():
    import ast
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / 'src/potbot/controller/execution_cogs.py').read_text())
    constructor = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '__init__' and any((isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute) and (k.func.attr == 'bind_context') for k in ast.walk(n)))))
    assert constructor is not None

@pytest.fixture
def actual_cog_module(rig, monkeypatch):
    """Import the actual runner while explicitly substituting OS/source adapters.

    This exercises routing and legacy wire behavior, not a qualified process.
    The existing sandbox-worker suite remains the constructor/admission gate.
    """
    import hashlib
    import importlib.util
    import json
    from contextlib import asynccontextmanager
    contracts = sys.modules['potbot.cogs.contracts']
    contracts.CogRunContext = object
    monkeypatch.setitem(sys.modules, 'potbot.tools.agent_homes', SimpleNamespace(AgentHomeRegistry=type('Registry', (), {})))
    monkeypatch.setitem(sys.modules, 'potbot.tools.execution_contracts', SimpleNamespace(WorkBinding=object, WORK_KINDS=('tool',)))
    monkeypatch.setitem(sys.modules, 'potbot.tools.shared_workspace_isolation', SimpleNamespace(SharedWorkspaceBubblewrapBackend=type('Backend', (), {}), SharedWorkspaceRuntime=type('Runtime', (), {})))
    requests = []

    class Supervisor:

        def __init__(self, backend):
            self.backend = backend
            self.monitor_config = None

        def run(self, request, *, cancelled):
            assert not cancelled()
            requests.append(request)
            rig.events.append('legacy-run')
            return SimpleNamespace(processes_stopped=True, outcome='completed', exit_code=0, stdout_truncated=False, isolation_identity='c' * 64, stdout=json.dumps(dict(schema_version=1, type_ref='syn://type/out', value={'ok': True})).encode())
    sys.modules['potbot.tools.supervisor'].ProcessSupervisor = Supervisor
    sys.modules['potbot.tools.workspace_supervision'].admission_digest = lambda s: hashlib.sha256(s.encode()).hexdigest()

    class Mesh:

        @asynccontextmanager
        async def node(self, *args):
            yield SimpleNamespace(fault_event=threading.Event())
    monkeypatch.setitem(sys.modules, 'potbot.tools.syn_worker_nodes', SimpleNamespace(SynWorkerNodeMesh=Mesh))
    path = Path(__file__).resolve().parents[2] / 'src/potbot/tools/sandbox_cog_worker.py'
    spec = importlib.util.spec_from_file_location('_context_actual_cog_unit', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    executor = object.__new__(module.SandboxedCogExecutor)
    executor.__dict__.update(rig.executor.__dict__)
    executor.syn_mesh = Mesh()
    executor.homes = SimpleNamespace(lookup=lambda *args: SimpleNamespace(directory='alice', uid=420, gid=69, workspace_id=rig.actor.workspace_id, agent_id=rig.agent))
    binding = rig.op.binding
    binding.kind = 'tool'
    binding.input_type_ref = 'syn://type/in'
    binding.parent_event_id = None
    binding.links = ()
    operation = module.SandboxedCogOperation(binding, rig.op.argv, 'd' * 64)
    return SimpleNamespace(module=module, executor=executor, operation=operation, requests=requests)

def test_existing_runner_keeps_legacy_typed_json_bytes(rig, actual_cog_module):
    import json
    selected = actual_cog_module
    assert selected.operation.protocol == 'typed-json/v1'
    result = asyncio.run(selected.executor.execute(selected.operation, rig.source, rig.cog, rig.actor, rig.agent))
    assert result.value == {'ok': True}
    assert len(selected.requests) == 1
    wire = json.loads(selected.requests[0].stdin)
    assert wire == dict(schema_version=1, operation_ref='syn://thing/work', input={'type_ref': 'syn://type/in', 'value': {'hello': 1}}, invocation_id=str(rig.run))
    assert rig.events.index('prepare') < rig.events.index('legacy-run')
    assert rig.events.index('legacy-run') < rig.events.index('finish') < rig.events.index('drop-projection')
    assert 'rpc' not in rig.events

def test_existing_runner_routes_only_explicit_context_profile(rig, actual_cog_module):
    from dataclasses import replace
    selected = actual_cog_module
    operation = replace(selected.operation, protocol='context-rpc/v1')
    result = asyncio.run(selected.executor.execute(operation, rig.source, rig.cog, rig.actor, rig.agent))
    assert result.value == {'ok': True}
    assert 'rpc' in rig.events and 'legacy-run' not in rig.events

def test_existing_runner_rejects_unrecognised_protocol(rig, actual_cog_module):
    from dataclasses import replace
    from potbot.domain.errors import InvalidInputError
    with pytest.raises(InvalidInputError):
        replace(actual_cog_module.operation, protocol='guess-stdout')

def test_existing_runner_binding_cannot_cross_principal(rig, actual_cog_module):
    selected = actual_cog_module
    selected.executor._context_binding.principal = rig.actor
    other = WorkspaceContext(rig.actor.workspace_id, uuid4())
    with pytest.raises(ScopeDeniedError):
        selected.executor.bind_context(selected.executor._context_binding.database, other, (selected.operation,))
