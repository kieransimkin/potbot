"""Migration 7: immutable semantic releases and versioned consumer extensions."""

WORKSPACE_TABLES = (
    "semantic_component_releases",
    "semantic_releases",
    "semantic_catalog_entries",
    "semantic_active_releases",
    "task_semantic_pins",
    "packet_semantic_pins",
)
APPEND_ONLY_TABLES = tuple(
    table for table in WORKSPACE_TABLES if table != "semantic_active_releases"
)

MIGRATION_STATEMENTS = (
    """ALTER TABLE resolved_configurations ADD CONSTRAINT configuration_episode_identity
        UNIQUE(workspace_id,id,episode_id)""",
    """CREATE TABLE semantic_component_releases (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        component_key text NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,component_key))""",
    """CREATE TABLE semantic_releases (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        release_key text NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        consumer_contract text NOT NULL,
        manifest jsonb NOT NULL, documents jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,release_key),
        UNIQUE(workspace_id,content_hash))""",
    """CREATE TABLE semantic_catalog_entries (
        workspace_id uuid NOT NULL, release_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN (
            'data_type','entity_type','predicate','concept','retrieval_profile')),
        entry_id text NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        definition jsonb NOT NULL,
        PRIMARY KEY(workspace_id,release_id,kind,entry_id),
        FOREIGN KEY(workspace_id,release_id) REFERENCES semantic_releases(workspace_id,id))""",
    """CREATE TABLE semantic_active_releases (
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id), release_id uuid NOT NULL,
        update_token bigint NOT NULL DEFAULT 0 CHECK(update_token >= 0),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(workspace_id,release_id) REFERENCES semantic_releases(workspace_id,id))""",
    """CREATE TABLE task_semantic_pins (
        workspace_id uuid NOT NULL, episode_id uuid NOT NULL, release_id uuid NOT NULL,
        configuration_id uuid NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,episode_id),
        FOREIGN KEY(workspace_id,episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY(workspace_id,release_id) REFERENCES semantic_releases(workspace_id,id),
        FOREIGN KEY(workspace_id,configuration_id,episode_id)
            REFERENCES resolved_configurations(workspace_id,id,episode_id))""",
    """CREATE TABLE packet_semantic_pins (
        workspace_id uuid NOT NULL, packet_id uuid NOT NULL, release_id uuid NOT NULL,
        content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,packet_id),
        FOREIGN KEY(workspace_id,packet_id) REFERENCES context_packets(workspace_id,id),
        FOREIGN KEY(workspace_id,release_id) REFERENCES semantic_releases(workspace_id,id))""",
)
