"""Migration 14: immutable workspace filesystem sandbox registration."""

WORKSPACE_TABLES = ("workspace_sandboxes",)
APPEND_ONLY_TABLES = ("workspace_sandboxes",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_sandboxes (
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
        root_path text NOT NULL CHECK(length(root_path) BETWEEN 2 AND 4096),
        root_key text NOT NULL UNIQUE CHECK(root_key ~ '^[0-9a-f]{64}$'),
        policy_version integer NOT NULL CHECK(policy_version=1),
        isolation_requirement text NOT NULL DEFAULT 'required'
            CHECK(isolation_requirement='required'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
)
