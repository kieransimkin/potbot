"""Offline readiness checks retain blockers and never confer live qualification."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.models.preflight import (
    HostObservation,
    LocalHostProfile,
    PinnedFile,
    inspect_local_host,
    preflight_document,
    preflight_local,
    weights_inventory_digest,
)
from potbot.models.registry import DeploymentSpec


def host_profile(root: Path, *, json_template: bool = False) -> LocalHostProfile:
    directory = root / "models" / "pinned-fixture"
    directory.mkdir(parents=True)
    template = "fixture {{ messages }}"
    assets = [
        ("model-01.safetensors", b"first fixture shard", "weights"),
        ("model-02.safetensors", b"second fixture shard", "weights"),
        ("tokenizer.json", b'{"fixture": "tokenizer"}', "tokenizer"),
        ("config.json", b'{"fixture": "configuration"}', "configuration"),
    ]
    if json_template:
        assets.append(
            ("tokenizer_config.json", json.dumps({"chat_template": template}).encode(), "template")
        )
    else:
        assets.append(("chat_template.jinja", template.encode(), "template"))
    files = []
    for name, content, kind in assets:
        (directory / name).write_bytes(content)
        files.append(PinnedFile(name, hashlib.sha256(content).hexdigest(), len(content), kind))
    deployment = DeploymentSpec(
        canonical_model="microsoft/Phi-4-mini-instruct",
        weights_digest=weights_inventory_digest(tuple(files)),
        backend="fixture-backend",
        backend_version="1.2.3",
        quantization="fixture-only",
        tokenizer_revision="b" * 40,
        chat_template_digest=hashlib.sha256(template.encode()).hexdigest(),
        output_adapter="json_envelope_v1",
        tool_protocol="none",
        usable_context_tokens=1024,
        max_output_tokens=128,
        prompt_versions=(("reasoning", "fixture-v1"),),
        roles=("reasoning",),
        response_schemas=("fixture-response-v1",),
        resource_profile_json=json.dumps(
            {"memory_bytes": 500, "load_ms": 20, "device": "cpu", "measurement": "estimated"}
        ),
    )
    return LocalHostProfile(
        deployment,
        "a" * 40,
        "models/pinned-fixture",
        tuple(files),
        "fixture-backend",
        200,
        300,
    )


def observation(**changes: Any) -> HostObservation:
    return replace(HostObservation("fixture-backend", "1.2.3", "cpu", 2000, "fixture"), **changes)


def codes(report: Any) -> set[str]:
    return {blocker.code for blocker in report.blockers}


@pytest.mark.parametrize("json_template", (False, True))
def test_offline_manifest_success_means_only_ready_for_load_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_template: bool
) -> None:
    profile = host_profile(tmp_path, json_template=json_template)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preflight must not load models, import backends or use network")

    monkeypatch.setattr(socket, "socket", unexpected)
    monkeypatch.setattr(importlib, "import_module", unexpected)
    result = preflight_local(profile, project_root=tmp_path, observation=observation())
    assert result.status == "ready_for_load_check"
    assert result.checked_files == 5
    assert result.checked_bytes == sum(file.bytes for file in profile.files)
    assert result.required_memory_bytes == 1000
    exported = result.to_dict()
    assert exported["qualified"] is False
    assert exported["loading_tested"] is False
    assert exported["generation_tested"] is False
    assert exported["network_used"] is False
    assert exported["host_observation"]["origin"] == "fixture"
    assert exported["deployment_fingerprint"] == profile.deployment.fingerprint
    assert LocalHostProfile.from_dict(profile.to_dict()) == profile


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"backend_version": None}, "backend_unavailable"),
        ({"backend_version": "1.2.4"}, "backend_version_mismatch"),
        ({"backend_distribution": "another-backend"}, "backend_identity_mismatch"),
        ({"device": "cuda:0"}, "device_unavailable"),
        ({"available_memory_bytes": None}, "host_memory_unknown"),
        ({"available_memory_bytes": 999}, "insufficient_memory"),
    ),
)
def test_observed_runtime_and_capacity_must_match(
    tmp_path: Path, changes: dict[str, Any], expected: str
) -> None:
    profile = host_profile(tmp_path)
    result = preflight_local(profile, project_root=tmp_path, observation=observation(**changes))
    assert result.status == "blocked"
    assert expected in codes(result)
    assert result.to_dict()["qualified"] is False


def test_backend_cannot_be_satisfied_by_a_different_installed_package(tmp_path: Path) -> None:
    profile = host_profile(tmp_path)
    profile = replace(profile, deployment=replace(profile.deployment, backend="another-backend"))
    assert "backend_identity_mismatch" in codes(
        preflight_local(profile, project_root=tmp_path, observation=observation())
    )


@pytest.mark.parametrize("field", ("context_memory_bytes", "runtime_overhead_bytes"))
def test_zero_or_unknown_memory_does_not_imply_the_model_fits(tmp_path: Path, field: str) -> None:
    profile = replace(host_profile(tmp_path), **{field: 0})
    result = preflight_local(profile, project_root=tmp_path, observation=observation())
    assert "resource_profile_incomplete" in codes(result)
    assert result.required_memory_bytes is None


def test_weights_are_all_hashed_and_inventory_order_is_canonical(tmp_path: Path) -> None:
    profile = host_profile(tmp_path)
    assert weights_inventory_digest(profile.files) == weights_inventory_digest(profile.files[::-1])
    (tmp_path / profile.model_directory / "model-02.safetensors").write_bytes(
        b"changed bytes here!!"
    )
    result = preflight_local(profile, project_root=tmp_path, observation=observation())
    assert result.status == "blocked"
    assert "model_file_unavailable" in codes(result)


def test_declared_weight_and_template_identity_are_checked(tmp_path: Path) -> None:
    profile = host_profile(tmp_path)
    changed = replace(
        profile,
        deployment=replace(
            profile.deployment, weights_digest="e" * 64, chat_template_digest="f" * 64
        ),
    )
    result = preflight_local(changed, project_root=tmp_path, observation=observation())
    assert {"weights_identity_mismatch", "template_identity_mismatch"} <= codes(result)
    assert changed.fingerprint != profile.fingerprint


@pytest.mark.parametrize("linked_directory", (False, True))
def test_symlinks_are_not_followed(tmp_path: Path, linked_directory: bool) -> None:
    profile = host_profile(tmp_path)
    directory = tmp_path / profile.model_directory
    if linked_directory:
        relocated = directory.with_name("elsewhere")
        directory.rename(relocated)
        directory.symlink_to(relocated, target_is_directory=True)
        expected = "model_directory_unavailable"
    else:
        original = directory / "tokenizer.json"
        destination = tmp_path / "outside-tokenizer.json"
        original.rename(destination)
        original.symlink_to(destination)
        expected = "model_file_unavailable"
    result = preflight_local(profile, project_root=tmp_path, observation=observation())
    assert expected in codes(result)


@pytest.mark.parametrize(
    "unsafe", ("../elsewhere", "/tmp/weights", "models/../secrets", ".", "C:\\models")
)
def test_model_directories_cannot_escape_project_models(tmp_path: Path, unsafe: str) -> None:
    profile = host_profile(tmp_path)
    with pytest.raises(InvalidInputError):
        replace(profile, model_directory=unsafe)


@pytest.mark.parametrize("change", ("revision", "context", "backend", "resources", "secret"))
def test_incomplete_or_secret_bearing_configuration_has_a_sanitized_blocker(
    tmp_path: Path, change: str
) -> None:
    document = host_profile(tmp_path).to_dict()
    if change == "revision":
        document["model_revision"] = "main"
    elif change == "context":
        del document["deployment"]["usable_context_tokens"]
    elif change == "backend":
        document["deployment"]["backend_version"] = ""
    elif change == "resources":
        del document["runtime_overhead_bytes"]
    else:
        document["api_key"] = "CANARY-PROVIDER-SECRET"
    result = preflight_document(document, project_root=tmp_path)
    assert result.status == "blocked"
    assert codes(result) == {"invalid_host_profile"}
    assert "CANARY" not in json.dumps(result.to_dict())
    assert result.checked_files == 0


def test_local_host_metadata_check_does_not_import_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = host_profile(tmp_path)
    requested = []

    def version(name: str) -> str:
        requested.append(name)
        return "1.2.3"

    monkeypatch.setattr(importlib.metadata, "version", version)
    result = inspect_local_host(profile)
    assert requested == ["fixture-backend"]
    assert result.backend_version == "1.2.3"
    assert result.device == "cpu"
    assert result.origin == "local_os"


def test_raw_host_document_is_validated_before_decoding(tmp_path: Path) -> None:
    profile = host_profile(tmp_path)
    raw = json.dumps(profile.to_dict()).encode()
    report = preflight_document(raw, project_root=tmp_path, observation=observation())
    assert report.status == "ready_for_load_check"
    assert report.host_profile_fingerprint == profile.fingerprint


@pytest.mark.parametrize(
    "raw",
    (
        '{"schema_version":2,"schema_version":1}',
        '{"context_memory_bytes":NaN}',
        '{"context_memory_bytes":Infinity}',
        '{"context_memory_bytes":1e999}',
        '{"context_memory_bytes":9223372036854775808}',
        '{"schema_version":1} trailing text',
        "[" * 100 + "0" + "]" * 100,
        b"\xff",
        b" " * (1024 * 1024 + 1),
    ),
)
def test_ambiguous_or_unbounded_host_json_is_sanitized(tmp_path: Path, raw: bytes | str) -> None:
    report = preflight_document(raw, project_root=tmp_path)
    assert report.status == "blocked"
    assert codes(report) == {"invalid_host_profile"}
    assert report.checked_files == 0


def test_missing_backend_is_an_observation_not_a_model_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = host_profile(tmp_path)

    def unavailable(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", unavailable)
    assert inspect_local_host(profile).backend_version is None
