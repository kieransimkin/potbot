"""Migration 25: task-bound, crash-reconcilable reasoning provider receipts.

No prompts, secret values or response bodies are persisted in these records.
The run lease fences dispatch, while the existing BudgetLedger owns charging.
"""

WORKSPACE_TABLES = ("reasoning_budget_invocations",)
APPEND_ONLY_TABLES = ()

MIGRATION_STATEMENTS = (
    """CREATE TABLE reasoning_budget_invocations (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        run_id uuid NOT NULL,
        node_id uuid NOT NULL,
        episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL,
        reservation_id uuid NOT NULL,
        provider text NOT NULL,
        model text NOT NULL,
        prompt_hash text NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
        status text NOT NULL CHECK (status IN
            ('reserved','dispatched','observed','settled','released','indeterminate')),
        actual jsonb CHECK (actual IS NULL OR jsonb_typeof(actual)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,run_id,node_id),
        FOREIGN KEY(workspace_id,run_id)
            REFERENCES reasoning_runtime_runs(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id)
            REFERENCES episodes(workspace_id,id),
        FOREIGN KEY(workspace_id,reservation_id)
            REFERENCES budget_reservations(workspace_id,id)
    )""",
    "CREATE INDEX reasoning_budget_run ON reasoning_budget_invocations(workspace_id,run_id,created_at)",
    "CREATE INDEX reasoning_budget_unresolved ON reasoning_budget_invocations(workspace_id,status,updated_at)",
)
