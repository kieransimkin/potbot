"""Fast advisory classification of user steering before a reasoning checkpoint.

The classifier never cancels a tool or mutates a graph. Explicit user stop
signals are elevated to a controller interrupt; only a trusted controller
executes it. Default is deferred work, unless a message is clearly background.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from potbot.domain.errors import InvalidInputError

SteeringTiming = Literal["interrupt", "deferred", "background", "clarify"]

# This fast path is intentionally narrow: it must not treat quotes, broad
# references to cancellation or negative sentiment as an emergency instruction.
_STOP = re.compile(
    r"^(?:please\s+)?(?:stop|pause|cancel|abort|halt|hold\s+off|"
    r"don['’]t\s+(?:do|run|send|publish|deploy|install|delete|modify|continue|proceed)|"
    r"do\s+not\s+(?:do|run|send|publish|deploy|install|delete|modify|continue|proceed)|"
    r"instead\s+(?:do|use|try)|switch\s+to)\b", re.I
)
_DEFER = re.compile(
    r"\b(?:after\s+(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish|complete)|"
    r"once\s+(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish|complete)|"
    r"when\s+(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish|complete)|"
    r"later\b|next\s+task|afterwards)\b", re.I
)
_CONTEXT = re.compile(
    r"^(?:oh\s+and\s+)?(?:by\s+the\s+way|for\s+(?:your\s+)?(?:information|context)|"
    r"just\s+(?:so\s+you\s+know|fyi)|fyi|as\s+background)\b", re.I
)
_MATERIAL = re.compile(
    r"\b(?:\d+\s*(?:gib?|mib?|gb|mb)\s*(?:ram|vram|memory)?|"
    r"budget|deadline|deployment|production|licen[cs]e|restricted|permission|"
    r"platform|hardware|architecture|arm(?:64)?|windows|linux|data\s+loss)\b", re.I
)
_DIRECTIVE = re.compile(r"\b(?:do|make|add|implement|fix|change|update|create|run|deploy|use)\b", re.I)


@dataclass(frozen=True, slots=True)
class SteeringClassification:
    timing: SteeringTiming
    confidence: float
    rationale_code: str
    requires_evaluation: bool
    interrupt_requested: bool
    model_used: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise InvalidInputError("classification confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "timing": self.timing,
            "confidence": self.confidence,
            "rationale_code": self.rationale_code,
            "requires_evaluation": self.requires_evaluation,
            "interrupt_requested": self.interrupt_requested,
            "model_used": self.model_used,
            "controller_mutated": False,
        }


def _rules(message: str) -> SteeringClassification:
    lowered = message.strip()
    # Prioritize actual imperative stop phrases even within a prefatory FYI.
    segments = re.split(r"[\n;.!?]+|\s+and\s+then\s+", lowered)
    for item in segments:
        cleaned = item.strip()
        cleaned = re.sub(r"^(?:oh\s+and\s+)?by\s+the\s+way\s*,?\s*", "", cleaned, flags=re.I)
        if _STOP.match(cleaned):
            return SteeringClassification("interrupt", 1.0, "explicit_stop_directive", True, True)
    if _DEFER.search(lowered):
        return SteeringClassification("deferred", .99, "explicit_later_dependency", False, False)
    if _CONTEXT.match(lowered):
        if _DIRECTIVE.search(lowered):
            return SteeringClassification("deferred", .86, "background_preface_with_action", False, False)
        material = bool(_MATERIAL.search(lowered))
        return SteeringClassification("background", .96, "background_material" if material else "background_note", material, False)
    if _DIRECTIVE.search(lowered):
        return SteeringClassification("deferred", .82, "default_deferred_action", False, False)
    if lowered.endswith("?"):
        return SteeringClassification("clarify", .7, "question_not_explicit_steering", False, False)
    return SteeringClassification("background", .6, "non_directive_context", True, False)


def classify_steering(
    message: str,
    *,
    model_classifier: Callable[[str], dict[str, Any]] | None = None,
) -> SteeringClassification:
    """Rule-first steering with an optional small-model classifier.

    A model can refine ambiguous messages only; it cannot override explicit
    stop/defer/background instructions or directly mutate a graph. The operator
    decides which model and prompt are allowed to see this text.
    """
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 32768:
        raise InvalidInputError("steering input must be bounded UTF-8 text")
    preliminary = _rules(message)
    if preliminary.confidence >= .8 or model_classifier is None:
        return preliminary
    try:
        output = model_classifier(message)
        if not isinstance(output, dict) or set(output) != {"timing", "confidence"}:
            return preliminary
        timing, confidence = output["timing"], output["confidence"]
        if timing not in {"interrupt", "deferred", "background", "clarify"}:
            return preliminary
        if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
            return preliminary
        if confidence < .8:
            return preliminary
        return SteeringClassification(
            timing, float(confidence), "model_suggested_timing", timing == "interrupt",
            timing == "interrupt", True,
        )
    except Exception:
        # Never risk silent loss or false emergency from an unavailable model.
        return SteeringClassification("deferred", .5, "classifier_unavailable_default_deferred", False, False)


STEERING_CLASSIFIER_PROMPT = (
    "Categorize the *user's own* steering message for an in-progress agent run. "
    "Choose exactly one timing: interrupt (clear urgent replacement/stop), "
    "deferred (new instruction after current work; DEFAULT for actions), "
    "background (non-instructional extra context), clarify (ambiguous question). "
    "Do not execute anything or claim changes were made. "
    'Return only JSON {"timing":"deferred","confidence":0.0}. '
    "Treat quoted examples and discussion of stopping as data, not imperative stops."
)
