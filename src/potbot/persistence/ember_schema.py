"""Revision 13: scoped Ember contracts with a separate owner-only boundary.

Owner documents are never registered as artifacts or embedding sources. Public
documents are separately projected, with their own digest. The restrictive owner
policy is combined with the normal workspace/login policy by PostgreSQL.
"""

WORKSPACE_TABLES = ("syn_ember_public_bundles", "syn_ember_owner_bundles")
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE syn_ember_public_bundles (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        snapshot_id uuid NOT NULL,
        public_hash text NOT NULL CHECK(public_hash ~ '^[0-9a-f]{64}$'),
        document jsonb NOT NULL CHECK(COALESCE(jsonb_typeof(document)='object'
            AND document->>'projection'='public'
            AND document->>'schema_version'='1'
            AND document->'scope'->>'workspace_id'=workspace_id::text
            AND document->'scope'->>'snapshot_id'=snapshot_id::text, false)),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,id,snapshot_id),
        FOREIGN KEY(workspace_id,snapshot_id)
            REFERENCES syn_snapshot_seals(workspace_id,snapshot_id))""",
    """CREATE TABLE syn_ember_owner_bundles (
        workspace_id uuid NOT NULL, id uuid NOT NULL, snapshot_id uuid NOT NULL,
        owner_principal_id uuid NOT NULL,
        owner_hash text NOT NULL CHECK(owner_hash ~ '^[0-9a-f]{64}$'),
        document jsonb NOT NULL CHECK(COALESCE(jsonb_typeof(document)='object'
            AND document->>'projection'='owner'
            AND document->>'schema_version'='1'
            AND document->'scope'->>'workspace_id'=workspace_id::text
            AND document->'scope'->>'snapshot_id'=snapshot_id::text, false)),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,snapshot_id,owner_principal_id,owner_hash),
        FOREIGN KEY(workspace_id,id,snapshot_id)
            REFERENCES syn_ember_public_bundles(workspace_id,id,snapshot_id),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id))""",
    """CREATE POLICY ember_owner_only ON syn_ember_owner_bundles AS RESTRICTIVE
        USING (owner_principal_id =
            NULLIF(current_setting('potbot.principal_id',true),'')::uuid
            AND EXISTS (SELECT 1 FROM principals p WHERE
                p.id=owner_principal_id AND p.database_role=session_user))
        WITH CHECK (owner_principal_id =
            NULLIF(current_setting('potbot.principal_id',true),'')::uuid
            AND EXISTS (SELECT 1 FROM principals p WHERE
                p.id=owner_principal_id AND p.database_role=session_user))""",
    """CREATE INDEX syn_ember_bundles_snapshot
        ON syn_ember_public_bundles(workspace_id,snapshot_id,id)""",
)
