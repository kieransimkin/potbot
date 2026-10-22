"""Durable API job queue and commit-ordered, immutable event replay."""

WORKSPACE_TABLES = ("jobs", "job_event_cursors", "job_events")
APPEND_ONLY_TABLES = ("job_events",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE jobs (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL,
        job_type text NOT NULL CHECK (job_type = 'model.generate'),
        input jsonb NOT NULL CHECK (jsonb_typeof(input) = 'object'),
        selected_models jsonb NOT NULL CHECK (jsonb_typeof(selected_models) = 'array'),
        state text NOT NULL DEFAULT 'queued' CHECK (state IN
            ('queued','running','cancelling','succeeded','failed','cancelled','indeterminate')),
        update_token bigint NOT NULL DEFAULT 0 CHECK (update_token >= 0),
        generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
        lease_owner text,
        lease_until timestamptz,
        heartbeat_at timestamptz,
        cancellation_requested boolean NOT NULL DEFAULT false,
        dispatched_at timestamptz,
        progress jsonb NOT NULL DEFAULT '{"stage":"queued"}'::jsonb,
        result jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
        error_code text,
        output_bytes bigint NOT NULL DEFAULT 0 CHECK (output_bytes BETWEEN 0 AND 4194304),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        started_at timestamptz,
        finished_at timestamptz,
        PRIMARY KEY (workspace_id,id),
        FOREIGN KEY (workspace_id,episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY (workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        CHECK ((state IN ('running','cancelling')) =
            (lease_owner IS NOT NULL AND lease_until IS NOT NULL)),
        CHECK ((state IN ('succeeded','failed','cancelled','indeterminate')) =
            (finished_at IS NOT NULL)),
        CHECK (state <> 'cancelling' OR cancellation_requested)
    )""",
    "CREATE INDEX jobs_queue ON jobs(workspace_id,state,created_at,id)",
    "CREATE INDEX jobs_episode ON jobs(workspace_id,episode_id,created_at,id)",
    """CREATE TABLE job_event_cursors (
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
        last_sequence bigint NOT NULL DEFAULT 0
            CHECK (last_sequence BETWEEN 0 AND 9007199254740991)
    )""",
    """CREATE TABLE job_events (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        sequence bigint NOT NULL CHECK (sequence BETWEEN 1 AND 9007199254740991),
        id uuid NOT NULL,
        job_id uuid NOT NULL,
        event_type text NOT NULL,
        data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,sequence),
        UNIQUE (workspace_id,id),
        FOREIGN KEY (workspace_id,job_id) REFERENCES jobs(workspace_id,id)
    )""",
    "CREATE INDEX job_events_replay ON job_events(workspace_id,job_id,sequence)",
)
