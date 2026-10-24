"""Advisory profiles for Potbot's default instruction-tuned models.

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
    architecture: str = "unknown"
    total_parameters_b: float | None = None
    active_parameters_b: float | None = None
    roles: tuple[str, ...] = ()
    memory_note: str = "No measured residency or VRAM qualification"

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_model": self.canonical_model,
            "architecture": self.architecture,
            "total_parameters_b": self.total_parameters_b,
            "active_parameters_b": self.active_parameters_b,
            "roles": list(self.roles or (self.role,)),
            "memory_note": self.memory_note,
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


_MODERN_PROFILES = (
    # Identifiers are operator-verifiable candidate references, not pinned revisions.
    # Upstream repository spellings and weight availability must be verified at install.
    ModelProfile("google/gemma-4-26b-a4b-it", "reasoning", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/google",),
                 ("MoE candidate; verify exact repository and tokenizer before download.",),
                 access_conditions_required=True, architecture="moe", total_parameters_b=26,
                 active_parameters_b=4, roles=("reasoning", "coding")),
    ModelProfile("Qwen/Qwen3.5-35B-A3B", "reasoning", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/Qwen",),
                 ("35B-class MoE candidate; Qwen3.6 naming is not assumed to be a verified repository.",),
                 architecture="moe", total_parameters_b=35, active_parameters_b=3,
                 roles=("reasoning", "coding")),
    ModelProfile("LiquidAI/LFM2.5-8B-A1B", "reasoning", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/LiquidAI",),
                 ("Verify architecture and model repository before selecting an inference backend.",),
                 architecture="moe", total_parameters_b=8, active_parameters_b=1,
                 roles=("reasoning", "coding")),
    ModelProfile("microsoft/Phi-4-mini-reasoning", "reasoning", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/microsoft/Phi-4-mini-reasoning",),
                 ("Compact dense reasoning candidate; not the Phi-4-mini-instruct model.",),
                 architecture="dense", total_parameters_b=4,
                 roles=("reasoning",)),
    ModelProfile("Qwen/Qwen3-30B-A3B-Instruct-2507", "coding", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507",),
                 ("All expert weights count toward memory residency.",),
                 architecture="moe", total_parameters_b=30, active_parameters_b=3,
                 roles=("reasoning", "coding")),
    ModelProfile("openai/gpt-oss-20b", "reasoning", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/openai/gpt-oss-20b",),
                 ("Backend and quantization require independent qualification.",),
                 architecture="moe", total_parameters_b=20, active_parameters_b=4,
                 roles=("reasoning", "coding")),
    ModelProfile("google/gemma-4-e4b-it", "reasoning", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/google",),
                 ("Verify exact repository and access conditions before download.",),
                 access_conditions_required=True, architecture="dense", total_parameters_b=4,
                 roles=("reasoning", "coding")),
    ModelProfile("Qwen/Qwen3.5-9B", "coding", "tokenizer-native-v1", "native",
                 ("https://huggingface.co/Qwen",),
                 ("Dense low-memory candidate; qualify quantization and context separately.",),
                 architecture="dense", total_parameters_b=9,
                 roles=("reasoning", "coding")),
)


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
    return _MODERN_PROFILES + _PROFILES


def get_model_profile(canonical_model: str) -> ModelProfile:
    for profile in default_model_profiles():
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
