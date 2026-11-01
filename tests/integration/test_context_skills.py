"""Migration 32 acceptance on the existing disposable PostgreSQL app_env only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest

from potbot.context.bootstrap import grant_shared_context_writer, revoke_shared_context_writer
from potbot.context.model import ROLES, sha256
from potbot.context.rpc_contract import ContextRequest, SkillRequest
from potbot.context.service import WorkspaceContextService
from potbot.context.sessions import ContextSessionStore, worker_context_name
from potbot.context.skill_projection import SkillLibraryProjector
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.persistence.database import admin_connection, provision_principal

pytestmark = pytest.mark.postgres
CONTENT = "---\nname: text-tools\ndescription: Edit text\n---\n\n# Steps\nKeep input as data.\n"


def setup(env, *, principal=None, database=None, agent_id=None):
    principal = principal or env.context
    database = database or env.db
    agent_id = agent_id or uuid4()
    service = WorkspaceContextService(database)
    service.provision_private(principal, "agent", str(agent_id), idempotency_key=uuid4().hex)
    store = ContextSessionStore(database)
    return service, store, store.issue(principal, agent_id, uuid4())


def create(store, session):
    req = SkillRequest(
        uuid4(), "skills.create", name="text-tools", content=CONTENT, reason="learned workflow"
    )
    return req, store.execute(session, req)


def publish(store, session, skill, revision=1):
    return store.execute(
        session,
        SkillRequest(
            uuid4(),
            "skills.publish",
            skill_id=UUID(skill["skill_id"]),
            expected_revision=revision,
            reason="share explicitly",
        ),
    )


def read_skill(skill, role="MODEL.md"):
    return ContextRequest(uuid4(), "context.read", "skill", role, skill_id=UUID(skill["skill_id"]))


def test_create_provisions_shared_triple_and_worker_context_atomically(app_env):
    service, store, session = setup(app_env)
    req, skill = create(store, session)
    assert store.execute(session, req) == skill
    docs = service.read_triple(app_env.context, "skill", skill["context_name"])
    assert set(docs) == set(ROLES) and all(doc["revision"] == 1 for doc in docs.values())
    worker = service.read_triple(
        app_env.context, "worker", worker_context_name(session.agent_id, session.run_id)
    )
    assert set(worker) == set(ROLES)
    assert (
        store.execute(session, ContextRequest(uuid4(), "context.read", "worker", "MODEL.md"))[
            "revision"
        ]
        == 1
    )
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM workspace_context_skill_receipts WHERE session_id=%s",
                (session.session_id,),
            ).fetchone()["n"]
            == 1
        )
        versions = conn.execute(
            "SELECT revision,session_id FROM workspace_context_skill_versions WHERE skill_id=%s",
            (UUID(skill["skill_id"]),),
        ).fetchall()
        assert versions == [{"revision": 1, "session_id": session.session_id}]


def test_namespaces_are_separate_and_same_principal_agents_cannot_edit_peers(app_env):
    _, store, first = setup(app_env)
    _, _, second = setup(app_env)
    _, one = create(store, first)
    _, two = create(store, second)
    assert one["skill_id"] != two["skill_id"] and one["namespace"] != two["namespace"]
    for req in (
        SkillRequest(
            uuid4(), "skills.publish", skill_id=UUID(one["skill_id"]), expected_revision=1
        ),
        ContextRequest(
            uuid4(),
            "context.update",
            "skill",
            "MODEL.md",
            "# steal",
            1,
            skill_id=UUID(one["skill_id"]),
        ),
        read_skill(one),
    ):
        with pytest.raises(ScopeDeniedError):
            store.execute(second, req)
    publish(store, first, one)
    assert store.execute(second, read_skill(one))["revision"] == 1
    with pytest.raises(ScopeDeniedError):
        store.execute(
            second,
            SkillRequest(
                uuid4(), "skills.archive", skill_id=UUID(one["skill_id"]), expected_revision=1
            ),
        )


def test_publishing_pins_skill_code_but_preserves_live_context_across_edits(app_env):
    _, store, author = setup(app_env)
    _, skill = create(store, author)
    released = publish(store, author, skill)
    document = store.execute(author, read_skill(skill))
    changed = store.execute(
        author,
        ContextRequest(
            uuid4(),
            "context.update",
            "skill",
            "MODEL.md",
            "# More accurate model\n",
            document["revision"],
            skill_id=UUID(skill["skill_id"]),
        ),
    )
    newer = CONTENT + "\nNew draft only.\n"
    store.execute(
        author,
        SkillRequest(
            uuid4(),
            "skills.save",
            skill_id=UUID(skill["skill_id"]),
            content=newer,
            expected_revision=1,
        ),
    )
    _, _, consumer = setup(app_env)
    view = store.execute(
        consumer,
        SkillRequest(
            uuid4(),
            "skills.load",
            skill_id=UUID(skill["skill_id"]),
            release_sha256=released["release_sha256"],
        ),
    )
    assert view["content"] == CONTENT and view["sha256"] == sha256(CONTENT)
    assert view["context"]["MODEL.md"]["revision"] == changed["revision"]
    assert view["context"]["MODEL.md"]["content"] == "# More accurate model\n"
    assert view["trusted"] is False and view["automatically_activated"] is False
    with pytest.raises(ScopeDeniedError):
        store.execute(
            consumer,
            SkillRequest(
                uuid4(),
                "skills.load",
                skill_id=UUID(skill["skill_id"]),
                release_sha256=sha256(newer),
            ),
        )


def test_parallel_saves_use_one_skill_cas_across_independent_sessions(app_env):
    _, store, first = setup(app_env)
    _, skill = create(store, first)
    second = store.issue(app_env.context, first.agent_id, uuid4())
    barrier = Barrier(2)

    def save(session):
        barrier.wait(timeout=5)
        try:
            return store.execute(
                session,
                SkillRequest(
                    uuid4(),
                    "skills.save",
                    skill_id=UUID(skill["skill_id"]),
                    content=CONTENT + "\n" + str(session.run_id),
                    expected_revision=1,
                ),
            )
        except RevisionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (first, second)))
    assert sum(result is not None for result in results) == 1
    with app_env.db.transaction(app_env.context) as conn:
        rows = conn.execute(
            "SELECT revision FROM workspace_context_skill_versions WHERE skill_id=%s ORDER BY revision",
            (UUID(skill["skill_id"]),),
        ).fetchall()
        assert rows == [{"revision": 1}, {"revision": 2}]


def test_publication_retry_and_cross_method_uuid_reuse(app_env):
    _, store, session = setup(app_env)
    create_req, skill = create(store, session)
    req = SkillRequest(
        uuid4(), "skills.publish", skill_id=UUID(skill["skill_id"]), expected_revision=1
    )
    barrier = Barrier(2)

    def call(_):
        barrier.wait(timeout=5)
        return store.execute(session, req)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(call, range(2)))
    assert responses[0] == responses[1]
    for reused in (
        replace(req, request_id=create_req.request_id),
        ContextRequest(
            create_req.request_id, "context.update", "agent", "MODEL.md", "# wrong reuse", 1
        ),
    ):
        with pytest.raises(RevisionConflictError):
            store.execute(session, reused)
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM workspace_context_skill_releases WHERE skill_id=%s",
                (UUID(skill["skill_id"]),),
            ).fetchone()["n"]
            == 1
        )


def test_shared_agents_edit_requires_explicit_grant_and_revocation_blocks_replay(app_env):
    service, store, session = setup(app_env)
    doc = service.read(app_env.context, "sandbox", str(app_env.context.workspace_id), "AGENTS.md")
    req = ContextRequest(
        uuid4(),
        "context.update",
        "sandbox",
        "AGENTS.md",
        "# Scoped instructions\nNever override host policy.\n",
        doc["revision"],
    )
    with pytest.raises(ScopeDeniedError):
        store.execute(session, req)
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        grant_shared_context_writer(
            conn,
            app_env.context.workspace_id,
            UUID(doc["object_id"]),
            app_env.context.principal_id,
            ("AGENTS.md",),
        )
    assert store.execute(session, req)["revision"] == doc["revision"] + 1
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        revoke_shared_context_writer(
            conn,
            app_env.context.workspace_id,
            UUID(doc["object_id"]),
            app_env.context.principal_id,
            ("AGENTS.md",),
        )
    with pytest.raises(ScopeDeniedError):
        store.execute(session, req)


def test_peer_rls_hides_unpublished_versions_and_protects_identity(app_env):
    _, store, session = setup(app_env)
    _, skill = create(store, session)
    peer = provision_principal(
        app_env.admin_dsn,
        app_env.schema,
        urlsplit(app_env.other_db.dsn).username,
        app_env.context.workspace_id,
        principal_id=app_env.other_context.principal_id,
    )
    with app_env.other_db.transaction(peer) as conn:
        assert conn.execute("SELECT id FROM workspace_context_skills").fetchall() == []
        assert (
            conn.execute("SELECT revision FROM workspace_context_skill_versions").fetchall() == []
        )
    publish(store, session, skill)
    store.execute(
        session,
        SkillRequest(
            uuid4(),
            "skills.save",
            skill_id=UUID(skill["skill_id"]),
            content=CONTENT + "\nprivate draft",
            expected_revision=1,
        ),
    )
    with app_env.other_db.transaction(peer) as conn:
        assert len(conn.execute("SELECT id FROM workspace_context_skills").fetchall()) == 1
        assert conn.execute("SELECT revision FROM workspace_context_skill_versions").fetchall() == [
            {"revision": 1}
        ]
        assert (
            conn.execute(
                "UPDATE workspace_context_skills SET state='archived' WHERE id=%s",
                (UUID(skill["skill_id"]),),
            ).rowcount
            == 0
        )
    with pytest.raises(RevisionConflictError), app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "UPDATE workspace_context_skills SET owner_agent_id=%s WHERE id=%s",
            (uuid4(), UUID(skill["skill_id"])),
        )
    with pytest.raises(ScopeDeniedError), app_env.db.transaction(app_env.context) as conn:
        conn.execute(
            "INSERT INTO workspace_context_objects (workspace_id,id,kind,name,visibility) VALUES (%s,%s,'operator','forged','shared')",
            (app_env.context.workspace_id, uuid4()),
        )


def test_archival_preserves_history_and_revoked_session_cannot_recover_skill_receipts(app_env):
    _, store, session = setup(app_env)
    _, skill = create(store, session)
    release = publish(store, session, skill)
    req = SkillRequest(
        uuid4(), "skills.archive", skill_id=UUID(skill["skill_id"]), expected_revision=1
    )
    receipt = store.execute(session, req)
    assert receipt["state"] == "archived" and store.execute(session, req) == receipt
    assert store.execute(session, SkillRequest(uuid4(), "skills.list"))["skills"] == []
    with pytest.raises(ScopeDeniedError):
        store.execute(
            session,
            SkillRequest(
                uuid4(),
                "skills.load",
                skill_id=UUID(skill["skill_id"]),
                release_sha256=release["release_sha256"],
            ),
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM workspace_context_skill_versions WHERE skill_id=%s",
                (UUID(skill["skill_id"]),),
            ).fetchone()["n"]
            == 1
        )
    store.revoke(session)
    with pytest.raises(ScopeDeniedError):
        store.execute(session, req)


def test_database_publication_materializes_readonly_current_context(app_env, tmp_path):
    service, store, session = setup(app_env)
    _, skill = create(store, session)
    publish(store, session, skill)
    root = tmp_path / "shared-skills"
    root.mkdir(mode=0o755)
    projector = SkillLibraryProjector(root)
    assert projector.sync(service, app_env.context)["skills"] == 1
    import json

    old_index = json.loads((root / "_agents/catalogue.json").read_text())
    old_path = root / old_index["skills"][0]["path"]
    previous = (old_path / "MODEL.md").read_bytes()
    store.execute(
        session,
        ContextRequest(
            uuid4(),
            "context.update",
            "skill",
            "MODEL.md",
            "# verified",
            1,
            skill_id=UUID(skill["skill_id"]),
        ),
    )
    assert projector.sync(service, app_env.context)["changed"] is True
    new_index = json.loads((root / "_agents/catalogue.json").read_text())
    assert old_index["skills"][0]["path"] != new_index["skills"][0]["path"]
    assert (old_path / "MODEL.md").read_bytes() == previous
    assert (root / new_index["skills"][0]["path"] / "MODEL.md").read_text() == "# verified"


def test_failed_draft_edit_rolls_back_all_version_and_receipt_effects(app_env):
    _, store, session = setup(app_env)
    _, skill = create(store, session)
    with pytest.raises(RevisionConflictError):
        store.execute(
            session,
            SkillRequest(
                uuid4(),
                "skills.save",
                skill_id=UUID(skill["skill_id"]),
                content=CONTENT + "\nwrong revision",
                expected_revision=7,
            ),
        )
    with app_env.db.transaction(app_env.context) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM workspace_context_skill_versions WHERE skill_id=%s",
                (UUID(skill["skill_id"]),),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM workspace_context_skill_receipts WHERE session_id=%s",
                (session.session_id,),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT request_count FROM workspace_context_sessions WHERE id=%s",
                (session.session_id,),
            ).fetchone()["request_count"]
            == 1
        )
