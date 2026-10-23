"""Migration 21: immutable bindings/receipts/cursors for the existing execution ledger."""

WORKSPACE_TABLES = (
    "execution_run_contracts",
    "execution_provenance_receipts",
    "execution_receipt_cursors",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES
MIGRATION_STATEMENTS = (
    """CREATE TABLE execution_run_contracts (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), run_id uuid NOT NULL,
        owner_principal_id uuid NOT NULL, snapshot_id uuid NOT NULL,
        scope_hash text NOT NULL CHECK(scope_hash ~ '^[0-9a-f]{64}$'), scope jsonb NOT NULL,
        input_artifact_id uuid NOT NULL, input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
        parent_run_id uuid, parent_event_id uuid, emitter_run_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,run_id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,snapshot_id) REFERENCES syn_snapshots(workspace_id,id),
        FOREIGN KEY(workspace_id,input_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,parent_run_id) REFERENCES execution_run_contracts(workspace_id,run_id),
        FOREIGN KEY(workspace_id,parent_event_id) REFERENCES syn_execution_events(workspace_id,id),
        CHECK(parent_run_id IS NULL OR parent_run_id <> run_id),
        CHECK((parent_run_id IS NULL) = (parent_event_id IS NULL)),
        CHECK(jsonb_typeof(scope)='object' AND octet_length(scope::text)<=32768)
    )""",
    "CREATE INDEX execution_contract_parent ON execution_run_contracts(workspace_id,parent_run_id)",
    """CREATE TABLE execution_provenance_receipts (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        root_run_id uuid NOT NULL, owner_principal_id uuid NOT NULL,
        body_artifact_id uuid NOT NULL, trace_artifact_id uuid NOT NULL, signature_artifact_id uuid NOT NULL,
        body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[0-9a-f]{64}$'),
        trace_sha256 text NOT NULL CHECK(trace_sha256 ~ '^[0-9a-f]{64}$'),
        signer_hash text NOT NULL CHECK(signer_hash ~ '^[0-9a-f]{64}$'),
        verified_at bigint NOT NULL CHECK(verified_at>=0), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,root_run_id),
        FOREIGN KEY(workspace_id,root_run_id) REFERENCES execution_run_contracts(workspace_id,run_id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,body_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,trace_artifact_id) REFERENCES artifacts(workspace_id,id),
        FOREIGN KEY(workspace_id,signature_artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
    """CREATE TABLE execution_receipt_cursors (
        workspace_id uuid NOT NULL REFERENCES workspaces(id), id uuid NOT NULL,
        receipt_id uuid NOT NULL, owner_principal_id uuid NOT NULL,
        body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[0-9a-f]{64}$'),
        policy_hash text NOT NULL CHECK(policy_hash ~ '^[0-9a-f]{64}$'),
        after_index integer NOT NULL CHECK(after_index BETWEEN 0 AND 2048),
        expires_at timestamptz NOT NULL,
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,receipt_id) REFERENCES execution_provenance_receipts(workspace_id,id),
        FOREIGN KEY(workspace_id,owner_principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
)
