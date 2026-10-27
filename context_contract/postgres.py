"""PostgreSQL authoritative context storage (psycopg 3)."""
from dataclasses import dataclass
from .core import Conflict, InvalidContext, digest, templates, validate

MAX_BYTES = 1_048_576

@dataclass(frozen=True)
class Document:
    content: str
    revision: int
    sha256: str

class PostgresContextStore:
    def __init__(self, pool):
        """pool is a psycopg_pool.ConnectionPool, managed by the operator."""
        self.pool = pool

    def migrate(self):
        from pathlib import Path
        sql = (Path(__file__).parent / "migrations" / "001_context.sql").read_text()
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute(sql)

    @staticmethod
    def _check_content(content):
        if not isinstance(content, str) or "\\x00" in content or len(content.encode("utf-8")) > MAX_BYTES:
            raise InvalidContext("invalid document content")

    def read(self, kind, name, role):
        validate(kind, name, role)
        with self.pool.connection() as conn:
            row = conn.execute("""
                SELECT d.content, d.revision, d.sha256
                FROM context_documents d JOIN context_objects o ON o.id=d.object_id
                WHERE o.kind=%s AND o.name=%s AND d.role=%s
            """, (kind, name, role)).fetchone()
        return Document(*row) if row else Document("", 0, digest(""))

    def write(self, kind, name, role, content, expected_revision, actor, reason=""):
        validate(kind, name, role)
        self._check_content(content)
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise InvalidContext("invalid revision")
        if not isinstance(actor, str) or not actor.strip():
            raise InvalidContext("operator must supply authenticated actor")
        sha = digest(content)
        with self.pool.connection() as conn:
            with conn.transaction():
                # Serialize creation and updates of the same logical object.
                conn.execute("""
                    INSERT INTO context_objects(kind,name) VALUES (%s,%s)
                    ON CONFLICT(kind,name) DO NOTHING
                """, (kind, name))
                obj = conn.execute("""
                    SELECT id FROM context_objects WHERE kind=%s AND name=%s FOR UPDATE
                """, (kind, name)).fetchone()[0]
                current = conn.execute("""
                    SELECT revision FROM context_documents
                    WHERE object_id=%s AND role=%s FOR UPDATE
                """, (obj, role)).fetchone()
                revision = current[0] if current else 0
                if revision != expected_revision:
                    raise Conflict(f"stale revision: expected {expected_revision}, current {revision}")
                next_revision = revision + 1
                conn.execute("""
                    INSERT INTO context_documents(object_id,role,content,revision,sha256)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(object_id,role) DO UPDATE SET
                      content=EXCLUDED.content, revision=EXCLUDED.revision,
                      sha256=EXCLUDED.sha256, updated_at=now()
                """, (obj,role,content,next_revision,sha))
                conn.execute("""
                    INSERT INTO context_revisions(object_id,role,revision,content,sha256,actor,reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (obj,role,next_revision,content,sha,actor,reason))
                conn.execute("""
                    INSERT INTO context_events(object_id,role,revision,actor,event_type,payload)
                    VALUES (%s,%s,%s,%s,'document.updated',%s::jsonb)
                """, (obj,role,next_revision,actor,'{"sha256":"'+sha+'"}'))
        return Document(content,next_revision,sha)

    def provision(self, kind, name, actor):
        """Race-safe, never overwrites an existing document, even an empty one."""
        result = {}
        for role, initial in templates(kind,name).items():
            while True:
                current = self.read(kind,name,role)
                if current.revision:
                    result[role] = current
                    break
                try:
                    result[role] = self.write(kind,name,role,initial,0,actor,"initial provisioning")
                    break
                except Conflict:
                    continue
        return result

    def history(self, kind, name, role, limit=50):
        validate(kind,name,role)
        if not isinstance(limit,int) or not 1 <= limit <= 500:
            raise InvalidContext("invalid history limit")
        with self.pool.connection() as conn:
            return conn.execute("""
                SELECT r.revision,r.sha256,r.actor,r.reason,r.created_at
                FROM context_revisions r JOIN context_objects o ON o.id=r.object_id
                WHERE o.kind=%s AND o.name=%s AND r.role=%s
                ORDER BY r.revision DESC LIMIT %s
            """,(kind,name,role,limit)).fetchall()
