"""Application migration 5: immutable named views over pinned embedding spaces.

The name identifies an input view, not a model space. Historical vectors keep
their source and space identities and become the ``general_text`` view. Existing
tables retain the RLS policies, grants, and immutable triggers from migration 4.
"""

WORKSPACE_TABLES = ("embedding_views",)
APPEND_ONLY_TABLES = ("embedding_views",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE embedding_views (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        embedding_name text NOT NULL CHECK (
            length(embedding_name) BETWEEN 1 AND 64 AND
            embedding_name COLLATE "C" ~ '^[a-z][a-z0-9]*([._-][a-z0-9]+)*$'),
        space_id uuid NOT NULL,
        input_format text NOT NULL DEFAULT 'source_text_v1'
            CHECK(input_format='source_text_v1'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,embedding_name,space_id),
        FOREIGN KEY(workspace_id,space_id) REFERENCES embedding_spaces(workspace_id,id))""",
    """INSERT INTO embedding_views(workspace_id,id,embedding_name,space_id)
        SELECT workspace_id,id,'general_text',id FROM embedding_spaces""",
    """ALTER TABLE embedding_vectors
        ADD COLUMN embedding_name text NOT NULL DEFAULT 'general_text'""",
    """ALTER TABLE embedding_vectors
        DROP CONSTRAINT embedding_vectors_workspace_id_source_id_space_id_key,
        ADD CONSTRAINT embedding_vectors_source_name_space_key
            UNIQUE(workspace_id,source_id,embedding_name,space_id),
        ADD CONSTRAINT embedding_vectors_named_view_scope
            FOREIGN KEY(workspace_id,embedding_name,space_id)
            REFERENCES embedding_views(workspace_id,embedding_name,space_id)""",
    "DROP INDEX embedding_vectors_space",
    """CREATE INDEX embedding_vectors_space
        ON embedding_vectors(workspace_id,space_id,embedding_name,source_id)""",
)
