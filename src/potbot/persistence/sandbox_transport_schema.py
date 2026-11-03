"""Durable outcome and reservation projection for sandbox transport attempts."""

WORKSPACE_TABLES = ("sandbox_transport_attempts",)
APPEND_ONLY_TABLES: tuple[str, ...] = ()
MIGRATION_STATEMENTS = (
    """CREATE TABLE sandbox_transport_attempts (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        target_id uuid NOT NULL,
        effect_id uuid NOT NULL,
        request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
        episode_id uuid NOT NULL,
        reservation_id uuid NOT NULL,
        agent_id uuid NOT NULL,
        installation_sha256 text NOT NULL CHECK(installation_sha256 ~ '^[0-9a-f]{64}$'),
        state text NOT NULL CHECK(state IN ('prepared','dispatched','applied','cancelled')),
        receipt jsonb,
        PRIMARY KEY(workspace_id,run_id),
        UNIQUE(workspace_id,target_id,effect_id),
        UNIQUE(workspace_id,reservation_id),
        FOREIGN KEY(workspace_id,run_id) REFERENCES execution_run_contracts(workspace_id,run_id),
        FOREIGN KEY(workspace_id,episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY(workspace_id,reservation_id) REFERENCES budget_reservations(workspace_id,id),
        CHECK(receipt IS NULL OR (state IN ('applied','cancelled') AND jsonb_typeof(receipt)='object')),
        CHECK(state <> 'applied' OR receipt IS NOT NULL)
    )""",
    """CREATE INDEX execution_transport_lane ON execution_run_contracts
       (workspace_id, (scope->>'kind'), (scope->>'operation_ref'))
       WHERE scope->>'effect_class'<>'read_only'""",
)
