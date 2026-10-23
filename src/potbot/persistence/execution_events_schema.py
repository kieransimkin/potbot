"""Migration 19: durable causal SYN execution events, independent of queued job IDs.

Events share the existing workspace commit-ordered cursor lock with job_events.
Legacy job event identities and API/WebSocket projections are unchanged.
"""

WORKSPACE_TABLES = ("syn_execution_events",)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE syn_execution_events (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        sequence bigint NOT NULL CHECK(sequence BETWEEN 1 AND 9007199254740991),
        id uuid NOT NULL,
        event_ref text NOT NULL,
        event_type_ref text NOT NULL,
        event_type_version text NOT NULL,
        subject_type_ref text NOT NULL,
        subject_version text NOT NULL,
        run_id uuid NOT NULL,
        parent_run_id uuid,
        parent_event_id uuid,
        trigger_kind text NOT NULL,
        trigger_ref text NOT NULL,
        trigger_input_sha256 text NOT NULL CHECK(trigger_input_sha256 ~ '^[0-9a-f]{64}$'),
        emitter_run_id uuid NOT NULL,
        emitter_principal_id uuid NOT NULL,
        stage text NOT NULL CHECK(stage IN ('started','updated','completed','failed','cancelled','indeterminate')),
        input_artifact_ref text,
        output_artifact_ref text,
        output_sha256 text,
        elapsed_ms bigint CHECK(elapsed_ms >= 0),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,sequence),
        UNIQUE(workspace_id,event_ref),
        FOREIGN KEY(workspace_id,emitter_principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,parent_event_id)
            REFERENCES syn_execution_events(workspace_id,id),
        CHECK(event_ref = 'syn://event/' || id::text),
        CHECK(parent_event_id IS NULL OR parent_run_id IS NOT NULL),
        CHECK(output_sha256 IS NULL OR output_artifact_ref IS NOT NULL)
    )""",
    "CREATE INDEX syn_execution_events_run ON syn_execution_events(workspace_id,run_id,sequence)",
    "CREATE INDEX syn_execution_events_parent ON syn_execution_events(workspace_id,parent_event_id)",
)
