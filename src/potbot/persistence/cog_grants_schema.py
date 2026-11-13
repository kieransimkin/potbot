"""Immutable operator grant revisions, readable only by their scoped principal."""

WORKSPACE_TABLES = ("cog_grant_revisions",)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE cog_grant_revisions (
        workspace_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        agent_id uuid NOT NULL,
        generation bigint NOT NULL CHECK (generation > 0),
        node_sha256 text NOT NULL CHECK (node_sha256 ~ '^[0-9a-f]{64}$'),
        revision_sha256 text NOT NULL CHECK (revision_sha256 ~ '^[0-9a-f]{64}$'),
        policy_sha256 text NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
        grants jsonb NOT NULL CHECK (jsonb_typeof(grants)='array'
            AND jsonb_array_length(grants) <= 128),
        published_by text NOT NULL DEFAULT session_user,
        published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,principal_id,agent_id,generation),
        FOREIGN KEY (workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE POLICY cog_grant_principal_scope ON cog_grant_revisions AS RESTRICTIVE
        USING (principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)""",
)
