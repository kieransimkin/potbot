"""Advisory local-model shortlist; never treats estimates as deployment readiness."""
from __future__ import annotations

from typing import Any

from potbot.models.profiles import default_model_profiles


def shortlist_local_models(*, vram_mb: int, ram_offload_mb: int = 0,
                           role: str = "reasoning", bits_per_weight: float = 4.5,
                           runtime_headroom_mb: int = 2048) -> list[dict[str, Any]]:
    """Sort plausible candidates; actual loading requires an exact qualified deployment.

    Estimates use total (not active) parameters and do not model per-layer
    quantization, KV cache, shared memory, or backend-specific tensor placement.
    """
    if min(vram_mb, ram_offload_mb, runtime_headroom_mb) < 0 or not 2 <= bits_per_weight <= 16:
        raise ValueError("invalid resource estimate")
    available = max(0, vram_mb + ram_offload_mb - runtime_headroom_mb)
    result = []
    for profile in default_model_profiles():
        if role not in (profile.roles or (profile.role,)) or profile.total_parameters_b is None:
            continue
        estimate = int(profile.total_parameters_b * 1000 * bits_per_weight / 8)
        if estimate > available:
            continue
        result.append({"model": profile.canonical_model,
                       "estimated_weight_mb": estimate,
                       "available_mb": available,
                       "qualified": False,
                       "requires_exact_deployment": True})
    return sorted(result, key=lambda item: (item["estimated_weight_mb"], item["model"]))
