"""SQLite-backed cross-process context documents with CAS and immutable revisions.

The operator must authenticate actors and authorize operations *before* calling this
store. An optional authorizer provides defense in depth; it is not authentication.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Callable

from .core import Conflict, InvalidContext, templates, validate

MAX_BYTES = 1_048_576


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Denied(PermissionError):
    """Actor was not authorized for this operation."""


class DurableContextStore:
    """Durable context revisions; one transaction per document update.

    SQLite serializes writers across processes with BEGIN IMMEDIATE. The
    database must be on a local filesystem supporting SQLite locks, not NFS.
    """

    def __init__(self, database: str | Path, authorizer: Callable | None = None):
        self.database = str(database)
        self.authorizer = authorizer
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS context_documents (
                    kind TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(kind, name, role)
                );
                CREATE TABLE IF NOT EXISTS context_revisions (
                    kind TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
                    revision INTEGER NOT NULL, digest TEXT NOT NULL,
                    content TEXT NOT NULL, actor TEXT NOT NULL,
                    reason TEXT NOT NULL, recorded_at TEXT NOT NULL
                      DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY(kind, name, role, revision)
                );
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=30000')
            db.execute('PRAGMA journal_mode=WAL')
            yield db
        finally:
            db.close()

    def _authorize(self, actor, action, kind, name, role):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise InvalidContext('actor identity required')
        if self.authorizer is not None and not self.authorizer(actor, action, kind, name, role):
            raise Denied(f'{actor} cannot {action} {kind}/{name}/{role}')

    def read(self, kind, name, role, *, actor):
        validate(kind, name, role)
        self._authorize(actor, 'read', kind, name, role)
        with self._connect() as db:
            row = db.execute('SELECT content, digest, revision FROM context_documents '
                             'WHERE kind=? AND name=? AND role=?', (kind, name, role)).fetchone()
        return row if row else ('', _hash(''), 0)

    def write(self, kind, name, role, content, expected_revision, *, actor, reason=''):
        validate(kind, name, role)
        self._authorize(actor, 'write', kind, name, role)
        if not isinstance(content, str) or '\x00' in content or len(content.encode('utf-8')) > MAX_BYTES:
            raise InvalidContext('invalid document content')
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise InvalidContext('expected_revision must be nonnegative integer')
        if not isinstance(reason, str) or len(reason) > 2048:
            raise InvalidContext('invalid reason')
        digest = _hash(content)
        with self._connect() as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT revision FROM context_documents '
                                 'WHERE kind=? AND name=? AND role=?', (kind, name, role)).fetchone()
                current = row[0] if row else 0
                if current != expected_revision:
                    raise Conflict(f'stale revision: expected {expected_revision}, current {current}')
                revision = current + 1
                db.execute('INSERT INTO context_documents(kind,name,role,content,digest,revision) '
                           'VALUES(?,?,?,?,?,?) ON CONFLICT(kind,name,role) DO UPDATE SET '
                           'content=excluded.content,digest=excluded.digest,revision=excluded.revision',
                           (kind, name, role, content, digest, revision))
                db.execute('INSERT INTO context_revisions '
                           '(kind,name,role,revision,digest,content,actor,reason) VALUES(?,?,?,?,?,?,?,?)',
                           (kind, name, role, revision, digest, content, actor, reason))
                db.commit()
                return digest, revision
            except BaseException:
                db.rollback()
                raise

    def history(self, kind, name, role, *, actor, limit=100):
        validate(kind, name, role)
        self._authorize(actor, 'read', kind, name, role)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise InvalidContext('limit out of range')
        with self._connect() as db:
            return db.execute('SELECT revision,digest,actor,reason,recorded_at '
                              'FROM context_revisions WHERE kind=? AND name=? AND role=? '
                              'ORDER BY revision DESC LIMIT ?', (kind, name, role, limit)).fetchall()

    def restore(self, kind, name, role, revision, expected_revision, *, actor, reason='restore'):
        validate(kind, name, role)
        self._authorize(actor, 'write', kind, name, role)
        with self._connect() as db:
            row = db.execute('SELECT content FROM context_revisions '
                             'WHERE kind=? AND name=? AND role=? AND revision=?',
                             (kind, name, role, revision)).fetchone()
        if row is None:
            raise InvalidContext('revision not found')
        return self.write(kind, name, role, row[0], expected_revision, actor=actor, reason=reason)

    def provision(self, kind, name, *, actor):
        """Create missing documents only; never replace existing user content."""
        for role, content in templates(kind, name).items():
            self._authorize(actor, 'write', kind, name, role)
            try:
                self.write(kind, name, role, content, 0, actor=actor, reason='provision')
            except Conflict:
                pass
