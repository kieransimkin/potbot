"""Append-only baseline, exact-state checks, assessments and patch exports."""

WORKSPACE_TABLES = (
    "verification_profiles",
    "verification_runs",
    "verification_attempts",
    "verification_observations",
    "criterion_assessments",
    "patch_exports",
    "patch_pipelines",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE verification_profiles (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL, base_state_id uuid NOT NULL, environment_id uuid NOT NULL,
        content jsonb NOT NULL, content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,base_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,environment_id) REFERENCES environment_manifests(workspace_id,id)
    )""",
    """CREATE TABLE verification_runs (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        profile_id uuid NOT NULL, contract_revision_id uuid NOT NULL,
        target_state_id uuid NOT NULL, environment_id uuid NOT NULL,
        application_id uuid,
        purpose text NOT NULL CHECK(purpose IN ('baseline','candidate','import')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,profile_id) REFERENCES verification_profiles(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id) REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,target_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,application_id) REFERENCES patch_applications(workspace_id,id),
        FOREIGN KEY(workspace_id,environment_id) REFERENCES environment_manifests(workspace_id,id)
    )""",
    """CREATE TABLE verification_attempts (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        verification_run_id uuid NOT NULL, check_id text NOT NULL, candidate_id uuid NOT NULL,
        action_id uuid NOT NULL, reservation_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,verification_run_id,check_id),
        UNIQUE(workspace_id,action_id),
        FOREIGN KEY(workspace_id,episode_id,verification_run_id) REFERENCES verification_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,candidate_id) REFERENCES candidate_workspaces(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,action_id) REFERENCES actions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,reservation_id) REFERENCES budget_reservations(workspace_id,episode_id,id)
    )""",
    """CREATE TABLE verification_observations (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        verification_run_id uuid NOT NULL, check_id text NOT NULL, attempt_id uuid, execution_run_id uuid,
        status text NOT NULL CHECK(status IN ('passed','failed','blocked','not_run','inconclusive','infrastructure_error')),
        expected_definition_digest text NOT NULL, actual_definition_digest text NOT NULL,
        reason text NOT NULL, details jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,verification_run_id,check_id),
        FOREIGN KEY(workspace_id,episode_id,verification_run_id) REFERENCES verification_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,attempt_id) REFERENCES verification_attempts(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,execution_run_id) REFERENCES action_runs(workspace_id,episode_id,id)
    )""",
    """CREATE TABLE criterion_assessments (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL, application_id uuid NOT NULL,
        baseline_run_id uuid NOT NULL, candidate_run_id uuid NOT NULL,
        content jsonb NOT NULL, content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id) REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,application_id) REFERENCES patch_applications(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,baseline_run_id) REFERENCES verification_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,candidate_run_id) REFERENCES verification_runs(workspace_id,episode_id,id)
    )""",
    """CREATE TABLE patch_exports (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        assessment_id uuid NOT NULL, manifest_artifact_id uuid NOT NULL, patch_artifact_id uuid NOT NULL,
        manifest_digest text NOT NULL CHECK(manifest_digest ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,assessment_id) REFERENCES criterion_assessments(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,manifest_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,patch_artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE TABLE patch_pipelines (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL, base_state_id uuid NOT NULL,
        profile_id uuid NOT NULL, patch_artifact_id uuid NOT NULL, receipt_artifact_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id) REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,base_state_id) REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,profile_id) REFERENCES verification_profiles(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,patch_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,receipt_artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
)
