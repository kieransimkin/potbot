"""Frozen configuration precedence, secret handling, and concrete environment identity."""

from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.domain.configuration import resolve_configuration
from potbot.domain.errors import InvalidInputError
from potbot.models.configuration import EnvironmentManifest


def test_precedence_freeze_and_protected_values() -> None:
    layers = {
        "installation": {"max_packet_tokens": 100, "allowed_tools": ["compile"]},
        "deployment": {"max_packet_tokens": 200},
        "workspace": {"max_packet_tokens": 300},
        "repository": {"max_packet_tokens": 400},
        "task": {"max_packet_tokens": 500},
    }
    resolved = resolve_configuration(layers)
    assert resolved.settings["max_packet_tokens"] == 500
    layers["task"]["max_packet_tokens"] = 900
    result = resolved.settings
    result["max_packet_tokens"] = 1000
    assert resolved.settings["max_packet_tokens"] == 500
    with pytest.raises(InvalidInputError, match="protected"):
        resolve_configuration(layers, forbidden_overrides=frozenset({"max_packet_tokens"}))


@pytest.mark.parametrize(
    "layer",
    [
        {"unknown": {}},
        {"task": {"not_a_setting": True}},
        {"task": {"secret_refs": {"api": "value-that-is-secret"}}},
        {"task": {"physical_inference_concurrency": True}},
        {"task": {"policy_versions": {"search": ""}}},
    ],
)
def test_bad_or_secret_configuration_rejected(layer: dict) -> None:
    with pytest.raises(InvalidInputError):
        resolve_configuration(layer)


def test_secret_references_and_environment_fingerprint() -> None:
    config = resolve_configuration(
        {"workspace": {"secret_refs": {"search": "secret://env/SEARCH_KEY#v1"}}}
    )
    assert "SEARCH_KEY" in config.content_json
    manifest = EnvironmentManifest(
        uuid4(),
        config.fingerprint,
        "linux-6.1",
        "CPython-3.12.14",
        (("gcc", "12.2"), ("uv", "0.12.23")),
        secret_refs=(("search", "secret://env/SEARCH_KEY#v1"),),
    )
    assert (
        replace(manifest, toolchains=tuple(reversed(manifest.toolchains))).fingerprint
        == manifest.fingerprint
    )
    assert replace(manifest, interpreter="CPython-3.13.1").fingerprint != manifest.fingerprint
    with pytest.raises(InvalidInputError):
        replace(manifest, relevant_inputs=(("API_KEY", "raw-secret"),))
    with pytest.raises(InvalidInputError):
        replace(manifest, secret_refs=(("api", "raw-secret"),))
