"""Job input and public-progress boundaries reject malformed or secret-bearing metadata."""

from copy import deepcopy
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.domain.jobs import JobSpec, lease_duration, progress_data, result_metadata, safe_integer


def payload():
    return {
        "job_type": "model.generate",
        "episode_id": str(uuid4()),
        "input": {"prompt": "private-prompt-canary"},
        "selected_models": [{"provider": "openai", "model": "fixture-model", "role": "reasoning"}],
    }


def test_input_is_copied_and_excluded_from_job_repr() -> None:
    value = payload()
    spec = JobSpec.from_dict(value)
    value["input"]["prompt"] = "changed"
    value["selected_models"][0]["model"] = "changed"
    assert spec.input["prompt"] == "private-prompt-canary"
    assert spec.selected_models[0]["model"] == "fixture-model"
    assert "private-prompt-canary" not in repr(spec)
    assert JobSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()


@pytest.mark.parametrize(
    "field,value",
    [
        ("episode_id", None),
        ("episode_id", "unknown"),
        ("job_type", []),
        ("job_type", "arbitrary.shell"),
        ("input", []),
        ("input", {"bad": float("nan")}),
        ("input", {"bad": "\ud800"}),
        ("selected_models", []),
        ("selected_models", [{"provider": "openai", "model": "x", "role": []}]),
        (
            "selected_models",
            [{"provider": "openai", "model": "x", "role": "coding", "api_key": "secret"}],
        ),
        (
            "selected_models",
            [{"provider": "https://untrusted.example", "model": "x", "role": "coding"}],
        ),
    ],
)
def test_invalid_submission_fields_are_classified(field, value) -> None:
    request = payload()
    request[field] = value
    with pytest.raises(InvalidInputError):
        JobSpec.from_dict(request)


def test_unknown_fields_duplicate_models_and_large_input_rejected() -> None:
    request = payload()
    request["api_key"] = "secret"
    with pytest.raises(InvalidInputError):
        JobSpec.from_dict(request)
    request = payload()
    request["selected_models"].append(deepcopy(request["selected_models"][0]))
    with pytest.raises(InvalidInputError):
        JobSpec.from_dict(request)
    request = payload()
    request["input"] = {"prompt": "x" * (1024 * 1024)}
    with pytest.raises(InvalidInputError):
        JobSpec.from_dict(request)


@pytest.mark.parametrize("value", [True, -1, 2**53, 0.5, "1", None])
def test_progress_and_cursor_counts_use_safe_integers(value) -> None:
    with pytest.raises(InvalidInputError):
        safe_integer(value, "Cursor")


@pytest.mark.parametrize(
    "stage,completed,total",
    [
        ("arbitrary secret text", 0, 1),
        ("working", 1, None),
        ("working", 2, 1),
        ("working", True, 1),
        ("working", -1, 1),
    ],
)
def test_invalid_progress_is_rejected(stage, completed, total) -> None:
    with pytest.raises(InvalidInputError):
        progress_data(stage, completed, total)


def test_valid_public_progress_and_result_metadata() -> None:
    assert progress_data("generating", 1, 2) == {"stage": "generating", "completed": 1, "total": 2}
    result = {
        "invocation_ids": [str(uuid4()), str(uuid4())],
        "completed_models": 2,
        "accounting": "pending_reconciliation",
        "usage": {"calls": 2, "input_tokens": 4},
    }
    assert result_metadata(result) == result


@pytest.mark.parametrize(
    "result",
    [
        {"exception": "provider secret"},
        {"api_key": "secret"},
        {"output": "arbitrary data"},
        {"usage": {"api_key": 42}},
        {"usage": {"calls": True}},
        {"invocation_id": "not-a-uuid"},
        {"invocation_ids": ["bad"]},
        {"accounting": "verified_paid_invoice"},
        {"finish_reason": "raw provider exception with secrets"},
        {"completed_models": 17},
    ],
)
def test_arbitrary_metadata_never_becomes_public_result(result) -> None:
    with pytest.raises(InvalidInputError):
        result_metadata(result)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 0, 3601, "1"])
def test_lease_duration_is_finite_and_bounded(value) -> None:
    with pytest.raises(InvalidInputError):
        lease_duration(value)
