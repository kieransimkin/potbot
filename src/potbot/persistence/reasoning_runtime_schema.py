"""Migration 24: fenced reasoning snapshots, steering and append-only events.

Reasoning workspace authority remains PostgreSQL RLS; a run lease fences stale
writers. External provider calls are never retried after uncertain dispatch.
"""

WORKSPACE_TABLES = ("reasoning_runtime_runs", "reasoning_runtime_steering", "reasoning_runtime_events")
APPEND_ONLY_TABLES = ("reasoning_runtime_events",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE reasoning_runtime_runs (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        state text NOT NULL CHECK (state IN ('running','completed','failed','indeterminate')),
        lease_token uuid,
        lease_until timestamptz,
        generation bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
        checkpoint bigint NOT NULL DEFAULT 0 CHECK (checkpoint >= 0),
        event_cursor bigint NOT NULL DEFAULT 0 CHECK (event_cursor >= 0),
        accepting_steering boolean NOT NULL DEFAULT true,
        snapshot jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(snapshot)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        FOREIGN KEY (workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK ((state='running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
    )""",
    "CREATE INDEX reasoning_runtime_run_states ON reasoning_runtime_runs (workspace_id,state,lease_until)",
    """CREATE TABLE reasoning_runtime_steering (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        id text NOT NULL CHECK (id ~ '^[0-9a-f]{64}$'),
        request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
        prompt text NOT NULL CHECK (octet_length(prompt) BETWEEN 1 AND 8192),
        status text NOT NULL CHECK (status IN ('queued','decided')),
        decision jsonb CHECK (decision IS NULL OR jsonb_typeof(decision)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,run_id,id),
        UNIQUE (workspace_id,run_id,request_id),
        FOREIGN KEY (workspace_id,run_id) REFERENCES reasoning_runtime_runs(workspace_id,id)
    )""",
    "CREATE INDEX reasoning_runtime_steering_pending ON reasoning_runtime_steering(workspace_id,run_id,status,created_at)",
    """CREATE TABLE reasoning_runtime_events (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        sequence bigint NOT NULL CHECK(sequence > 0),
        type text NOT NULL,
        data jsonb NOT NULL CHECK(jsonb_typeof(data)='object'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,run_id,sequence),
        FOREIGN KEY(workspace_id,run_id) REFERENCES reasoning_runtime_runs(workspace_id,id)
    )""",
)
