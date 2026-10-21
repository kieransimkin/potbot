"""Migration 8: scoped execution intents, observations, and information needs."""

WORKSPACE_TABLES = (
    "knowledge_views",
    "knowledge_view_evidence",
    "search_nodes",
    "actions",
    "action_dependencies",
    "action_runs",
    "action_completions",
    "run_recovery_claims",
    "run_reconciliations",
    "information_needs",
    "need_revisions",
    "need_revision_evidence",
    "need_status",
)
APPEND_ONLY_TABLES = (
    "knowledge_views",
    "knowledge_view_evidence",
    "search_nodes",
    "actions",
    "action_dependencies",
    "action_completions",
    "run_recovery_claims",
    "run_reconciliations",
    "information_needs",
    "need_revisions",
    "need_revision_evidence",
)

MIGRATION_STATEMENTS = (
    """CREATE TABLE knowledge_views (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL, repository_state_id uuid NOT NULL,
        semantic_release_id uuid NOT NULL, content jsonb NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,repository_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,semantic_release_id) REFERENCES semantic_releases(workspace_id,id)
    )""",
    """CREATE TABLE knowledge_view_evidence (
        workspace_id uuid NOT NULL, knowledge_view_id uuid NOT NULL, evidence_id uuid NOT NULL,
        PRIMARY KEY(workspace_id,knowledge_view_id,evidence_id),
        FOREIGN KEY(workspace_id,knowledge_view_id) REFERENCES knowledge_views(workspace_id,id),
        FOREIGN KEY(workspace_id,evidence_id) REFERENCES evidence_events(workspace_id,id)
    )""",
    """CREATE TABLE search_nodes (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        knowledge_view_id uuid NOT NULL, parent_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,knowledge_view_id)
            REFERENCES knowledge_views(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,parent_id)
            REFERENCES search_nodes(workspace_id,episode_id,id),
        CHECK(parent_id IS NULL OR parent_id <> id)
    )""",
    """CREATE TABLE actions (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL, node_id uuid NOT NULL,
        kind text NOT NULL, tool_definition_digest text NOT NULL
            CHECK(tool_definition_digest ~ '^[0-9a-f]{64}$'),
        proposal jsonb NOT NULL, proposal_digest text NOT NULL
            CHECK(proposal_digest ~ '^[0-9a-f]{64}$'),
        producer_kind text NOT NULL CHECK(producer_kind IN ('controller','model')),
        producer_id text NOT NULL, deployment_id uuid,
        external_invocation_ref jsonb,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,node_id) REFERENCES search_nodes(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id),
        CHECK((producer_kind='model')=(deployment_id IS NOT NULL)),
        CHECK(external_invocation_ref IS NULL)
    )""",
    """CREATE TABLE action_dependencies (
        workspace_id uuid NOT NULL, episode_id uuid NOT NULL,
        action_id uuid NOT NULL, dependency_id uuid NOT NULL,
        PRIMARY KEY(workspace_id,action_id,dependency_id),
        FOREIGN KEY(workspace_id,episode_id,action_id) REFERENCES actions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,dependency_id) REFERENCES actions(workspace_id,episode_id,id),
        CHECK(action_id <> dependency_id)
    )""",
    """CREATE TABLE action_runs (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL, action_id uuid NOT NULL,
        candidate_id uuid NOT NULL, contract_revision_id uuid NOT NULL, input_state_id uuid NOT NULL,
        reservation_id uuid NOT NULL, lease_owner text NOT NULL, lease_generation bigint NOT NULL,
        input_fingerprint text NOT NULL CHECK(input_fingerprint ~ '^[0-9a-f]{64}$'),
        owner_instance uuid NOT NULL, attempt integer NOT NULL DEFAULT 1 CHECK(attempt=1),
        status text NOT NULL CHECK(status IN ('proposed','running','completed','nonzero_exit',
            'timeout','cancelled','infrastructure_error','inconclusive','indeterminate')),
        started_at timestamptz, finished_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        UNIQUE(workspace_id,action_id), UNIQUE(workspace_id,reservation_id),
        FOREIGN KEY(workspace_id,episode_id,action_id) REFERENCES actions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,candidate_id) REFERENCES candidate_workspaces(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,input_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,reservation_id)
            REFERENCES budget_reservations(workspace_id,episode_id,id)
    )""",
    "CREATE INDEX action_runs_active ON action_runs(workspace_id,episode_id,status)",
    """CREATE UNIQUE INDEX candidate_active_operation ON action_runs(workspace_id,candidate_id)
        WHERE status IN ('proposed','running','indeterminate')""",
    """CREATE TABLE action_completions (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL, run_id uuid NOT NULL,
        outcome text NOT NULL, exit_code integer, elapsed_ms bigint NOT NULL CHECK(elapsed_ms >= 0),
        stdout_artifact_id uuid NOT NULL, stderr_artifact_id uuid NOT NULL,
        result_state_id uuid, result_fingerprint text NOT NULL
            CHECK(result_fingerprint ~ '^[0-9a-f]{64}$'),
        receipt_digest text NOT NULL CHECK(receipt_digest ~ '^[0-9a-f]{64}$'),
        details jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id),
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES action_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,stdout_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,stderr_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,result_state_id) REFERENCES repository_states(workspace_id,id)
    )""",
    """CREATE TABLE run_recovery_claims (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        run_id uuid NOT NULL, candidate_id uuid NOT NULL, owner text NOT NULL,
        from_generation bigint NOT NULL CHECK(from_generation>0),
        generation bigint NOT NULL CHECK(generation=from_generation+1),
        receipt_digest text NOT NULL CHECK(receipt_digest ~ '^[0-9a-f]{64}$'),
        result_fingerprint text NOT NULL CHECK(result_fingerprint ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,candidate_id,generation),
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES action_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,candidate_id) REFERENCES candidate_workspaces(workspace_id,id),
        CHECK(length(owner) BETWEEN 1 AND 200)
    )""",
    """CREATE TABLE run_reconciliations (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL, run_id uuid NOT NULL,
        decision text NOT NULL CHECK(decision IN ('recognized_completion','unresolved')),
        observation jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES action_runs(workspace_id,episode_id,id)
    )""",
    "ALTER TABLE evidence_events ADD COLUMN run_id uuid, ADD COLUMN node_id uuid",
    """ALTER TABLE evidence_events ADD CONSTRAINT event_run_scope
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES action_runs(workspace_id,episode_id,id),
        ADD CONSTRAINT event_node_scope FOREIGN KEY(workspace_id,episode_id,node_id)
            REFERENCES search_nodes(workspace_id,episode_id,id),
        ADD CONSTRAINT event_execution_requires_episode
            CHECK((run_id IS NULL AND node_id IS NULL) OR episode_id IS NOT NULL)""",
    """CREATE TABLE information_needs (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL, node_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,node_id) REFERENCES search_nodes(workspace_id,episode_id,id)
    )""",
    """CREATE TABLE need_revisions (
        workspace_id uuid NOT NULL, id uuid NOT NULL, need_id uuid NOT NULL, predecessor_id uuid,
        content jsonb NOT NULL, content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,need_id,id),
        FOREIGN KEY(workspace_id,need_id) REFERENCES information_needs(workspace_id,id),
        FOREIGN KEY(workspace_id,need_id,predecessor_id) REFERENCES need_revisions(workspace_id,need_id,id),
        CHECK(predecessor_id IS NULL OR predecessor_id<>id)
    )""",
    """CREATE TABLE need_revision_evidence (
        workspace_id uuid NOT NULL, revision_id uuid NOT NULL, evidence_id uuid NOT NULL,
        PRIMARY KEY(workspace_id,revision_id,evidence_id),
        FOREIGN KEY(workspace_id,revision_id) REFERENCES need_revisions(workspace_id,id),
        FOREIGN KEY(workspace_id,evidence_id) REFERENCES evidence_events(workspace_id,id)
    )""",
    """CREATE TABLE need_status (
        workspace_id uuid NOT NULL, need_id uuid NOT NULL, current_revision_id uuid NOT NULL,
        status text NOT NULL CHECK(status IN ('open','resolved','blocked','cancelled')),
        update_token bigint NOT NULL DEFAULT 0 CHECK(update_token>=0),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,need_id),
        FOREIGN KEY(workspace_id,need_id,current_revision_id)
            REFERENCES need_revisions(workspace_id,need_id,id)
    )""",
)
