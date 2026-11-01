"""Release safety checks, without database credentials or a live guest."""
from uuid import uuid4

import pytest

from potbot.context.agent import ContextIdentity, ContextMutationBroker
from potbot.context.model import ROLES, document_templates
from potbot.context.syn_release import context_syn_catalog, context_syn_types
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.semantics.catalog import extension_types


def test_context_roles_do_not_promote_memory_or_model_into_instructions():
    docs = document_templates("sandbox", str(uuid4()))
    assert set(docs) == set(ROLES)
    assert "Observed state" in docs["MODEL.md"]
    assert "History" in docs["MEMORY.md"]


def test_host_broker_rejects_foreign_contexts_before_service_dispatch():
    workspace, principal, agent = uuid4(), uuid4(), uuid4()
    actor = ContextIdentity(WorkspaceContext(workspace, principal), agent)
    for kind, name, role in (
        ("sandbox", str(uuid4()), "AGENTS.md"),
        ("agent", str(uuid4()), "MEMORY.md"),
        ("tool", "arbitrary", "MODEL.md"),
    ):
        with pytest.raises(ScopeDeniedError):
            ContextMutationBroker._permitted(actor, kind, name, role)
    # All three roles are deliberately writable through the broker in the
    # current contract. The service's PostgreSQL grants/CAS remain mandatory.
    for role in ROLES:
        ContextMutationBroker._permitted(actor, "agent", str(agent), role)
        ContextMutationBroker._permitted(actor, "sandbox", str(workspace), role)


def test_syn_file_role_extension_is_opt_in():
    original = extension_types()
    added = context_syn_types()
    merged = context_syn_catalog()
    assert len(added) == 3
    assert len(original) == 47
    assert len(merged) == 529 + len(original) + 3
    assert {"MODEL.md", "MEMORY.md", "SKILL.md"} == {row.name for row in added}
    assert extension_types() == original  # Original 0.1.0 remains pinned.
