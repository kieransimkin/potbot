"""Migration 33 acceptance on the existing disposable PostgreSQL fixture."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import pytest
from potbot.context.bootstrap import grant_shared_context_writer
from potbot.context.catalogue import RuntimeObject, list_objects, register_runtime_objects
from potbot.context.model import ROLES
from potbot.context.rpc_contract import CatalogueRequest, ContextRequest, WorkRequest
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSessionStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.persistence.database import admin_connection, provision_principal
pytestmark = pytest.mark.postgres

def session(env):
    service = WorkspaceContextService(env.db)
    agent = uuid4()
    service.provision_private(env.context, 'agent', str(agent), idempotency_key=uuid4().hex)
    store = ContextSessionStore(env.db)
    return (service, store, store.issue(env.context, agent, uuid4()))

def source_row(env, family, key):
    with env.db.transaction(env.context) as conn:
        return conn.execute('SELECT s.*,o.kind,o.name FROM workspace_context_sources s JOIN workspace_context_objects o ON o.workspace_id=s.workspace_id AND o.id=s.object_id WHERE s.workspace_id=%s AND s.source_family=%s AND s.source_key=%s', (env.context.workspace_id, family, key)).fetchone()

def test_source_creation_and_triple_are_one_atomic_transaction(app_env):
    identity = uuid4()
    with app_env.db.transaction(app_env.context) as conn:
        conn.execute('INSERT INTO repositories(workspace_id,id,name,source_uri) VALUES(%s,%s,%s,%s)', (app_env.context.workspace_id, identity, 'lifecycle', 'file:///test-only'))
        row = conn.execute("SELECT object_id FROM workspace_context_sources WHERE workspace_id=%s AND source_family='repository' AND source_key=%s", (app_env.context.workspace_id, str(identity))).fetchone()
        assert row is not None
        assert conn.execute('SELECT count(*) AS n FROM workspace_context_documents WHERE workspace_id=%s AND object_id=%s', (app_env.context.workspace_id, row['object_id'])).fetchone()['n'] == 3
    assert source_row(app_env, 'repository', str(identity))['state'] == 'active'
    aborted = uuid4()
    with pytest.raises(RuntimeError), app_env.db.transaction(app_env.context) as conn:
        conn.execute('INSERT INTO repositories(workspace_id,id,name,source_uri) VALUES(%s,%s,%s,%s)', (app_env.context.workspace_id, aborted, 'rollback', 'file:///test-only'))
        raise RuntimeError('force rollback')
    assert source_row(app_env, 'repository', str(aborted)) is None

def test_registered_models_operator_gateway_have_context_without_instruction_payloads(app_env):
    with app_env.db.transaction(app_env.context) as conn:
        models = conn.execute('SELECT id FROM model_catalog WHERE workspace_id=%s', (app_env.context.workspace_id,)).fetchall()
    for model in models:
        assert source_row(app_env, 'model', str(model['id'])) is not None
    for family, key in [('operator', str(app_env.context.workspace_id)), ('gateway', 'tool-gateway')]:
        row = source_row(app_env, family, key)
        assert row is not None
        docs = WorkspaceContextService(app_env.db).read_triple(app_env.context, row['kind'], row['name'])
        assert set(docs) == set(ROLES)
        assert all((doc['revision'] == 1 for doc in docs.values()))

def test_native_kind_source_helper_is_idempotent_and_owner_only(app_env):
    ns = uuid4()
    refs = [f'{ns}:syn://{kind}/context-test' for kind in ('type', 'thing', 'event', 'ember', 'cog', 'credential', 'enigma', 'pondle', 'snug', 'om')]
    ids = []
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        for ref in refs:
            ids.append(conn.execute("SELECT observe_context_source(%s,'syn',%s,'primitive',NULL) AS id", (app_env.context.workspace_id, ref)).fetchone()['id'])
        again = conn.execute("SELECT observe_context_source(%s,'syn',%s,'primitive',NULL) AS id", (app_env.context.workspace_id, refs[0])).fetchone()['id']
    assert again == ids[0] and len(set(ids)) == 10
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute('SELECT count(*) AS n FROM workspace_context_revisions WHERE workspace_id=%s AND object_id=ANY(%s)', (app_env.context.workspace_id, ids)).fetchone()['n'] == 30
    with pytest.raises(ScopeDeniedError), app_env.db.transaction(app_env.context) as conn:
        conn.execute("SELECT observe_context_source(%s,'syn','spoof','primitive',NULL)", (app_env.context.workspace_id,))

def test_registration_version_upgrade_retirement_and_reactivation_preserve_history(app_env):
    service, store, grant = session(app_env)
    ref = RuntimeObject('tool', 'syn://thing/test-' + uuid4().hex, 'a' * 64)
    with app_env.db.transaction(app_env.context) as conn:
        identity = register_runtime_objects(conn, app_env.context, [ref])[0]
    req = ContextRequest(uuid4(), 'context.update', 'object', 'MODEL.md', '# learned', 1, object_id=identity)
    assert store.execute(grant, req)['revision'] == 2
    for value in [replace(ref, fingerprint='b' * 64), replace(ref, fingerprint='b' * 64, state='retired')]:
        with app_env.db.transaction(app_env.context) as conn:
            assert register_runtime_objects(conn, app_env.context, [value]) == (identity,)
    with pytest.raises(ScopeDeniedError):
        store.execute(grant, req)
    assert service.read(app_env.context, 'tool', ref.name(app_env.context), 'MODEL.md')['content'] == '# learned'
    with app_env.db.transaction(app_env.context) as conn:
        register_runtime_objects(conn, app_env.context, [ref])
        register_runtime_objects(conn, app_env.context, [ref])
        assert conn.execute('SELECT count(*) AS n FROM workspace_context_runtime_sources WHERE object_id=%s', (identity,)).fetchone()['n'] == 4
    assert store.execute(grant, req)['revision'] == 2

def test_generic_object_path_cannot_read_another_agent(app_env):
    service, store, grant = session(app_env)
    foreign = service.provision_private(app_env.context, 'agent', str(uuid4()), idempotency_key=uuid4().hex)
    with pytest.raises(ScopeDeniedError):
        store.execute(grant, ContextRequest(uuid4(), 'context.read', 'object', 'MODEL.md', object_id=UUID(foreign['object_id'])))

def test_source_writer_grant_is_required_and_retirement_blocks_replay(app_env):
    service, store, grant = session(app_env)
    key = 'native-test-' + uuid4().hex
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        identity = conn.execute("SELECT observe_context_source(%s,'tool',%s,'tool',NULL) AS id", (app_env.context.workspace_id, key)).fetchone()['id']
    request = ContextRequest(uuid4(), 'context.update', 'object', 'AGENTS.md', '# scoped guidance', 1, object_id=identity)
    with pytest.raises(ScopeDeniedError):
        store.execute(grant, request)
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        grant_shared_context_writer(conn, app_env.context.workspace_id, identity, app_env.context.principal_id, ('AGENTS.md',))
    assert store.execute(grant, request)['revision'] == 2
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        conn.execute("SELECT observe_context_source(%s,'tool',%s,'tool',NULL,'retired')", (app_env.context.workspace_id, key))
    with pytest.raises(ScopeDeniedError):
        store.execute(grant, request)

def test_runtime_catalogue_cannot_be_read_by_another_principal(app_env):
    _, store, grant = session(app_env)
    record = RuntimeObject('workflow', 'syn://thing/private-' + uuid4().hex, 'a' * 64)
    with app_env.db.transaction(app_env.context) as conn:
        identity = register_runtime_objects(conn, app_env.context, [record])[0]
    peer = provision_principal(app_env.admin_dsn, app_env.schema, urlsplit(app_env.other_db.dsn).username, app_env.context.workspace_id, principal_id=app_env.other_context.principal_id)
    with app_env.other_db.transaction(peer) as conn:
        assert conn.execute('SELECT object_id FROM workspace_context_runtime_sources WHERE object_id=%s', (identity,)).fetchone() is None
        assert all((item['id'] != str(identity) for item in list_objects(conn, peer)['objects']))

def test_final_work_result_is_durable_idempotent_and_fences_later_mutations(app_env):
    service, store, grant = session(app_env)
    request = WorkRequest(uuid4(), 'work.result', dict(schema_version=1, type_ref='syn://type/out', value={'answer': 2}))
    first = store.complete_work(grant, request)
    assert ContextSessionStore(app_env.db).complete_work(grant, request) == first
    with pytest.raises(RevisionConflictError):
        store.complete_work(grant, replace(request, request_id=uuid4()))
    for denied in [ContextRequest(uuid4(), 'context.update', 'agent', 'MODEL.md', '# too late', 1), CatalogueRequest(uuid4())]:
        with pytest.raises(ScopeDeniedError):
            store.execute(grant, denied)
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute('SELECT request_count FROM workspace_context_sessions WHERE id=%s', (grant.session_id,)).fetchone()['request_count'] == 1
        assert conn.execute('SELECT count(*) AS n FROM workspace_context_work_results WHERE session_id=%s', (grant.session_id,)).fetchone()['n'] == 1
    store.renew(grant)
    store.revoke(grant)
    with pytest.raises(ScopeDeniedError):
        store.complete_work(grant, request)

def test_final_result_uuid_cannot_replace_context_receipt(app_env):
    _, store, grant = session(app_env)
    key = uuid4()
    store.execute(grant, ContextRequest(key, 'context.update', 'agent', 'MODEL.md', '# saved', 1))
    with pytest.raises(RevisionConflictError):
        store.complete_work(grant, WorkRequest(key, 'work.result', {'value': 1}))
    assert store.execute(grant, ContextRequest(uuid4(), 'context.read', 'agent', 'MODEL.md'))['revision'] == 2

def test_concurrent_final_results_have_one_winner(app_env):
    _, store, grant = session(app_env)
    barrier = Barrier(2)

    def finish(n):
        barrier.wait(timeout=5)
        try:
            return store.complete_work(grant, WorkRequest(uuid4(), 'work.result', {'value': n}))
        except RevisionConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, [1, 2]))
    assert sum((result is not None for result in results)) == 1

def test_final_result_and_fence_rollback_when_transaction_aborts(app_env):
    _, store, grant = session(app_env)
    from potbot.context.sessions import _BoundTransaction
    with pytest.raises(RuntimeError), app_env.db.transaction(app_env.context) as conn:
        bound = ContextSessionStore(_BoundTransaction(app_env.context, conn))
        bound.complete_work(grant, WorkRequest(uuid4(), 'work.result', {'value': 1}))
        raise RuntimeError('rollback')
    assert store.execute(grant, ContextRequest(uuid4(), 'context.read', 'agent', 'MODEL.md'))['revision'] == 1
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute('SELECT 1 FROM workspace_context_work_results WHERE session_id=%s', (grant.session_id,)).fetchone() is None
