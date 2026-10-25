"""Advisory small-model catalogue and explicitly selected quantized artifacts.

A model card is not an installed or qualified deployment. Quantization estimates
are planning aids, not assertions that an exact GGUF exists or fits in VRAM.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from potbot.domain.errors import InvalidInputError

ModelKind = Literal["generator", "reranker", "encoder", "embedding"]


@dataclass(frozen=True, slots=True)
class SpecialistModel:
    model_id: str
    total_parameters_b: float
    active_parameters_b: float
    architecture: Literal["dense", "moe", "hybrid", "encoder"]
    kind: ModelKind
    roles: tuple[str, ...]
    source_url: str
    gated: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if (
            not self.model_id or not self.source_url.startswith("https://")
            or self.kind not in ("generator", "reranker", "encoder", "embedding")
            or not self.roles or len(set(self.roles)) != len(self.roles)
            or not 0 < self.active_parameters_b <= self.total_parameters_b <= 1000
        ):
            raise InvalidInputError("invalid advisory specialist model")


# Upstream canonical model cards checked October 2026. No immutable weight revision
# or quantized file is endorsed or downloaded by catalogue discovery.
_MODELS: tuple[SpecialistModel, ...] = (
    SpecialistModel("Qwen/Qwen3.5-0.8B", .8, .8, "hybrid", "generator",
                    ("triage", "steering", "atomicity", "planning"),
                    "https://huggingface.co/Qwen/Qwen3.5-0.8B"),
    SpecialistModel("Qwen/Qwen3.5-2B", 2, 2, "hybrid", "generator",
                    ("reasoning", "planning", "hypothesis", "steering", "critique"),
                    "https://huggingface.co/Qwen/Qwen3.5-2B"),
    SpecialistModel("Qwen/Qwen3-0.6B", .6, .6, "dense", "generator",
                    ("triage", "steering", "atomicity", "classification"),
                    "https://huggingface.co/Qwen/Qwen3-0.6B"),
    SpecialistModel("Qwen/Qwen3-1.7B", 1.7, 1.7, "dense", "generator",
                    ("reasoning", "hypothesis", "planning", "critique", "steering"),
                    "https://huggingface.co/Qwen/Qwen3-1.7B"),
    SpecialistModel("Qwen/Qwen2.5-Coder-0.5B-Instruct", .5, .5, "dense", "generator",
                    ("coding", "diff_classification", "code_triage"),
                    "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct"),
    SpecialistModel("Qwen/Qwen2.5-Coder-1.5B-Instruct", 1.5, 1.5, "dense", "generator",
                    ("coding", "code_triage", "patch_review"),
                    "https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct"),
    SpecialistModel("LiquidAI/LFM2.5-230M", .23, .23, "hybrid", "generator",
                    ("triage", "steering", "tool_selection", "argument_extraction"),
                    "https://huggingface.co/LiquidAI/LFM2.5-230M",
                    notes="Not suitable for reasoning-heavy or code-generation tasks."),
    SpecialistModel("LiquidAI/LFM2.5-350M", .35, .35, "hybrid", "generator",
                    ("triage", "steering", "tool_selection", "classification"),
                    "https://huggingface.co/LiquidAI/LFM2.5-350M"),
    SpecialistModel("LiquidAI/LFM2.5-8B-A1B", 8.3, 1.5, "moe", "generator",
                    ("tool_selection", "planning", "reasoning", "critique"),
                    "https://huggingface.co/LiquidAI/LFM2.5-8B-A1B",
                    notes="Hybrid MoE, total weights ~8.3B; LFM licence and exact backend qualification required."),
    SpecialistModel("LiquidAI/LFM2.5-1.2B-Instruct", 1.17, 1.17, "hybrid", "generator",
                    ("triage", "steering", "planning", "tool_selection"),
                    "https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct",
                    notes="Check LFM licence, backend support and actual tokenizer."),
    SpecialistModel("LiquidAI/LFM2.5-1.2B-Thinking", 1.17, 1.17, "hybrid", "generator",
                    ("reasoning", "planning", "hypothesis", "critique"),
                    "https://huggingface.co/LiquidAI/LFM2.5-1.2B-Thinking",
                    notes="Check LFM licence, exact decoding controls and backend."),
    SpecialistModel("google/functiongemma-270m-it", .27, .27, "dense", "generator",
                    ("tool_selection", "argument_extraction", "intent"),
                    "https://huggingface.co/google/functiongemma-270m-it", gated=True,
                    notes="Specialized function calling; task-specific fine-tuning recommended."),
    SpecialistModel("HuggingFaceTB/SmolLM2-135M-Instruct", .135, .135, "dense", "generator",
                    ("triage", "classification_baseline"),
                    "https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct"),
    SpecialistModel("HuggingFaceTB/SmolLM2-360M-Instruct", .36, .36, "dense", "generator",
                    ("triage", "steering", "classification_baseline"),
                    "https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct"),
    SpecialistModel("HuggingFaceTB/SmolLM3-3B", 3, 3, "dense", "generator",
                    ("reasoning", "coding", "planning", "critique"),
                    "https://huggingface.co/HuggingFaceTB/SmolLM3-3B"),
    SpecialistModel("ibm-granite/granite-4.0-h-tiny", 7, 1, "moe", "generator",
                    ("tool_selection", "planning", "coding", "reasoning"),
                    "https://huggingface.co/ibm-granite/granite-4.0-h-tiny",
                    notes="7B total parameters, approximately 1B active; memory uses total."),
    SpecialistModel("Qwen/Qwen3-Reranker-0.6B", .6, .6, "dense", "reranker",
                    ("evidence_reranking", "source_relevance"),
                    "https://huggingface.co/Qwen/Qwen3-Reranker-0.6B",
                    notes="Reranking interface, not an ordinary chat-completion model."),
    SpecialistModel("Qwen/Qwen3-Embedding-0.6B", .6, .6, "dense", "embedding",
                    ("semantic_retrieval", "similarity", "deduplication"),
                    "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B"),
    SpecialistModel("answerdotai/ModernBERT-base", .15, .15, "encoder", "encoder",
                    ("classification_finetune", "embedding_finetune"),
                    "https://huggingface.co/answerdotai/ModernBERT-base",
                    notes="Base encoder; requires a trained task head before classification."),
)


# These are *planning schemes*, not known available artifact files. GGUF codec
# overhead and backend implementation vary. The measured manifest owns file size.
_QUANT_BITS: tuple[tuple[str, float], ...] = (
    ("Q2_K", 2.75), ("Q3_K_M", 3.50), ("Q4_K_M", 4.85),
    ("Q5_K_M", 5.70), ("Q6_K", 6.60), ("Q8_0", 8.50),
    ("F16", 16.00), ("BF16", 16.00),
)


def specialist_models(*, kind: ModelKind | None = None, role: str | None = None) -> tuple[SpecialistModel, ...]:
    return tuple(
        item for item in _MODELS
        if (kind is None or item.kind == kind) and (role is None or role in item.roles)
    )


def specialist_model(model_id: str) -> SpecialistModel:
    for item in _MODELS:
        if item.model_id == model_id:
            return item
    raise InvalidInputError("unknown specialist model identifier")


def quantization_choices(model_id: str) -> tuple[str, ...]:
    model = specialist_model(model_id)
    # GGUF is not assumed available or compatible with non-generative runtimes.
    if model.kind != "generator":
        return ("BF16",)
    return tuple(q for q, _ in _QUANT_BITS)


def parameter_estimate(total_parameters_b: float, quantization: str, *,
                       extra_context_vram_mib: int = 1024) -> dict[str, object]:
    """Estimates for model profiles that do not have an exact quantized artifact."""
    if (
        not isinstance(total_parameters_b, int | float) or isinstance(total_parameters_b, bool)
        or not 0 < total_parameters_b <= 1000 or quantization not in dict(_QUANT_BITS)
        or type(extra_context_vram_mib) is not int
        or not 0 <= extra_context_vram_mib <= 262144
    ):
        raise InvalidInputError("invalid parameter-based quantization preview")
    bits = dict(_QUANT_BITS)[quantization]
    weight_mib = math.ceil(total_parameters_b * 1e9 * bits / 8 / 2**20)
    return {
        "quantization": quantization, "bits_per_weight_estimate": bits,
        "estimated_disk_mib": weight_mib + max(32, math.ceil(weight_mib * .03)),
        "estimated_weight_only_vram_mib": weight_mib,
        "illustrative_full_gpu_vram_mib": weight_mib + extra_context_vram_mib,
        "context_runtime_allowance_mib": extra_context_vram_mib,
        "exact_artifact_available": None, "exact_disk_bytes": None,
        "measured_peak_vram_mib": None, "installed": False, "qualified": False,
        "requires_exact_artifact_discovery": True,
        "estimate_warning": (
            "Parameter-derived estimates only: actual GGUF/quantization availability, "
            "disk size, KV cache, runtime allocation and offloading must be qualified."
        ),
    }


def quantization_preview(model_id: str, quantization: str, *,
                         extra_context_vram_mib: int = 1024) -> dict[str, object]:
    """Produce a labelled approximation; never make an installation/readiness claim."""
    model = specialist_model(model_id)
    if quantization not in quantization_choices(model_id):
        raise InvalidInputError("quantization is not a candidate for this model kind")
    report = parameter_estimate(model.total_parameters_b, quantization,
                                extra_context_vram_mib=extra_context_vram_mib)
    return {"model_id": model_id, "architecture": model.architecture,
            "kind": model.kind, "total_parameters_b": model.total_parameters_b,
            "active_parameters_b": model.active_parameters_b, **report}



def shortlist_specialists(*, role: str, quantization: str = "Q4_K_M",
                          vram_mib: int, disk_free_mib: int,
                          context_headroom_mib: int = 1024) -> tuple[dict[str, object], ...]:
    """Advisory full-GPU planning shortlist; never asserts a model will load."""
    if (
        type(vram_mib) is not int or type(disk_free_mib) is not int
        or min(vram_mib, disk_free_mib) < 0
    ):
        raise InvalidInputError("hardware capacities must be nonnegative integers")
    selected = []
    for model in specialist_models(role=role):
        if quantization not in quantization_choices(model.model_id):
            continue
        preview = quantization_preview(model.model_id, quantization,
                                       extra_context_vram_mib=context_headroom_mib)
        if (preview["estimated_disk_mib"] <= disk_free_mib
                and preview["illustrative_full_gpu_vram_mib"] <= vram_mib):
            selected.append({**preview, "planning_feasible": True,
                             "runtime_qualification_required": True})
    return tuple(sorted(selected, key=lambda x: (x["estimated_disk_mib"], x["model_id"])))


@dataclass(frozen=True, slots=True)
class QuantizedArtifact:
    model_id: str
    quantization: str
    revision: str
    filename: str
    sha256: str
    size_bytes: int
    artifact_repository: str | None = None

    def __post_init__(self) -> None:
        specialist_model(self.model_id)
        if (
            self.quantization not in quantization_choices(self.model_id)
            or not re.fullmatch(r"[0-9a-f]{40}", self.revision)
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
            or not isinstance(self.filename, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}", self.filename)
            or type(self.size_bytes) is not int or not 0 < self.size_bytes <= 10**12
        ):
            raise InvalidInputError("exact quantized artifact identity is required")
        if self.artifact_repository is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
            self.artifact_repository,
        ):
            raise InvalidInputError("artifact repository must be an exact Hugging Face identity")
        if self.filename.endswith(".gguf") and specialist_model(self.model_id).kind == "generator":
            pass
        elif self.filename.endswith(".safetensors") and self.quantization in {"F16", "BF16"}:
            pass
        else:
            raise InvalidInputError("artifact format and requested quantization do not match")


def install_exact_artifact(
    artifact: QuantizedArtifact,
    *,
    models_root: Path,
    fetch: Callable[[QuantizedArtifact, Path], Path],
) -> Path:
    """Download only a caller-verified immutable artifact into an owned directory.

    The fetch callback is the operator's qualified Hugging Face/registry transport.
    This layer verifies the resulting local bytes; it never loads arbitrary code.
    """
    from potbot.tools.temporary import create_directory

    root = models_root.resolve()
    if not root.is_dir():
        raise InvalidInputError("model root must already exist")
    candidate = root / artifact.model_id.replace("/", "--") / artifact.revision / artifact.quantization
    # Check each controlled path component rather than traversing intermediate symlinks.
    first = create_directory(root / artifact.model_id.replace("/", "--"))
    second = create_directory(first / artifact.revision)
    candidate = create_directory(second / artifact.quantization)
    target = candidate / artifact.filename
    if target.is_symlink():
        raise InvalidInputError("symlinked model targets are prohibited")
    def verified(path: Path) -> bool:
        if path.stat().st_size != artifact.size_bytes:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(4 * 1024**2), b""):
                digest.update(block)
        return digest.hexdigest() == artifact.sha256
    if target.exists():
        if verified(target):
            return target
        raise InvalidInputError("conflicting installed model bytes; refusing overwrite")
    obtained = fetch(artifact, candidate)
    if (obtained.is_symlink() or obtained.resolve() != target.resolve()
            or not obtained.is_file() or not verified(obtained)):
        raise InvalidInputError("downloaded model failed identity, path or size verification")
    return obtained
