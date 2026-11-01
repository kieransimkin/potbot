"""Migration 32: agent-owned skill drafts, shared immutable releases and live context.

Old migrations are not rewritten. The only additional shared-context creation
right is the exact object predeclared by an owned skill. This grants no right
to create sandbox/operator objects or to write another author's documents.
"""

WORKSPACE_TABLES = (
    "workspace_context_skills", "workspace_context_skill_versions",
    "workspace_context_skill_releases", "workspace_context_skill_receipts",
)
APPEND_ONLY_TABLES = (
    "workspace_context_skill_versions", "workspace_context_skill_releases",
    "workspace_context_skill_receipts",
)

_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"
_OWNER = f"owner_principal_id={_PRINCIPAL}"
_AUTHOR = f"""{_OWNER} AND EXISTS (SELECT 1 FROM workspace_context_objects a
    WHERE a.workspace_id=workspace_context_skills.workspace_id
      AND a.id=workspace_context_skills.agent_object_id
      AND a.kind='agent' AND a.name=workspace_context_skills.owner_agent_id::text
      AND a.visibility='private' AND a.owner_principal_id={_PRINCIPAL})"""

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_context_skills (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        owner_principal_id uuid NOT NULL,
        owner_agent_id uuid NOT NULL,
        agent_object_id uuid NOT NULL,
        slug text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,62}$'),
        context_object_id uuid NOT NULL,
        context_name text NOT NULL,
        draft_revision bigint NOT NULL DEFAULT 1 CHECK (draft_revision > 0),
        published boolean NOT NULL DEFAULT false,
        latest_sha256 text CHECK (latest_sha256 ~ '^[0-9a-f]{64}$'),
        state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,owner_agent_id,slug),
        UNIQUE(workspace_id,context_object_id),
        UNIQUE(workspace_id,context_name),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,agent_object_id)
            REFERENCES workspace_context_objects(workspace_id,id),
        FOREIGN KEY(workspace_id,context_object_id)
            REFERENCES workspace_context_objects(workspace_id,id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK (context_name='agent-' || replace(owner_agent_id::text,'-','') || '.' || slug),
        CHECK (published = (latest_sha256 IS NOT NULL))
    )""",
    """CREATE TABLE workspace_context_skill_versions (
        workspace_id uuid NOT NULL,
        skill_id uuid NOT NULL,
        revision bigint NOT NULL CHECK (revision > 0),
        content text NOT NULL CHECK (octet_length(content)<=1048576),
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        session_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,skill_id,revision),
        UNIQUE(workspace_id,skill_id,revision,sha256),
        FOREIGN KEY(workspace_id,skill_id) REFERENCES workspace_context_skills(workspace_id,id),
        FOREIGN KEY(workspace_id,session_id) REFERENCES workspace_context_sessions(workspace_id,id)
    )""",
    """ALTER TABLE workspace_context_skills ADD CONSTRAINT skill_draft_head
        FOREIGN KEY(workspace_id,id,draft_revision)
        REFERENCES workspace_context_skill_versions(workspace_id,skill_id,revision)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE workspace_context_skill_releases (
        workspace_id uuid NOT NULL,
        skill_id uuid NOT NULL,
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        source_revision bigint NOT NULL,
        session_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,skill_id,sha256),
        FOREIGN KEY(workspace_id,skill_id,source_revision,sha256)
            REFERENCES workspace_context_skill_versions(workspace_id,skill_id,revision,sha256),
        FOREIGN KEY(workspace_id,session_id) REFERENCES workspace_context_sessions(workspace_id,id)
    )""",
    """ALTER TABLE workspace_context_skills ADD CONSTRAINT skill_release_head
        FOREIGN KEY(workspace_id,id,latest_sha256)
        REFERENCES workspace_context_skill_releases(workspace_id,skill_id,sha256)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE workspace_context_skill_receipts (
        workspace_id uuid NOT NULL,
        session_id uuid NOT NULL,
        request_id uuid NOT NULL,
        request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
        skill_id uuid NOT NULL,
        result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,session_id,request_id),
        FOREIGN KEY(workspace_id,session_id) REFERENCES workspace_context_sessions(workspace_id,id),
        FOREIGN KEY(workspace_id,skill_id) REFERENCES workspace_context_skills(workspace_id,id)
    )""",
    f"""CREATE POLICY skill_catalog_read ON workspace_context_skills
        AS RESTRICTIVE FOR SELECT USING (published OR {_OWNER})""",
    f"""CREATE POLICY skill_catalog_insert ON workspace_context_skills
        AS RESTRICTIVE FOR INSERT WITH CHECK ({_AUTHOR})""",
    f"""CREATE POLICY skill_catalog_update ON workspace_context_skills
        AS RESTRICTIVE FOR UPDATE USING ({_OWNER}) WITH CHECK ({_AUTHOR})""",
    f"""CREATE POLICY skill_version_read ON workspace_context_skill_versions
        AS RESTRICTIVE FOR SELECT USING (
            EXISTS (SELECT 1 FROM workspace_context_skills k
                WHERE k.workspace_id=workspace_context_skill_versions.workspace_id
                  AND k.id=workspace_context_skill_versions.skill_id
                  AND k.owner_principal_id={_PRINCIPAL})
            OR EXISTS (SELECT 1 FROM workspace_context_skill_releases r
                WHERE r.workspace_id=workspace_context_skill_versions.workspace_id
                  AND r.skill_id=workspace_context_skill_versions.skill_id
                  AND r.source_revision=workspace_context_skill_versions.revision))""",
    # Release rows are shared under workspace RLS. Insertion additionally proves
    # author/session ownership; a peer cannot publish a private version.
    *tuple(
        f"""CREATE POLICY skill_author_insert ON {table} AS RESTRICTIVE FOR INSERT
            WITH CHECK (EXISTS (SELECT 1 FROM workspace_context_skills k
                JOIN workspace_context_sessions s
                  ON s.workspace_id=k.workspace_id AND s.agent_id=k.owner_agent_id
                 AND s.principal_id=k.owner_principal_id
                WHERE k.workspace_id={table}.workspace_id AND k.id={table}.skill_id
                  AND k.owner_principal_id={_PRINCIPAL} AND k.state='active'
                  AND s.id={table}.session_id AND s.state='active'
                  AND s.expires_at>pg_catalog.clock_timestamp()))"""
        for table in ("workspace_context_skill_versions", "workspace_context_skill_releases")
    ),
    """CREATE POLICY skill_receipt_owner ON workspace_context_skill_receipts
        AS RESTRICTIVE FOR ALL
        USING (EXISTS (SELECT 1 FROM workspace_context_sessions s
            WHERE s.workspace_id=workspace_context_skill_receipts.workspace_id
              AND s.id=workspace_context_skill_receipts.session_id))
        WITH CHECK (EXISTS (SELECT 1 FROM workspace_context_sessions s
            WHERE s.workspace_id=workspace_context_skill_receipts.workspace_id
              AND s.id=workspace_context_skill_receipts.session_id))""",
    """CREATE FUNCTION check_context_skill_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            IF (NEW.workspace_id,NEW.id,NEW.owner_principal_id,NEW.owner_agent_id,
                NEW.agent_object_id,NEW.slug,NEW.context_object_id,NEW.context_name,NEW.created_at)
               IS DISTINCT FROM
               (OLD.workspace_id,OLD.id,OLD.owner_principal_id,OLD.owner_agent_id,
                OLD.agent_object_id,OLD.slug,OLD.context_object_id,OLD.context_name,OLD.created_at)
               OR OLD.state='archived' OR (OLD.published AND NOT NEW.published)
               OR NEW.draft_revision NOT IN (OLD.draft_revision,OLD.draft_revision+1)
            THEN
                RAISE EXCEPTION 'invalid skill lifecycle transition' USING ERRCODE='55000';
            END IF;
            RETURN NEW;
        END; $$""",
    """CREATE TRIGGER context_skill_transition BEFORE UPDATE ON workspace_context_skills
        FOR EACH ROW EXECUTE FUNCTION check_context_skill_transition()""",
    # Immutable object identity and the deferred FK prevent namespace adoption.
    # No SECURITY DEFINER function, credentials or admin grant is exposed.
    f"""ALTER POLICY context_object_isolation ON workspace_context_objects
        WITH CHECK ((visibility='private' AND owner_principal_id={_PRINCIPAL}) OR
            (visibility='shared' AND kind='skill' AND EXISTS (
                SELECT 1 FROM workspace_context_skills k
                WHERE k.workspace_id=workspace_context_objects.workspace_id
                  AND k.context_object_id=workspace_context_objects.id
                  AND k.context_name=workspace_context_objects.name
                  AND k.owner_principal_id={_PRINCIPAL} AND k.state='active')))""",
    *tuple(
        f"""ALTER POLICY context_writer_{operation.lower()} ON {table}
            {('USING (' + predicate + ')') if operation == 'UPDATE' else ''}
            WITH CHECK ({predicate})"""
        for table, operations in (
            ("workspace_context_documents", ("UPDATE", "INSERT")),
            ("workspace_context_revisions", ("INSERT",)),
            ("workspace_context_events", ("INSERT",)),
        )
        for operation in operations
        for predicate in (
            f"""EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id={table}.workspace_id AND o.id={table}.object_id
                  AND ((o.visibility='private' AND o.owner_principal_id={_PRINCIPAL})
                    OR (o.visibility='shared' AND EXISTS (
                        SELECT 1 FROM workspace_context_write_grants g
                        WHERE g.workspace_id={table}.workspace_id AND g.object_id={table}.object_id
                          AND g.role={table}.role AND g.principal_id={_PRINCIPAL}))
                    OR (o.kind='skill' AND o.visibility='shared' AND EXISTS (
                        SELECT 1 FROM workspace_context_skills k
                        WHERE k.workspace_id=o.workspace_id AND k.context_object_id=o.id
                          AND k.context_name=o.name AND k.owner_principal_id={_PRINCIPAL}
                          AND k.state='active'))))""",
        )
    ),
)
