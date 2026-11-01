"""Live PostgreSQL/RLS tests. Only the repository's disposable app_env is used."""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import pytest
from potbot.context.bootstrap import grant_shared_context_writer, revoke_shared_context_writer
from potbot.context.rpc_contract import ContextRequest
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSessionStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.persistence.database import admin_connection, provision_principal
pytestmark = pytest.mark.postgres

def setup_session(env, *, request_limit=256):
    principal = env.context
    agent_id, run_id = (uuid4(), uuid4())
    service = WorkspaceContextService(env.db)
    service.provision_private(principal, 'agent', str(agent_id), idempotency_key=uuid4().hex)
    sessions = ContextSessionStore(env.db)
    grant = sessions.issue(principal, agent_id, run_id, request_limit=request_limit)
    return (service, sessions, grant)

def update(*, scope='agent', role='MODEL.md', revision=1, content='# New state\n', key=None):
    return ContextRequest(key or uuid4(), 'context.update', scope, role, content, revision, 'observed')

def read(role='MODEL.md'):
    return ContextRequest(uuid4(), 'context.read', 'agent', role)

def test_issue_is_bound_to_existing_private_agent_and_digest_only_secret(app_env):
    _, sessions, grant = setup_session(app_env)
    assert sessions.execute(grant, read())['revision'] == 1
    with app_env.db.transaction(app_env.context) as conn:
        row = conn.execute('SELECT * FROM workspace_context_sessions WHERE id=%s', (grant.session_id,)).fetchone()
        assert row['agent_id'] == grant.agent_id and row['run_id'] == grant.run_id
        assert grant.secret not in str(row)
        assert len(row['secret_sha256']) == 64 and row['request_count'] == 1
    with pytest.raises(ScopeDeniedError):
        sessions.issue(app_env.context, uuid4(), uuid4())
    for forged in (replace(grant, secret='x' * 43), replace(grant, generation=2), replace(grant, agent_id=uuid4()), replace(grant, run_id=uuid4())):
        with pytest.raises(ScopeDeniedError):
            sessions.execute(forged, read())

def test_cas_receipt_restart_and_idempotent_reply_share_one_commit(app_env):
    service, sessions, grant = setup_session(app_env)
    request = update()
    result = sessions.execute(grant, request)
    assert result['revision'] == 2
    newer = sessions.execute(grant, update(revision=2, content='# Third\n'))
    assert newer['revision'] == 3
    assert ContextSessionStore(app_env.db).execute(grant, request) == result
    with pytest.raises(RevisionConflictError):
        sessions.execute(grant, replace(request, content='# Other\n'))
    assert service.read(app_env.context, 'agent', str(grant.agent_id), 'MODEL.md')['revision'] == 3
    with app_env.db.transaction(app_env.context) as conn:
        rows = conn.execute('SELECT r.revision,s.agent_id,s.run_id FROM workspace_context_session_receipts r JOIN workspace_context_sessions s ON s.workspace_id=r.workspace_id AND s.id=r.session_id WHERE r.session_id=%s ORDER BY r.revision', (grant.session_id,)).fetchall()
        assert [r['revision'] for r in rows] == [2, 3]
        assert all((r['agent_id'] == grant.agent_id and r['run_id'] == grant.run_id for r in rows))
    with pytest.raises(ScopeDeniedError), app_env.db.transaction(app_env.context) as conn:
        conn.execute('UPDATE workspace_context_session_receipts SET revision=revision WHERE session_id=%s', (grant.session_id,))

def test_parallel_identical_requests_have_one_receipt_and_revision(app_env):
    service, sessions, grant = setup_session(app_env)
    barrier = Barrier(2)
    request = update()

    def call(_):
        barrier.wait(timeout=5)
        return sessions.execute(grant, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert results[0] == results[1]
    assert service.read(app_env.context, 'agent', str(grant.agent_id), 'MODEL.md')['revision'] == 2
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute('SELECT count(*) AS n FROM workspace_context_session_receipts WHERE session_id=%s', (grant.session_id,)).fetchone()['n'] == 1

def test_independent_sessions_cannot_overwrite_same_expected_revision(app_env):
    service, sessions, first = setup_session(app_env)
    second = sessions.issue(app_env.context, first.agent_id, uuid4())
    barrier = Barrier(2)

    def call(grant):
        barrier.wait(timeout=5)
        try:
            return sessions.execute(grant, update())
        except RevisionConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, (first, second)))
    assert sum((x is not None for x in results)) == 1
    assert service.read(app_env.context, 'agent', str(first.agent_id), 'MODEL.md')['revision'] == 2

def test_shared_grant_checked_even_before_receipt_replay(app_env):
    service, sessions, grant = setup_session(app_env)
    request = update(scope='sandbox')
    with pytest.raises(ScopeDeniedError):
        sessions.execute(grant, request)
    document = service.read(app_env.context, 'sandbox', str(app_env.context.workspace_id), 'MODEL.md')
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        grant_shared_context_writer(conn, app_env.context.workspace_id, UUID(document['object_id']), app_env.context.principal_id, ('MODEL.md',))
    assert sessions.execute(grant, request)['revision'] == 2
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        revoke_shared_context_writer(conn, app_env.context.workspace_id, UUID(document['object_id']), app_env.context.principal_id, ('MODEL.md',))
    with pytest.raises(ScopeDeniedError):
        sessions.execute(grant, request)

def test_revocation_fences_replays_and_cannot_be_renewed(app_env):
    _, sessions, grant = setup_session(app_env)
    request = update()
    sessions.execute(grant, request)
    sessions.revoke(grant)
    sessions.revoke(grant)
    with pytest.raises(ScopeDeniedError):
        sessions.execute(grant, request)
    with pytest.raises(ScopeDeniedError):
        sessions.renew(grant)
    with app_env.db.transaction(app_env.context) as conn:
        row = conn.execute('SELECT state,generation FROM workspace_context_sessions WHERE id=%s', (grant.session_id,)).fetchone()
        assert row == {'state': 'revoked', 'generation': 2}
    with pytest.raises(RevisionConflictError), app_env.db.transaction(app_env.context) as conn:
        conn.execute("UPDATE workspace_context_sessions SET state='active',revoked_at=NULL WHERE id=%s", (grant.session_id,))

def test_new_revocation_and_writer_have_a_serial_order(app_env):
    _, sessions, grant = setup_session(app_env)
    barrier = Barrier(2)

    def write():
        barrier.wait(timeout=5)
        try:
            return sessions.execute(grant, update())
        except ScopeDeniedError:
            return None

    def revoke():
        barrier.wait(timeout=5)
        sessions.revoke(grant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        revoker = pool.submit(revoke)
        writer.result(timeout=10)
        revoker.result(timeout=10)
    with pytest.raises(ScopeDeniedError):
        sessions.execute(grant, update())

def test_request_budget_still_allows_identical_receipt_recovery(app_env):
    _, sessions, grant = setup_session(app_env, request_limit=1)
    request = update()
    receipt = sessions.execute(grant, request)
    assert sessions.execute(grant, request) == receipt
    with pytest.raises(ScopeDeniedError):
        sessions.execute(grant, read())
    with pytest.raises(ScopeDeniedError):
        sessions.execute(grant, update(revision=2))

def test_same_workspace_peer_cannot_enumerate_or_forge_sessions(app_env):
    _, sessions, grant = setup_session(app_env)
    sessions.execute(grant, update())
    peer = provision_principal(app_env.admin_dsn, app_env.schema, urlsplit(app_env.other_db.dsn).username, app_env.context.workspace_id, principal_id=app_env.other_context.principal_id)
    with app_env.other_db.transaction(peer) as conn:
        assert conn.execute('SELECT id FROM workspace_context_sessions').fetchall() == []
        assert conn.execute('SELECT session_id FROM workspace_context_session_receipts').fetchall() == []
    with pytest.raises(ScopeDeniedError):
        ContextSessionStore(app_env.other_db).execute(replace(grant, principal=peer), read())

def test_session_identity_and_budget_are_immutable(app_env):
    _, _, grant = setup_session(app_env)
    for statement in ('UPDATE workspace_context_sessions SET run_id=gen_random_uuid() WHERE id=%s', 'UPDATE workspace_context_sessions SET request_limit=1024 WHERE id=%s'):
        with pytest.raises(RevisionConflictError), app_env.db.transaction(app_env.context) as conn:
            conn.execute(statement, (grant.session_id,))

def test_expiry_while_waiting_for_document_lock_rolls_back_every_effect(app_env):
    service, sessions, grant = setup_session(app_env)
    object_id = UUID(service.read(app_env.context, 'agent', str(grant.agent_id), 'MODEL.md')['object_id'])
    locked = Event()

    class ObservedStore(ContextSessionStore):

        def _lock(self, conn, capability, *, live=True):
            row = super()._lock(conn, capability, live=live)
            locked.set()
            return row
    observed = ObservedStore(app_env.db)
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        conn.execute("UPDATE workspace_context_sessions SET expires_at=clock_timestamp()+interval '2 seconds' WHERE id=%s", (grant.session_id,))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with admin_connection(app_env.admin_dsn, app_env.schema) as blocker:
            blocker.execute("SELECT revision FROM workspace_context_documents WHERE workspace_id=%s AND object_id=%s AND role='MODEL.md' FOR UPDATE", (app_env.context.workspace_id, object_id))
            future = pool.submit(observed.execute, grant, update())
            assert locked.wait(3), 'writer did not authenticate before the test deadline'
            time.sleep(2.1)
        with pytest.raises(ScopeDeniedError):
            future.result(timeout=10)
    assert service.read(app_env.context, 'agent', str(grant.agent_id), 'MODEL.md')['revision'] == 1
    with app_env.db.transaction(app_env.context) as conn:
        assert conn.execute('SELECT count(*) AS n FROM workspace_context_session_receipts WHERE session_id=%s', (grant.session_id,)).fetchone()['n'] == 0
        assert conn.execute("SELECT count(*) AS n FROM workspace_context_revisions WHERE object_id=%s AND role='MODEL.md'", (object_id,)).fetchone()['n'] == 1
    with pytest.raises(ScopeDeniedError):
        sessions.renew(grant)
    sessions.revoke(grant)
