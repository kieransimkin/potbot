"""Generation input and exact deployment identities are independent of providers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.models.invocations import GenerateRequest, GenerationResult, PacketSource
from potbot.models.registry import DEFAULT_MODELS, DeploymentSpec


def spec() -> DeploymentSpec:
    return DeploymentSpec(
        "microsoft/Phi-4-mini-instruct",
        "a" * 64,
        "scripted",
        "1.0",
        "none",
        "b" * 40,
        "c" * 64,
        "json-v1",
        "none",
        4096,
        512,
        (("reasoning", "v1"),),
        ("reasoning",),
        ("actions-v1",),
    )


def test_exact_deployment_changes_identity() -> None:
    deployment = spec()
    for altered in (
        replace(deployment, weights_digest="d" * 64),
        replace(deployment, tokenizer_revision="e" * 40),
        replace(deployment, chat_template_digest="f" * 64),
    ):
        assert altered.fingerprint != deployment.fingerprint
    assert len(DEFAULT_MODELS) == 13
    assert [role for _, role in DEFAULT_MODELS].count("reasoning") == 8
    with pytest.raises(InvalidInputError):
        replace(deployment, tokenizer_revision="main")
    with pytest.raises(InvalidInputError):
        replace(deployment, output_adapter="")
    with pytest.raises(InvalidInputError):
        replace(deployment, max_output_tokens=5000)


def test_generation_request_freezes_sampling_and_requires_limits() -> None:
    request = GenerateRequest(
        uuid4(),
        "reasoning",
        uuid4(),
        "actions-v1",
        '{"temperature":0,"seed":42}',
        256,
        datetime.now(UTC) + timedelta(minutes=1),
        uuid4(),
    )
    assert request.to_dict()["sampling"] == {"temperature": 0, "seed": 42}
    for invalid in (
        '{"temperature":NaN}',
        '{"temperature":-1}',
        '{"temperature":0,"arbitrary":true}',
    ):
        with pytest.raises(InvalidInputError):
            replace(request, sampling_json=invalid)
    with pytest.raises(InvalidInputError):
        replace(request, deadline=datetime.now())
    with pytest.raises(InvalidInputError):
        replace(request, deadline="tomorrow")
    with pytest.raises(InvalidInputError):
        PacketSource("model_claim", uuid4())


def test_interrupted_generation_retains_output_and_usage() -> None:
    result = GenerationResult(
        uuid4(), None, 32, 8, 100, "cancelled", "partial", "cancelled", "cancelled"
    )
    assert result.output_tokens == 8 and result.raw_output_artifact_id
    with pytest.raises(InvalidInputError):
        replace(result, status="succeeded", error_code=None)
    with pytest.raises(InvalidInputError):
        replace(result, error_code=None)
