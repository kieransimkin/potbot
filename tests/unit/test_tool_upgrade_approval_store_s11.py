"""Offline SQL-shape fixture for S11 CAS and durable decisions; PostgreSQL RLS separate."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
from potbot.domain.errors import RevisionConflictError, StateMismatchError
from potbot.persistence.tool_upgrade_approval_schema import MIGRATION_STATEMENTS


class Cursor:
    def __init__(self, cur): self.cur = cur
    def convert(self, row):
        if row is None: return None
        value = dict(row)
        for name in ("id", "workspace_id", "principal_id", "agent_id", "worker_token", "approval_principal_id"):
            if value.get(name): value[name] = UUID(value[name])
        if "updated_at" in value: value["updated_at"] = datetime.fromisoformat(value["updated_at"])
        return value
    def fetchone(self): return self.convert(self.cur.fetchone())
    def fetchall(self): return [self.convert(x) for x in self.cur.fetchall()]


class SQLitePG:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.create_function("clock_timestamp", 0, lambda: datetime.now(UTC).isoformat())
        self.db.create_function("regexp", 2, lambda pattern,value: bool(re.fullmatch(pattern, str(value))))
        self.db.execute("CREATE TABLE workspace_memberships(workspace_id TEXT,principal_id TEXT,PRIMARY KEY(workspace_id,principal_id))")
        self.db.execute("CREATE TABLE workspaces(id TEXT PRIMARY KEY)")
        for stmt in MIGRATION_STATEMENTS:
            sql = stmt.replace(" uuid", " TEXT").replace("uuid ", "TEXT ")
            sql = sql.replace(" timestamptz", " TEXT").replace(" ~ ", " REGEXP ")
            sql = sql.replace("DEFAULT clock_timestamp()", "DEFAULT CURRENT_TIMESTAMP")
            sql = re.sub(r"tests jsonb NOT NULL CHECK \(jsonb_typeof\(tests\)='array'\s+AND jsonb_array_length\(tests\) BETWEEN 2 AND 16\)", "tests TEXT NOT NULL", sql)
            self.db.execute(sql)
        self.statements = []
    @contextmanager
    def transaction(self, context):
        self.db.execute("BEGIN")
        try:
            yield self
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
    def execute(self, sql, args=()):
        self.statements.append(sql)
        if "pg_catalog.pg_advisory_xact_lock" in sql:
            return Cursor(self.db.execute("SELECT 1"))
        statement = sql.replace("%s::jsonb", "?").replace("%s", "?").replace(" FOR UPDATE SKIP LOCKED", "").replace(" FOR UPDATE", "")
        return Cursor(self.db.execute(statement, tuple(str(x) if isinstance(x, UUID) else x for x in args)))


@pytest.fixture
def context_store():
    db = SQLitePG()
    workspace, principal, agent = uuid4(), uuid4(), uuid4()
    context = SimpleNamespace(workspace_id=workspace, principal_id=principal)
    db.db.execute("INSERT INTO workspaces VALUES(?)", (str(workspace),))
    db.db.execute("INSERT INTO workspace_memberships VALUES(?,?)", (str(workspace), str(principal)))
    db.db.commit()
    return UiToolUpgradeStore(db), context, agent, db


def _tested():
    return {"id": str(uuid4()), "slug": "stemlab", "state": "ready",
            "old_commit": "a"*40, "new_commit": "b"*40,
            "suite_sha256": "c"*64, "test_evidence_sha256": "d"*64,
            "test_results": [{"name": "smoke-run", "kind": "smoke", "elapsed_ms": 13, "passed": True},
                             {"name": "unit-run", "kind": "unit", "elapsed_ms": 20, "passed": True}]}


def test_publish_approve_claim_indeterminate_and_audit_sequence(context_store):
    store, context, agent, db = context_store
    t = _tested()
    record = store.publish_test(context, agent_id=agent, tested=t)
    assert record["state"] == "awaiting_approval" and record["passedTests"] == 2
    assert store.publish_test(context, agent_id=agent, tested=t) == record
    assert len(store.list(context)["items"]) == 1
    with pytest.raises(RevisionConflictError):
        store.decide(context, UUID(t["id"]), expected_revision=0, evidence_sha256="0"*64, approve=True)
    result = store.decide(context, UUID(t["id"]), expected_revision=0,
                          evidence_sha256="d"*64, approve=True, setup_token="S"*64)
    assert result["revision"] == 1 and result["state"] == "approved"
    with pytest.raises(RevisionConflictError):
        store.decide(context, UUID(t["id"]), expected_revision=0, evidence_sha256="d"*64, approve=True, setup_token="S"*64)
    claim = store.claim(context, agent_id=agent, setup_token="S"*64)
    assert claim["agent_id"] == agent
    assert claim["worker_token"]
    assert store.claim(context, agent_id=agent, setup_token="S"*64) is None
    unknown = store.finish(context, operation=UUID(t["id"]), worker_token=claim["worker_token"], active=False)
    assert unknown["state"] == "indeterminate"
    assert store.claim(context, agent_id=agent, setup_token="S"*64) is None
    actions = [row[0] for row in db.db.execute("SELECT action FROM ui_tool_upgrade_audit ORDER BY recorded_at,id")]
    assert set(actions) == {"published", "approved", "claimed", "indeterminate"}


def test_successful_activation_is_not_fully_active_before_repin(context_store):
    store, context, agent, db = context_store
    t = _tested()
    store.publish_test(context, agent_id=agent, tested=t)
    store.decide(context, UUID(t["id"]), expected_revision=0,
                 evidence_sha256="d"*64, approve=True, setup_token="S"*64)
    claim = store.claim(context, agent_id=agent, setup_token="S"*64)
    status = store.finish(context, operation=UUID(t["id"]), worker_token=claim["worker_token"], active=True)
    assert status["state"] == "awaiting_repin"
    with pytest.raises(StateMismatchError):
        store.confirm_repin(context, operation=UUID(t["id"]), tested_sha256="0"*64)
    status = store.confirm_repin(context, operation=UUID(t["id"]), tested_sha256="d"*64)
    assert status["state"] == "active"


def test_agent_scoped_host_claim_does_not_consume_peer_approval(context_store):
    store, context, agent, _ = context_store
    t = _tested()
    store.publish_test(context, agent_id=agent, tested=t)
    store.decide(context, UUID(t["id"]), expected_revision=0,
                 evidence_sha256="d"*64, approve=True, setup_token="S"*64)
    assert store.claim(context, agent_id=uuid4(), setup_token="S"*64) is None
    assert store.claim(context, agent_id=agent, setup_token="S"*64)["agent_id"] == agent


def test_approval_must_match_native_vault_token(context_store):
    store, context, agent, _ = context_store
    t = _tested()
    store.publish_test(context, agent_id=agent, tested=t)
    store.decide(context, UUID(t["id"]), expected_revision=0,
                 evidence_sha256="d"*64, approve=True, setup_token="S"*64)
    from potbot.domain.errors import ScopeDeniedError
    with pytest.raises(ScopeDeniedError):
        store.claim(context, agent_id=agent, setup_token="F"*64)
    assert store.claim(context, agent_id=agent, setup_token="S"*64)["agent_id"] == agent
