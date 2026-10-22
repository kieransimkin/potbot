"""Hosted request identities and conservative accounting without fictitious weights."""

MIGRATION_STATEMENTS = (
    """ALTER TABLE budget_reservations DROP CONSTRAINT budget_reservations_origin_check""",
    """ALTER TABLE budget_reservations ADD CONSTRAINT budget_reservations_origin_check
        CHECK (origin IN ('measured','synthetic','provider_reported','conservative_estimate'))""",
    """ALTER TABLE budget_usage DROP CONSTRAINT budget_usage_origin_check""",
    """ALTER TABLE budget_usage ADD CONSTRAINT budget_usage_origin_check
        CHECK (origin IN ('measured','synthetic','provider_reported','conservative_estimate'))""",
    """CREATE TABLE hosted_requests (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        job_id uuid NOT NULL,
        episode_id uuid NOT NULL,
        ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 15),
        request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
        policy_hash text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
        request jsonb NOT NULL,
        policy jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        UNIQUE (workspace_id,job_id,ordinal),
        FOREIGN KEY (workspace_id,job_id) REFERENCES jobs(workspace_id,id),
        FOREIGN KEY (workspace_id,episode_id) REFERENCES episodes(workspace_id,id)
    )""",
    """CREATE TABLE hosted_invocations (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        status text NOT NULL CHECK (status IN (
            'prepared','reserved','dispatched','observed','completed','rejected','indeterminate','reconciled')),
        reservation_id uuid,
        result jsonb,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        FOREIGN KEY (workspace_id,id) REFERENCES hosted_requests(workspace_id,id),
        FOREIGN KEY (workspace_id,reservation_id) REFERENCES budget_reservations(workspace_id,id)
    )""",
    """CREATE TABLE hosted_invocation_events (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        invocation_id uuid NOT NULL,
        kind text NOT NULL CHECK (kind IN (
            'prepared','reserved','dispatched','observed','completed','rejected','indeterminate','reconciled')),
        details jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        FOREIGN KEY (workspace_id,invocation_id) REFERENCES hosted_requests(workspace_id,id)
    )""",
    "CREATE INDEX hosted_requests_job ON hosted_requests(workspace_id,job_id,ordinal)",
    "CREATE INDEX hosted_invocations_state ON hosted_invocations(workspace_id,status)",
    "CREATE INDEX hosted_events_request ON hosted_invocation_events(workspace_id,invocation_id,created_at)",
)

WORKSPACE_TABLES = ("hosted_requests", "hosted_invocations", "hosted_invocation_events")
APPEND_ONLY_TABLES = ("hosted_requests", "hosted_invocation_events")
