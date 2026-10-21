"""Task contracts and budget ledger application migration."""

MIGRATION_STATEMENTS = (
    """
    CREATE TABLE contract_revisions (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        predecessor_id uuid,
        content jsonb NOT NULL,
        content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id, id),
        UNIQUE (workspace_id, episode_id, id),
        FOREIGN KEY (workspace_id, episode_id) REFERENCES episodes (workspace_id, id),
        FOREIGN KEY (workspace_id, episode_id, predecessor_id)
            REFERENCES contract_revisions (workspace_id, episode_id, id),
        CHECK (predecessor_id IS NULL OR predecessor_id <> id)
    )
    """,
    """
    ALTER TABLE episodes ADD CONSTRAINT episodes_active_contract_fk
        FOREIGN KEY (workspace_id, id, active_contract_id)
        REFERENCES contract_revisions (workspace_id, episode_id, id)
    """,
    """
    CREATE TABLE budget_limits (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        dimensions jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id, id),
        FOREIGN KEY (workspace_id, id) REFERENCES episodes (workspace_id, id)
    )
    """,
    """
    CREATE TABLE budget_reservations (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        reservation_key text NOT NULL,
        amounts jsonb NOT NULL,
        status text NOT NULL CHECK (status IN ('reserved','settled','released','indeterminate')),
        actual jsonb,
        origin text CHECK (origin IN ('measured','synthetic','provider_reported')),
        reason text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id, id),
        UNIQUE (workspace_id, episode_id, reservation_key),
        UNIQUE (workspace_id, episode_id, id),
        FOREIGN KEY (workspace_id, episode_id) REFERENCES episodes (workspace_id, id),
        CHECK ((status='settled' AND actual IS NOT NULL AND origin IS NOT NULL)
            OR (status<>'settled' AND actual IS NULL AND origin IS NULL))
    )
    """,
    """
    CREATE TABLE budget_usage (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        reservation_id uuid NOT NULL,
        amounts jsonb NOT NULL,
        origin text NOT NULL CHECK (origin IN ('measured','synthetic','provider_reported')),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id, id),
        UNIQUE (workspace_id, reservation_id),
        FOREIGN KEY (workspace_id, episode_id, reservation_id)
            REFERENCES budget_reservations (workspace_id, episode_id, id)
    )
    """,
    "CREATE INDEX budget_reservations_episode ON budget_reservations(workspace_id,episode_id,status)",
    "CREATE INDEX budget_usage_episode ON budget_usage(workspace_id,episode_id)",
)
WORKSPACE_TABLES = ("contract_revisions", "budget_limits", "budget_reservations", "budget_usage")
APPEND_ONLY_TABLES = ("contract_revisions", "budget_usage")
