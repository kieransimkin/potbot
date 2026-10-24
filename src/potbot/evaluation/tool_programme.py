"""Evidence-only programme qualification. A passing subset never completes a stage.

This module consumes the existing runner's receipts; it does not execute tools,
grant capabilities, create a second event ledger or infer hardware acceptance.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash

MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
REQUIRED_CHECKS = (
    "lint",
    "format",
    "types",
    "hierarchy-assets",
    "agent-security-assets",
    "unit-and-git",
    "wheel-build",
    "installed-domain",
    "db-bootstrap",
    "db-create",
    "db-migrate",
    "db-check",
    "db-cleanup",
    "postgres",
    "sandbox",
)
# Acceptance of these explicit, narrow fixtures is independent of whole-stage
# completion. In particular T01.3 has unimplemented universal producer adoption.
FEATURE_GATES = {
    "inventory": ("T01.1", "tool-inventory/T01.1-gate.json"),
    "semantic_contracts": ("T01.2", "tool-contracts/T01.2-gate.json"),
    "provenance": ("T01.3", "execution-provenance/T01.3-core-gate.json"),
    "artifact_ranges": ("T01.3", "artifact-ranges/T01.3-range-gate.json"),
    "artifact_uploads": ("T01.3", "artifact-uploads/T01.3-upload-gate.json"),
    "transport_cogs": ("T02.4", "transport-cogs/transport-gate.json"),
}


def _load(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise InvalidInputError("Programme evidence escapes its report directory")
    if not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise InvalidInputError("Programme evidence is missing or exceeds its bound")
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise InvalidInputError("Programme evidence is unreadable") from error
    if not isinstance(row, dict):
        raise InvalidInputError("Programme evidence must be an object")
    return row


def junit_counts(path: Path) -> dict[str, int]:
    """Count actual cases, never trust an asserted suite total or a green exit."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise InvalidInputError("Required JUnit receipt is unavailable")
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise InvalidInputError("JUnit entities are unsupported")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise InvalidInputError("Required JUnit receipt is malformed") from error
    cases = list(root.iter("testcase"))
    return {
        "collected": len(cases),
        "failed": sum(c.find("failure") is not None for c in cases),
        "errors": sum(c.find("error") is not None for c in cases),
        "skipped": sum(c.find("skipped") is not None for c in cases),
    }


def evaluate(
    *,
    stages: list[dict[str, Any]],
    artifacts: Path,
    source: dict[str, Any],
    version: str,
    enabled_features: tuple[str, ...] = tuple(FEATURE_GATES),
    mode: str = "implemented",
) -> dict[str, Any]:
    if mode not in {"implemented", "full_programme"}:
        raise InvalidInputError("Unknown tool programme gate mode")
    expected_ids = {f"T{phase:02}.{stage}" for phase in range(1, 13) for stage in range(1, 5)}
    ids = [row.get("id") for row in stages]
    if len(ids) != 48 or set(ids) != expected_ids:
        raise InvalidInputError("Programme must retain all 48 distinct stages")
    if len(set(enabled_features)) != len(enabled_features) or not set(enabled_features) <= set(
        FEATURE_GATES
    ):
        raise InvalidInputError("Unknown or repeated programme feature")
    if (
        set(source) != {"commit", "tree", "clean"}
        or not re.fullmatch(r"[0-9a-f]{40}", str(source["commit"]))
        or not re.fullmatch(r"[0-9a-f]{40}", str(source["tree"]))
        or source["clean"] is not True
    ):
        raise InvalidInputError("Qualification requires an exact clean committed source")
    blockers = []
    try:
        summary = _load(artifacts, "summary.json")
    except InvalidInputError:
        summary = {}
    if (
        summary.get("success") is not True
        or summary.get("source") != source
        or summary.get("source_at_start") != source
        or summary.get("postgres_requested") is not True
        or summary.get("sandbox_requested") is not True
    ):
        blockers.append("The source-bound mandatory runner did not complete successfully")
    checks = summary.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    for label in REQUIRED_CHECKS:
        matches = [row for row in checks if isinstance(row, dict) and row.get("check") == label]
        if (
            len(matches) != 1
            or type(matches[0].get("exit_code")) is not int
            or matches[0]["exit_code"] != 0
        ):
            blockers.append(f"Missing, duplicate or failed required check: {label}")
    suites = {}
    for label in ("unit-and-git", "postgres", "sandbox"):
        try:
            counts = junit_counts(artifacts / f"{label}.xml")
            suites[label] = counts
            if counts["collected"] == 0 or counts["failed"] or counts["errors"]:
                blockers.append(f"Required suite failed or collected no tests: {label}")
            if label in {"postgres", "sandbox"} and counts["skipped"]:
                blockers.append(f"Mandatory qualification suite skipped tests: {label}")
        except InvalidInputError:
            blockers.append(f"Required suite has no valid JUnit receipt: {label}")
    feature_reports: list[dict[str, Any]] = []
    for feature, (stage, relative) in FEATURE_GATES.items():
        enabled = feature in enabled_features
        result, reason, evidence_hash = "disabled", "Not enabled in this invocation", None
        if enabled:
            try:
                evidence = _load(artifacts, relative)
                evidence_hash = content_hash(evidence)
                if (
                    evidence.get("result") != "passed"
                    or evidence.get("source_commit") != source["commit"]
                    or evidence.get("version") != version
                ):
                    raise InvalidInputError("Feature evidence differs from the qualified source")
                result, reason = "passed", "Source-bound fixture gate passed"
            except InvalidInputError:
                result, reason = "unqualified", "Missing, failed or stale mandatory feature gate"
                blockers.append(f"{feature}: {reason}")
        feature_reports.append(
            {
                "feature": feature,
                "stage": stage,
                "enabled": enabled,
                "result": result,
                "reason": reason,
                "evidence": relative,
                "evidence_sha256": evidence_hash,
            }
        )
    by_stage: dict[str, dict[str, Any]] = {}
    for stage_row in stages:
        stage_id = stage_row["id"]
        dependencies = stage_row.get("hard_prerequisites", [])
        if not isinstance(dependencies, list) or any(d not in expected_ids for d in dependencies):
            raise InvalidInputError("Stage has an invalid hard prerequisite")
        by_stage[stage_id] = {
            "stage": stage_id,
            "name": stage_row["name"],
            "implementation_status": stage_row["status"],
            "result": "incomplete",
            "hard_prerequisites": dependencies,
            "features": [
                f["feature"]
                for f in feature_reports
                if f["stage"] == stage_id and f["result"] == "passed"
            ],
            "reason": "Whole-stage acceptance is not established by a passing feature subset",
        }
    # Only the two fully implemented stages have whole-stage fixtures today.
    # No other completion can be supplied by an untrusted caller's JSON report.
    for stage_id, feature in (("T01.1", "inventory"), ("T01.2", "semantic_contracts")):
        row = by_stage[stage_id]
        if (
            not blockers
            and row["implementation_status"] == "complete"
            and feature in row["features"]
        ):
            row.update(result="passed", reason="Complete stage has its source-bound Cog acceptance")
    unresolved = [r["stage"] for r in by_stage.values() if r["result"] != "passed"]
    fully_qualified = not blockers and not unresolved
    profile_passed = not blockers
    if mode == "full_programme" and unresolved:
        blockers.append("Unqualified programme stages: " + ", ".join(unresolved))
    report = {
        "schema_version": 1,
        "programme": "potbot-universal-tool-mcp-integration",
        "mode": mode,
        "source": source,
        "version": version,
        "implemented_profile_passed": profile_passed,
        "full_programme_qualified": fully_qualified,
        "result": "passed" if not blockers else "failed",
        "suites": suites,
        "features": feature_reports,
        "stages": list(by_stage.values()),
        "blockers": blockers,
        "limits": [
            "Fixture acceptance does not qualify live providers or physical devices",
            "Disabled features do not pass; missing enabled mandatory gates fail",
            "Runtime tools continue to use installed SYN Cogs and syn_execution_events",
        ],
    }
    return {**report, "report_sha256": content_hash(report)}
