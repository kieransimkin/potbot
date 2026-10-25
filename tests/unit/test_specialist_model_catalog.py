"""No-model-download tests for the specialist catalogue and exact artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.models.cli import main as models_cli
from potbot.models.specialist_catalog import (
    QuantizedArtifact,
    install_exact_artifact,
    quantization_choices,
    quantization_preview,
    specialist_model,
    specialist_models,
)


def test_verified_upstream_models_cover_small_router_coder_and_specialists() -> None:
    catalogue = specialist_models()
    ids = {item.model_id for item in catalogue}
    assert len(ids) == len(catalogue) >= 15
    assert "Qwen/Qwen3.5-0.8B" in ids
    assert "Qwen/Qwen2.5-Coder-0.5B-Instruct" in ids
    assert "google/functiongemma-270m-it" in ids
    assert "Qwen/Qwen3-Reranker-0.6B" in ids
    assert "answerdotai/ModernBERT-base" in ids
    assert specialist_model("google/functiongemma-270m-it").gated
    assert specialist_model("ibm-granite/granite-4.0-h-tiny").active_parameters_b == 1
    assert "BF16" in quantization_choices("answerdotai/ModernBERT-base")
    assert "Q4_K_M" not in quantization_choices("answerdotai/ModernBERT-base")
    assert {item.model_id for item in specialist_models(role="coding")}


def test_all_weight_estimates_use_total_moe_parameter_count() -> None:
    moe = quantization_preview("ibm-granite/granite-4.0-h-tiny", "Q4_K_M")
    dense = quantization_preview("Qwen/Qwen3-1.7B", "Q4_K_M")
    assert moe["estimated_disk_mib"] > dense["estimated_disk_mib"]
    assert moe["total_parameters_b"] == 7
    assert moe["active_parameters_b"] == 1
    assert moe["measured_peak_vram_mib"] is None
    assert moe["exact_artifact_available"] is None
    assert moe["qualified"] is False
    bf16 = quantization_preview("Qwen/Qwen3-1.7B", "BF16")
    q2 = quantization_preview("Qwen/Qwen3-1.7B", "Q2_K")
    assert bf16["estimated_disk_mib"] > q2["estimated_disk_mib"]
    with pytest.raises(InvalidInputError):
        quantization_preview("Qwen/Qwen3-1.7B", "fake")
    with pytest.raises(InvalidInputError):
        quantization_preview("Qwen/Qwen3-1.7B", "Q4_K_M", extra_context_vram_mib=-1)


def test_exact_quantization_download_requires_hash_and_pinned_revision(tmp_path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    payload = b"synthetic model weights - never a real deployment"
    artifact = QuantizedArtifact(
        "Qwen/Qwen3-0.6B", "Q4_K_M", "a" * 40, "model.Q4_K_M.gguf",
        hashlib.sha256(payload).hexdigest(), len(payload),
    )
    calls = []

    def fetch(a, destination):
        calls.append(a)
        output = destination / a.filename
        output.write_bytes(payload)
        return output

    path = install_exact_artifact(artifact, models_root=root, fetch=fetch)
    assert path.read_bytes() == payload
    assert install_exact_artifact(artifact, models_root=root, fetch=fetch) == path
    assert len(calls) == 1
    with pytest.raises(InvalidInputError):
        install_exact_artifact(replace(artifact, sha256="b" * 64), models_root=root, fetch=fetch)
    with pytest.raises(InvalidInputError):
        replace(artifact, revision="main")
    with pytest.raises(InvalidInputError):
        replace(artifact, filename="../escape.gguf")
    assert list(tmp_path.glob("escape*")) == []


def test_invalid_download_does_not_claim_installation(tmp_path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    artifact = QuantizedArtifact(
        "Qwen/Qwen3-0.6B", "Q4_K_M", "a" * 40, "weights.gguf", "b" * 64, 6,
    )

    def fetch(_artifact, destination):
        path = destination / "weights.gguf"
        path.write_bytes(b"wrong!")
        return path

    with pytest.raises(InvalidInputError):
        install_exact_artifact(artifact, models_root=root, fetch=fetch)


def test_catalog_cli_reports_estimates_as_unqualified(capsys) -> None:
    assert models_cli(["specialists", "--model", "Qwen/Qwen3-0.6B", "--quantization", "Q5_K_M"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["advisory"] is True
    assert report["models"][0]["selected_preview"]["quantization"] == "Q5_K_M"
    assert report["models"][0]["selected_preview"]["installed"] is False
    assert models_cli(["catalog"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert any(row["quantization_planning_previews"] for row in catalog["profiles"])
    assert all(not row["qualified"] for row in catalog["profiles"])


def test_advisory_shortlist_requires_disk_and_gpu_headroom() -> None:
    from potbot.models.specialist_catalog import shortlist_specialists

    small = shortlist_specialists(role="triage", vram_mib=2048, disk_free_mib=1500)
    assert any(item["model_id"] == "Qwen/Qwen3-0.6B" for item in small)
    assert all(item["planning_feasible"] and not item["qualified"] for item in small)
    assert not any(item["model_id"] == "ibm-granite/granite-4.0-h-tiny" for item in small)
    with pytest.raises(InvalidInputError):
        shortlist_specialists(role="triage", vram_mib=-1, disk_free_mib=1000)
