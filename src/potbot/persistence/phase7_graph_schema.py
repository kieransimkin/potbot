"""Migration 18: immutable provenance links for audited research and solve graphs.

A link records a source observation, not a patch verification, activated Cog or
resolved information need. Historical migrations remain byte-for-byte unchanged.
"""

WORKSPACE_TABLES = ("research_solve_links",)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE research_solve_links (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        episode_id uuid NOT NULL,
        research_run_id uuid NOT NULL,
        solve_run_id uuid NOT NULL,
        parent_node_id uuid NOT NULL,
        node_id uuid NOT NULL,
        view_id uuid NOT NULL,
        evidence_event_id uuid NOT NULL,
        source_receipt_digest text NOT NULL CHECK(source_receipt_digest ~ '^[0-9a-f]{64}$'),
        link_digest text NOT NULL CHECK(link_digest ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL CHECK(COALESCE(
            jsonb_typeof(content)='object'
            AND content->>'schema_version'='1'
            AND content->>'primary_type_ref'='synny://type/potbot-research-solve-link'
            AND content->>'research_run_id'=research_run_id::text
            AND content->>'solve_run_id'=solve_run_id::text
            AND content->>'parent_node_id'=parent_node_id::text
            AND content->>'observation_node_id'=node_id::text
            AND content->>'knowledge_view_id'=view_id::text
            AND content->>'source_event_id'=evidence_event_id::text
            AND content->>'source_receipt_digest'=source_receipt_digest
            AND content->>'qualification'='verified_immutable_repository_source_only'
            AND content->>'patch_verified'='false'
            AND content->>'need_resolved'='false'
            AND content->>'model_dispatched'='false', false)),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,solve_run_id,research_run_id),
        UNIQUE(workspace_id,solve_run_id,node_id),
        FOREIGN KEY(workspace_id,episode_id,research_run_id)
            REFERENCES research_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,episode_id,solve_run_id)
            REFERENCES solve_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,solve_run_id,parent_node_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,solve_run_id,node_id)
            REFERENCES solve_nodes(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,solve_run_id,view_id)
            REFERENCES solve_views(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,evidence_event_id)
            REFERENCES evidence_events(workspace_id,id),
        CHECK(parent_node_id <> node_id)
    )""",
    """CREATE INDEX research_solve_links_task ON research_solve_links
        (workspace_id,episode_id,solve_run_id,created_at,id)""",
)
