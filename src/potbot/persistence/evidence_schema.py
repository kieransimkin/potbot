"""Application migration 2: immutable payloads/evidence and durable outbox jobs."""

WORKSPACE_TABLES = ("artifacts", "evidence_events", "outbox_jobs")
APPEND_ONLY_TABLES = ("artifacts", "evidence_events")

MIGRATION_STATEMENTS = (
    """CREATE TABLE artifacts (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
        byte_length bigint NOT NULL CHECK (byte_length >= 0),
        media_type text NOT NULL CHECK (length(media_type) BETWEEN 1 AND 255),
        backend text NOT NULL CHECK (length(backend) BETWEEN 1 AND 100),
        locator text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id, id),
        UNIQUE (workspace_id, digest)
    )""",
    """CREATE TABLE evidence_events (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
        episode_id uuid,
        event_type text NOT NULL,
        occurred_at timestamptz NOT NULL,
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        producer_kind text NOT NULL CHECK
            (producer_kind IN ('controller','human','tool','model')),
        producer_id text NOT NULL,
        deployment_id uuid,
        causation_id uuid,
        correlation_id uuid NOT NULL,
        payload_id uuid,
        details jsonb NOT NULL CHECK (jsonb_typeof(details) = 'object'),
        content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
        PRIMARY KEY (workspace_id, id),
        FOREIGN KEY (workspace_id, episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY (workspace_id, payload_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY (workspace_id, causation_id) REFERENCES evidence_events(workspace_id,id),
        CHECK ((producer_kind = 'model') = (deployment_id IS NOT NULL)),
        CHECK (event_type <> 'tool_observation' OR producer_kind = 'tool'),
        CHECK (event_type <> 'user_input' OR producer_kind = 'human'),
        CHECK (event_type <> 'controller_decision' OR producer_kind = 'controller')
    )""",
    "CREATE INDEX evidence_episode_order ON evidence_events(workspace_id,episode_id,recorded_at,id)",
    """CREATE TABLE outbox_jobs (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        job_key text NOT NULL,
        job_type text NOT NULL,
        event_id uuid NOT NULL,
        payload_id uuid,
        policy_version text NOT NULL,
        schema_version integer NOT NULL CHECK (schema_version = 1),
        status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','completed','failed')),
        generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
        attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
        available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        lease_until timestamptz,
        lease_owner text,
        completion_artifact_id uuid,
        last_error_code text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        UNIQUE (workspace_id,job_key),
        FOREIGN KEY (workspace_id,event_id) REFERENCES evidence_events(workspace_id,id),
        FOREIGN KEY (workspace_id,payload_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY (workspace_id,completion_artifact_id) REFERENCES artifacts(workspace_id,id),
        CHECK ((status = 'running') = (lease_until IS NOT NULL AND lease_owner IS NOT NULL)),
        CHECK (status <> 'completed' OR completion_artifact_id IS NOT NULL)
    )""",
    "CREATE INDEX outbox_claim ON outbox_jobs(workspace_id,status,available_at,created_at)",
)
