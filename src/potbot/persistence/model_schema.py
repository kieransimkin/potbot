"""Immutable configuration, deployment, invocation, and initial vector records."""

WORKSPACE_TABLES = (
    "resolved_configurations",
    "environment_manifests",
    "model_catalog",
    "model_deployments",
    "deployment_health",
    "deployment_qualifications",
    "context_packets",
    "context_packet_sources",
    "model_invocations",
    "embedding_spaces",
    "embedding_sources",
    "embedding_source_heads",
    "embedding_vectors",
)
APPEND_ONLY_TABLES = tuple(
    t
    for t in WORKSPACE_TABLES
    if t not in {"deployment_health", "deployment_qualifications", "embedding_source_heads"}
)
MIGRATION_STATEMENTS = (
    """CREATE TABLE resolved_configurations (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        episode_id uuid, content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,episode_id),
        FOREIGN KEY(workspace_id,episode_id) REFERENCES episodes(workspace_id,id))""",
    """CREATE TABLE environment_manifests (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        configuration_id uuid NOT NULL, content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,content_hash),
        FOREIGN KEY(workspace_id,configuration_id) REFERENCES resolved_configurations(workspace_id,id))""",
    """CREATE TABLE model_catalog (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        canonical_model text NOT NULL, role text NOT NULL CHECK(role IN ('reasoning','coding')),
        qualification text NOT NULL DEFAULT 'unqualified' CHECK(qualification='unqualified'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,canonical_model))""",
    """CREATE TABLE model_deployments (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        canonical_model text NOT NULL, content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,content_hash))""",
    """CREATE TABLE deployment_health (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        deployment_id uuid NOT NULL, health text NOT NULL CHECK(health IN ('unknown','healthy','unavailable','degraded')),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,deployment_id), FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id))""",
    """CREATE TABLE deployment_qualifications (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        deployment_id uuid NOT NULL, qualified boolean NOT NULL DEFAULT false,
        qualification_event_id uuid NOT NULL, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,deployment_id),
        FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id),
        FOREIGN KEY(workspace_id,qualification_event_id) REFERENCES evidence_events(workspace_id,id))""",
    """CREATE TABLE context_packets (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL, episode_id uuid,
        deployment_id uuid NOT NULL, artifact_id uuid NOT NULL, content_hash text NOT NULL,
        content jsonb NOT NULL, token_count integer NOT NULL CHECK(token_count>=0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,deployment_id),
        FOREIGN KEY(workspace_id,episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id))""",
    """CREATE TABLE context_packet_sources (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        packet_id uuid NOT NULL, ordinal integer NOT NULL CHECK(ordinal>=0), artifact_id uuid,event_id uuid,
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,packet_id,ordinal),
        CHECK((artifact_id IS NOT NULL)::integer+(event_id IS NOT NULL)::integer=1),
        FOREIGN KEY(workspace_id,packet_id) REFERENCES context_packets(workspace_id,id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,event_id) REFERENCES evidence_events(workspace_id,id))""",
    """CREATE TABLE model_invocations (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        deployment_id uuid NOT NULL, context_packet_id uuid NOT NULL,
        input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'), input_manifest jsonb NOT NULL,
        raw_output_artifact_id uuid NOT NULL, parsed_result jsonb, input_tokens bigint NOT NULL CHECK(input_tokens>=0),
        output_tokens bigint NOT NULL CHECK(output_tokens>=0), elapsed_ms bigint NOT NULL CHECK(elapsed_ms>=0),
        stop_reason text NOT NULL, parse_status text NOT NULL CHECK(parse_status IN ('parsed','invalid','partial','not_attempted')),
        status text NOT NULL CHECK(status IN ('succeeded','failed','cancelled','timed_out','indeterminate')),
        error_code text, usage_origin text NOT NULL CHECK(usage_origin IN ('synthetic','measured','provider_reported')),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(workspace_id,id),
        CHECK((status='succeeded' AND parse_status='parsed' AND parsed_result IS NOT NULL AND error_code IS NULL) OR (status<>'succeeded' AND error_code IS NOT NULL)),
        FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id),
        FOREIGN KEY(workspace_id,context_packet_id,deployment_id) REFERENCES context_packets(workspace_id,id,deployment_id),
        FOREIGN KEY(workspace_id,raw_output_artifact_id) REFERENCES artifacts(workspace_id,id))""",
    """CREATE TABLE embedding_spaces (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'), content jsonb NOT NULL,
        dimensions integer NOT NULL CHECK(dimensions=1024), metric text NOT NULL CHECK(metric='cosine'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,content_hash))""",
    """CREATE TABLE embedding_sources (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        repository_id uuid NOT NULL, source_key text NOT NULL, artifact_id uuid NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'), content text NOT NULL,
        metadata jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,repository_id,source_key,content_hash),
        UNIQUE(workspace_id,repository_id,source_key,id), UNIQUE(workspace_id,id,content_hash),
        FOREIGN KEY(workspace_id,repository_id) REFERENCES repositories(workspace_id,id),
        FOREIGN KEY(workspace_id,artifact_id) REFERENCES artifacts(workspace_id,id))""",
    """CREATE TABLE embedding_source_heads (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        repository_id uuid NOT NULL, source_key text NOT NULL, source_id uuid,
        generation bigint NOT NULL CHECK(generation>0), active boolean NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,repository_id,source_key), CHECK(active=(source_id IS NOT NULL)),
        FOREIGN KEY(workspace_id,repository_id) REFERENCES repositories(workspace_id,id),
        FOREIGN KEY(workspace_id,repository_id,source_key,source_id) REFERENCES embedding_sources(workspace_id,repository_id,source_key,id))""",
    """CREATE TABLE embedding_vectors (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        source_id uuid NOT NULL, space_id uuid NOT NULL, embedding public.vector(1024) NOT NULL CHECK(abs(public.vector_norm(embedding)-1)<0.0001),
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        reservation_id uuid NOT NULL, usage_origin text NOT NULL CHECK(usage_origin IN ('synthetic','measured','provider_reported')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,source_id,space_id),
        FOREIGN KEY(workspace_id,source_id,content_hash) REFERENCES embedding_sources(workspace_id,id,content_hash),
        FOREIGN KEY(workspace_id,space_id) REFERENCES embedding_spaces(workspace_id,id),
        FOREIGN KEY(workspace_id,reservation_id) REFERENCES budget_reservations(workspace_id,id))""",
    "CREATE INDEX embedding_vectors_space ON embedding_vectors(workspace_id,space_id,source_id)",
    "ALTER TABLE evidence_events ADD CONSTRAINT event_deployment_scope FOREIGN KEY(workspace_id,deployment_id) REFERENCES model_deployments(workspace_id,id)",
)
