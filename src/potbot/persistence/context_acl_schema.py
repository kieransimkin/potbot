"""Migration 30: explicit operator-granted writers for shared context.

No application login may grant itself privileges: a RESTRICTIVE RLS policy
permits SELECT on its own grants but rejects INSERT/UPDATE/DELETE. Host admin
installs grants; the existing workspace RLS remains in force.
"""

WORKSPACE_TABLES = ("workspace_context_write_grants",)
APPEND_ONLY_TABLES = ()

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_context_write_grants (
        workspace_id uuid NOT NULL,
        object_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN ('AGENTS.md','MODEL.md','MEMORY.md')),
        granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,object_id,principal_id,role),
        FOREIGN KEY (workspace_id,object_id,role)
            REFERENCES workspace_context_documents(workspace_id,object_id,role),
        FOREIGN KEY (workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE POLICY context_grant_read_only ON workspace_context_write_grants
        AS RESTRICTIVE FOR ALL
        USING (principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (false)
    """,
    # A principal can mutate its own private contexts. Shared contexts additionally
    # require explicit, role-specific grants, installed only by the admin connection.
    *tuple(
        f"""CREATE POLICY context_writer_{operation.lower()} ON {table}
            AS RESTRICTIVE FOR {operation}
            {('USING (' + predicate + ')') if operation == 'UPDATE' else ''}
            WITH CHECK ({predicate})
        """
        for table, operations in (
            ("workspace_context_documents", ("UPDATE", "INSERT")),
            ("workspace_context_revisions", ("INSERT",)),
            ("workspace_context_events", ("INSERT",)),
        )
        for operation in operations
        for predicate in (
            f"""EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id={table}.workspace_id
                  AND o.id={table}.object_id
                  AND ((o.visibility='private' AND o.owner_principal_id =
                    NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
                  OR (o.visibility='shared' AND EXISTS (
                    SELECT 1 FROM workspace_context_write_grants g
                    WHERE g.workspace_id={table}.workspace_id
                      AND g.object_id={table}.object_id
                      AND g.role={table}.role
                      AND g.principal_id = NULLIF(
                        pg_catalog.current_setting('potbot.principal_id',true),'')::uuid
                  ))))""",
        )
    ),
)
