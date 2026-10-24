"""Migration 23: immutable scoped upload sessions, chunk slots and terminal receipts."""

WORKSPACE_TABLES = ("artifact_uploads", "artifact_upload_chunks", "artifact_upload_terminals")
APPEND_ONLY_TABLES = WORKSPACE_TABLES
MIGRATION_STATEMENTS = (
    """CREATE TABLE artifact_uploads (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        owner_principal_id uuid NOT NULL,
        artifact_sha256 text NOT NULL CHECK(artifact_sha256 ~ '^[0-9a-f]{64}$'),
        byte_length bigint NOT NULL CHECK(byte_length BETWEEN 0 AND 67108864),
        media_type text NOT NULL CHECK(length(media_type) BETWEEN 1 AND 255),
        policy_hash text NOT NULL CHECK(policy_hash ~ '^[0-9a-f]{64}$'),
        ttl_seconds integer NOT NULL CHECK(ttl_seconds BETWEEN 1 AND 300),
        expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,owner_principal_id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE artifact_upload_chunks (
        workspace_id uuid NOT NULL, upload_id uuid NOT NULL, owner_principal_id uuid NOT NULL,
        chunk_index integer NOT NULL CHECK(chunk_index BETWEEN 0 AND 8191),
        artifact_id uuid NOT NULL,
        chunk_sha256 text NOT NULL CHECK(chunk_sha256 ~ '^[0-9a-f]{64}$'),
        byte_length integer NOT NULL CHECK(byte_length BETWEEN 1 AND 8192),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,upload_id,chunk_index),
        FOREIGN KEY(workspace_id,upload_id,owner_principal_id) REFERENCES artifact_uploads(workspace_id,id,owner_principal_id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE TABLE artifact_upload_terminals (
        workspace_id uuid NOT NULL, upload_id uuid NOT NULL, owner_principal_id uuid NOT NULL,
        state text NOT NULL CHECK(state IN ('committed','aborted')), artifact_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,upload_id),
        CHECK((state='committed')=(artifact_id IS NOT NULL)),
        FOREIGN KEY(workspace_id,upload_id,owner_principal_id) REFERENCES artifact_uploads(workspace_id,id,owner_principal_id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
)
