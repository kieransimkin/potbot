"""Migration 16: immutable scoped research queries, lineage and source observations.

Admission of a query is not permission to dispatch a tool. Research effects
need a separate budgeted executor and a retained source snapshot/artifact.
"""

WORKSPACE_TABLES = ("research_actions", "research_observations")
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE research_actions (
        workspace_id uuid NOT NULL, id uuid NOT NULL,
        episode_id uuid NOT NULL, need_id uuid NOT NULL,
        need_revision_id uuid NOT NULL, contract_revision_id uuid NOT NULL,
        repository_state_id uuid NOT NULL,
        predecessor_id uuid,
        identity_digest text NOT NULL CHECK(identity_digest ~ '^[0-9a-f]{64}$'),
        payload_digest text NOT NULL CHECK(payload_digest ~ '^[0-9a-f]{64}$'),
        source_category text NOT NULL CHECK(source_category IN (
            'repository','documentation','example_code','mcp')),
        operation text NOT NULL CHECK(operation IN (
            'read_file','find_literal','search_example_code','query_source')),
        content jsonb NOT NULL CHECK(COALESCE(
            jsonb_typeof(content)='object'
            AND content->>'schema_version'='1'
            AND content->>'primary_type_ref'='synny://type/potbot-research-action'
            AND content->>'action_id'=id::text
            AND content->'scope'->>'workspace_id'=workspace_id::text
            AND content->'scope'->>'task_id'=episode_id::text
            AND content->'scope'->>'need_id'=need_id::text
            AND content->'scope'->>'need_revision_id'=need_revision_id::text
            AND content->'scope'->>'contract_revision_id'=contract_revision_id::text
            AND content->'scope'->>'repository_state_id'=repository_state_id::text
            AND content->'query'->>'category'=source_category
            AND content->'query'->>'operation'=operation, false)),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,episode_id,id),
        UNIQUE(workspace_id,episode_id,identity_digest),
        FOREIGN KEY(workspace_id,episode_id,need_id)
            REFERENCES information_needs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,need_id,need_revision_id)
            REFERENCES need_revisions(workspace_id,need_id,id),
        FOREIGN KEY(workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,repository_state_id)
            REFERENCES repository_states(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id,predecessor_id)
            REFERENCES research_actions(workspace_id,episode_id,id),
        CHECK(predecessor_id IS NULL OR predecessor_id<>id)
    )""",
    """CREATE INDEX research_actions_need ON research_actions
        (workspace_id,episode_id,need_id,created_at,id)""",
    """CREATE TABLE research_observations (
        workspace_id uuid NOT NULL, id uuid NOT NULL,
        episode_id uuid NOT NULL, action_id uuid NOT NULL,
        source_artifact_id uuid NOT NULL,
        observation_digest text NOT NULL CHECK(observation_digest ~ '^[0-9a-f]{64}$'),
        status text NOT NULL CHECK(status IN (
            'inspected','no_match','not_found','non_text','over_limit','unavailable')),
        content jsonb NOT NULL CHECK(COALESCE(
            jsonb_typeof(content)='object'
            AND content->>'schema_version'='1'
            AND content->>'primary_type_ref'='synny://type/potbot-research-observation'
            AND content->>'action_id'=action_id::text
            AND content->>'status'=status, false)),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,episode_id,action_id,observation_digest),
        FOREIGN KEY(workspace_id,episode_id,action_id)
            REFERENCES research_actions(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,source_artifact_id)
            REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE INDEX research_observations_action ON research_observations
        (workspace_id,episode_id,action_id,created_at,id)""",
)
