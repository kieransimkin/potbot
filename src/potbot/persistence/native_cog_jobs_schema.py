"""Admit the native Cog job kind without rewriting the original queue migration."""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()
MIGRATION_STATEMENTS = (
    "ALTER TABLE jobs DROP CONSTRAINT jobs_job_type_check",
    """ALTER TABLE jobs ADD CONSTRAINT jobs_job_type_check
       CHECK (job_type = 'model.generate'
           OR (job_type = 'cog.execute' AND selected_models = '[]'::jsonb))""",
)
