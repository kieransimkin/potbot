"""Migration 6: immutable repository snapshots and fenced candidate ownership."""

WORKSPACE_TABLES = (
    "repository_states",
    "repository_state_parents",
    "candidate_workspaces",
    "workspace_leases",
    "workspace_reconciliations",
)
APPEND_ONLY_TABLES = (
    "repository_states",
    "repository_state_parents",
    "workspace_reconciliations",
)

MIGRATION_STATEMENTS = (
    """CREATE TABLE repository_states (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        repository_id uuid NOT NULL, environment_id uuid NOT NULL,
        identity_hash text NOT NULL CHECK(identity_hash ~ '^[0-9a-f]{64}$'),
        content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
        snapshot_artifact_id uuid NOT NULL, commit_id text NOT NULL,
        tree_id text NOT NULL, manifest jsonb NOT NULL,
        parent_ids uuid[] NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,repository_id,id),
        UNIQUE(workspace_id,repository_id,identity_hash),
        FOREIGN KEY(workspace_id,repository_id) REFERENCES repositories(workspace_id,id),
        FOREIGN KEY(workspace_id,environment_id) REFERENCES environment_manifests(workspace_id,id),
        FOREIGN KEY(workspace_id,snapshot_artifact_id) REFERENCES artifacts(workspace_id,id),
        CHECK(jsonb_typeof(manifest)='object'), CHECK(cardinality(parent_ids)<=64))""",
    """CREATE FUNCTION check_repository_predecessors() RETURNS trigger
        LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        DECLARE parent uuid;
        BEGIN
            IF NEW.id=ANY(NEW.parent_ids) OR
               cardinality(NEW.parent_ids)<>(SELECT count(DISTINCT p) FROM unnest(NEW.parent_ids) p)
            THEN RAISE EXCEPTION 'invalid repository lineage' USING ERRCODE='23514'; END IF;
            FOREACH parent IN ARRAY NEW.parent_ids LOOP
                IF NOT EXISTS (SELECT 1 FROM repository_states s
                    WHERE s.workspace_id=NEW.workspace_id AND s.repository_id=NEW.repository_id
                      AND s.id=parent)
                THEN RAISE EXCEPTION 'repository predecessor unavailable' USING ERRCODE='23503';
                END IF;
            END LOOP;
            RETURN NEW;
        END; $$""",
    """CREATE TRIGGER repository_predecessors BEFORE INSERT ON repository_states
        FOR EACH ROW EXECUTE FUNCTION check_repository_predecessors()""",
    """CREATE TABLE repository_state_parents (
        workspace_id uuid NOT NULL, repository_id uuid NOT NULL,
        state_id uuid NOT NULL, parent_id uuid NOT NULL,
        PRIMARY KEY(workspace_id,state_id,parent_id), CHECK(state_id<>parent_id),
        FOREIGN KEY(workspace_id,repository_id,state_id)
            REFERENCES repository_states(workspace_id,repository_id,id),
        FOREIGN KEY(workspace_id,repository_id,parent_id)
            REFERENCES repository_states(workspace_id,repository_id,id))""",
    """CREATE FUNCTION check_repository_edge() RETURNS trigger
        LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM repository_states s
                WHERE s.workspace_id=NEW.workspace_id AND s.id=NEW.state_id
                  AND NEW.parent_id=ANY(s.parent_ids))
            THEN RAISE EXCEPTION 'lineage is fixed at state publication' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$""",
    """CREATE TRIGGER repository_edge BEFORE INSERT ON repository_state_parents
        FOR EACH ROW EXECUTE FUNCTION check_repository_edge()""",
    """CREATE FUNCTION check_repository_edges_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
            IF cardinality(NEW.parent_ids)<>(SELECT count(*) FROM repository_state_parents p
                WHERE p.workspace_id=NEW.workspace_id AND p.state_id=NEW.id)
            THEN RAISE EXCEPTION 'repository lineage publication incomplete' USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END; $$""",
    """CREATE CONSTRAINT TRIGGER repository_edges_complete AFTER INSERT ON repository_states
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION check_repository_edges_complete()""",
    """CREATE TABLE candidate_workspaces (
        workspace_id uuid NOT NULL, id uuid NOT NULL, repository_id uuid NOT NULL,
        state_id uuid NOT NULL, root_id uuid NOT NULL, ownership_token uuid NOT NULL,
        status text NOT NULL CHECK(status IN ('ready','leased','quarantined','removed')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,repository_id,id),
        FOREIGN KEY(workspace_id,repository_id,state_id)
            REFERENCES repository_states(workspace_id,repository_id,id))""",
    """CREATE TABLE workspace_leases (
        workspace_id uuid NOT NULL, candidate_id uuid NOT NULL, owner text NOT NULL,
        generation bigint NOT NULL CHECK(generation>0), expires_at timestamptz NOT NULL,
        status text NOT NULL CHECK(status IN ('active','released','quarantined')),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,candidate_id),
        FOREIGN KEY(workspace_id,candidate_id) REFERENCES candidate_workspaces(workspace_id,id),
        CHECK(length(owner) BETWEEN 1 AND 200))""",
    """CREATE TABLE workspace_reconciliations (
        workspace_id uuid NOT NULL, id uuid NOT NULL, candidate_id uuid NOT NULL,
        generation bigint NOT NULL CHECK(generation>0), principal_id uuid NOT NULL,
        observed_digest text NOT NULL CHECK(observed_digest ~ '^[0-9a-f]{64}$'),
        reason text NOT NULL, processes_stopped boolean NOT NULL CHECK(processes_stopped),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,candidate_id) REFERENCES candidate_workspaces(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK(length(reason) BETWEEN 1 AND 2000))""",
)
