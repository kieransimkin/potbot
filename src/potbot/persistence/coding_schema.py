"""Phase 4 coding contracts, scripted results and explicit patch applications."""

WORKSPACE_TABLES = (
    "implementation_contracts",
    "implementation_search_receipts",
    "coding_results",
    "patch_application_intents",
    "patch_applications",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE implementation_contracts (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL, base_state_id uuid NOT NULL,
        content jsonb NOT NULL, content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,base_state_id) REFERENCES repository_states(workspace_id,id)
    )""",
    """CREATE TABLE implementation_search_receipts (
        workspace_id uuid NOT NULL, implementation_id uuid NOT NULL, artifact_id uuid NOT NULL,
        PRIMARY KEY(workspace_id,implementation_id,artifact_id),
        FOREIGN KEY(workspace_id,implementation_id) REFERENCES implementation_contracts(workspace_id,id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE TABLE coding_results (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        implementation_id uuid NOT NULL, base_state_id uuid NOT NULL, deployment_id uuid NOT NULL,
        packet_id uuid NOT NULL, invocation_id uuid NOT NULL, reservation_id uuid NOT NULL,
        patch_artifact_id uuid, inability_code text, content jsonb NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        UNIQUE(workspace_id,invocation_id), UNIQUE(workspace_id,reservation_id),
        CHECK((patch_artifact_id IS NOT NULL)::integer+(inability_code IS NOT NULL)::integer=1),
        FOREIGN KEY(workspace_id,episode_id,implementation_id)
            REFERENCES implementation_contracts(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,base_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id),
        FOREIGN KEY(workspace_id,packet_id,deployment_id) REFERENCES context_packets(workspace_id,id,deployment_id),
        FOREIGN KEY(workspace_id,invocation_id) REFERENCES model_invocations(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,reservation_id)
            REFERENCES budget_reservations(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,patch_artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE TABLE patch_application_intents (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        coding_result_id uuid NOT NULL, implementation_id uuid NOT NULL, candidate_id uuid NOT NULL,
        base_state_id uuid NOT NULL, patch_artifact_id uuid NOT NULL, action_id uuid NOT NULL,
        imported_from text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id), UNIQUE(workspace_id,action_id),
        FOREIGN KEY(workspace_id,episode_id,coding_result_id) REFERENCES coding_results(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,implementation_id)
            REFERENCES implementation_contracts(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,candidate_id) REFERENCES candidate_workspaces(workspace_id,id),
        FOREIGN KEY(workspace_id,base_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,patch_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,action_id) REFERENCES actions(workspace_id,episode_id,id)
    )""",
    """CREATE TABLE patch_applications (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        run_id uuid NOT NULL, result_state_id uuid NOT NULL,
        status text NOT NULL CHECK(status IN ('applied','failed')),
        details jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id),
        FOREIGN KEY(workspace_id,episode_id,id) REFERENCES patch_application_intents(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES action_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,result_state_id) REFERENCES repository_states(workspace_id,id)
    )""",
)
