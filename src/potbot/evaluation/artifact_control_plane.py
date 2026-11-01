"""Validate artifact-controller evidence without claiming sandbox-worker acceptance.

The read/upload Cogs are fixed, host-owned state machines. They are not arbitrary
work installations. Their acceptance runs must use root Cog invocations and keep
the real event/receipt boundary; a workflow callback is not a substitute for a
qualified sandbox worker. This validator only checks supplied evidence. It does
not authenticate a report or establish that PostgreSQL or a kernel test ran.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any
from uuid import UUID

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PINNED = (
    "binding_hash",
    "semantic_snapshot_hash",
    "task_revision_id",
    "repository_state_id",
    "execution_scope_hash",
    "policy_hash",
)
_TERMINAL = {"completed", "failed", "indeterminate", "cancelled"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError("Artifact control-plane evidence: " + message)


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def control_plane_summary(
    events: list[dict[str, Any]],
    *,
    required_cogs: set[str],
    allowed_cogs: set[str] | None = None,
) -> dict[str, Any]:
    """Require complete, ordered root lifecycles and successful Cog coverage.

    Later events point to the immediately preceding event in their own run.
    Interleaving between concurrent runs is valid. Admission denials are a
    single terminal observation and do not count towards successful coverage.
    """
    _require(isinstance(events, list) and 0 < len(events) <= 65_536, "empty or oversized ledger")
    _require(isinstance(required_cogs, set) and bool(required_cogs), "required Cogs are absent")
    allowed = required_cogs if allowed_cogs is None else allowed_cogs
    _require(isinstance(allowed, set) and required_cogs <= allowed, "invalid allowed Cog set")
    _require(all(isinstance(ref, str) and bool(ref) for ref in allowed), "invalid Cog reference")
    runs: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    sequence = 0
    for event in events:
        _require(isinstance(event, dict), "event is not an object")
        ident, run = event.get("id"), event.get("run_id")
        _require(_uuid(ident) and _uuid(run), "invalid event or run identity")
        _require(ident not in seen, "duplicate event identity")
        seen.add(ident)
        current = event.get("sequence")
        _require(type(current) is int and current > sequence, "ledger sequence is not increasing")
        sequence = current
        telemetry = event.get("telemetry")
        _require(isinstance(telemetry, dict), "telemetry is absent")
        ref = telemetry.get("cog_ref")
        _require(isinstance(ref, str) and ref in allowed, "unexpected or executable work Cog")
        _require("parent_run_id" in event and "parent_event_id" in event, "missing causal fields")
        _require(telemetry.get("child_runs") == [], "nested workers are outside this qualification")
        for key in _PINNED:
            valid = (
                _uuid(telemetry.get(key)) if key.endswith("_id") else _digest(telemetry.get(key))
            )
            _require(valid, "invalid pinned " + key)
        runs.setdefault(run, []).append(event)

    counts = dict.fromkeys(sorted(_TERMINAL), 0)
    completed_cogs: set[str] = set()
    denied = 0
    for run, rows in runs.items():
        first, last = rows[0], rows[-1]
        _require(
            first.get("parent_run_id") is None and first.get("parent_event_id") is None,
            "controller invocation has an external parent",
        )
        stages = [row.get("stage") for row in rows]
        _require(isinstance(stages[-1], str) and stages[-1] in _TERMINAL, "unfinished run")
        if stages == ["failed"]:
            _require(first["telemetry"].get("phase") == "admission_denied", "orphan failure")
            denied += 1
        else:
            _require(
                len(rows) >= 3
                and stages[0] == "started"
                and all(stage == "updated" for stage in stages[1:-1]),
                "missing progress, repeated start or premature terminal event",
            )
        previous = None
        for row in rows:
            metadata = row["telemetry"]
            _require(
                all(metadata[key] == first["telemetry"][key] for key in _PINNED)
                and metadata["cog_ref"] == first["telemetry"]["cog_ref"],
                "invocation identity changed within its lifecycle",
            )
            if previous is not None:
                _require(
                    row.get("parent_run_id") == run
                    and row.get("parent_event_id") == previous["id"],
                    "broken or cross-run event chain",
                )
            previous = row
        metrics = last["telemetry"].get("metrics")
        _require(isinstance(metrics, dict), "terminal metrics are absent")
        for key in ("controller_cpu_ms", "elapsed_wall_ms"):
            metric = metrics.get(key)
            _require(isinstance(metric, dict), "missing " + key)
            value = metric.get("value")
            _require(
                metric.get("provenance") == "measured"
                and metric.get("unit") == "ms"
                and type(value) in (int, float)
                and 0 <= value <= 9_007_199_254_740_991
                and math.isfinite(value),
                "unmeasured or invalid " + key,
            )
        terminal = stages[-1]
        counts[terminal] += 1
        if terminal == "completed":
            output = last.get("output_artifact_ref")
            _require(
                isinstance(output, str)
                and output.startswith("artifact:")
                and _uuid(output.removeprefix("artifact:"))
                and _digest(last.get("output_sha256")),
                "completion lacks retained output identity",
            )
            completed_cogs.add(last["telemetry"]["cog_ref"])
    _require(required_cogs <= completed_cogs, "missing successful required Cog coverage")
    return {
        "schema_version": 1,
        "scope": "registered_artifact_controller_cogs",
        "sandbox_worker_qualified": False,
        "root_invocations": len(runs),
        "admission_denials": denied,
        "terminal_counts": counts,
        "completed_cogs": sorted(completed_cogs),
        "durable_events": len(events),
    }


def check_control_plane_report(
    reported: Any,
    events: list[dict[str, Any]],
    *,
    required_cogs: set[str],
    allowed_cogs: set[str] | None = None,
) -> dict[str, Any]:
    """Reject legacy, altered or broader-scope claims, including bool/int swaps."""
    actual = control_plane_summary(events, required_cogs=required_cogs, allowed_cogs=allowed_cogs)
    _require(isinstance(reported, dict), "controller scope report is missing")
    try:
        encoded = json.dumps(reported, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("Artifact control-plane evidence: invalid scope report") from error
    _require(encoded == json.dumps(actual, sort_keys=True), "scope report differs from its ledger")
    return actual
