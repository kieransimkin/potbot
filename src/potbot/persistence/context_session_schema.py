"""Migration 31: host-issued agent context leases and immutable RPC receipts.

Uses the existing restricted-login/workspace RLS migration harness. These are
context-operation leases, not a replacement for OS process-admission leases.
"""

WORKSPACE_TABLES = ("workspace_context_sessions", "workspace_context_session_receipts")
APPEND_ONLY_TABLES = ("workspace_context_session_receipts",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_context_sessions (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        agent_object_id uuid NOT NULL,
        agent_id uuid NOT NULL,
        run_id uuid NOT NULL,
        secret_sha256 text NOT NULL CHECK (secret_sha256 ~ '^[0-9a-f]{64}$'),
        generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
        state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        expires_at timestamptz NOT NULL,
        deadline_at timestamptz NOT NULL,
        revoked_at timestamptz,
        request_limit integer NOT NULL CHECK (request_limit BETWEEN 1 AND 1024),
        request_count integer NOT NULL DEFAULT 0,
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,agent_object_id)
            REFERENCES workspace_context_objects(workspace_id,id),
        CHECK (expires_at > created_at AND expires_at <= deadline_at),
        CHECK (deadline_at <= created_at + interval '1 hour'),
        CHECK (request_count BETWEEN 0 AND request_limit),
        CHECK ((state='revoked') = (revoked_at IS NOT NULL))
    )""",
    "CREATE INDEX context_sessions_active ON workspace_context_sessions "
    "(workspace_id,principal_id,expires_at) WHERE state='active'",
    """CREATE POLICY context_session_owner ON workspace_context_sessions
        AS RESTRICTIVE FOR ALL
        USING (principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid
            AND EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id=workspace_context_sessions.workspace_id
                  AND o.id=workspace_context_sessions.agent_object_id
                  AND o.kind='agent' AND o.name=workspace_context_sessions.agent_id::text
                  AND o.visibility='private'
                  AND o.owner_principal_id=workspace_context_sessions.principal_id))
    """,
    """CREATE FUNCTION check_context_session_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF (NEW.workspace_id,NEW.id,NEW.principal_id,NEW.agent_object_id,
                NEW.agent_id,NEW.run_id,NEW.secret_sha256,NEW.created_at,
                NEW.deadline_at,NEW.request_limit) IS DISTINCT FROM
               (OLD.workspace_id,OLD.id,OLD.principal_id,OLD.agent_object_id,
                OLD.agent_id,OLD.run_id,OLD.secret_sha256,OLD.created_at,
                OLD.deadline_at,OLD.request_limit)
               OR OLD.state='revoked'
               OR NEW.request_count < OLD.request_count
               OR (NEW.state='active' AND
                   (NEW.generation <> OLD.generation OR OLD.expires_at <= clock_timestamp()))
               OR (NEW.state='revoked' AND NEW.generation <> OLD.generation+1)
            THEN
                RAISE EXCEPTION 'invalid context session transition' USING ERRCODE='55000';
            END IF;
            RETURN NEW;
        END; $$""",
    """CREATE TRIGGER context_session_transition BEFORE UPDATE ON workspace_context_sessions
        FOR EACH ROW EXECUTE FUNCTION check_context_session_transition()""",
    """CREATE TABLE workspace_context_session_receipts (
        workspace_id uuid NOT NULL,
        session_id uuid NOT NULL,
        request_id uuid NOT NULL,
        request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
        object_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN ('AGENTS.md','MODEL.md','MEMORY.md')),
        revision bigint NOT NULL CHECK (revision > 0),
        result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,session_id,request_id),
        FOREIGN KEY(workspace_id,session_id)
            REFERENCES workspace_context_sessions(workspace_id,id),
        FOREIGN KEY(workspace_id,object_id,role,revision)
            REFERENCES workspace_context_revisions(workspace_id,object_id,role,revision)
    )""",
    """CREATE POLICY context_receipt_owner ON workspace_context_session_receipts
        AS RESTRICTIVE FOR ALL
        USING (EXISTS (SELECT 1 FROM workspace_context_sessions s
            WHERE s.workspace_id=workspace_context_session_receipts.workspace_id
              AND s.id=workspace_context_session_receipts.session_id))
        WITH CHECK (EXISTS (SELECT 1 FROM workspace_context_sessions s
            WHERE s.workspace_id=workspace_context_session_receipts.workspace_id
              AND s.id=workspace_context_session_receipts.session_id))
    """,
)
