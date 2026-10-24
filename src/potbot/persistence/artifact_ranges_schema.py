"""Migration 22: actor/policy/revision-bound read leases and append-only revocation."""

WORKSPACE_TABLES = ("artifact_read_leases", "artifact_read_revocations")
APPEND_ONLY_TABLES = WORKSPACE_TABLES
MIGRATION_STATEMENTS = (
    """CREATE TABLE artifact_read_leases (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        owner_principal_id uuid NOT NULL, artifact_id uuid NOT NULL,
        artifact_sha256 text NOT NULL CHECK(artifact_sha256 ~ '^[0-9a-f]{64}$'),
        byte_length bigint NOT NULL CHECK(byte_length BETWEEN 0 AND 67108864),
        policy_hash text NOT NULL CHECK(policy_hash ~ '^[0-9a-f]{64}$'),
        ttl_seconds integer NOT NULL CHECK(ttl_seconds BETWEEN 1 AND 300),
        expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,owner_principal_id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE TABLE artifact_read_revocations (
        workspace_id uuid NOT NULL, lease_id uuid NOT NULL, owner_principal_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,lease_id),
        FOREIGN KEY(workspace_id,lease_id,owner_principal_id) REFERENCES artifact_read_leases(workspace_id,id,owner_principal_id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
)
