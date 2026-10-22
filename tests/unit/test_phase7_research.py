"""Phase 7 search lineage, immutable source snapshots and anti-fabrication tests."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from subprocess import run
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.research.contracts import (
    ObservationStatus,
    ResearchAction,
    ResearchActionBook,
    ResearchCategory,
    ResearchOperation,
    ResearchQuery,
    ResearchScope,
)
from potbot.research.repository import SnapshotResearchAdapter
from potbot.tools.repository_snapshot import capture_snapshot


def context():
    return ResearchScope(*[uuid4() for _ in range(6)], "b" * 64)


def query(
    *,
    operation=ResearchOperation.FIND_LITERAL,
    search="TypeError",
    locator="src",
    required=("TypeError",),
    target_version="3.12",
    max_results=8,
    max_source_bytes=262144,
):
    return ResearchQuery(
        category=ResearchCategory.REPOSITORY,
        operation=operation,
        locator=locator,
        search_text=search,
        required_identifiers=required,
        expected_observation="Find exact call sites",
        decision_effect="Choose a compatible implementation",
        evidence_requirement="Inspected source bytes and content digest",
        stopping_condition="Applicable example or no matching code",
        fallback="Leave question open",
        target_version=target_version,
        max_results=max_results,
        max_source_bytes=max_source_bytes,
    )


def action(*, scope=None, q=None, predecessor=None):
    return ResearchAction(uuid4(), scope or context(), q or query(), predecessor)


def repository(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "example.py").write_text(
        "def cleanup():\n    # TypeError on 3.12\n    return True\n", encoding="utf-8"
    )
    (root / "src" / "other.py").write_text('def version():\n    return "3.11"\n', encoding="utf-8")
    (root / "src" / "binary.py").write_bytes(b"\xff\xff\x00")
    (root / "src" / "shortcut.py").symlink_to("example.py")
    run(["git", "add", "-A"], cwd=root, check=True)
    run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    return root, capture_snapshot(root)


def inspector(tmp_path, scope):
    root, snapshot = repository(tmp_path)
    return (
        root,
        SnapshotResearchAdapter(
            snapshot,
            repository_state_id=scope.repository_state_id,
            workspace_id=scope.workspace_id,
            task_id=scope.task_id,
        ),
        snapshot,
    )


def test_research_action_round_trip_and_exact_identity():
    a = action()
    assert ResearchAction.from_dict(a.to_dict()) == a
    assert a.identity == replace(a, action_id=uuid4()).identity
    assert a.identity != replace(a, query=replace(a.query, target_version="3.13")).identity
    assert a.identity != replace(a, query=replace(a.query, search_text="typeerror")).identity
    assert a.identity != replace(a, scope=replace(a.scope, need_revision_id=uuid4())).identity
    tampered = a.to_dict()
    tampered["identity"] = "a" * 64
    with pytest.raises(InvalidInputError):
        ResearchAction.from_dict(tampered)


def test_lineage_preserves_required_identifiers_scope_and_versions():
    a = action()
    book = ResearchActionBook(
        workspace_id=a.scope.workspace_id,
        task_id=a.scope.task_id,
        need_revisions={a.scope.need_id: a.scope.need_revision_id},
    )
    assert book.admit(a) == a.action_id
    assert book.admit(replace(a, action_id=uuid4())) == a.action_id
    b = action(
        scope=a.scope,
        q=replace(a.query, search_text="TypeError in async callbacks"),
        predecessor=a.action_id,
    )
    assert book.admit(b) == b.action_id
    with pytest.raises(RevisionConflictError):
        book.admit(
            action(
                scope=a.scope,
                predecessor=a.action_id,
                q=replace(query(), required_identifiers=(), search_text="async"),
            )
        )
    with pytest.raises(RevisionConflictError):
        book.admit(
            action(
                scope=a.scope, predecessor=a.action_id, q=replace(query(), target_version="3.11")
            )
        )
    with pytest.raises(RevisionConflictError):
        book.admit(action(scope=a.scope, predecessor=uuid4()))
    with pytest.raises(ScopeDeniedError):
        book.admit(action())
    with pytest.raises(RevisionConflictError):
        book.admit(action(scope=replace(a.scope, need_revision_id=uuid4())))


def test_research_query_rejects_unsafe_paths_and_unbounded_search():
    for path in ("../../secret", "/tmp/token", ".git/config", "src/../private", "C:/secrets"):
        with pytest.raises(InvalidInputError):
            query(locator=path)
    with pytest.raises(InvalidInputError):
        query(max_source_bytes=262145)
    with pytest.raises(InvalidInputError):
        query(max_results=0)
    with pytest.raises(InvalidInputError):
        query(operation=ResearchOperation.READ_FILE, search="TypeError")
    with pytest.raises(InvalidInputError):
        replace(query(), required_identifiers=("Type Error",))
    with pytest.raises(InvalidInputError):
        replace(query(), category=ResearchCategory.DOCUMENTATION)


def test_inspection_binds_revision_and_does_not_read_changed_checkout(tmp_path):
    scope = context()
    root, reader, snapshot = inspector(tmp_path, scope)
    a = action(
        scope=scope,
        q=query(
            locator="src/example.py",
            operation=ResearchOperation.READ_FILE,
            search="whole_file",
            required=(),
        ),
    )
    before = reader.inspect(a, now=datetime(2026, 10, 22, tzinfo=UTC))
    (root / "src" / "example.py").write_text("PRIVATE CHANGED CONTENT", encoding="utf-8")
    after = reader.inspect(a, now=datetime(2026, 10, 22, tzinfo=UTC))
    assert before == after
    assert "PRIVATE CHANGED CONTENT" not in after[0].excerpt
    assert before[0].source_revision == snapshot.content_digest
    assert before[0].line_start == 1
    with pytest.raises(RevisionConflictError):
        reader.inspect(replace(a, scope=replace(scope, repository_state_id=uuid4())))


def test_search_results_are_actually_inspected_and_bound_to_file_digests(tmp_path):
    scope = context()
    _root, reader, _snap = inspector(tmp_path, scope)
    a = action(scope=scope)
    found = reader.inspect(a, now=datetime(2026, 10, 22, tzinfo=UTC))
    assert len(found) == 1
    assert found[0].status == ObservationStatus.INSPECTED
    assert found[0].resolved_locator == "src/example.py"
    assert "TypeError" in found[0].excerpt
    assert found[0].content_digest is not None
    assert found[0].scan_complete is True
    assert found[0].license_status == "unknown"
    assert found[0].fingerprint == found[0].fingerprint
    assert b"potbot-research-observation" in found[0].canonical_bytes()


def test_nontext_missing_over_budget_and_symlink_are_classified(tmp_path):
    scope = context()
    _root, reader, _snap = inspector(tmp_path, scope)

    def read(path, bytes_limit=262144):
        return reader.inspect(
            action(
                scope=scope,
                q=query(
                    locator=path,
                    operation=ResearchOperation.READ_FILE,
                    search="whole_file",
                    required=(),
                    max_source_bytes=bytes_limit,
                ),
            )
        )[0]

    assert read("src/binary.py").status == ObservationStatus.NON_TEXT
    assert read("src/shortcut.py").status == ObservationStatus.UNAVAILABLE
    assert read("src/not-present.py").status == ObservationStatus.NOT_FOUND
    assert read("src/example.py", 4).status == ObservationStatus.OVER_LIMIT


def test_limited_example_search_does_not_claim_complete_retrieval(tmp_path):
    scope = context()
    _root, reader, _snap = inspector(tmp_path, scope)
    a = action(
        scope=scope,
        q=query(
            operation=ResearchOperation.SEARCH_EXAMPLE_CODE,
            search="def",
            required=(),
            max_results=1,
        ),
    )
    hits = reader.inspect(a)
    assert len(hits) == 1
    assert hits[0].status == ObservationStatus.INSPECTED
    assert hits[0].scan_complete is False
    assert hits[0].license_status == "unknown"
    limited = action(scope=scope, q=query(max_source_bytes=1))
    assert reader.inspect(limited)[0].status == ObservationStatus.OVER_LIMIT


def test_action_identity_includes_evidence_policy_allowances_and_lineage():
    original = action()
    assert (
        original.identity
        != replace(
            original,
            query=replace(
                original.query,
                max_results=original.query.max_results + 1,
            ),
        ).identity
    )
    assert (
        original.identity
        != replace(
            original,
            query=replace(
                original.query,
                max_source_bytes=original.query.max_source_bytes - 1,
            ),
        ).identity
    )
    assert (
        original.identity
        != replace(
            original,
            query=replace(
                original.query,
                evidence_requirement="Require a different source version",
            ),
        ).identity
    )
    assert original.identity != replace(original, predecessor_id=uuid4()).identity
