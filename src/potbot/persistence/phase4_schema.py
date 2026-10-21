"""Revision 10 joins both published histories without changing prior checksums."""

from potbot.persistence import coding_schema, verification_schema

WORKSPACE_TABLES = coding_schema.WORKSPACE_TABLES + verification_schema.WORKSPACE_TABLES
APPEND_ONLY_TABLES = coding_schema.APPEND_ONLY_TABLES + verification_schema.APPEND_ONLY_TABLES
MIGRATION_STATEMENTS = (
    "ALTER TABLE actions ADD COLUMN stdin_artifact_id uuid, ADD CONSTRAINT actions_stdin_artifact_fk "
    "FOREIGN KEY(workspace_id,stdin_artifact_id) REFERENCES artifacts(workspace_id,id)",
    *coding_schema.MIGRATION_STATEMENTS,
    *verification_schema.MIGRATION_STATEMENTS,
)
