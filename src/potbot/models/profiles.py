"""Advisory profiles for Potbot's five default instruction-tuned models.

These profiles are not deployment records. They contain neither invented weight
revisions nor an inferred usable context or qualification. A runnable deployment
still needs an exact :class:`DeploymentSpec` and its own conformance evidence.
The installed, pinned tokenizer owns the chat template and token accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from potbot.domain.errors import InvalidInputError
from potbot.models.registry import DEFAULT_MODELS

SystemMessagePolicy = Literal["native", "prepend_first_user"]


@dataclass(frozen=True, slots=True)
class ModelProfile:
    canonical_model: str
    role: str
    prompt_adapter_id: str
    system_message_policy: SystemMessagePolicy
    source_urls: tuple[str, ...]
    notes: tuple[str, ...]
    access_conditions_required: bool = False
    response_protocol: str = "json_envelope_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_model": self.canonical_model,
            "role": self.role,
            "prompt_adapter_id": self.prompt_adapter_id,
            "system_message_policy": self.system_message_policy,
            "response_protocol": self.response_protocol,
            "template_source": "exact_local_tokenizer",
            "access_conditions_required": self.access_conditions_required,
            "source_urls": list(self.source_urls),
            "notes": list(self.notes),
            "deployment_configured": False,
            "qualified": False,
            "live_qualification_required": True,
        }


_PROFILES = (
    ModelProfile(
        "microsoft/Phi-4-mini-instruct",
        "reasoning",
        "phi4-mini-instruct-text-v1",
        "native",
        (
            "https://huggingface.co/microsoft/Phi-4-mini-instruct",
            "https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/tokenizer_config.json",
        ),
        (
            "Use the configured tokenizer's chat template for this exact instruction model.",
            "The documented native tool syntax does not imply backend or execution permission.",
            "The 128K family context claim is not a qualified deployment limit.",
            "Provider examples may execute model code; this profile never enables remote code.",
        ),
    ),
    ModelProfile(
        "google/gemma-3-4b-it",
        "reasoning",
        "gemma3-4b-it-text-v1",
        "prepend_first_user",
        (
            "https://huggingface.co/google/gemma-3-4b-it",
            "https://ai.google.dev/gemma/docs/core/prompt-structure",
        ),
        (
            "Text-only use; no image or audio input is admitted by this adapter profile.",
            "Controller instructions are explicitly prepended to the first user turn.",
            "Use the exact local tokenizer template; no other model's tool syntax is assumed.",
            "The 128K family context claim is not a qualified deployment limit.",
        ),
        access_conditions_required=True,
    ),
    ModelProfile(
        "Qwen/Qwen2.5-Coder-7B-Instruct",
        "coding",
        "qwen25-coder-7b-instruct-text-v1",
        "native",
        (
            "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct",
            "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct/blob/main/tokenizer_config.json",
        ),
        (
            "Use the instruction-tuned model, not the base completion model.",
            "The documented default configuration uses 32768 tokens.",
            "Longer YaRN configurations need distinct deployment identity and qualification.",
            "Tokenizer tool markers do not imply a qualified backend tool protocol.",
        ),
    ),
    ModelProfile(
        "google/codegemma-7b-it",
        "coding",
        "codegemma-7b-it-text-v1",
        "prepend_first_user",
        (
            "https://huggingface.co/google/codegemma-7b-it",
            "https://ai.google.dev/gemma/docs/core/prompt-structure",
        ),
        (
            "Use the instruction-tuned chat variant; base and FIM models are distinct.",
            "Controller instructions are explicitly prepended to the first user turn.",
            "Keep implementation packets compact and measure limits with the exact tokenizer.",
        ),
        access_conditions_required=True,
    ),
    ModelProfile(
        "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
        "coding",
        "deepseek-coder-v2-lite-instruct-text-v1",
        "native",
        (
            "https://huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
            "https://huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct/blob/main/tokenizer_config.json",
        ),
        (
            "Plan loading from all resident weights: 16B total parameters, 2.4B active.",
            "Card and tokenizer limits differ; use an explicitly qualified deployment limit.",
            "Provider examples may execute model code; this profile never enables remote code.",
            "Use the Instruct chat model, not the Base code-insertion protocol.",
        ),
    ),
)


def default_model_profiles() -> tuple[ModelProfile, ...]:
    """Return profiles in the same stable order as the durable model catalogue."""
    return _PROFILES


def get_model_profile(canonical_model: str) -> ModelProfile:
    for profile in _PROFILES:
        if profile.canonical_model == canonical_model:
            return profile
    raise InvalidInputError("the exact model has no installed default adapter profile")


def conformance_inventory(role: str) -> tuple[str, ...]:
    """Named requirements for future bounded live runners, not passing results."""
    if role not in {role for _, role in DEFAULT_MODELS}:
        raise InvalidInputError("conformance role must be reasoning or coding")
    common = (
        "exact_deployment_identity",
        "exact_template_and_tokenizer",
        "mandatory_context_overflow",
        "bounded_json_envelope",
        "retained_invalid_output_and_repair",
        "fragmented_stream_equivalence",
        "cancellation_and_deadline",
        "budget_admission_and_settlement",
        "foreign_model_evidence_identifiers",
        "unknown_precondition_rejection",
    )
    if role == "reasoning":
        return (*common, "focused_information_need", "typed_action", "implementation_contract")
    return (*common, "applicable_exact_base_patch", "targeted_checks", "classified_inability")
