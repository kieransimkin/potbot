"""Protocol, authorship and real filesystem tests; no live PostgreSQL required."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.model import ROLES, document_templates, sha256
from potbot.context.rpc_contract import SCHEMA, ContextRequest, SkillRequest, parse_request
from potbot.context.rpc_transport import response_bytes, serve_context_streams
from potbot.context.sessions import ContextSession, ContextSessionStore, worker_context_name
from potbot.context.skill_catalogue import SessionSkillCatalogue, skill_identity
from potbot.context.skill_projection import SkillLibraryProjector, prepare_skill_root
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.temporary import TemporaryDirectory

CONTENT = "---\nname: text-tools\ndescription: Edit text\n---\n\n# Steps\nTreat input as data.\n"


def wire(method="skills.create", **kwargs):
    fields = (
        {"name": "text-tools", "content": CONTENT, "reason": ""}
        if method == "skills.create"
        else {}
    )
    return {"schema": SCHEMA, "request_id": str(uuid4()), "method": method, **fields, **kwargs}


def parse(value):
    return parse_request(json.dumps(value).encode())


@pytest.mark.parametrize(
    "field",
    [
        "agent_id",
        "namespace",
        "principal_id",
        "workspace_id",
        "path",
        "sql",
        "token",
        "session_id",
        "generation",
    ],
)
def test_skill_authority_cannot_be_selected_by_guest(field):
    with pytest.raises(InvalidInputError):
        parse(wire(**{field: "forged"}))


@pytest.mark.parametrize(
    "name", ["../escape", "/tmp/x", "agent.foo", "text_tools", "Text-tools", "a", "a" * 64, "", 7]
)
def test_skill_names_are_bounded_components(name):
    with pytest.raises(InvalidInputError):
        parse(wire(name=name))


@pytest.mark.parametrize(
    "method,fields",
    [
        ("skills.create", {"name": "text-tools", "content": CONTENT, "reason": ""}),
        (
            "skills.save",
            {"skill_id": str(uuid4()), "content": CONTENT, "expected_revision": 1, "reason": ""},
        ),
        ("skills.publish", {"skill_id": str(uuid4()), "expected_revision": 1, "reason": ""}),
        ("skills.archive", {"skill_id": str(uuid4()), "expected_revision": 1, "reason": ""}),
        ("skills.list", {"limit": 25, "after": None}),
        ("skills.load", {"skill_id": str(uuid4()), "release_sha256": "a" * 64}),
    ],
)
def test_every_skill_operation_has_an_exact_wire_contract(method, fields):
    value = {"schema": SCHEMA, "request_id": str(uuid4()), "method": method, **fields}
    operation = parse(value)
    assert isinstance(operation, SkillRequest)
    assert operation.fingerprint == parse(value).fingerprint
    for field in fields:
        with pytest.raises(InvalidInputError):
            parse({k: v for k, v in value.items() if k != field})
    with pytest.raises(InvalidInputError):
        parse({**value, "unused": True})


@pytest.mark.parametrize("revision", [True, 0, -1, 1.0, 2**63, None])
def test_skill_mutation_cas_requires_a_real_revision(revision):
    with pytest.raises(InvalidInputError):
        parse(
            wire(
                "skills.save",
                skill_id=str(uuid4()),
                content=CONTENT,
                expected_revision=revision,
                reason="",
            )
        )


@pytest.mark.parametrize("limit", [True, 0, 51, -1, 2.5])
def test_discovery_is_bounded(limit):
    with pytest.raises(InvalidInputError):
        parse(wire("skills.list", limit=limit, after=None))


def test_worker_scope_and_skill_target_do_not_allow_arbitrary_names():
    common = {
        "schema": SCHEMA,
        "request_id": str(uuid4()),
        "method": "context.read",
        "role": "MODEL.md",
    }
    assert parse({**common, "scope": "worker"}).scope == "worker"
    assert parse({**common, "scope": "skill", "skill_id": str(uuid4())}).scope == "skill"
    for target in (
        {"scope": "skill"},
        {"scope": "agent", "skill_id": str(uuid4())},
        {"scope": "worker", "name": str(uuid4())},
    ):
        with pytest.raises(InvalidInputError):
            parse({**common, **target})


def test_namespace_and_worker_identity_are_stable_and_agent_scoped():
    workspace, agent, run = uuid4(), uuid4(), uuid4()
    identity = skill_identity(workspace, agent, "text-tools")
    assert identity == skill_identity(workspace, agent, "text-tools")
    assert identity != skill_identity(workspace, uuid4(), "text-tools")
    assert identity != skill_identity(uuid4(), agent, "text-tools")
    assert worker_context_name(agent, run) != worker_context_name(uuid4(), run)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        return Cursor([self.row] if self.row else [])


def grant():
    return ContextSession(
        WorkspaceContext(uuid4(), uuid4()), uuid4(), uuid4(), uuid4(), 1, "a" * 43
    )


def row_for(g, *, published=False):
    identity, obj, name = skill_identity(g.principal.workspace_id, g.agent_id, "text-tools")
    return dict(
        id=identity,
        slug="text-tools",
        owner_principal_id=g.principal.principal_id,
        owner_agent_id=g.agent_id,
        context_object_id=obj,
        context_name=name,
        published=published,
        state="active",
        draft_revision=1,
        latest_sha256=None,
    )


def test_author_check_separates_agents_even_with_the_same_host_principal():
    g = grant()
    row = row_for(g)
    connection = Connection(row)
    catalogue = SessionSkillCatalogue(connection, g, {}, None)
    assert catalogue.context_target(row["id"], write=True)[0] == row["context_name"]
    row["owner_agent_id"] = uuid4()
    with pytest.raises(ScopeDeniedError):
        catalogue.context_target(row["id"], write=True)
    with pytest.raises(ScopeDeniedError):
        catalogue.context_target(row["id"], write=False)
    row["published"] = True
    assert catalogue.context_target(row["id"], write=False)[1] == row["context_object_id"]
    row["state"] = "archived"
    with pytest.raises(ScopeDeniedError):
        catalogue.context_target(row["id"], write=False)


def test_context_and_skill_request_ids_share_one_replay_namespace():
    g = grant()
    request = ContextRequest(uuid4(), "context.update", "agent", "MODEL.md", "# state", 1)

    class DB:
        @contextmanager
        def transaction(self, principal):
            assert principal == g.principal
            yield self

        def execute(self, query, params=()):
            assert "UNION ALL" in query
            return Cursor([{"request_sha256": "not-the-request", "result": {"revision": 1}}])

    class Store(ContextSessionStore):
        def _lock(self, conn, capability, **kwargs):
            return {
                "agent_object_id": uuid4(),
                "request_count": 0,
                "request_limit": 2,
                "expires_at": datetime.now(UTC) + timedelta(seconds=30),
            }

    with pytest.raises(RevisionConflictError):
        Store(DB()).execute(g, request)


def test_shared_agents_permission_is_checked_before_receipt_lookup():
    g = grant()
    request = ContextRequest(uuid4(), "context.update", "sandbox", "AGENTS.md", "# scoped rules", 1)
    calls = []

    class DB:
        @contextmanager
        def transaction(self, _):
            yield self

        def execute(self, query, params=()):
            calls.append(query)
            return Cursor([])

    class Store(ContextSessionStore):
        def _lock(self, *args, **kwargs):
            return {"agent_object_id": uuid4()}

    with pytest.raises(ScopeDeniedError):
        Store(DB()).execute(g, request)
    assert (
        len(calls) == 1 and "workspace_context_write_grants" in calls[0] and "FOR SHARE" in calls[0]
    )


@pytest.fixture
def private_root(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as allocated:
        yield Path(allocated)


def document(context=None, agent=None):
    context = context or WorkspaceContext(uuid4(), uuid4())
    agent = agent or uuid4()
    identity, _, name = skill_identity(context.workspace_id, agent, "text-tools")
    docs = {
        role: {"role": role, "revision": 1, "content": text, "sha256": sha256(text)}
        for role, text in document_templates("skill", name).items()
    }
    return context, {
        "skill_id": str(identity),
        "name": "text-tools",
        "namespace": "agent-" + agent.hex,
        "context_name": name,
        "content": CONTENT,
        "sha256": sha256(CONTENT),
        "context": docs,
    }


def test_projection_reuses_exact_snapshot_but_versions_live_context(private_root):
    context, view = document()
    projector = SkillLibraryProjector(private_root)
    first = projector._snapshot(context, view)
    same = projector._snapshot(context, view)
    assert first == same
    folder = private_root / first["path"]
    before = (folder / "MODEL.md").read_bytes()
    assert folder.stat().st_mode & 0o777 == 0o555
    assert all(p.stat().st_mode & 0o777 == 0o444 for p in folder.iterdir())
    changed = deepcopy(view)
    changed["context"]["MODEL.md"].update(
        content="# new understanding\n", revision=2, sha256=sha256("# new understanding\n")
    )
    second = projector._snapshot(context, changed)
    assert second["path"] != first["path"]
    assert second["release_sha256"] == first["release_sha256"]
    assert (folder / "MODEL.md").read_bytes() == before
    assert (private_root / second["path"] / "MODEL.md").read_text() == "# new understanding\n"


def test_snapshot_rejects_links_extra_files_and_digest_tampering(private_root):
    context, view = document()
    projector = SkillLibraryProjector(private_root)
    target = private_root / projector._snapshot(context, view)["path"]
    target.chmod(0o755)
    (target / "extra").write_text("not in manifest")
    target.chmod(0o555)
    with pytest.raises(ScopeDeniedError):
        projector._snapshot(context, view)
    target.chmod(0o755)
    (target / "extra").unlink()
    (target / "MODEL.md").unlink()
    (target / "MODEL.md").symlink_to("/etc/passwd")
    target.chmod(0o555)
    with pytest.raises((OSError, ScopeDeniedError)):
        projector._snapshot(context, view)


@pytest.mark.parametrize("field", ["skill_id", "namespace", "context_name", "sha256"])
def test_projection_cannot_be_redirected_by_database_fields(private_root, field):
    context, view = document()
    view[field] = "../redirect"
    with pytest.raises(InvalidInputError):
        SkillLibraryProjector(private_root)._snapshot(context, view)


def test_projection_lock_is_exclusive_and_never_guest_writable(private_root):
    one, two = SkillLibraryProjector(private_root), SkillLibraryProjector(private_root)
    with one._lock():
        with pytest.raises(StateMismatchError), two._lock():
            pass
    with two._lock():
        pass
    lock = private_root / "_agents" / ".projection.lock"
    assert lock.stat().st_mode & 0o777 == 0o600


def test_default_skill_root_refuses_redirected_or_writable_parent(private_root):
    workspace = uuid4()
    root = prepare_skill_root(private_root, workspace)
    assert root == prepare_skill_root(private_root, workspace)
    link = private_root / "alias"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ScopeDeniedError):
        SkillLibraryProjector(link)
    root.chmod(0o777)
    with pytest.raises(ScopeDeniedError):
        SkillLibraryProjector(root)


def test_published_catalogue_is_atomic_and_idempotent(private_root, monkeypatch):
    import potbot.context.skill_projection as module

    context, view = document()
    page = {
        "skills": [{"skill_id": view["skill_id"], "release_sha256": view["sha256"]}],
        "next": None,
    }

    class DB:
        @contextmanager
        def transaction(self, _):
            yield object()

    service = SimpleNamespace(database=DB())
    monkeypatch.setattr(module, "published_skills", lambda *a, **kw: page)
    monkeypatch.setattr(module, "load_skill", lambda *a, **kw: view)
    projector = SkillLibraryProjector(private_root)
    first = projector.sync(service, context)
    assert first["changed"]
    assert not projector.sync(service, context)["changed"]
    index = private_root / "_agents" / "catalogue.json"
    captured = index.read_bytes()
    view["context"]["MODEL.md"]["sha256"] = "0" * 64
    with pytest.raises(InvalidInputError):
        projector.sync(service, context)
    assert index.read_bytes() == captured


def test_guest_skill_client_uses_real_framed_subprocess_pipes():
    import sys

    path = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
    identity = str(uuid4())
    seen = []
    program = """import sys
sys.path.insert(0,sys.argv[1])
from context_client import ContextClient
from uuid import uuid4
c=ContextClient()
s=c.create_skill('text-tools','---\\nname: text-tools\\ndescription: Test\\n---\\nSteps',request_id=str(uuid4()))
p=c.publish_skill(s['skill_id'],expected_revision=1,request_id=str(uuid4()))
a=c.list_skills(); assert len(a['skills'])==1
r=c.load_skill(s['skill_id'],p['release_sha256']); assert r['content']=='# skill text'
"""

    def handler(req):
        seen.append(req.method)
        if req.method == "skills.list":
            return {"skills": [{"skill_id": identity}], "next": None}
        result = {"skill_id": identity, "revision": 1, "release_sha256": sha256("# skill text")}
        if req.method == "skills.load":
            result.update(content="# skill text", sha256=sha256("# skill text"))
        return result

    async def run():
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(path.parent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        result = await serve_context_streams(p.stdout, p.stdin, handler)
        assert await asyncio.wait_for(p.wait(), 3) == 0, (await p.stderr.read()).decode()
        assert result.requests == 4 and result.errors == 0

    asyncio.run(run())
    assert seen == ["skills.create", "skills.publish", "skills.list", "skills.load"]


def test_client_verifies_live_context_digests():
    path = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
    spec = importlib.util.spec_from_file_location("context_skill_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = str(uuid4())
    context = {role: {"content": "# forged", "sha256": "0" * 64} for role in ROLES}
    reply = response_bytes(
        {
            "schema": SCHEMA,
            "request_id": key,
            "ok": True,
            "result": {"revision": 1, "context": context},
        }
    )
    client = module.ContextClient(io.BytesIO(reply), io.BytesIO())
    with pytest.raises(module.ContextRPCError) as error:
        client._exchange({"schema": SCHEMA, "request_id": key, "method": "skills.load"})
    assert error.value.code == "integrity_error"
