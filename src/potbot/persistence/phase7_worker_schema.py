"""Migration 17: workspace-scoped, fenced and non-replayable research execution.

Only repository-snapshot inspection is executable in this increment. Network
sources require separate authenticated budget, disclosure and egress controls.
No prior migration or checksum is changed.
"""

WORKSPACE_TABLES = ("research_runs", "research_run_receipts")
APPEND_ONLY_TABLES = ("research_run_receipts",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE research_runs (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        action_id uuid NOT NULL,
        source_artifact_id uuid NOT NULL,
        action_identity text NOT NULL CHECK(action_identity ~ '^[0-9a-f]{64}$'),
        source_revision text NOT NULL CHECK(source_revision ~ '^[0-9a-f]{64}$'),
        state text NOT NULL DEFAULT 'queued' CHECK(state IN
            ('queued','running','succeeded','blocked','indeterminate')),
        generation bigint NOT NULL DEFAULT 0 CHECK(generation >= 0),
        lease_owner text,
        lease_until timestamptz,
        started_at timestamptz,
        finished_at timestamptz,
        result_artifact_id uuid,
        receipt_digest text CHECK(receipt_digest IS NULL OR receipt_digest ~ '^[0-9a-f]{64}$'),
        error_code text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,action_id),
        UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,action_id)
            REFERENCES research_actions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,source_artifact_id)
            REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,result_artifact_id)
            REFERENCES artifacts(workspace_id,id),
        CHECK ((state='running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)),
        CHECK ((state IN ('succeeded','blocked','indeterminate')) = (finished_at IS NOT NULL)),
        CHECK (state<>'succeeded' OR (result_artifact_id IS NOT NULL AND receipt_digest IS NOT NULL)),
        CHECK (state='succeeded' OR result_artifact_id IS NULL)
    )""",
    """CREATE INDEX research_runs_queue ON research_runs
        (workspace_id,state,created_at,id)""",
    """CREATE TABLE research_run_receipts (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        run_id uuid NOT NULL,
        result_artifact_id uuid NOT NULL,
        receipt_digest text NOT NULL CHECK(receipt_digest ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,run_id),
        FOREIGN KEY(workspace_id,episode_id,run_id)
            REFERENCES research_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,result_artifact_id)
            REFERENCES artifacts(workspace_id,id)
    )""",
)
