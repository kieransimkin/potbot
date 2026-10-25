"""Migration 28: durable, workspace-scoped test evidence and human upgrade decisions.

The HTTP API never runs an upgrade. Operator publication binds an independently
read-back S9 record; a verified approval merely queues a host-side transaction.
State changes use CAS, with indeterminate outcomes never automatically retried.
"""

WORKSPACE_TABLES = ("ui_tool_upgrade_approvals", "ui_tool_upgrade_audit")
APPEND_ONLY_TABLES = ("ui_tool_upgrade_audit",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE ui_tool_upgrade_approvals (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        agent_id uuid NOT NULL,
        slug text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,62}$'),
        old_commit text NOT NULL CHECK (old_commit ~ '^[a-f0-9]{40}$'),
        new_commit text NOT NULL CHECK (new_commit ~ '^[a-f0-9]{40}$'),
        evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[a-f0-9]{64}$'),
        suite_sha256 text NOT NULL CHECK (suite_sha256 ~ '^[a-f0-9]{64}$'),
        passed_tests integer NOT NULL CHECK (passed_tests BETWEEN 2 AND 16),
        tests jsonb NOT NULL CHECK (jsonb_typeof(tests)='array'
            AND jsonb_array_length(tests) BETWEEN 2 AND 16),
        revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
        state text NOT NULL DEFAULT 'awaiting_approval' CHECK (state IN
            ('awaiting_approval','approved','rejected','executing','awaiting_repin',
             'active','indeterminate','rolled_back')),
        worker_token uuid,
        approval_principal_id uuid,
        approval_mac text CHECK (approval_mac IS NULL OR approval_mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,id,agent_id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK (old_commit != new_commit),
        CHECK ((state='executing')=(worker_token IS NOT NULL)),
        CHECK (approval_principal_id IS NULL OR state!='awaiting_approval'),
        CHECK (state!='approved' OR approval_mac IS NOT NULL)
    )""",
    "CREATE INDEX ui_tool_upgrade_approval_status ON ui_tool_upgrade_approvals(workspace_id,state,updated_at,id)",
    "CREATE INDEX ui_tool_upgrade_approval_owner ON ui_tool_upgrade_approvals(workspace_id,principal_id,created_at,id)",
    """CREATE TABLE ui_tool_upgrade_audit (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        upgrade_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        action text NOT NULL CHECK (action IN
            ('published','approved','rejected','claimed','activated','indeterminate','reconciled')),
        prior_state text NOT NULL,
        next_state text NOT NULL,
        revision bigint NOT NULL CHECK (revision>=0),
        evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[a-f0-9]{64}$'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,upgrade_id)
            REFERENCES ui_tool_upgrade_approvals(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "CREATE INDEX ui_tool_upgrade_audit_chain ON ui_tool_upgrade_audit(workspace_id,upgrade_id,revision)",
)
