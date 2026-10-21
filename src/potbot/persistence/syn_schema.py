"""Application migration 6: scoped, atomically published SYN snapshots.

A deferred snapshot-to-seal foreign key prevents partial imports from committing.
Membership inserts require a visible, unsealed snapshot: another transaction cannot
append to an import while its creator publishes it. Sources retain the existing
artifact and named-vector authorities and use snapshot-specific source keys.
"""

WORKSPACE_TABLES = (
    "syn_namespaces",
    "syn_snapshots",
    "syn_types",
    "syn_type_parents",
    "syn_type_ancestry",
    "syn_objects",
    "syn_snapshot_seals",
    "syn_inventory_cursors",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """ALTER TABLE artifacts ADD CONSTRAINT artifacts_syn_digest_scope
        UNIQUE(workspace_id,id,digest)""",
    """ALTER TABLE embedding_sources ADD CONSTRAINT embedding_sources_syn_scope
        UNIQUE(workspace_id,id,repository_id,artifact_id)""",
    """CREATE TABLE syn_namespaces (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        name text COLLATE "C" NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
        repository_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,name),
        UNIQUE(workspace_id,id,repository_id),
        FOREIGN KEY(workspace_id,repository_id) REFERENCES repositories(workspace_id,id))""",
    """CREATE TABLE syn_snapshots (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        namespace_id uuid NOT NULL, repository_id uuid NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        source_hash text NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
        catalog_hash text NOT NULL CHECK(catalog_hash ~ '^[0-9a-f]{64}$'),
        source_label text NOT NULL CHECK(length(source_label) BETWEEN 1 AND 2000),
        catalog_mode text NOT NULL CHECK(catalog_mode IN ('complete','extend_builtin')),
        artifact_id uuid NOT NULL,
        type_count integer NOT NULL CHECK(type_count>=0),
        object_count integer NOT NULL CHECK(object_count BETWEEN 0 AND 100000),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,namespace_id,content_hash),
        UNIQUE(workspace_id,id,repository_id),
        FOREIGN KEY(workspace_id,namespace_id,repository_id)
            REFERENCES syn_namespaces(workspace_id,id,repository_id),
        FOREIGN KEY(workspace_id,artifact_id,content_hash)
            REFERENCES artifacts(workspace_id,id,digest))""",
    """CREATE TABLE syn_types (
        workspace_id uuid NOT NULL, snapshot_id uuid NOT NULL, repository_id uuid NOT NULL,
        ref text COLLATE "C" NOT NULL CHECK(
            ref ~ '^(syn|synny)://type/[a-z0-9]+([-_][a-z0-9]+)*$'
            AND length(split_part(ref,'/',4)) BETWEEN 1 AND 255),
        definition jsonb NOT NULL CHECK(
            jsonb_typeof(definition)='object' AND definition ?& ARRAY['ref','parents']
            AND definition->>'ref'=ref AND jsonb_typeof(definition->'parents')='array'),
        source_id uuid NOT NULL, artifact_id uuid NOT NULL,
        PRIMARY KEY(workspace_id,snapshot_id,ref),
        UNIQUE(workspace_id,snapshot_id,source_id),
        FOREIGN KEY(workspace_id,snapshot_id,repository_id)
            REFERENCES syn_snapshots(workspace_id,id,repository_id),
        FOREIGN KEY(workspace_id,source_id,repository_id,artifact_id)
            REFERENCES embedding_sources(workspace_id,id,repository_id,artifact_id))""",
    """CREATE UNIQUE INDEX syn_types_slug_identity
        ON syn_types(workspace_id,snapshot_id,split_part(ref,'/',4))""",
    """CREATE TABLE syn_type_parents (
        workspace_id uuid NOT NULL, snapshot_id uuid NOT NULL,
        child_ref text COLLATE "C" NOT NULL, parent_ref text COLLATE "C" NOT NULL,
        PRIMARY KEY(workspace_id,snapshot_id,child_ref,parent_ref),
        CHECK(child_ref<>parent_ref),
        FOREIGN KEY(workspace_id,snapshot_id,child_ref)
            REFERENCES syn_types(workspace_id,snapshot_id,ref),
        FOREIGN KEY(workspace_id,snapshot_id,parent_ref)
            REFERENCES syn_types(workspace_id,snapshot_id,ref))""",
    """CREATE TABLE syn_type_ancestry (
        workspace_id uuid NOT NULL, snapshot_id uuid NOT NULL,
        descendant_ref text COLLATE "C" NOT NULL, ancestor_ref text COLLATE "C" NOT NULL,
        PRIMARY KEY(workspace_id,snapshot_id,descendant_ref,ancestor_ref),
        FOREIGN KEY(workspace_id,snapshot_id,descendant_ref)
            REFERENCES syn_types(workspace_id,snapshot_id,ref),
        FOREIGN KEY(workspace_id,snapshot_id,ancestor_ref)
            REFERENCES syn_types(workspace_id,snapshot_id,ref))""",
    """CREATE INDEX syn_ancestry_by_ancestor
        ON syn_type_ancestry(workspace_id,snapshot_id,ancestor_ref,descendant_ref)""",
    """CREATE TABLE syn_objects (
        workspace_id uuid NOT NULL, snapshot_id uuid NOT NULL, repository_id uuid NOT NULL,
        ref text COLLATE "C" NOT NULL,
        kind text NOT NULL CHECK(kind IN (
            'thing','event','ember','cog','credential','enigma','pondle','snug','om')),
        type_ref text COLLATE "C" NOT NULL, source_id uuid NOT NULL, artifact_id uuid NOT NULL,
        CHECK(ref ~ ('^syn://' || kind || '/[a-z0-9]+([-_][a-z0-9]+)*$')
            AND length(substring(ref FROM length(kind)+8)) BETWEEN 1 AND 255),
        PRIMARY KEY(workspace_id,snapshot_id,ref),
        UNIQUE(workspace_id,snapshot_id,source_id),
        FOREIGN KEY(workspace_id,snapshot_id,repository_id)
            REFERENCES syn_snapshots(workspace_id,id,repository_id),
        FOREIGN KEY(workspace_id,snapshot_id,type_ref)
            REFERENCES syn_types(workspace_id,snapshot_id,ref),
        FOREIGN KEY(workspace_id,source_id,repository_id,artifact_id)
            REFERENCES embedding_sources(workspace_id,id,repository_id,artifact_id))""",
    """CREATE INDEX syn_objects_by_type
        ON syn_objects(workspace_id,snapshot_id,type_ref,ref)""",
    """CREATE TABLE syn_snapshot_seals (
        workspace_id uuid NOT NULL, snapshot_id uuid NOT NULL,
        published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,snapshot_id),
        FOREIGN KEY(workspace_id,snapshot_id) REFERENCES syn_snapshots(workspace_id,id))""",
    """ALTER TABLE syn_snapshots ADD CONSTRAINT syn_snapshot_publication
        FOREIGN KEY(workspace_id,id) REFERENCES syn_snapshot_seals(workspace_id,snapshot_id)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE syn_inventory_cursors (
        workspace_id uuid NOT NULL, id uuid NOT NULL, snapshot_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        query_hash text NOT NULL CHECK(query_hash ~ '^[0-9a-f]{64}$'),
        last_ref text COLLATE "C",
        delivered_count integer NOT NULL CHECK(delivered_count>=0),
        total_count integer NOT NULL CHECK(total_count>=delivered_count),
        page_size integer NOT NULL CHECK(page_size BETWEEN 1 AND 1000),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK((last_ref IS NULL)=(delivered_count=0)),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,snapshot_id)
            REFERENCES syn_snapshot_seals(workspace_id,snapshot_id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,snapshot_id,last_ref)
            REFERENCES syn_objects(workspace_id,snapshot_id,ref))""",
    """CREATE POLICY syn_cursor_principal ON syn_inventory_cursors AS RESTRICTIVE
        USING (principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE FUNCTION guard_syn_membership() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE snapshot_row record; source_row record;
        BEGIN
            SELECT namespace_id,repository_id,content_hash INTO snapshot_row
                FROM syn_snapshots WHERE workspace_id=NEW.workspace_id AND id=NEW.snapshot_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'snapshot is not accessible' USING ERRCODE='23503';
            END IF;
            IF EXISTS (SELECT 1 FROM syn_snapshot_seals
                WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id) THEN
                RAISE EXCEPTION 'published snapshot membership is immutable' USING ERRCODE='55000';
            END IF;
            IF TG_TABLE_NAME IN ('syn_types','syn_objects') THEN
                SELECT source_key,metadata INTO source_row FROM embedding_sources
                    WHERE workspace_id=NEW.workspace_id AND id=NEW.source_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'source is not accessible' USING ERRCODE='23503';
                END IF;
                IF source_row.source_key IS DISTINCT FROM
                    ('syn:' || snapshot_row.namespace_id::text || ':' || NEW.snapshot_id::text
                    || ':' || NEW.ref)
                    OR source_row.metadata->>'source_url' IS DISTINCT FROM NEW.ref
                    OR source_row.metadata->>'revision' IS DISTINCT FROM snapshot_row.content_hash
                THEN
                    RAISE EXCEPTION 'source provenance does not match snapshot' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN NEW;
        END; $$""",
    """CREATE FUNCTION validate_syn_publication() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE snapshot_row record;
        BEGIN
            SELECT type_count,object_count INTO snapshot_row FROM syn_snapshots
                WHERE workspace_id=NEW.workspace_id AND id=NEW.snapshot_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'snapshot is not accessible' USING ERRCODE='23503';
            END IF;
            IF snapshot_row.type_count<>(SELECT count(*) FROM syn_types
                WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id)
                OR snapshot_row.object_count<>(SELECT count(*) FROM syn_objects
                WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id) THEN
                RAISE EXCEPTION 'snapshot member counts do not match' USING ERRCODE='23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM syn_types t
                WHERE t.workspace_id=NEW.workspace_id AND t.snapshot_id=NEW.snapshot_id
                AND (SELECT array_agg(p.parent_ref ORDER BY p.parent_ref COLLATE "C")
                    FROM syn_type_parents p WHERE p.workspace_id=t.workspace_id
                    AND p.snapshot_id=t.snapshot_id AND p.child_ref=t.ref)
                    IS DISTINCT FROM
                    (SELECT array_agg(value ORDER BY value COLLATE "C")
                    FROM jsonb_array_elements_text(t.definition->'parents'))
            ) THEN
                RAISE EXCEPTION 'type definition parents do not match edges' USING ERRCODE='23514';
            END IF;
            IF EXISTS (
                WITH RECURSIVE reach(descendant_ref,ancestor_ref) AS (
                    SELECT child_ref,parent_ref FROM syn_type_parents
                        WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id
                    UNION
                    SELECT r.descendant_ref,p.parent_ref FROM reach r JOIN syn_type_parents p
                        ON p.child_ref=r.ancestor_ref AND p.workspace_id=NEW.workspace_id
                        AND p.snapshot_id=NEW.snapshot_id
                ) SELECT 1 FROM reach WHERE descendant_ref=ancestor_ref
            ) THEN
                RAISE EXCEPTION 'type parent graph contains a cycle' USING ERRCODE='23514';
            END IF;
            IF EXISTS (
                WITH RECURSIVE expected(descendant_ref,ancestor_ref) AS (
                    SELECT ref,ref FROM syn_types
                        WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id
                    UNION
                    SELECT e.descendant_ref,p.parent_ref FROM expected e JOIN syn_type_parents p
                        ON p.child_ref=e.ancestor_ref AND p.workspace_id=NEW.workspace_id
                        AND p.snapshot_id=NEW.snapshot_id
                ), actual AS (
                    SELECT descendant_ref,ancestor_ref FROM syn_type_ancestry
                        WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id
                ) SELECT 1 FROM (
                    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
                    UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)
                ) differences
            ) THEN
                RAISE EXCEPTION 'type ancestry is not the exact closure' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$""",
    # Pin the installation's trusted schema after pg_catalog, with pg_temp last.
    # This supports non-default application schemas without allowing temporary
    # relations to impersonate the tables read by an insert guard.
    """DO $$ DECLARE application_schema text := current_schema(); BEGIN
        EXECUTE format('ALTER FUNCTION guard_syn_membership() SET search_path = pg_catalog, %I, pg_temp',
            application_schema);
        EXECUTE format('ALTER FUNCTION validate_syn_publication() SET search_path = pg_catalog, %I, pg_temp',
            application_schema);
        END; $$""",
    *(
        f"CREATE TRIGGER syn_membership_guard BEFORE INSERT ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION guard_syn_membership()"
        for table in (
            "syn_types",
            "syn_type_parents",
            "syn_type_ancestry",
            "syn_objects",
            "syn_snapshot_seals",
        )
    ),
    """CREATE TRIGGER syn_publication_validation BEFORE INSERT ON syn_snapshot_seals
        FOR EACH ROW EXECUTE FUNCTION validate_syn_publication()""",
)
