"""Quick steering triage must default to deferred without missing explicit stops."""

from __future__ import annotations

import pytest

from potbot.controller.steering_triage import classify_steering
from potbot.domain.errors import InvalidInputError


@pytest.mark.parametrize(("message", "timing", "impact"), (
    ("Stop. Don't deploy this.", "interrupt", True),
    ("Don't do that, use PostgreSQL instead", "interrupt", True),
    ("By the way, stop publishing immediately", "interrupt", True),
    ("After you're done with that, add a dashboard", "deferred", False),
    ("Also implement automatic backups", "deferred", False),
    ("Oh and by the way, add a health dashboard", "deferred", False),
    ("Oh and by the way, it's a demo song", "background", False),
    ("FYI the ARM device has 2 GB RAM", "background", True),
    ("For context, this runs in production", "background", True),
))
def test_timing_defaults(message, timing, impact) -> None:
    outcome = classify_steering(message)
    assert outcome.timing == timing
    assert outcome.interrupt_requested == (timing == "interrupt")
    assert outcome.requires_evaluation is impact
    assert outcome.to_dict()["controller_mutated"] is False


def test_model_only_handles_ambiguous_messages() -> None:
    calls = []
    def model(message):
        calls.append(message)
        return {"timing": "deferred", "confidence": .94}
    assert classify_steering("Actually, that's interesting", model_classifier=model).model_used
    assert len(calls) == 1
    assert not classify_steering("Stop", model_classifier=model).model_used
    assert len(calls) == 1


def test_invalid_or_failed_model_never_silently_claims_completion() -> None:
    assert classify_steering("Maybe", model_classifier=lambda _: {"timing": "invalid", "confidence": 1}).model_used is False
    result = classify_steering("Maybe", model_classifier=lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    assert result.timing == "deferred" and result.rationale_code == "classifier_unavailable_default_deferred"
    with pytest.raises(InvalidInputError):
        classify_steering(" ")
