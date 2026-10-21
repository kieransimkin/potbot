"""First-class questions retain immutable history and scoped evidence references."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.tasks import TaskService
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement
from potbot.persistence.database import Database

pytestmark = pytest.mark.postgres


def task(app_env):
    contract = ContractSpec(
        objective="Choose a reusable workflow",
        baseline_ref="git:" + "1" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(
            Requirement("R1", "Existing workflows are checked", "Record an evidenced decision"),
        ),
        change_scope=("docs/",),
        capabilities=("research",),
        verification_policy="research-v1",
        budget=(("tokens", 0), ("calls", 10), ("tool_ms", 30000), ("concurrency", 2)),
    )
    created = TaskService(app_env.db).submit(
        app_env.context, app_env.repository_id, contract, "need-task"
    )
    return UUID(created["task_id"])


def need() -> NeedSpec:
    return NeedSpec(
        question="Which existing DanceFlow workflow can supply the required export?",
        pending_decision="Reuse, adapt or implement an export workflow",
        alternatives=(
            "Reuse an existing workflow",
            "Adapt a compatible workflow",
            "Implement the missing behavior",
        ),
        required_evidence=("Versioned workflow description", "Observed export fixture"),
        resolution_criteria=("A qualified candidate satisfies the task's export contract",),
        scope="Registered workflows visible to this workspace",
        priority=80,
        budget=(("calls", 2),),
        query_profiles=("aboutness", "applicable_procedure"),
    )


def test_need_creation_revision_restart_and_exact_idempotent_replay(app_env) -> None:
    episode = task(app_env)
    store = InformationNeedStore(app_env.db)
    first = store.create(app_env.context, episode, need(), "need-1")
    assert first["status"] == "open" and first["update_token"] == 0
    assert first["content"]["query_profiles"] == ["aboutness", "applicable_procedure"]
    assert store.create(app_env.context, episode, need(), "need-1") == first
    identifier, predecessor = UUID(first["id"]), UUID(first["current_revision_id"])
    changed = replace(need(), question="Which workflow supports the newly required alpha channel?")
    second = store.revise(app_env.context, identifier, 0, predecessor, changed, "need-revision")
    assert second["update_token"] == 1
    assert (
        store.revise(app_env.context, identifier, 0, predecessor, changed, "need-revision")
        == second
    )
    restarted = InformationNeedStore(Database(app_env.db.dsn))
    assert restarted.inspect(app_env.context, identifier) == second
    history = restarted.history(app_env.context, identifier)
    assert [row["content"]["question"] for row in history] == [need().question, changed.question]
    assert history[1]["predecessor_id"] == str(predecessor)
    assert history[0]["id"] == str(predecessor)


def test_stale_token_wrong_need_predecessor_and_changed_replay_leave_history_unchanged(
    app_env,
) -> None:
    episode = task(app_env)
    store = InformationNeedStore(app_env.db)
    first = store.create(app_env.context, episode, need(), "first")
    other = store.create(
        app_env.context, episode, replace(need(), question="Another question?"), "other"
    )
    identifier, predecessor = UUID(first["id"]), UUID(first["current_revision_id"])
    with pytest.raises(RevisionConflictError):
        store.create(app_env.context, episode, replace(need(), priority=99), "first")
    with pytest.raises(RevisionConflictError):
        store.revise(app_env.context, identifier, 1, predecessor, need(), "stale")
    with pytest.raises(RevisionConflictError):
        store.revise(
            app_env.context,
            identifier,
            0,
            UUID(other["current_revision_id"]),
            need(),
            "wrong-predecessor",
        )
    assert len(store.history(app_env.context, identifier)) == 1
    assert store.inspect(app_env.context, identifier) == first
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(app_env.context) as connection:
            connection.execute("UPDATE need_revisions SET content='{}' WHERE id=%s", (predecessor,))


def test_two_concurrent_revision_requests_have_one_cas_winner(app_env) -> None:
    episode = task(app_env)
    store = InformationNeedStore(app_env.db)
    first = store.create(app_env.context, episode, need(), "initial")

    def revise(label):
        try:
            return store.revise(
                app_env.context,
                UUID(first["id"]),
                0,
                UUID(first["current_revision_id"]),
                replace(need(), question=f"Question {label}?"),
                f"revision-{label}",
            )
        except RevisionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, ("left", "right")))
    assert len([value for value in results if value is not None]) == 1
    assert len(store.history(app_env.context, UUID(first["id"]))) == 2
    assert store.inspect(app_env.context, UUID(first["id"]))["update_token"] == 1


def test_cross_workspace_and_fabricated_evidence_rejected_without_partial_rows(app_env) -> None:
    episode = task(app_env)
    store = InformationNeedStore(app_env.db)
    with app_env.db.transaction(app_env.context) as connection:
        actual_evidence = connection.execute(
            "SELECT id FROM evidence_events WHERE episode_id=%s LIMIT 1", (episode,)
        ).fetchone()["id"]
    first = store.create(
        app_env.context, episode, replace(need(), known_evidence=(actual_evidence,)), "evidenced"
    )
    with pytest.raises(ScopeDeniedError):
        store.create(app_env.context, episode, replace(need(), known_evidence=(uuid4(),)), "fake")
    foreign = InformationNeedStore(app_env.other_db)
    with pytest.raises(ScopeDeniedError):
        foreign.inspect(app_env.other_context, UUID(first["id"]))
    with pytest.raises(ScopeDeniedError):
        foreign.create(app_env.other_context, episode, need(), "foreign-task")
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM information_needs").fetchone()["n"] == 1
        )
        assert connection.execute("SELECT count(*) AS n FROM need_revisions").fetchone()["n"] == 1
        assert (
            connection.execute("SELECT evidence_id FROM need_revision_evidence").fetchone()[
                "evidence_id"
            ]
            == actual_evidence
        )


def test_invalid_query_views_and_unbounded_need_values_fail_before_persistence(app_env) -> None:
    episode = task(app_env)
    store = InformationNeedStore(app_env.db)
    with pytest.raises(InvalidInputError):
        store.create(
            app_env.context,
            episode,
            replace(need(), query_profiles=("invented_memory_truth",)),
            "invalid-profile",
        )
    with pytest.raises(InvalidInputError):
        replace(need(), priority=True)
    with pytest.raises(InvalidInputError):
        replace(need(), required_evidence=())
    with pytest.raises(InvalidInputError):
        replace(need(), budget=(("calls", -1),))
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM information_needs").fetchone()["n"] == 0
        )
