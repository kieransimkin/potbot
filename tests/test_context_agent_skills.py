"""Unit checks for the host-only context/skill addition (no PostgreSQL required)."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.context.agent import (
    AgentContextLifecycle, ContextIdentity, ContextMutationBroker, context_instruction_packet,
)
from potbot.context.model import document_templates, sha256
from potbot.context.skills import SharedSkillReleaseStore, validate_skill
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError


class FakeHome:
    guest_path = "/home/agent-home"


class FakeRegistry:
    def __init__(self):
        self.provisioned = False

    def provision(self, workspace_id, agent_id):
        self.provisioned = True
        return FakeHome()

    def lookup(self, workspace_id, agent_id):
        return FakeHome() if self.provisioned else None


class FakeContextService:
    def __init__(self):
        self.provisions = []
        self.calls = []

    def provision_private(self, ctx, kind, name, **kwargs):
        self.provisions.append((ctx, kind, name, kwargs))
        return {"kind": kind, "name": name}

    def read_triple(self, ctx, kind, name):
        return {role: {"content": content, "revision": 1, "sha256": sha256(content)}
                for role, content in document_templates(kind, name).items()}

    def update(self, ctx, kind, name, role, **kwargs):
        self.calls.append((ctx, kind, name, role, kwargs))
        return {"revision": kwargs["expected_revision"] + 1,
                "sha256": sha256(kwargs["content"]), "role": role}


class FakeLedger:
    def __init__(self):
        self.requests = []
        self.completions = []

    def image_status(self, workspace_id):
        return {"digest": "a" * 64}

    def claim(self, request, **kwargs):
        self.requests.append(request)
        return object()

    def finish(self, lease, *, result_sha256, uncertain=False):
        self.completions.append((result_sha256, uncertain))


def test_lifecycle_reconciles_agent_after_home_registration():
    service = FakeContextService()
    registry = FakeRegistry()
    identity = ContextIdentity(WorkspaceContext(uuid4(), uuid4()), uuid4())
    manager = AgentContextLifecycle(service, registry)
    manager.ensure_placeholders = lambda identity, home: None  # standalone lifecycle contract
    home = manager.provision(identity, idempotency_key="first")
    assert home.guest_path == "/home/agent-home"
    assert service.provisions[0][1:3] == ("agent", str(identity.agent_id))
    packet = context_instruction_packet(manager.read(identity))
    document = json.loads(packet)
    assert [x["role"] for x in document["documents"]] == list(document_templates("agent", "a")) * 2
    assert all(x["classification"] == "untrusted-context-data"
               for x in document["documents"] if x["role"] in {"MODEL.md", "MEMORY.md"})


def test_broker_enforces_agent_scope_and_lease():
    service = FakeContextService()
    registry = FakeRegistry()
    ledger = FakeLedger()
    identity = ContextIdentity(WorkspaceContext(uuid4(), uuid4()), uuid4())
    broker = ContextMutationBroker(service=service, registry=registry, ledger=ledger)
    with pytest.raises(ScopeDeniedError):
        broker.update(identity, "agent", str(identity.agent_id), "MODEL.md",
                      content="hello", expected_revision=1, idempotency_key="a")
    registry.provision(identity.principal.workspace_id, identity.agent_id)
    with pytest.raises(ScopeDeniedError):
        broker.update(identity, "sandbox", str(identity.principal.workspace_id), "AGENTS.md",
                      content="bad", expected_revision=1, idempotency_key="b")
    with pytest.raises(ScopeDeniedError):
        broker.update(identity, "agent", str(uuid4()), "MODEL.md",
                      content="bad", expected_revision=1, idempotency_key="c")
    result = broker.update(identity, "agent", str(identity.agent_id), "MODEL.md",
                           content="new", expected_revision=1, idempotency_key="good")
    assert result["revision"] == 2
    assert ledger.requests[0].operation == "context.update"
    assert ledger.requests[0].resources[0].name.endswith(":model.md")
    assert ledger.completions == [(sha256("new"), False)]


def test_packet_limit_and_missing_roles():
    identity = ContextIdentity(WorkspaceContext(uuid4(), uuid4()), uuid4())
    fake = FakeContextService()
    manager = AgentContextLifecycle(fake, FakeRegistry())
    with pytest.raises(ScopeDeniedError):
        manager.read(identity)
    bundle = {"guest_home": "/home/a", "sandbox": fake.read_triple(identity.principal, "sandbox", "a"),
              "agent": fake.read_triple(identity.principal, "agent", "b")}
    with pytest.raises(InvalidInputError):
        context_instruction_packet(bundle, max_bytes=1024)
    del bundle["agent"]["MODEL.md"]
    with pytest.raises(InvalidInputError):
        context_instruction_packet(bundle)


def test_shared_skill_release_is_immutable_and_idempotent(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir(mode=0o700)
    store = SharedSkillReleaseStore(root)
    skill = "---\nname: text-tools\ndescription: Edit text safely\n---\n\n# Steps\nDo things.\n"
    first = store.publish("text-tools", skill)
    second = store.publish("text-tools", skill)
    assert first == second
    assert (first.path / "SKILL.md").read_text() == skill
    assert (first.path / "MODEL.md").is_file()
    assert (first.path / "MEMORY.md").is_file()
    with pytest.raises(InvalidInputError):
        store.publish("invalid_name", skill)
    with pytest.raises(InvalidInputError):
        store.publish("text-tools", skill.replace("name: text-tools", "name: other"))
    corrupted = first.path / "SKILL.md"
    corrupted.chmod(0o600)
    corrupted.write_text("corrupted")
    corrupted.chmod(0o444)
    with pytest.raises(StateMismatchError):
        store.publish("text-tools", skill)


def test_skill_root_and_redirected_slug_rejected(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir(mode=0o700)
    bad = tmp_path / "insecure"
    bad.mkdir(mode=0o777)
    bad.chmod(0o777)
    with pytest.raises(ScopeDeniedError):
        SharedSkillReleaseStore(bad)
    (root / "text-tools").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ScopeDeniedError):
        SharedSkillReleaseStore(root).publish(
            "text-tools", "---\nname: text-tools\ndescription: something\n---\n")


def test_agent_placeholder_creation_and_refusal_of_existing_content(tmp_path: Path):
    from potbot.context.projection import AgentContextProjection, ContextProjectionManager
    from potbot.context.model import ROLES
    from potbot.domain.errors import ScopeDeniedError

    workspace = uuid4()
    agent = uuid4()
    ctx = WorkspaceContext(workspace, uuid4())
    source = tmp_path / "operator"
    source.mkdir(mode=0o700)
    guest = tmp_path / "workspace-home"
    guest.mkdir()
    folder = guest / "agent-01"
    folder.mkdir()
    registry = FakeRegistry()
    registry.provisioned = True
    registry.ledger = FakeLedger()
    registry._guard = lambda *_args: object()
    registry._dir_fd = lambda: __import__("os").open(guest, __import__("os").O_RDONLY | __import__("os").O_DIRECTORY)
    home = FakeHome()
    home.directory = "agent-01"
    manager = AgentContextLifecycle(FakeContextService(), registry)
    manager.ensure_placeholders(ContextIdentity(ctx, agent), home)
    assert {x.name for x in folder.iterdir()} == set(ROLES)
    manager.ensure_placeholders(ContextIdentity(ctx, agent), home)
    projection_manager = ContextProjectionManager(service=FakeContextService(), principal=ctx,
                                                   parent=source)
    projection = projection_manager.capture_object("agent", str(agent))
    binding = AgentContextProjection(agent, "agent-01", projection)
    assert len(binding.inspect(home=guest)) == 3
    (folder / "MODEL.md").chmod(0o600)
    (folder / "MODEL.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ScopeDeniedError):
        binding.inspect(home=guest)
    projection_manager.close()


def test_context_acl_sql_has_explicit_shared_writer_boundary():
    from potbot.persistence.context_acl_schema import MIGRATION_STATEMENTS
    sql = "\n".join(MIGRATION_STATEMENTS)
    assert "workspace_context_write_grants" in sql
    assert "AS RESTRICTIVE FOR UPDATE" in sql
    assert "WITH CHECK (false)" in sql
    assert "o.visibility='shared'" in sql
    assert "current_setting('potbot.principal_id',true)" in sql
