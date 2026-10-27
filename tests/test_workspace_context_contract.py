"""No-Postgres safety checks for the packaged context model."""
from uuid import uuid4

import pytest

from potbot.context.model import (
    ROLES,
    document_templates,
    sandbox_object_id,
    sha256,
    validate_content,
    validate_identity,
)
from potbot.domain.errors import InvalidInputError


def test_templates_are_deterministic_and_distinguish_model_from_memory():
    first = document_templates("skill", "render")
    assert first == document_templates("skill", "render")
    assert set(first) == set(ROLES)
    assert "Observed state" in first["MODEL.md"]
    assert "History" in first["MEMORY.md"]
    assert "Instructions" in first["AGENTS.md"]


@pytest.mark.parametrize("bad", ["../run", "skill/name", "", "a" * 129, "-option"])
def test_names_are_path_safe(bad):
    with pytest.raises(InvalidInputError):
        validate_identity("agent", bad, "MODEL.md")


def test_role_and_utf8_restrictions():
    with pytest.raises(InvalidInputError):
        validate_identity("tool", "one", "agents.md")
    with pytest.raises(InvalidInputError):
        validate_content("unsafe\x00text")
    with pytest.raises(InvalidInputError):
        validate_content("\ud800")
    with pytest.raises(InvalidInputError):
        validate_content("☃" * 400_000)
    assert len(sha256("test")) == 64


def test_workspace_sandbox_id_is_stable_and_isolated():
    a, b = uuid4(), uuid4()
    assert sandbox_object_id(a) == sandbox_object_id(a)
    assert sandbox_object_id(a) != sandbox_object_id(b)
