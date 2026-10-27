"""Migration 29: workspace-scoped MODEL/MEMORY/AGENTS context journal.

The earlier context_contract migration is a standalone prototype. These tables
live in Potbot's real workspace database and participate in its RLS/migration
ledger. Private context RLS is restrictive in addition to workspace RLS.
"""

WORKSPACE_TABLES = (
    "workspace_context_objects",
    "workspace_context_documents",
    "workspace_context_revisions",
    "workspace_context_events",
)
APPEND_ONLY_TABLES = (
    "workspace_context_objects",  # identities are immutable, including visibility/owner
    "workspace_context_revisions",
    "workspace_context_events",
)

_ALLOWED = "('AGENTS.md','MODEL.md','MEMORY.md')"

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_context_objects (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        kind text NOT NULL CHECK (kind IN (
            'sandbox','agent','operator','tool','skill','worker','workflow',
            'gateway','model','connector','primitive')),
        name text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'),
        visibility text NOT NULL CHECK (visibility IN ('shared','private')),
        owner_principal_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        UNIQUE(workspace_id,kind,name),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK ((visibility='shared' AND owner_principal_id IS NULL)
            OR (visibility='private' AND owner_principal_id IS NOT NULL))
    )""",
    f"""CREATE TABLE workspace_context_documents (
        workspace_id uuid NOT NULL,
        object_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN {_ALLOWED}),
        content text NOT NULL CHECK (octet_length(content)<=1048576),
        revision bigint NOT NULL CHECK (revision>0),
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,object_id,role),
        FOREIGN KEY(workspace_id,object_id)
            REFERENCES workspace_context_objects(workspace_id,id)
    )""",
    f"""CREATE TABLE workspace_context_revisions (
        workspace_id uuid NOT NULL,
        object_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN {_ALLOWED}),
        revision bigint NOT NULL CHECK (revision>0),
        content text NOT NULL CHECK (octet_length(content)<=1048576),
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
        actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 160),
        reason text NOT NULL DEFAULT '' CHECK (length(reason)<=1000),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,object_id,role,revision),
        FOREIGN KEY(workspace_id,object_id,role)
            REFERENCES workspace_context_documents(workspace_id,object_id,role)
    )""",
    f"""CREATE TABLE workspace_context_events (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        workspace_id uuid NOT NULL,
        object_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN {_ALLOWED}),
        revision bigint NOT NULL,
        actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 160),
        event_type text NOT NULL CHECK (event_type IN
            ('document.provisioned','document.updated')),
        payload jsonb NOT NULL DEFAULT '{{}}'::jsonb
            CHECK (jsonb_typeof(payload)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(workspace_id,object_id,role,revision)
            REFERENCES workspace_context_revisions(workspace_id,object_id,role,revision)
    )""",
    "CREATE INDEX workspace_context_events_stream ON "
    "workspace_context_events(workspace_id,id)",
    "CREATE INDEX workspace_context_revisions_recent ON "
    "workspace_context_revisions(workspace_id,object_id,role,revision DESC)",
    # The migration harness adds a workspace RLS policy later. RESTRICTIVE
    # policies below are AND-ed with it. Direct SQL from an application login
    # cannot enumerate or update another principal's private context.
    """CREATE POLICY context_object_isolation ON workspace_context_objects
        AS RESTRICTIVE FOR ALL
        USING (visibility='shared' OR owner_principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (visibility='private' AND owner_principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
    """,
    *tuple(
        f"""CREATE POLICY context_private_isolation ON {table}
            AS RESTRICTIVE FOR ALL
            USING (EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id={table}.workspace_id
                  AND o.id={table}.object_id))
            WITH CHECK (EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id={table}.workspace_id
                  AND o.id={table}.object_id))
        """
        for table in ("workspace_context_documents", "workspace_context_revisions",
                      "workspace_context_events")
    ),
)
