"""Integration contracts and real immutable views; SQL/worker doubles are explicit."""
from __future__ import annotations
import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from potbot.context.catalogue import OBJECT_KINDS, RuntimeObject, list_objects, object_target
from potbot.context.model import ROLES, document_templates, sha256
from potbot.context.object_projection import ObjectContextProjector
from potbot.context.rpc_contract import CatalogueRequest, ContextRequest, SCHEMA, WorkRequest, parse_request
from potbot.context.runtime_integration import ContextExecutionBinding, context_parent, operation_record
from potbot.context.work_exchange import ContextWorkExchange, bounded_work
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError, StateMismatchError
from potbot.tools.temporary import TemporaryDirectory

def wire(method, **kwargs):
    return json.dumps(dict(schema=SCHEMA, request_id=str(uuid4()), method=method, **kwargs)).encode()

@pytest.mark.parametrize('field', ['agent_id', 'principal', 'workspace_id', 'path', 'sql', 'session_id', 'generation', 'token'])
def test_new_methods_cannot_add_authority_fields(field):
    for method, args in [('objects.list', dict(limit=25, after=None)), ('work.input', {}), ('work.result', dict(result={'value': 1}))]:
        with pytest.raises(InvalidInputError):
            parse_request(wire(method, **args, **{field: 'forged'}))

@pytest.mark.parametrize('limit', [True, 0, 51, -1, 1.5, '25'])
def test_object_discovery_is_bounded(limit):
    with pytest.raises(InvalidInputError):
        parse_request(wire('objects.list', limit=limit, after=None))

def test_object_selector_requires_exact_scope_and_preserves_old_fingerprint():
    old = ContextRequest(uuid4(), 'context.read', 'agent', 'MODEL.md')
    expected = sha256(json.dumps(['context.read', 'agent', 'MODEL.md', None, None, ''], separators=(',', ':')))
    assert old.fingerprint == expected
    identity = uuid4()
    parsed = parse_request(wire('context.read', scope='object', role='MODEL.md', object_id=str(identity)))
    assert parsed.object_id == identity
    for args in [dict(scope='agent', object_id=str(identity)), dict(scope='object'), dict(scope='object', object_id=str(identity), skill_id=str(uuid4()))]:
        with pytest.raises(InvalidInputError):
            parse_request(wire('context.read', role='MODEL.md', **args))

@pytest.mark.parametrize('kind', ['agent', 'worker', 'sandbox', 'skill', '../tool'])
def test_generic_runtime_catalogue_cannot_create_identity_scopes(kind):
    with pytest.raises(InvalidInputError):
        RuntimeObject(kind, 'syn://thing/test', 'a' * 64)
    assert not {'agent', 'worker', 'sandbox', 'skill'} & set(OBJECT_KINDS)

def test_runtime_object_identity_survives_definition_changes_and_is_principal_scoped():
    principal = WorkspaceContext(uuid4(), uuid4())
    first = RuntimeObject('tool', 'syn://thing/example', 'a' * 64)
    changed = replace(first, fingerprint='b' * 64)
    assert changed.name(principal) == first.name(principal)
    assert first.name(WorkspaceContext(principal.workspace_id, uuid4())) != first.name(principal)
    assert len(first.name(principal)) <= 128

@pytest.mark.parametrize('value', [[], {'x': float('nan')}, {'x': 'x' * 17000}, {'x': '\ud800'}])
def test_work_values_must_be_bounded_finite_json_objects(value):
    with pytest.raises(InvalidInputError):
        bounded_work(value)

def envelope():
    return dict(schema_version=1, operation_ref='syn://thing/echo', invocation_id=str(uuid4()), input={'type_ref': 'syn://type/in', 'value': {'hello': 1}})

class WorkStore:

    def __init__(self):
        self.reads = 0
        self.result = None

    def admit_work_read(self, grant):
        self.reads += 1

    def complete_work(self, grant, request):
        if self.result and self.result != (request.request_id, request.fingerprint):
            raise RevisionConflictError('changed result')
        self.result = (request.request_id, request.fingerprint)
        return {'revision': 1, 'sha256': 'a' * 64}

def test_work_input_and_result_are_detached_and_retry_is_identical():
    original = envelope()
    exchange = ContextWorkExchange(original, 'syn://type/out')
    original['input']['value']['hello'] = 2
    store = WorkStore()
    received = exchange.execute(store, None, WorkRequest(uuid4(), 'work.input'))
    assert received['work']['input']['value'] == {'hello': 1}
    result = dict(schema_version=1, type_ref='syn://type/out', value={'ok': True})
    request = WorkRequest(uuid4(), 'work.result', result)
    receipt = exchange.execute(store, None, request)
    result['value']['ok'] = False
    assert exchange.execute(store, None, request) == receipt
    returned = exchange.result
    returned['value']['ok'] = False
    assert exchange.result['value']['ok'] is True
    with pytest.raises(StateMismatchError):
        exchange.execute(store, None, WorkRequest(uuid4(), 'work.input'))
    with pytest.raises(RevisionConflictError):
        exchange.execute(store, None, replace(request, request_id=uuid4()))

@pytest.mark.parametrize('result', [dict(schema_version=True, type_ref='syn://type/out', value=1), dict(schema_version=1, type_ref='syn://type/foreign', value=1), dict(schema_version=1, type_ref='syn://type/out', value=1, command='ignored')])
def test_work_result_cannot_change_port_or_envelope(result):
    exchange = ContextWorkExchange(envelope(), 'syn://type/out')
    store = WorkStore()
    with pytest.raises(ScopeDeniedError):
        exchange.execute(store, None, WorkRequest(uuid4(), 'work.result', result))
    assert store.result is None and exchange.result is None

class Rows:

    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

class QueryQueue:

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        assert self.responses, sql
        return Rows(self.responses.pop(0))

def test_catalogue_keyset_pagination_never_uses_offset_or_invents_authority():
    actor = WorkspaceContext(uuid4(), uuid4())
    ids = sorted([uuid4(), uuid4()], key=lambda x: x.int)
    conn = QueryQueue([dict(id=x, kind='tool') for x in ids])
    result = list_objects(conn, actor, limit=1)
    assert result['next'] == str(ids[0]) and len(result['objects']) == 1
    assert conn.calls[0][1][-1] == 2 and 'OFFSET' not in conn.calls[0][0]

def test_retired_object_and_missing_shared_grant_block_before_document_access():
    actor, identity = (WorkspaceContext(uuid4(), uuid4()), uuid4())
    row = dict(id=identity, kind='tool', name='test', visibility='shared', family='tool')
    for responses in [([],), ([row], [{'state': 'retired'}]), ([row], [{'state': 'active'}], [])]:
        conn = QueryQueue(*responses)
        with pytest.raises(ScopeDeniedError):
            object_target(conn, actor, identity, 'MODEL.md', write=True)
    conn = QueryQueue([row], [{'state': 'active'}], [{'role': 'MODEL.md'}])
    assert object_target(conn, actor, identity, 'MODEL.md', write=True) == ('tool', 'test')
    assert 'FOR SHARE' in conn.calls[-1][0]

def test_runtime_mutation_uses_exclusive_registration_lock_not_share_upgrade():
    actor, identity = (WorkspaceContext(uuid4(), uuid4()), uuid4())
    conn = QueryQueue([dict(id=identity, kind='tool', name='test', family='runtime', visibility='private')], [{'revision': 1}], [{'state': 'active'}])
    assert object_target(conn, actor, identity, 'MEMORY.md', write=True) == ('tool', 'test')
    assert 'FOR UPDATE' in conn.calls[1][0]

def docs(kind='tool', name='source.test', revision=1):
    return {role: dict(content=text, revision=revision, sha256=sha256(text)) for role, text in document_templates(kind, name).items()}

def test_object_projection_immutable_versioning_and_retained_old_views(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as folder:
        root = Path(folder)
        projector = ObjectContextProjector(root)
        item = dict(id=str(uuid4()), kind='tool', name='source.test', visibility='shared')
        firstdocs = docs()
        first = projector._object_snapshot(item, firstdocs)
        again = projector._object_snapshot(item, firstdocs)
        assert first == again
        path = root / first['path']
        old = (path / 'MODEL.md').read_bytes()
        newdocs = docs()
        newdocs['MODEL.md'] = {'revision': 2, 'content': '# Updated\n', 'sha256': sha256('# Updated\n')}
        second = projector._object_snapshot(item, newdocs)
        assert second['path'] != first['path']
        assert (path / 'MODEL.md').read_bytes() == old
        assert path.stat().st_mode & 146 == 0
        assert all(((path / role).stat().st_mode & 146 == 0 for role in ROLES))
        assert not (root / '_agents').exists()

def test_private_object_never_enters_shared_mount(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as folder:
        projector = ObjectContextProjector(Path(folder))
        with pytest.raises(InvalidInputError):
            projector._object_snapshot(dict(id=str(uuid4()), kind='tool', name='private', visibility='private'), docs())
        assert list(projector.library.iterdir()) == []

def test_shared_projection_rejects_symlink_and_corruption(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as folder:
        root = Path(folder)
        projector = ObjectContextProjector(root)
        item = dict(id=str(uuid4()), kind='tool', name='source.test', visibility='shared')
        first = projector._object_snapshot(item, docs())
        path = root / first['path']
        path.chmod(493)
        with pytest.raises(ScopeDeniedError):
            projector._object_snapshot(item, docs())
        path.chmod(365)
        target = path / 'MODEL.md'
        target.chmod(420)
        target.write_text('corrupt')
        target.chmod(292)
        with pytest.raises((ScopeDeniedError, StateMismatchError)):
            projector._object_snapshot(item, docs())

def test_context_parent_is_private_and_rejects_mount_overlap(tmp_path):
    host = tmp_path / 'host'
    host.mkdir(mode=448)
    guest = tmp_path / 'guest'
    guest.mkdir()
    identity = uuid4()
    runtime = SimpleNamespace(rootfs=guest, home=guest, model_staging=None, shared_models=None, shared_tools=None, shared_skills=None, ai_config_dir=None, ai_history_dir=None)
    selected = context_parent(SimpleNamespace(path=host / 'ledger'), runtime, identity)
    assert selected.stat().st_mode & 63 == 0
    runtime.shared_skills = host
    with pytest.raises(ScopeDeniedError):
        context_parent(SimpleNamespace(path=host / 'ledger'), runtime, identity)

def test_operator_cancellation_is_part_of_the_normal_execution_binding():
    import threading
    actor = WorkspaceContext(uuid4(), uuid4())
    binding = ContextExecutionBinding(object(), actor)
    monitor = SimpleNamespace(require_active=lambda: None)
    executor = SimpleNamespace(supervisor=SimpleNamespace(_closing=threading.Event(), backend=SimpleNamespace(ai_monitor=monitor, operator_attachment=None)))
    binding.check(executor, actor)
    executor.supervisor._closing.set()
    with pytest.raises(ScopeDeniedError):
        binding.check(executor, actor)
    with pytest.raises(ScopeDeniedError):
        binding.check(executor, WorkspaceContext(actor.workspace_id, uuid4()))

def test_declared_protocol_changes_installation_fingerprint():
    binding = SimpleNamespace(kind='workflow', operation_ref='syn://thing/work', to_dict=lambda: {'kind': 'workflow'})
    op = SimpleNamespace(binding=binding, argv=('/usr/bin/work',), executable_sha256='a' * 64, protocol='typed-json/v1')
    old = operation_record(op)
    op.protocol = 'context-rpc/v1'
    assert operation_record(op).fingerprint != old.fingerprint
    assert operation_record(op).reference == old.reference and old.kind == 'workflow'

def test_client_new_methods_send_no_identity_and_read_the_declared_shapes():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('stage10d_guest', root / 'sandbox/clients/context_client.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = object.__new__(module.ContextClient)
    calls = []

    def exchange(request):
        calls.append((request['method'], request))
        return {'work': envelope(), 'objects': []}
    client._exchange = exchange
    assert client.list_objects()['objects'] == []
    assert client.work_input()['operation_ref'] == 'syn://thing/echo'
    client.finish_work('syn://type/out', 1, request_id=str(uuid4()))
    client.read('object', 'MODEL.md', object_id=str(uuid4()))
    assert {name for name, _ in calls} == {'objects.list', 'work.input', 'work.result', 'context.read'}
    assert not any(({'agent_id', 'principal', 'workspace_id', 'session_id'} & set(args) for _, args in calls))

def test_real_guest_subprocess_exchanges_typed_work_and_object_discovery():
    import sys
    from potbot.context.rpc_transport import serve_context_streams
    root = Path(__file__).resolve().parents[2]
    program = f"\nimport sys\nfrom uuid import uuid4\nsys.path.insert(0,{str(root / 'sandbox/clients')!r})\nfrom context_client import ContextClient\nclient=ContextClient()\nwork=client.work_input()\nassert work['input']['value']=={{'hello':1}}\nassert client.list_objects()['objects']==[]\nclient.finish_work('syn://type/out',{{'answer':2}},request_id=str(uuid4()))\n"
    exchange = ContextWorkExchange(envelope(), 'syn://type/out')
    store = WorkStore()

    def handler(request):
        if isinstance(request, CatalogueRequest):
            return {'objects': [], 'next': None}
        return exchange.execute(store, None, request)

    async def exercise():
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', program, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        result = await asyncio.wait_for(serve_context_streams(process.stdout, process.stdin, handler), 5)
        stderr = await process.stderr.read()
        assert await process.wait() == 0, stderr
        assert result.requests == 3 and result.errors == 0
    asyncio.run(exercise())
    assert exchange.result['value'] == {'answer': 2} and store.reads == 1

def test_read_only_installed_work_cannot_gain_context_writes():
    from potbot.context.rpc_contract import SkillRequest
    exchange = ContextWorkExchange(envelope(), 'syn://type/out', allow_mutations=False)
    exchange.admit(ContextRequest(uuid4(), 'context.read', 'agent', 'MODEL.md'))
    exchange.admit(CatalogueRequest(uuid4()))
    with pytest.raises(ScopeDeniedError):
        exchange.admit(ContextRequest(uuid4(), 'context.update', 'agent', 'MODEL.md', '# forbidden', 1))
    with pytest.raises(ScopeDeniedError):
        exchange.admit(SkillRequest(uuid4(), 'skills.create', name='new-skill', content='text'))
    exchange.admit(WorkRequest(uuid4(), 'work.result', {'value': 1}))
