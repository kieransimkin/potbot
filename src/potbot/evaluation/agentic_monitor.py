"""Read-only evaluation of verified-style workflow and assumption health events.

Events may be supplied by jobs, CI or qualified Cogs. A claimed receipt hash is
not independently verified here; model routing cannot be changed by this code.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from potbot.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class HealthEvent:
    kind: Literal["workflow", "assumption", "model"]
    identity: str
    case_id: str
    status: Literal["passed", "failed", "unavailable", "indeterminate"]
    observed_at: str
    source_digest: str
    receipt_digest: str | None = None
    latency_ms: int | None = None
    money_microunits: int | None = None

    @classmethod
    def from_dict(cls, value: object) -> HealthEvent:
        required = {"kind", "identity", "case_id", "status", "observed_at", "source_digest"}
        optional = {"receipt_digest", "latency_ms", "money_microunits"}
        if not isinstance(value, dict) or not required <= value.keys() or set(value) - required - optional:
            raise InvalidInputError("monitor observation fields are invalid")
        try:
            event = cls(**value)
            if not isinstance(event.observed_at, str):
                raise ValueError
            observed = datetime.fromisoformat(event.observed_at.replace("Z", "+00:00"))
            if (event.kind not in {"workflow", "assumption", "model"}
                    or event.status not in {"passed", "failed", "unavailable", "indeterminate"}
                    or not 1 <= len(event.identity) <= 160
                    or not 1 <= len(event.case_id) <= 160
                    or not isinstance(event.observed_at, str)
                    or not 10 <= len(event.observed_at) <= 64
                    or observed.tzinfo is None
                    or not isinstance(event.source_digest, str)
                    or len(event.source_digest) != 64
                    or any(c not in "0123456789abcdef" for c in event.source_digest)
                    or event.receipt_digest is not None and (
                        len(event.receipt_digest) != 64 or any(
                            c not in "0123456789abcdef" for c in event.receipt_digest
                        )
                    )
                    or event.latency_ms is not None and (type(event.latency_ms) is not int or event.latency_ms < 0)
                    or event.money_microunits is not None and (
                        type(event.money_microunits) is not int or event.money_microunits < 0
                    )):
                raise ValueError
            return event
        except (TypeError, ValueError):
            raise InvalidInputError("invalid monitor event") from None


def review_health_events(events: list[HealthEvent], *, window: int = 12,
                         regression_threshold: float = 0.20) -> dict[str, Any]:
    """Compare two nonoverlapping windows with matching scope and source digest."""
    if not 2 <= window <= 100 or not 0 < regression_threshold <= 1:
        raise InvalidInputError("monitor window or threshold invalid")
    groups: dict[tuple[str, str, str], list[HealthEvent]] = defaultdict(list)
    seen: set[tuple[str, str, str, str]] = set()
    for event in events:
        key = (event.kind, event.identity, event.case_id, event.observed_at)
        if key in seen:
            raise InvalidInputError("duplicate monitor observations cannot inflate evidence")
        seen.add(key)
        groups[(event.kind, event.identity, event.source_digest)].append(event)
    snapshots = []
    alerts = []
    for (kind, identity, source_digest), records in sorted(groups.items()):
        records.sort(key=lambda x: (x.observed_at, x.case_id))
        recent = records[-window:]
        earlier = records[-2 * window:-window]
        complete_recent = [r for r in recent if r.status in {"passed", "failed"}]
        rate = (sum(r.status == "passed" for r in complete_recent) / len(complete_recent)
                if complete_recent else None)
        snapshots.append({
            "kind": kind, "identity": identity, "source_digest": source_digest,
            "observations": len(records), "recent_complete": len(complete_recent),
            "recent_pass_rate": round(rate, 5) if rate is not None else None,
            "missing_receipts": sum(r.receipt_digest is None for r in recent),
            "indeterminate_or_unavailable": sum(
                r.status in {"indeterminate", "unavailable"} for r in recent
            ),
            "receipt_qualified": False,
        })
        if len(earlier) < window or len(recent) < window:
            continue
        if any(r.status not in {"passed", "failed"} for r in earlier + recent):
            alerts.append({"kind": kind, "identity": identity,
                           "condition": "incomplete_or_uncertain_window"})
            continue
        baseline = sum(r.status == "passed" for r in earlier) / window
        current = sum(r.status == "passed" for r in recent) / window
        if baseline - current >= regression_threshold:
            alerts.append({"kind": kind, "identity": identity,
                           "condition": "quality_regression",
                           "previous_rate": baseline, "current_rate": current})
    return {"schema_version": 1, "snapshots": snapshots, "alerts": alerts,
            "automatic_routing_changes": 0, "automatic_workflow_changes": 0,
            "qualified": False}
