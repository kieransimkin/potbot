"""Migration 26: real per-workspace UI preferences and artifact-transfer receipts.

Transfer bytes are progress observations, not proof of complete/installed weights.
All tables are RLS-scoped and the service records explicit principal ownership.
"""

WORKSPACE_TABLES = ("ui_preferences", "ui_artifact_transfers")
APPEND_ONLY_TABLES = ()

MIGRATION_STATEMENTS = (
    """CREATE TABLE ui_preferences (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        principal_id uuid NOT NULL,
        revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
        settings jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(settings)='object'),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE ui_artifact_transfers (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        idempotency_key uuid NOT NULL,
        model_id text NOT NULL CHECK (length(model_id) BETWEEN 3 AND 180),
        artifact_repository text NOT NULL CHECK (length(artifact_repository) BETWEEN 3 AND 180),
        revision text NOT NULL CHECK (revision ~ '^[0-9a-f]{40}$'),
        filename text NOT NULL CHECK (length(filename) BETWEEN 3 AND 200),
        quantization text NOT NULL CHECK (length(quantization) BETWEEN 2 AND 16),
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        expected_bytes bigint NOT NULL CHECK (expected_bytes BETWEEN 1 AND 137438953472),
        state text NOT NULL DEFAULT 'queued' CHECK (state IN
            ('queued','downloading','verifying','installed','failed','cancelled','indeterminate')),
        downloaded_bytes bigint NOT NULL DEFAULT 0 CHECK (downloaded_bytes>=0),
        verified_bytes bigint NOT NULL DEFAULT 0 CHECK (verified_bytes>=0),
        cancel_requested boolean NOT NULL DEFAULT false,
        lease_token uuid,
        generation bigint NOT NULL DEFAULT 0,
        error_code text CHECK (length(error_code)<=80),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,principal_id,idempotency_key),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK (downloaded_bytes<=expected_bytes),
        CHECK (verified_bytes<=downloaded_bytes),
        CHECK ((state IN ('downloading','verifying'))=(lease_token IS NOT NULL)),
        CHECK (state!='installed' OR verified_bytes=expected_bytes)
    )""",
    "CREATE INDEX ui_artifact_transfers_status ON ui_artifact_transfers(workspace_id,state,updated_at,id)",
    "CREATE INDEX ui_artifact_transfers_owner ON ui_artifact_transfers(workspace_id,principal_id,created_at,id)",
)
