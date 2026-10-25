from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4
import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.worker_attribution import validate_host_agent_binding, worker_subject


def test_principal_not_treated_as_registered_agent() -> None:
    workspace, principal = uuid4(), uuid4()
    context = WorkspaceContext(workspace, principal)
    subject = worker_subject(context, registry=None, agent_id=None, ledger=object())
    assert subject.subject_id == principal and subject.identity_kind == "principal"


def test_registered_agent_is_checked_at_dispatch() -> None:
    workspace, principal, agent = uuid4(), uuid4(), uuid4()
    context = WorkspaceContext(workspace, principal)
    ledger = object()
    registration = SimpleNamespace(agent_id=agent, workspace_id=workspace)
    registry = SimpleNamespace(ledger=ledger, lookup=lambda ws, a: registration)
    subject = worker_subject(context, registry=registry, agent_id=agent, ledger=ledger)
    assert subject.subject_id == agent and subject.identity_kind == "agent"
    registry.lookup = lambda ws, a: SimpleNamespace(agent_id=agent, workspace_id=uuid4())
    with pytest.raises(ScopeDeniedError):
        worker_subject(context, registry=registry, agent_id=agent, ledger=ledger)
    registry.lookup = lambda ws, a: None
    with pytest.raises(ScopeDeniedError):
        worker_subject(context, registry=registry, agent_id=agent, ledger=ledger)


def test_agent_registry_and_id_must_be_paired_and_same_ledger() -> None:
    agent = uuid4()
    with pytest.raises(InvalidInputError):
        validate_host_agent_binding(shared=True, registry=None, agent_id=agent, ledger=object())
    with pytest.raises(ScopeDeniedError):
        validate_host_agent_binding(shared=False, registry=SimpleNamespace(), agent_id=agent, ledger=object())
    with pytest.raises(ScopeDeniedError):
        validate_host_agent_binding(shared=True, registry=SimpleNamespace(ledger=object()),
                                    agent_id=agent, ledger=object())
