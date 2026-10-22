"""Migration 15: scoped, replayable Phase 6 search graph and Cog receipts.

Immutable decisions and transformation observations are separated from mutable
frontier projections. Expired claims quarantine rather than replay effects.
"""

WORKSPACE_TABLES = (
    "solve_runs", "solve_views", "solve_nodes", "solve_edges", "solve_frontier",
    "solve_frontier_events", "solve_cog_receipts", "solve_node_contributions",
)
APPEND_ONLY_TABLES = (
    "solve_views", "solve_nodes", "solve_edges", "solve_frontier_events", "solve_cog_receipts", "solve_node_contributions",
)

MIGRATION_STATEMENTS = (
    """CREATE TABLE solve_runs (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        episode_id uuid NOT NULL, contract_revision_id uuid NOT NULL,
        repository_state_id uuid NOT NULL, semantic_release_digest text NOT NULL
            CHECK(semantic_release_digest ~ '^[0-9a-f]{64}$'),
        policy jsonb NOT NULL CHECK(jsonb_typeof(policy)='object'),
        policy_digest text NOT NULL CHECK(policy_digest ~ '^[0-9a-f]{64}$'),
        status text NOT NULL DEFAULT 'ready'
            CHECK(status IN ('ready','running','paused','blocked','completed','cancelled','indeterminate')),
        generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,repository_state_id)
            REFERENCES repository_states(workspace_id,id)
    )""",
    """CREATE TABLE solve_views (
        workspace_id uuid NOT NULL, id uuid NOT NULL, run_id uuid NOT NULL,
        episode_id uuid NOT NULL, parent_id uuid,
        repository_state_id uuid NOT NULL,
        content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
        view_digest text NOT NULL CHECK(view_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id,id),
        UNIQUE(workspace_id,run_id,view_digest),
        FOREIGN KEY(workspace_id,episode_id,run_id)
            REFERENCES solve_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,run_id,parent_id)
            REFERENCES solve_views(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,repository_state_id)
            REFERENCES repository_states(workspace_id,id),
        CHECK(parent_id IS NULL OR parent_id<>id)
    )""",
    """CREATE UNIQUE INDEX solve_one_root_view ON solve_views(workspace_id,run_id)
        WHERE parent_id IS NULL""",
    """CREATE TABLE solve_nodes (
        workspace_id uuid NOT NULL, id uuid NOT NULL, run_id uuid NOT NULL,
        episode_id uuid NOT NULL, view_id uuid NOT NULL,
        parent_id uuid, kind text NOT NULL CHECK(kind IN (
            'root','proposal','information_need','observation','decision','candidate','verification')),
        depth integer NOT NULL CHECK(depth BETWEEN 0 AND 32),
        producer text NOT NULL CHECK(length(producer) BETWEEN 1 AND 256),
        payload_digest text NOT NULL CHECK(payload_digest ~ '^[0-9a-f]{64}$'),
        identity_hash text NOT NULL CHECK(identity_hash ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id,id),
        UNIQUE(workspace_id,run_id,identity_hash),
        FOREIGN KEY(workspace_id,episode_id,run_id)
            REFERENCES solve_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,run_id,view_id)
            REFERENCES solve_views(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,run_id,parent_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        CHECK(parent_id IS NULL OR parent_id<>id),
        CHECK((kind='root' AND depth=0 AND parent_id IS NULL) OR
              (kind<>'root' AND depth>0 AND parent_id IS NOT NULL))
    )""",
    """CREATE UNIQUE INDEX solve_one_root_node ON solve_nodes(workspace_id,run_id)
        WHERE kind='root'""",
    """CREATE TABLE solve_edges (
        workspace_id uuid NOT NULL, run_id uuid NOT NULL,
        source_id uuid NOT NULL, target_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ('derived_from','requires','observes',
            'continues','imports','verifies')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,run_id,source_id,target_id,kind),
        FOREIGN KEY(workspace_id,run_id,source_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,run_id,target_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        CHECK(source_id<>target_id)
    )""",
    """CREATE TABLE solve_frontier (
        workspace_id uuid NOT NULL, run_id uuid NOT NULL, node_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'ready' CHECK(status IN (
            'ready','claimed','finished','quarantined','paused')),
        generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),
        owner_id uuid, lease_until timestamptz,
        outcome_digest text CHECK(outcome_digest ~ '^[0-9a-f]{64}$'),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,run_id,node_id),
        FOREIGN KEY(workspace_id,run_id,node_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        CHECK((status='claimed') = (owner_id IS NOT NULL AND lease_until IS NOT NULL)),
        CHECK(status<>'finished' OR outcome_digest IS NOT NULL)
    )""",
    """CREATE TABLE solve_node_contributions (
        workspace_id uuid NOT NULL, id uuid NOT NULL, run_id uuid NOT NULL,
        node_id uuid NOT NULL, producer text NOT NULL CHECK(length(producer) BETWEEN 1 AND 256),
        evidence_digest text NOT NULL CHECK(evidence_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id,node_id,producer,evidence_digest),
        FOREIGN KEY(workspace_id,run_id,node_id)
            REFERENCES solve_nodes(workspace_id,run_id,id)
    )""",
    """CREATE INDEX solve_frontier_ready ON solve_frontier(workspace_id,run_id,status,updated_at)""",
    """CREATE TABLE solve_frontier_events (
        workspace_id uuid NOT NULL, id uuid NOT NULL,
        run_id uuid NOT NULL, node_id uuid NOT NULL,
        generation bigint NOT NULL CHECK(generation>=0),
        event_type text NOT NULL CHECK(event_type IN (
            'created','claimed','settled','expired','paused','resumed','reconciled')),
        actor_id uuid NOT NULL, detail jsonb NOT NULL CHECK(jsonb_typeof(detail)='object'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,run_id,node_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,actor_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE INDEX solve_frontier_events_run ON solve_frontier_events(workspace_id,run_id,recorded_at,id)""",
    """CREATE TABLE solve_cog_receipts (
        workspace_id uuid NOT NULL, id uuid NOT NULL,
        run_id uuid NOT NULL, node_id uuid NOT NULL,
        invocation_id uuid NOT NULL, step_index integer NOT NULL CHECK(step_index BETWEEN 0 AND 15),
        receipt jsonb NOT NULL CHECK(jsonb_typeof(receipt)='object'),
        receipt_digest text NOT NULL CHECK(receipt_digest ~ '^[0-9a-f]{64}$'),
        input_artifact_id uuid, output_artifact_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,run_id,node_id,invocation_id,step_index),
        UNIQUE(workspace_id,run_id,receipt_digest),
        FOREIGN KEY(workspace_id,run_id,node_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,input_artifact_id)
            REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,output_artifact_id)
            REFERENCES artifacts(workspace_id,id)
    )""",
)
