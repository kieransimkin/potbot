"""Operator defaults use the restricted login and never guess guest authority.

Database calls are explicit unit doubles. PostgreSQL authorization is exercised
by the separate integration suite, not by these bootstrap-order tests.
"""
from __future__ import annotations
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from potbot.context import operator_bootstrap as bootstrap
from potbot.domain.errors import ScopeDeniedError

@dataclass(frozen=True)
class Runtime:
    rootfs: Path
    home: Path
    model_staging: Path
    shared_models: Path
    shared_tools: Path
    ai_config_dir: Path
    ai_history_dir: Path
    shared_skills: Path | None = None

@pytest.fixture
def bootstrap_rig(tmp_path, monkeypatch):
    monkeypatch.delenv('POTBOT_OPERATOR_REQUIRED', raising=False)
    monkeypatch.delenv('POTBOT_CONTEXT_SCHEMA', raising=False)
    areas = []
    for name in ('rootfs', 'home', 'staging', 'models', 'tools', 'config', 'past'):
        path = tmp_path / name
        path.mkdir(mode=448)
        areas.append(path)
    private = tmp_path / 'operator'
    private.mkdir(mode=448)
    runtime = Runtime(*areas)
    workspace, principal = (uuid4(), uuid4())
    paths = SimpleNamespace(workspace_id=workspace, controller_ledger=private / 'ledger', runtime=lambda: runtime)
    events = []
    rows = [{'id': principal}]
    config = SimpleNamespace(workspace_id=str(workspace), principal_id=str(principal), schema='service_schema')

    class Result:

        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def fetchall(self):
            return self.value

    class Connection:

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            query = str(query)
            events.append(('sql', query))
            if 'session_user AS role' in query:
                return Result({'role': 'restricted_role'})
            if 'SELECT p.id' in query:
                assert params == (workspace,)
                return Result(rows)
            return Result(None)

    class SQL(str):

        def format(self, *args):
            return SQL(str(self).format(*args))

    def connect(dsn, **options):
        assert dsn == 'restricted-runtime-dsn'
        assert options['connect_timeout'] == 5
        assert 'search_path=pg_catalog' in options['options']
        events.append(('connect', dsn))
        return Connection()
    fake_sql = SimpleNamespace(SQL=SQL, Identifier=lambda s: '"' + s + '"')
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=connect, sql=fake_sql))
    monkeypatch.setitem(sys.modules, 'psycopg.rows', SimpleNamespace(dict_row=object()))

    class Database:

        def __init__(self, dsn, schema):
            self.dsn, self.schema = (dsn, schema)

        @contextmanager
        def transaction(self, actor):
            assert actor.workspace_id == workspace and actor.principal_id == principal
            events.append(('transaction', actor))
            yield Connection()

    def identifier(value):
        assert re.fullmatch('[a-z][a-z0-9_]{0,62}', value)
        return value

    def restricted(conn, schema, role):
        assert role == 'restricted_role'
        events.append(('restricted', schema))
    import potbot.persistence.database as database_module
    monkeypatch.setattr(database_module, 'Database', Database)
    monkeypatch.setattr(database_module, '_identifier', identifier, raising=False)
    monkeypatch.setattr(database_module, '_restricted_login', restricted, raising=False)

    def resolve():
        events.append(('resolve', 'runtime'))
        return 'restricted-runtime-dsn'
    monkeypatch.setitem(sys.modules, 'potbot.credentials', SimpleNamespace(resolve_postgres_dsn=resolve))
    monkeypatch.setitem(sys.modules, 'potbot.service', SimpleNamespace(ServiceConfig=SimpleNamespace(load=lambda _: config), config_path=lambda: private / 'service.json'))

    class Service:

        def __init__(self, database):
            self.database = database

        def read_triple(self, actor, kind, name):
            assert kind == 'sandbox' and name == str(workspace)
            events.append(('triple', actor))
            return {}
    monkeypatch.setattr(bootstrap, 'WorkspaceContextService', Service)
    return SimpleNamespace(paths=paths, runtime=runtime, workspace=workspace, principal=principal, private=private, events=events, rows=rows, config=config)

def test_default_identity_is_derived_from_restricted_login(bootstrap_rig):
    rig = bootstrap_rig
    manager = bootstrap.default_context_manager(rig.paths)
    assert manager.principal.principal_id == rig.principal
    assert manager.service.database.schema == 'potbot'
    assert manager.parent == rig.private / ('context-' + rig.workspace.hex)
    assert manager.parent.stat().st_mode & 63 == 0
    names = [event[0] for event in rig.events]
    assert names.index('restricted') < names.index('triple') < names.index('transaction')
    assert any(('workspace_context_sources' in str(event) for event in rig.events))
    manager.close()

def test_forged_explicit_principal_does_not_allocate_storage(bootstrap_rig):
    rig = bootstrap_rig
    with pytest.raises(ScopeDeniedError):
        bootstrap.default_context_manager(rig.paths, principal_id=uuid4())
    assert list(rig.private.iterdir()) == []
    assert not any((event[0] == 'triple' for event in rig.events))

@pytest.mark.parametrize('count', [0, 2])
def test_missing_or_ambiguous_login_membership_fails_closed(bootstrap_rig, count):
    rig = bootstrap_rig
    rig.rows[:] = [{'id': rig.principal}] * count
    with pytest.raises(ScopeDeniedError):
        bootstrap.default_context_manager(rig.paths)
    assert list(rig.private.iterdir()) == []

def test_native_service_uses_existing_schema_and_principal(bootstrap_rig, monkeypatch):
    rig = bootstrap_rig
    monkeypatch.setenv('POTBOT_OPERATOR_REQUIRED', '1')
    monkeypatch.setenv('POTBOT_CONTEXT_SCHEMA', 'not_the_service_schema')
    manager = bootstrap.default_context_manager(rig.paths)
    assert manager.service.database.schema == rig.config.schema
    assert manager.principal.principal_id == rig.principal
    manager.close()

@pytest.mark.parametrize('mismatch', ['workspace', 'principal', 'schema'])
def test_native_service_scope_cannot_be_overridden(bootstrap_rig, monkeypatch, mismatch):
    rig = bootstrap_rig
    monkeypatch.setenv('POTBOT_OPERATOR_REQUIRED', '1')
    options = {}
    if mismatch == 'workspace':
        rig.config.workspace_id = str(uuid4())
    elif mismatch == 'principal':
        options['principal_id'] = uuid4()
    else:
        options['schema'] = 'other_schema'
    with pytest.raises(ScopeDeniedError):
        bootstrap.default_context_manager(rig.paths, **options)
    assert rig.events == []

def test_standalone_explicit_schema_is_retained(bootstrap_rig):
    manager = bootstrap.default_context_manager(bootstrap_rig.paths, schema='another_schema')
    assert manager.service.database.schema == 'another_schema'
    manager.close()

@pytest.mark.parametrize('placement', ['guest', 'symlink', 'public'])
def test_explicit_snapshot_root_never_repairs_unsafe_permissions(bootstrap_rig, placement):
    rig = bootstrap_rig
    target = rig.private / 'snapshots'
    if placement == 'guest':
        target = rig.runtime.home / 'snapshots'
        target.mkdir(mode=448)
    elif placement == 'symlink':
        actual = rig.private / 'real'
        actual.mkdir(mode=448)
        target.symlink_to(actual, target_is_directory=True)
    else:
        target.mkdir(mode=493)
        target.chmod(493)
    with pytest.raises(ScopeDeniedError):
        bootstrap.default_context_manager(rig.paths, parent=target)
    if placement == 'public':
        assert target.stat().st_mode & 511 == 493

def test_runtime_authentication_failure_precedes_any_filesystem_write(bootstrap_rig, monkeypatch):
    rig = bootstrap_rig

    def deny(*args):
        raise ScopeDeniedError('privileged login rejected')
    import potbot.persistence.database as database_module
    monkeypatch.setattr(database_module, '_restricted_login', deny)
    with pytest.raises(ScopeDeniedError):
        bootstrap.default_context_manager(rig.paths)
    assert list(rig.private.iterdir()) == []
