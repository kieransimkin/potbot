"""Migration 20: additive structured statistics on the existing SYN event ledger."""

WORKSPACE_TABLES = ()
APPEND_ONLY_TABLES = ()
MIGRATION_STATEMENTS = (
    "ALTER TABLE syn_execution_events ADD COLUMN telemetry jsonb NOT NULL DEFAULT '{}'::jsonb "
    "CHECK(jsonb_typeof(telemetry)='object' AND octet_length(telemetry::text)<=65536)",
)
