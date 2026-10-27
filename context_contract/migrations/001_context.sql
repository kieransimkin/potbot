-- Run under an operator-controlled PostgreSQL role. No sandbox credentials.
CREATE TABLE IF NOT EXISTS context_objects (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL,
 name text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(kind,name)
);
CREATE TABLE IF NOT EXISTS context_documents (
 object_id bigint NOT NULL REFERENCES context_objects(id),
 role text NOT NULL CHECK (role IN ('AGENTS.md','MODEL.md','MEMORY.md')),
 content text NOT NULL,
 revision bigint NOT NULL CHECK (revision > 0),
 sha256 char(64) NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(object_id,role)
);
CREATE TABLE IF NOT EXISTS context_revisions (
 object_id bigint NOT NULL REFERENCES context_objects(id),
 role text NOT NULL CHECK (role IN ('AGENTS.md','MODEL.md','MEMORY.md')),
 revision bigint NOT NULL CHECK (revision > 0),
 content text NOT NULL,
 sha256 char(64) NOT NULL,
 actor text NOT NULL,
 reason text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(object_id,role,revision)
);
CREATE TABLE IF NOT EXISTS context_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 object_id bigint NOT NULL REFERENCES context_objects(id),
 role text NOT NULL,
 revision bigint NOT NULL,
 actor text NOT NULL,
 event_type text NOT NULL,
 payload jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 delivered_at timestamptz,
 FOREIGN KEY(object_id,role,revision)
  REFERENCES context_revisions(object_id,role,revision)
);
CREATE INDEX IF NOT EXISTS context_events_pending_idx
 ON context_events(id) WHERE delivered_at IS NULL;
