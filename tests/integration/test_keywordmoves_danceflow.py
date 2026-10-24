"""Real, pinned KeywordMoves offline workflow; no provider credentials or network.

CI checks out the canonical KeywordMoves source at the explicitly pinned commit.
The source is executed only by this dedicated acceptance job, never by Potbot's
untrusted model/tool scheduler or ordinary unit tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.controller.keywordmoves_workflows import (
    KeywordMovesWorkflow,
    WorkflowExecutionPolicy,
    WorkflowPlan,
    WorkflowStep,
)
from potbot.domain.errors import StateMismatchError

KEYWORDMOVES_REVISION = "9044ba40460af6e6165f6b2c49a0d2454704fdd7"


def _source() -> Path:
    selected = os.environ.get("POTBOT_KEYWORDMOVES_SOURCE")
    if not selected:
        pytest.skip("Real KeywordMoves checkout is required; use the pinned CI job")
    path = Path(selected).resolve(strict=True)
    found = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert found == KEYWORDMOVES_REVISION, "external KeywordMoves code was not pinned"
    sys.path.insert(0, str(path / "src"))
    return path


def test_real_keywordmoves_atomic_workflow_with_review_checkpoint(tmp_path: Path) -> None:
    """Execute authentic plugin code and retain source-specific evidence, not guesses."""
    _source()
    from keywordmoves.models import ExecutionContext, PluginRequest
    from keywordmoves.registry import LLMRegistry, PluginRegistry

    registry = PluginRegistry()
    llms = LLMRegistry()
    declared = {item.name: set(item.operations) for item in registry.describe()}
    assert {"text-library", "google-trends", "observed-evidence",
            "candidate-focus", "candidate-shortlist", "research-batch"} <= set(declared)
    matrix = WorkflowExecutionPolicy().inventory({
        entry.name: entry.operations for entry in registry.describe()
    })
    assert len(matrix) >= len(declared)
    assert any(entry["policy"] == "not_enabled" for entry in matrix)
    assert any(entry["policy"] == "offline_registered" for entry in matrix)
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "lyrics.txt").write_text(
        "Coastal memories are glowing. Coastal memories keep returning. "
        "Coastal summer memories with old friends. Coastal memories and summer friendships.",
        encoding="utf-8",
    )
    (root / "trends.csv").write_text(
        "Week,coastal memories: (United Kingdom)\n"
        "2026-09-27,10\n2026-10-04,20\n2026-10-11,30\n",
        encoding="utf-8",
    )
    (root / "observations.json").write_text(json.dumps({
        "schema": "keywordmoves-observations/v1", "observations": [{
            "phrase": "coastal memories", "source": "Reviewed YouTube export",
            "metric": "impressions", "value": 12, "unit": "count",
            "observed_at": "2026-10-20", "platform": "YouTube",
            "subject": "Example Song", "availability": "observed",
            "evidence_kind": "property_performance",
        }],
    }), encoding="utf-8")
    (root / "research.json").write_text(json.dumps({
        "schema": "keywordmoves-research-batch/v1", "tasks": [{
            "id": "offline-trends", "subject": "Example Song", "plugin": "google-trends",
            "operation": "import-interest", "inputs": ["trends.csv"],
            "options": {"geography": "GB", "observed_at": "2026-10-20"},
        }],
    }), encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def invoke(*, plugin, operation, inputs, keywords, options):
        assert operation in declared[plugin]
        assert (plugin, operation) not in {
            ("google-trends", "collect-browser"), ("research-batch", "collect")}
        calls.append((plugin, operation))
        return registry.get(plugin).run(
            PluginRequest(operation, keywords=keywords, inputs=inputs, options=options),
            ExecutionContext(llms=llms),
        ).to_dict()

    plan = WorkflowPlan(KEYWORDMOVES_REVISION, "Example Song", (
        WorkflowStep("literal", "text-library", "extract-literal",
                     ("file:lyrics.txt",), options=(("limit", 30), ("max_ngram", 2))),
        WorkflowStep("trends", "google-trends", "import-interest",
                     ("file:trends.csv",), options=(("geography", "GB"),
                                                     ("observed_at", "2026-10-20"))),
        WorkflowStep("observed", "observed-evidence", "import-observations",
                     ("file:observations.json",)),
        WorkflowStep("validate", "research-batch", "validate-plan",
                     ("file:research.json",)),
        WorkflowStep("merged", "@potbot", "merge-candidates",
                     ("step:literal", "step:trends", "step:observed")),
        WorkflowStep("focus", "candidate-focus", "related", ("step:merged",),
                     ("coastal memories",), options=(("limit", 30),)),
        WorkflowStep("pool", "@potbot", "bind-target", ("step:focus",),
                     options=(("subject", "Example Song"), ("platform", "YouTube"))),
        WorkflowStep("shortlist", "candidate-shortlist", "select",
                     ("file:review.json", "step:pool"),
                     options=(("limit", 3), ("minimum", 1))),
    ))
    output = tmp_path / "output"
    events = []
    flow = KeywordMovesWorkflow(plan=plan, input_root=root, output_root=output,
                                invoke=invoke, emit=lambda kind, value: events.append(kind))
    stage_one = flow.run()
    assert stage_one.status == "awaiting_input"
    assert stage_one.waiting_for == "file:review.json"
    assert len(stage_one.completed) == 7
    assert len(calls) == 5  # No synthetic substitution for real plugin calls.

    literal = json.loads((output / "results/literal.json").read_text())
    assert any(x["relationship"] == "literal-text-extracted" for x in literal["keywords"])
    trends = json.loads((output / "results/trends.json").read_text())
    assert trends["keywords"][0]["evidence"][0]["unit"] == "index_0_100"
    observed = json.loads((output / "results/observed.json").read_text())
    assert observed["keywords"][0]["evidence"][0]["metric"] == "impressions"
    merged = json.loads((output / "results/merged.json").read_text())
    assert all(row["score"] is None for row in merged["keywords"])
    assert any(len(row["metadata"]["source_lineage"]) > 1 for row in merged["keywords"])
    assert json.loads((output / "results/validate.json").read_text())["metadata"]["valid"]

    pool_path = output / "results/pool.json"
    pool = json.loads(pool_path.read_text())
    assert pool["keywords"]
    selected = pool["keywords"][:3]
    reviews = [{"phrase": row["phrase"], "fit": "direct", "intent": "listening",
                "clarity": "natural", "group": f"group-{i}", "priority": i + 1,
                "rationale": "Synthetic editorial fixture; checked only for workflow semantics."}
               for i, row in enumerate(selected)]
    (root / "review.json").write_text(json.dumps({
        "schema": "keywordmoves-shortlist-review/v1", "subject": "Example Song",
        "platform": "YouTube", "reviewed_at": "2026-10-24",
        "reviewer_kind": "analyst", "source_sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest(),
        "decisions": reviews,
    }), encoding="utf-8")
    final = flow.run(resume=True)
    assert final.status == "completed"
    assert final.completed[-1] == "shortlist"
    assert len(calls) == 6  # One additional real call, no replay.
    shortlist = json.loads((output / "results/shortlist.json").read_text())
    assert len(shortlist["keywords"]) >= 1
    assert all(row["metadata"]["shortlist_review"]["implementation_authorised"] is False
               for row in shortlist["keywords"])
    receipt = flow.evidence("shortlist", run_id=uuid4().hex)
    assert receipt.source_sha256 == final.artifacts["shortlist"]
    assert "keywordmoves.step.completed" in events

    report_dir = os.environ.get("POTBOT_KEYWORDMOVES_REPORT_DIR")
    if report_dir:
        destination = Path(report_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "keywordmoves-acceptance.json").write_text(json.dumps({
            "kind": "synthetic-offline-acceptance", "source_revision": KEYWORDMOVES_REVISION,
            "plan_sha256": plan.sha256, "status": final.status,
            "review_pause_observed": stage_one.status == "awaiting_input",
            "real_keywordmoves_invocations": len(calls),
            "stage_hashes": final.artifacts,
            "shortlist_candidates": len(shortlist["keywords"]),
            "plugin_operation_matrix": matrix,
            "network_or_paid_requests": 0,
            "publication_authorised": False,
        }, indent=2), encoding="utf-8")

    # An edited review must invalidate the checkpoint, never silently reselect.
    (root / "review.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StateMismatchError):
        flow.run(resume=True)


def test_real_keywordmoves_catalogue_keeps_native_and_remote_sources_separate() -> None:
    _source()
    from keywordmoves.registry import LLMRegistry, PluginRegistry

    keywords = PluginRegistry()
    llms = LLMRegistry()
    assert "openai" not in keywords.names()
    assert "openai" in llms.names()
    assert "huggingface-transformers" in llms.names()
    assert "research-batch" in keywords.names()
    assert "google-trends" in keywords.names()
    assert "hashtag-planner" in keywords.names()


def test_canonical_danceflow_tool_contract_is_pinned() -> None:
    source = os.environ.get("POTBOT_DANCEFLOW_SOURCE")
    if not source:
        pytest.skip("Pinned DanceFlow checkout is required")
    path = Path(source).resolve(strict=True)
    commit = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert commit == "8963a2f270ec5264ab78974ec22a77258e1e2161"
    guide = (path / "docs/tools/keywordmoves.md").read_text(encoding="utf-8")
    assert "github.com/kieransimkin/keywordmoves" in guide
    assert "extract-literal" in guide
    assert "observed-evidence" in guide
    assert "approval" in guide.lower() or "explicit scope and budget" in guide.lower()
    skill = (path / "plugins/danceflow/skills/keywordmoves/SKILL.md")
    assert skill.is_file()


def test_real_keywordmoves_monitor_history_preserves_import_identity(tmp_path: Path) -> None:
    """Exercise the separate source-owned SQLite monitoring API, without collection."""
    _source()
    from keywordmoves.monitoring.analysis import coverage
    from keywordmoves.monitoring.store import MonitorStore

    watchlist = tmp_path / "watches.json"
    watchlist.write_text(json.dumps({"schema": "keywordmoves-watchlist/v1", "watches": [{
        "id": "synthetic-youtube", "subject": "Example Song", "platform": "YouTube",
        "phrase": "coastal memories", "source": "Reviewed YouTube export",
        "metric": "impressions", "unit": "count", "evidence_kind": "property_performance",
    }]}), encoding="utf-8")
    observation = tmp_path / "observations.json"
    observation.write_text(json.dumps({"schema": "keywordmoves-observations/v1",
        "observations": [{"phrase": "coastal memories", "source": "Reviewed YouTube export",
                          "metric": "impressions", "unit": "count", "value": 12,
                          "observed_at": "2026-10-20", "platform": "YouTube",
                          "subject": "Example Song", "evidence_kind": "property_performance"}]}),
        encoding="utf-8")
    with MonitorStore(tmp_path / "evidence.sqlite", create=True) as store:
        assert store.add_watchlist(watchlist)["watches_received"] == 1
        first = store.ingest(observation, {"subject": "Example Song", "platform": "YouTube",
                                           "evidence_kind": "property_performance"})
        assert first["inserted"] >= 1
        repeat = store.ingest(observation, {"subject": "Example Song", "platform": "YouTube",
                                            "evidence_kind": "property_performance"})
        assert repeat["duplicate_snapshot"] is True
        assert len(store.observations()) >= 1
        summary = coverage(store, now="2026-10-24T12:00:00Z")
        assert summary["watch_count"] == 1
        assert store.verify()["verified"] is True
