"""Migration 33: context follows authoritative object registration, not UI usage.

Only sealed public Syn objects and the explicit root-object inventory below
participate. No arbitrary table scan, source payload, credential or code is
copied into Markdown. Existing context revisions are never reset.

The existing migration-owner role owns narrowly scoped trigger functions.
Runtime logins cannot execute those functions or write the source index.
"""

WORKSPACE_TABLES = ("workspace_context_sources", "workspace_context_source_events",
                    "workspace_context_runtime_sources", "workspace_context_work_results")
APPEND_ONLY_TABLES = ("workspace_context_source_events", "workspace_context_runtime_sources", "workspace_context_work_results")

# This is an audited coverage manifest, not a heuristic over every table.
SOURCE_TABLES = (
    "repositories", "episodes", "model_catalog", "actions",
    "execution_run_contracts", "syn_namespaces", "workspace_sandboxes",
)

MIGRATION_STATEMENTS = (
    # FORCE RLS already requires the installation owner to bypass it for existing
    # administrative backfills. Make the new trigger custody requirement explicit.
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
            WHERE rolname=current_user AND (rolsuper OR rolbypassrls)) THEN
            RAISE EXCEPTION 'context lifecycle migration requires the existing RLS-bypassing installation owner'
                USING ERRCODE='42501';
        END IF;
    END; $$""",
    """CREATE TABLE workspace_context_sources (
        workspace_id uuid NOT NULL,
        object_id uuid NOT NULL,
        source_family text NOT NULL CHECK (source_family IN (
            'repository','task','model','tool','execution','namespace','syn','operator','gateway')),
        source_key text COLLATE "C" NOT NULL CHECK (length(source_key) BETWEEN 1 AND 1024),
        state text NOT NULL CHECK (state IN ('active','retired')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,object_id),
        UNIQUE(workspace_id,source_family,source_key),
        FOREIGN KEY(workspace_id,object_id) REFERENCES workspace_context_objects(workspace_id,id)
    )""",
    """CREATE TABLE workspace_context_source_events (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        object_id uuid NOT NULL,
        state text NOT NULL CHECK (state IN ('active','retired')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,object_id) REFERENCES workspace_context_sources(workspace_id,object_id)
    )""",
    "CREATE INDEX context_sources_page ON workspace_context_sources(workspace_id,state,object_id)",
    *tuple(
        f"""CREATE POLICY context_source_custody ON {table} AS RESTRICTIVE
            USING (EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id={table}.workspace_id AND o.id={table}.object_id))
            WITH CHECK (false)"""
        for table in ("workspace_context_sources", "workspace_context_source_events")
    ),
    """CREATE FUNCTION observe_context_source(
        ws uuid, family text, source_key_value text, object_kind text,
        object_owner uuid, source_state text DEFAULT 'active'
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER AS $$
    DECLARE
        identity uuid;
        object_name text;
        row_object record;
        doc_role text;
        doc_content text;
        digest_value text;
        made boolean;
        changed uuid;
    BEGIN
        IF family NOT IN ('repository','task','model','tool','execution','namespace',
                          'syn','operator','gateway')
            OR object_kind NOT IN ('primitive','model','tool','worker','operator','gateway')
            OR length(source_key_value) NOT BETWEEN 1 AND 1024
            OR source_state NOT IN ('active','retired')
            OR ws IS NULL THEN
            RAISE EXCEPTION 'invalid context source' USING ERRCODE='23514';
        END IF;
        identity := substr(encode(sha256(convert_to('potbot-context-source/v1:' || ws::text || ':' || family || ':' || source_key_value,'UTF8')),'hex'),1,32)::uuid;
        object_name := 'source.' || family || '.' || replace(identity::text,'-','');
        INSERT INTO workspace_context_objects(workspace_id,id,kind,name,visibility,owner_principal_id)
            VALUES (ws,identity,object_kind,object_name,
                CASE WHEN object_owner IS NULL THEN 'shared' ELSE 'private' END,object_owner)
            ON CONFLICT DO NOTHING;
        SELECT kind,name,owner_principal_id INTO row_object FROM workspace_context_objects
            WHERE workspace_id=ws AND id=identity;
        IF NOT FOUND OR row_object.kind IS DISTINCT FROM object_kind
            OR row_object.name IS DISTINCT FROM object_name
            OR row_object.owner_principal_id IS DISTINCT FROM object_owner THEN
            RAISE EXCEPTION 'context source identity collision' USING ERRCODE='55000';
        END IF;
        FOREACH doc_role IN ARRAY ARRAY['AGENTS.md','MODEL.md','MEMORY.md'] LOOP
            doc_content := CASE doc_role
                WHEN 'AGENTS.md' THEN '# Instructions: ' || object_kind || ':' || object_name
                    || E'\n\nNo object-specific instructions configured.\n'
                WHEN 'MODEL.md' THEN '# Working model: ' || object_kind || ':' || object_name
                    || E'\n\n## Observed state\n\nUnknown.\n\n## Uncertainties\n\nDiscovery pending.\n'
                ELSE '# History: ' || object_kind || ':' || object_name
                    || E'\n\nNo observations recorded.\n' END;
            digest_value := encode(sha256(convert_to(doc_content,'UTF8')),'hex');
            INSERT INTO workspace_context_documents(workspace_id,object_id,role,content,revision,sha256)
                VALUES(ws,identity,doc_role,doc_content,1,digest_value)
                ON CONFLICT DO NOTHING RETURNING true INTO made;
            IF made IS TRUE THEN
                INSERT INTO workspace_context_revisions
                    (workspace_id,object_id,role,revision,content,sha256,actor,reason)
                    VALUES(ws,identity,doc_role,1,doc_content,digest_value,
                           'operator:lifecycle','authoritative object registration');
                INSERT INTO workspace_context_events
                    (workspace_id,object_id,role,revision,actor,event_type,payload)
                    VALUES(ws,identity,doc_role,1,'operator:lifecycle','document.provisioned',
                           jsonb_build_object('sha256',digest_value));
            END IF;
            made := false;
        END LOOP;
        INSERT INTO workspace_context_sources(workspace_id,object_id,source_family,source_key,state)
            VALUES(ws,identity,family,source_key_value,source_state)
            ON CONFLICT(workspace_id,object_id) DO UPDATE
                SET state=EXCLUDED.state,changed_at=clock_timestamp()
                WHERE workspace_context_sources.state IS DISTINCT FROM EXCLUDED.state
            RETURNING object_id INTO changed;
        IF changed IS NOT NULL THEN
            INSERT INTO workspace_context_source_events(workspace_id,object_id,state)
                VALUES(ws,identity,source_state);
        END IF;
        RETURN identity;
    END; $$""",
    """CREATE FUNCTION context_registered_source() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER AS $$
    DECLARE
        body jsonb;
        ws uuid;
        state_value text := CASE WHEN TG_OP='DELETE' THEN 'retired' ELSE 'active' END;
    BEGIN
        IF TG_OP='UPDATE' AND (
            to_jsonb(OLD)->>'workspace_id' IS DISTINCT FROM to_jsonb(NEW)->>'workspace_id'
            OR to_jsonb(OLD)->>'id' IS DISTINCT FROM to_jsonb(NEW)->>'id'
            OR to_jsonb(OLD)->>'run_id' IS DISTINCT FROM to_jsonb(NEW)->>'run_id') THEN
            RAISE EXCEPTION 'registered context source identity is immutable' USING ERRCODE='55000';
        END IF;
        body := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        ws := (body->>'workspace_id')::uuid;
        IF TG_RELID='repositories'::regclass THEN
            PERFORM observe_context_source(ws,'repository',body->>'id','primitive',NULL,state_value);
        ELSIF TG_RELID='episodes'::regclass THEN
            PERFORM observe_context_source(ws,'task',body->>'id','primitive',NULL,state_value);
        ELSIF TG_RELID='model_catalog'::regclass THEN
            PERFORM observe_context_source(ws,'model',body->>'id','model',NULL,state_value);
        ELSIF TG_RELID='actions'::regclass THEN
            -- A tool's memory survives individual invocations and version changes.
            -- Removing a historical action is not uninstalling that tool.
            IF TG_OP<>'DELETE' THEN
                PERFORM observe_context_source(ws,'tool',body->>'kind','tool',NULL);
            END IF;
        ELSIF TG_RELID='execution_run_contracts'::regclass THEN
            PERFORM observe_context_source(ws,'execution',body->>'run_id','worker',
                (body->>'owner_principal_id')::uuid,state_value);
        ELSIF TG_RELID='syn_namespaces'::regclass THEN
            PERFORM observe_context_source(ws,'namespace',body->>'id','primitive',NULL,state_value);
        ELSIF TG_RELID='workspace_sandboxes'::regclass THEN
            PERFORM observe_context_source(ws,'operator',ws::text,'operator',NULL,state_value);
            PERFORM observe_context_source(ws,'gateway','tool-gateway','gateway',NULL,state_value);
        ELSE
            RAISE EXCEPTION 'unapproved context source trigger' USING ERRCODE='42501';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END; $$""",
    """CREATE FUNCTION context_sealed_syn() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER AS $$
    DECLARE ns uuid; member record;
    BEGIN
        IF TG_RELID<>'syn_snapshot_seals'::regclass OR TG_OP<>'INSERT' THEN
            RAISE EXCEPTION 'unapproved context publication trigger' USING ERRCODE='42501';
        END IF;
        SELECT namespace_id INTO STRICT ns FROM syn_snapshots
            WHERE workspace_id=NEW.workspace_id AND id=NEW.snapshot_id;
        FOR member IN
            SELECT ref FROM syn_types WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id
            UNION ALL
            SELECT ref FROM syn_objects WHERE workspace_id=NEW.workspace_id AND snapshot_id=NEW.snapshot_id
        LOOP
            PERFORM observe_context_source(NEW.workspace_id,'syn',
                ns::text || ':' || regexp_replace(member.ref,'^synny:','syn:'),'primitive',NULL);
        END LOOP;
        RETURN NEW;
    END; $$""",
    """CREATE TABLE workspace_context_runtime_sources (
        revision bigint NOT NULL CHECK(revision>=1),
        workspace_id uuid NOT NULL,
        object_id uuid NOT NULL,
        owner_principal_id uuid NOT NULL,
        kind text NOT NULL CHECK (kind IN ('tool','workflow','gateway','model','connector','operator','primitive')),
        PRIMARY KEY(workspace_id,object_id,revision),
        reference text NOT NULL CHECK (length(reference) BETWEEN 1 AND 512 AND reference ~ '^[!-~]+$'),
        fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
        state text NOT NULL CHECK (state IN ('active','retired')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(workspace_id,object_id) REFERENCES workspace_context_objects(workspace_id,id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "CREATE INDEX context_runtime_versions ON workspace_context_runtime_sources(workspace_id,object_id,revision DESC)",
    """CREATE POLICY runtime_context_owner ON workspace_context_runtime_sources AS RESTRICTIVE
        USING (owner_principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (owner_principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid
            AND EXISTS (SELECT 1 FROM workspace_context_objects o
                WHERE o.workspace_id=workspace_context_runtime_sources.workspace_id
                  AND o.id=workspace_context_runtime_sources.object_id
                  AND o.kind=workspace_context_runtime_sources.kind AND o.visibility='private'
                  AND o.owner_principal_id=workspace_context_runtime_sources.owner_principal_id
                  AND o.name='runtime.' || replace(o.owner_principal_id::text,'-','') || '.' ||
                    encode(sha256(convert_to(workspace_context_runtime_sources.kind || ':' ||
                        workspace_context_runtime_sources.reference,'UTF8')),'hex')))""",
    """CREATE TABLE workspace_context_work_results (
        workspace_id uuid NOT NULL,
        session_id uuid NOT NULL,
        request_id uuid NOT NULL,
        request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
        result_json text NOT NULL CHECK (octet_length(result_json)<=16384 AND jsonb_typeof(result_json::jsonb)='object'),
        result_sha256 text NOT NULL CHECK (result_sha256=encode(sha256(convert_to(result_json,'UTF8')),'hex')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,session_id),
        UNIQUE(workspace_id,session_id,request_id),
        FOREIGN KEY(workspace_id,session_id) REFERENCES workspace_context_sessions(workspace_id,id)
    )""",
    """CREATE POLICY context_work_owner ON workspace_context_work_results AS RESTRICTIVE
        USING (EXISTS (SELECT 1 FROM workspace_context_sessions s
            WHERE s.workspace_id=workspace_context_work_results.workspace_id
              AND s.id=workspace_context_work_results.session_id))
        WITH CHECK (EXISTS (SELECT 1 FROM workspace_context_sessions s
            WHERE s.workspace_id=workspace_context_work_results.workspace_id
              AND s.id=workspace_context_work_results.session_id
              AND s.state='active' AND s.expires_at>clock_timestamp()))""",
    """ALTER TABLE workspace_context_sessions ADD COLUMN result_request_id uuid,
        ADD CONSTRAINT context_work_result_link FOREIGN KEY(workspace_id,id,result_request_id)
        REFERENCES workspace_context_work_results(workspace_id,session_id,request_id)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE FUNCTION check_context_work_result() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog AS $$ BEGIN
        IF OLD.result_request_id IS NOT NULL AND NEW.result_request_id IS DISTINCT FROM OLD.result_request_id THEN
            RAISE EXCEPTION 'work result is immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END; $$""",
    """CREATE TRIGGER context_work_result_guard BEFORE UPDATE ON workspace_context_sessions
        FOR EACH ROW EXECUTE FUNCTION check_context_work_result()""",
    # No runtime can directly invoke registration or attach these functions to a
    # temporary table. Only the migration owner installs these exact triggers.
    "REVOKE ALL ON FUNCTION observe_context_source(uuid,text,text,text,uuid,text) FROM PUBLIC",
    "REVOKE ALL ON FUNCTION context_registered_source() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION context_sealed_syn() FROM PUBLIC",
    """DO $$ DECLARE installed text := current_schema(); BEGIN
        EXECUTE format('ALTER FUNCTION observe_context_source(uuid,text,text,text,uuid,text) '
            'SET search_path = pg_catalog, %I, pg_temp',installed);
        EXECUTE format('ALTER FUNCTION context_registered_source() '
            'SET search_path = pg_catalog, %I, pg_temp',installed);
        EXECUTE format('ALTER FUNCTION context_sealed_syn() '
            'SET search_path = pg_catalog, %I, pg_temp',installed);
    END; $$""",
    *tuple(
        f"CREATE TRIGGER context_object_lifecycle AFTER INSERT OR UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION context_registered_source()"
        for table in SOURCE_TABLES
    ),
    "CREATE TRIGGER context_syn_lifecycle AFTER INSERT ON syn_snapshot_seals "
    "FOR EACH ROW EXECUTE FUNCTION context_sealed_syn()",
    # Backfill does not UPDATE source records or historical migration checksums.
    *tuple(
        f"SELECT observe_context_source(workspace_id,'{family}',{key},'{kind}',{owner}) FROM {table}"
        for table, family, key, kind, owner in (
            ("repositories", "repository", "id::text", "primitive", "NULL::uuid"),
            ("episodes", "task", "id::text", "primitive", "NULL::uuid"),
            ("model_catalog", "model", "id::text", "model", "NULL::uuid"),
            ("actions", "tool", "kind", "tool", "NULL::uuid"),
            ("execution_run_contracts", "execution", "run_id::text", "worker", "owner_principal_id"),
            ("syn_namespaces", "namespace", "id::text", "primitive", "NULL::uuid"),
            ("workspace_sandboxes", "operator", "workspace_id::text", "operator", "NULL::uuid"),
            ("workspace_sandboxes", "gateway", "'tool-gateway'", "gateway", "NULL::uuid"),
        )
    ),
    """SELECT observe_context_source(m.workspace_id,'syn',
            s.namespace_id::text || ':' || regexp_replace(m.ref,'^synny:','syn:'),'primitive',NULL)
        FROM (SELECT workspace_id,snapshot_id,ref FROM syn_types
              UNION ALL SELECT workspace_id,snapshot_id,ref FROM syn_objects) m
        JOIN syn_snapshots s ON s.workspace_id=m.workspace_id AND s.id=m.snapshot_id
        JOIN syn_snapshot_seals seal ON seal.workspace_id=s.workspace_id AND seal.snapshot_id=s.id""",
)
