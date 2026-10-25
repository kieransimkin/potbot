"""Migration 27: workspace- and principal-owned durable UI chat sessions.

A model call can be pending, completed, or indeterminate. No failed/unknown
provider call may be silently replayed as an ordinary conversational turn.
"""

WORKSPACE_TABLES = ("ui_chat_sessions", "ui_chat_turns")
APPEND_ONLY_TABLES = ()

MIGRATION_STATEMENTS = (
    """CREATE TABLE ui_chat_sessions (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
        revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,id,principal_id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "CREATE INDEX ui_chat_sessions_recent ON ui_chat_sessions(workspace_id,principal_id,updated_at DESC,id)",
    """CREATE TABLE ui_chat_turns (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        session_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        request_id uuid NOT NULL,
        ordinal bigint NOT NULL CHECK (ordinal BETWEEN 1 AND 200),
        user_text text NOT NULL CHECK (length(user_text) BETWEEN 1 AND 16000),
        assistant_text text CHECK (assistant_text IS NULL OR length(assistant_text)<=32000),
        state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','completed','indeterminate')),
        run_id text CHECK (run_id ~ '^[0-9a-f]{32}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,session_id,ordinal),
        UNIQUE(workspace_id,session_id,request_id),
        FOREIGN KEY(workspace_id,session_id,principal_id)
            REFERENCES ui_chat_sessions(workspace_id,id,principal_id),
        CHECK ((state='completed')=(assistant_text IS NOT NULL))
    )""",
    "CREATE INDEX ui_chat_turns_history ON ui_chat_turns(workspace_id,session_id,ordinal)",
)
