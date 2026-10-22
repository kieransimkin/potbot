"""Phase 7 execution migration and no-network operator contract."""

from uuid import uuid4

import pytest

from potbot.controller.research_worker import ResearchLease, ResearchRunStore
from potbot.domain.errors import InvalidInputError
from potbot.persistence.schema import application_migrations
from potbot.research.worker_cli import main


def test_new_migration_is_additive_and_fences_claims():
    revisions = application_migrations()
    assert tuple(item.version for item in revisions) == tuple(range(1, 19))
    assert (revisions[15].name, revisions[16].name) == ("phase7_schema", "phase7_worker_schema")
    assert set(revisions[16].workspace_tables) == {"research_runs", "research_run_receipts"}
    assert revisions[16].append_only_tables == ("research_run_receipts",)
    sql = "\n".join(revisions[16].statements)
    assert "FOR UPDATE" not in sql  # Runtime locking, not a mutable migration ledger.
    assert "UNIQUE(workspace_id,action_id)" in sql
    assert "generation bigint NOT NULL" in sql
    assert "lease_until timestamptz" in sql
    assert "FOREIGN KEY(workspace_id,episode_id,action_id)" in sql
    assert "FOREIGN KEY(workspace_id,source_artifact_id)" in sql
    assert "UNIQUE(workspace_id,run_id)" in sql
    assert "CHECK ((state='running') =" in sql
    assert "result_artifact_id" in sql


def test_invalid_lease_rejected_without_store_or_network():
    with pytest.raises(InvalidInputError):
        ResearchLease(uuid4(), uuid4(), uuid4(), uuid4(), "x", "y", "worker", 0, None)
    with pytest.raises(InvalidInputError):
        ResearchLease(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64, "b" * 64, "../danger", 1, None)


def test_worker_store_rejects_mismatched_artifact_database():
    from potbot.persistence.artifacts import ArtifactStore
    from potbot.persistence.database import Database

    one = Database("postgresql://unused@localhost/example")
    two = Database("postgresql://other@localhost/example")
    fake_artifacts = ArtifactStore.__new__(ArtifactStore)
    fake_artifacts.db = two
    with pytest.raises(InvalidInputError):
        ResearchRunStore(one, fake_artifacts)


def test_cli_parses_help_without_postgres_or_credential_access(capsys):
    with pytest.raises(SystemExit) as done:
        main(["--help"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert "run-once" in out and "quarantine-expired" in out
    assert "--artifact-root" in out
