"""Store optional verified Syn enrollment pins with Cog grant revisions."""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    """ALTER TABLE cog_grant_revisions ADD COLUMN enrollment_sha256 text
        CHECK (enrollment_sha256 ~ '^[0-9a-f]{64}$')""",
)
