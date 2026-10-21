"""Exact recipient counting and indivisible optional-memory qualification."""

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.errors import ContextOverflowError, InvalidInputError, StateMismatchError
from potbot.models.context import (
    ArtifactContextSource,
    FixtureContextRenderer,
    LocalTokenizerRenderer,
    MemoryBundle,
    TokenizedText,
    render_context,
)
from potbot.models.registry import DeploymentSpec


def deployment(renderer, context=100000, role="reasoning"):
    return DeploymentSpec(
        renderer.canonical_model,
        "a" * 64,
        "fixture",
        "1",
        "none",
        renderer.tokenizer_revision,
        renderer.chat_template_digest,
        "json-envelope-v1",
        "none",
        context,
        256,
        ((role, renderer.prompt_version),),
        (role,),
        ("action-proposal-v1" if role == "reasoning" else "coding-result-v1",),
    )


def render(renderer, *, spec=None, mandatory=None, optional=()):
    return render_context(
        deployment=spec or deployment(renderer),
        renderer=renderer,
        role="reasoning",
        response_schema="action-proposal-v1",
        output_limit=100,
        mandatory=mandatory or ({"requirement": "Keep every constraint and source version."},),
        optional=optional,
    )


def bundle(name, text, qualification="Use only with library version 2; evidence is unverified."):
    return {
        "name": name,
        "records": [
            {"source": {"id": "memory", "version": "2"}, "text": text},
            {"source": {"id": "qualification", "version": "1"}, "text": qualification},
        ],
    }


def test_two_recipient_tokenizers_measure_reproducible_exact_wire_input():
    first = FixtureContextRenderer(bytes_per_token=3)
    second = FixtureContextRenderer(bytes_per_token=7)
    one, two = render(first), render(second)
    assert one == render(first) and two == render(second)
    assert one["wire_text"] == two["wire_text"]
    assert one["wire_sha256"] == hashlib.sha256(one["wire_text"].encode()).hexdigest()
    assert one["input_tokens"] > two["input_tokens"]
    assert one["tokenizer_revision"] != two["tokenizer_revision"]
    assert one["token_ids_sha256"] != two["token_ids_sha256"]
    assert one["measurement_origin"] == two["measurement_origin"] == "synthetic"


def test_oversized_optional_bundle_is_omitted_with_all_qualifications_and_later_small_one_fits():
    renderer = FixtureContextRenderer()
    baseline = render(renderer)
    spec = deployment(renderer, baseline["input_tokens"] + 100 + 110)
    result = render(
        renderer,
        spec=spec,
        optional=(bundle("large", "not_allowed_in_context_" * 1000), bundle("small", "use json")),
    )
    assert result["included_optional_bundles"] == ["small"]
    assert result["omissions"][0]["name"] == "large"
    assert len(result["omissions"][0]["sources"]) == 2
    assert "not_allowed_in_context_" not in result["wire_text"]
    assert "Use only with library version 2" in result["wire_text"]
    assert result["input_tokens"] + result["output_limit"] <= spec.usable_context_tokens


def test_output_reservation_causes_mandatory_overflow_without_truncation():
    renderer = FixtureContextRenderer()
    baseline = render(renderer)
    with pytest.raises(ContextOverflowError):
        render(renderer, spec=deployment(renderer, baseline["input_tokens"] + 99))
    exact = render(renderer, spec=deployment(renderer, baseline["input_tokens"] + 100))
    assert exact["wire_text"] == baseline["wire_text"]


@pytest.mark.parametrize(
    "changed", ["tokenizer_revision", "chat_template_digest", "prompt_versions"]
)
def test_recipient_fingerprint_changes_reject_the_previous_renderer(changed):
    renderer = FixtureContextRenderer()
    spec = deployment(renderer)
    value = (("reasoning", "different-version"),) if changed == "prompt_versions" else "e" * 64
    with pytest.raises(StateMismatchError):
        render(renderer, spec=replace(spec, **{changed: value}))


class LocalTokenizer:
    chat_template = "exact-installed-template-v1"

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert tokenize is False and add_generation_prompt is True
        self.messages = messages
        return "<bos>" + repr(messages) + "<generation>"

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        return list(text.encode())


@pytest.mark.parametrize(
    "model,roles",
    [
        ("google/gemma-3-4b-it", ["user"]),
        ("google/codegemma-7b-it", ["user"]),
        ("microsoft/Phi-4-mini-instruct", ["system", "user"]),
    ],
)
def test_local_renderer_delegates_exact_template_without_duplicate_special_tokens(model, roles):
    tokenizer = LocalTokenizer()
    renderer = LocalTokenizerRenderer(tokenizer, canonical_model=model, tokenizer_revision="b" * 40)
    result = renderer.render("instructions", "source data")
    assert [message["role"] for message in tokenizer.messages] == roles
    assert "instructions" in result.text and "source data" in result.text
    assert result.token_ids == tuple(result.text.encode())
    assert renderer.measurement_origin == "measured"
    tokenizer.chat_template = "mutated"
    with pytest.raises(StateMismatchError):
        renderer.render("instructions", "source data")


def test_source_records_keep_scheme_spelling_versions_and_qualifiers():
    identity = uuid4()
    qualification = ArtifactContextSource(uuid4(), "b" * 64)
    source = ArtifactContextSource(
        identity,
        "a" * 64,
        "syn://type/potbot-data-text",
        "v2",
        (f"artifact:{qualification.artifact_id}",),
    )
    other = replace(source, type_ref="synny://type/potbot-data-text")
    assert source.to_dict()["type_ref"].startswith("syn://")
    assert other.to_dict()["type_ref"].startswith("synny://")
    memory = MemoryBundle("cross-model-memory", source, (qualification,), required=True)
    assert memory.to_dict()["content"]["version"] == "v2"
    assert memory.to_dict()["required"] is True
    assert memory.to_dict()["content"]["semantic_validation"] == "syntax_only"
    with pytest.raises(InvalidInputError):
        replace(source, type_ref="dt:memory")
    with pytest.raises(InvalidInputError):
        replace(source, qualifier_refs=(f"artifact:{qualification.artifact_id}",) * 2)
    with pytest.raises(InvalidInputError):
        TokenizedText("input", (True,))


def test_essential_qualification_cannot_be_referenced_without_its_artifact_in_bundle():
    qualification = ArtifactContextSource(uuid4(), "b" * 64)
    source = ArtifactContextSource(
        uuid4(), "a" * 64, qualifier_refs=(f"artifact:{qualification.artifact_id}",)
    )
    with pytest.raises(InvalidInputError, match="same bundle"):
        MemoryBundle("missing-qualification", source)
    with pytest.raises(InvalidInputError):
        MemoryBundle(
            "self-qualification",
            replace(source, qualifier_refs=(f"artifact:{source.artifact_id}",)),
            (qualification,),
        )
    with pytest.raises(InvalidInputError):
        replace(source, qualifier_refs=("https://example.test/qualifier",))
    complete = MemoryBundle("complete", source, (qualification,))
    assert MemoryBundle.from_dict(complete.to_dict()) == complete
