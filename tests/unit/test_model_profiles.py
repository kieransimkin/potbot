"""Default adapter metadata must never impersonate an exact deployment."""

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.models.profiles import (
    conformance_inventory,
    default_model_profiles,
    get_model_profile,
)
from potbot.models.registry import DEFAULT_MODELS


def test_default_variants_roles_and_unqualified_status_are_explicit() -> None:
    profiles = default_model_profiles()
    assert tuple((profile.canonical_model, profile.role) for profile in profiles) == DEFAULT_MODELS
    for profile in profiles:
        exported = profile.to_dict()
        assert exported["qualified"] is False
        assert exported["deployment_configured"] is False
        assert exported["live_qualification_required"] is True
        assert "weights_digest" not in exported
        assert "tokenizer_revision" not in exported
        assert "usable_context_tokens" not in exported
        assert exported["template_source"] == "exact_local_tokenizer"
        assert all(url.startswith("https://") for url in profile.source_urls)


@pytest.mark.parametrize(
    "uninstalled",
    (
        "google/codegemma-7b",
        "Qwen/Qwen2.5-Coder-7B",
        "deepseek-ai/DeepSeek-Coder-V2-Lite-Base",
        "google/gemma-3-4b-it-qat-q4_0-gguf",
    ),
)
def test_family_names_and_alternative_variants_are_never_substituted(uninstalled: str) -> None:
    with pytest.raises(InvalidInputError):
        get_model_profile(uninstalled)


def test_google_instructions_have_an_explicit_first_user_policy() -> None:
    for name in ("google/codegemma-7b-it", "google/gemma-3-4b-it"):
        profile = get_model_profile(name)
        assert profile.system_message_policy == "prepend_first_user"
        assert profile.access_conditions_required
    assert get_model_profile("microsoft/Phi-4-mini-instruct").system_message_policy == "native"


def test_conformance_inventory_distinguishes_reasoning_and_coding_without_results() -> None:
    reasoner = conformance_inventory("reasoning")
    coder = conformance_inventory("coding")
    assert "typed_action" in reasoner and "applicable_exact_base_patch" not in reasoner
    assert "applicable_exact_base_patch" in coder and "typed_action" not in coder
    assert "foreign_model_evidence_identifiers" in reasoner
    assert len(reasoner) == len(set(reasoner))
    with pytest.raises(InvalidInputError):
        conformance_inventory("reviewer")
