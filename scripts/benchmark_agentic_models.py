#!/usr/bin/env python3
"""Opt-in agentic role/prompt matrix benchmark (no downloads or paid calls by default).

Preflight: python scripts/benchmark_agentic_models.py preflight --suite ... --models ...
Run:       python scripts/benchmark_agentic_models.py run --suite ... --models ... --output-dir ...
Monitor:   python scripts/benchmark_agentic_models.py monitor --history-dir ... --output ...
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import sys
from dataclasses import replace
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.evaluation.agentic_model_bench import (
    ModelTarget,
    Observation,
    RoleSuite,
    judge_agreement,
    judge_case,
    monitor_history,
    observe_case,
    prompt_for_case,
    summarise,
)
from potbot.evaluation.agentic_model_transport import BenchmarkTransport
from potbot.evaluation.agentic_monitor import HealthEvent, review_health_events
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.prompt_compiler import PromptVariant
from potbot.service_credentials import ServiceCredentials

_VARIANTS: tuple[PromptVariant, ...] = ("generic", "family", "model_role")


def _read(path: Path, *, limit: int = 1024 * 1024) -> Any:
    raw = path.read_bytes()
    if len(raw) > limit:
        raise InvalidInputError("benchmark input exceeds the size limit")
    return json.loads(raw.decode("utf-8"))


def _targets(path: Path) -> list[ModelTarget]:
    document = _read(path)
    if not isinstance(document, dict) or set(document) != {"schema_version", "models"} or document["schema_version"] != 1 or not isinstance(document["models"], list):
        raise InvalidInputError("invalid model target document")
    targets = [ModelTarget.from_dict(x) for x in document["models"]]
    if not 1 <= len(targets) <= 16 or len({m.id for m in targets}) != len(targets):
        raise InvalidInputError("model targets must be distinct and bounded")
    return targets


def _report_html(document: dict[str, Any]) -> str:
    rows = document["summary"]["rows"]
    header = "<tr><th>Role</th><th>Model</th><th>Prompt</th><th>Completed</th><th>Pass</th><th>Wilson lower 95%</th><th>Median ms</th><th>Measured billed µunits</th></tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in (
            r["role"], r["model_id"], r["variant"],
            f'{r["completed"]}/{r["total"]}', r["accuracy"],
            r["wilson_lower_95"], r["median_latency_ms"],
            r["measured_billed_microunits"],
        )) + "</tr>" for r in rows
    )
    detail = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in (
            r["case_id"], r["role"], r["model_id"], r["variant"], r["input_profile"],
            r["trial"], r["score"], r["elapsed_ms"],
            (r["response"] if r["response"] is not None else "[output hash only: "
             + str(r["output_sha256"]) + "]"),
        )) + "</tr>" for r in sorted(document["results"], key=lambda v: (
            v["case_id"], v["variant"], v["trial"], v["model_id"]
        ))
    )
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<title>Potbot model-role benchmark</title>"
            "<style>body{font:15px system-ui;max-width:1300px;margin:3em auto;padding:0 1em}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:.5em;text-align:left}"
            "th{background:#eee}small{color:#555}</style>"
            "<h1>Potbot model-role comparison</h1>"
            "<p>Objective fixture/rubric matches, not verified end-to-end task execution. "
            "Scores and routing recommendations are <strong>advisory only</strong>.</p>"
            f"<p>Run {html.escape(document['run_id'])}; suite {html.escape(document['suite_sha256'])}</p>"
            f"<table>{header}{body}</table>"
            "<h2>Side-by-side attempts (same suite tasks and input hashes)</h2>"
            "<table><tr><th>Case</th><th>Role</th><th>Model</th><th>Prompt profile</th>"
            "<th>Input profile</th><th>Trial</th><th>Rubric match</th><th>Latency ms</th><th>Output</th></tr>"
            f"{detail}</table>"
            "<h2>Paired input-profile comparisons</h2>"
            f"<pre>{html.escape(json.dumps(document['summary']['input_comparisons'], indent=2))}</pre>"
            "<h2>Judge calibration</h2>"
            f"<pre>{html.escape(json.dumps(document['judges'], indent=2))}</pre>"
            "<small>Unknown tokens/billing are displayed as unknown, not as zero.</small></html>")


def _private_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation; benchmark receipts are never silently overwritten.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(contents)


def _preflight(models: list[ModelTarget], suite: RoleSuite, variants: list[PromptVariant], repeat: int) -> dict[str, Any]:
    matrix = [(case.id, case.role, model.id, variant, trial)
              for case in suite.cases for model in models if case.role in model.roles
              for variant in variants for trial in range(1, repeat + 1)]
    paid = sum(1 for _, _, mid, _, _ in matrix if next(m for m in models if m.id == mid).backend == "hosted")
    return {"suite_sha256": suite.identity, "matrix_attempts": len(matrix),
            "possible_hosted_calls": paid, "possible_local_calls": len(matrix) - paid,
            "network_started": False, "qualified": False}


def _parsed_variants(raw: str) -> list[PromptVariant]:
    variants = raw.split(",")
    if not variants or any(v not in _VARIANTS for v in variants) or len(set(variants)) != len(variants):
        raise InvalidInputError("choose distinct prompt variants: generic,family,model_role")
    return variants  # type: ignore[return-value]


def _execute(
    args: argparse.Namespace, suite: RoleSuite, models: list[ModelTarget],
    variants: list[PromptVariant], policy: HostedPolicy | None,
    *, run_id: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not 1 <= args.repeat <= 5:
        raise InvalidInputError("repeats must be between one and five")
    plan = _preflight(models, suite, variants, args.repeat)
    if not plan["matrix_attempts"] or plan["matrix_attempts"] > 1600:
        raise InvalidInputError("benchmark matrix is empty or exceeds 1600 attempts")
    if not args.confirm_prompt_disclosure:
        raise InvalidInputError("running any model requires explicit prompt disclosure consent")
    if plan["possible_hosted_calls"]:
        if (not args.allow_paid or policy is None or not policy.allow_prompt_disclosure
                or plan["possible_hosted_calls"] > args.max_paid_calls
                or args.max_cost_microunits <= 0):
            raise InvalidInputError("hosted benchmarks require policy, paid opt-in and finite caps")
        # Check the whole main matrix BEFORE sending any paid request. The
        # optional derived-judge cohort has unknown size, and is separately
        # bounded by the same running caps.
        admission = 0
        for case in suite.cases:
            for model in models:
                if model.backend != "hosted" or case.role not in model.roles:
                    continue
                assert model.provider is not None
                rule = policy.rule(model.provider, model.model)
                if (rule.input_microunits_per_million <= 0
                        and rule.output_microunits_per_million <= 0):
                    raise InvalidInputError("hosted benchmark price rules require a nonzero rate")
                for variant in variants:
                    packet = prompt_for_case(model.model, case, variant)
                    request, _, _ = policy.request(
                        {"provider": model.provider, "model": model.model,
                         "role": "coding" if case.role == "coding" else "reasoning"},
                        {"prompt": packet.user, "system": packet.system,
                         "max_output_tokens": model.max_output_tokens,
                         "timeout_seconds": 90},
                    )
                    policy.authorize(request)
                    admission += args.repeat * rule.charge(
                        len((packet.user + packet.system).encode()) + 1024,
                        model.max_output_tokens,
                    )
        if admission > args.max_cost_microunits:
            raise InvalidInputError("whole hosted benchmark exceeds global cost allowance")
    cost_reserved = 0
    paid_attempts = 0
    transport = BenchmarkTransport(
        policy=policy, credentials=ServiceCredentials() if policy else None,
        local_key=os.environ.get("POTBOT_BENCH_LOCAL_KEY", "local-only"),
    )

    def run_one(model: ModelTarget, packet: Any) -> Any:
        nonlocal cost_reserved, paid_attempts
        if model.backend == "hosted":
            assert policy is not None and model.provider is not None
            rule = policy.rule(model.provider, model.model)
            if paid_attempts >= args.max_paid_calls:
                raise InvalidInputError("total hosted benchmark call cap exhausted")
            # Policy-estimated max must be admitted before any possible network send.
            bound = rule.charge(len((packet.user + packet.system).encode()) + 1024,
                                model.max_output_tokens)
            if cost_reserved + bound > args.max_cost_microunits:
                raise InvalidInputError("global hosted benchmark budget exhausted")
            cost_reserved += bound  # Held even on uncertain provider completion.
            paid_attempts += 1
        return transport.request(model, packet)

    randomizer = random.Random(args.shuffle_seed)
    grid = [(case, model, variant, trial) for case in suite.cases for model in models
            if case.role in model.roles for variant in variants
            for trial in range(1, args.repeat + 1)]
    randomizer.shuffle(grid)
    observations: list[Observation] = []
    for index, (case, model, variant, trial) in enumerate(grid, start=1):
        key = {"case_id": case.id, "model_id": model.id,
               "variant": variant, "trial": trial}
        if on_event:
            # Persist before dispatch: a process crash cannot later be mistaken
            # for a call that was never attempted.
            on_event({"phase": "possibly_dispatched", **key})
        row = observe_case(model=model, case=case, variant=variant, trial=trial,
                           generate=run_one, retain_response=True)
        observations.append(row)
        if on_event:
            safe_row = replace(row, response=row.response if args.store_text else None)
            on_event({"phase": "terminal", "result": safe_row.to_dict()})
        print(f"[Potbot benchmark] {index}/{len(grid)} {model.id} "
              f"{case.role} {variant} {row.status}", file=sys.stderr)
    judge_rows: list[Observation] = []
    if args.judge_derived:
        # Only derive a pair when independently graded outputs have different
        # correctness. Never ask a model to judge its own output.
        by_case: dict[tuple[str, str, int], list[Observation]] = {}
        for row in observations:
            if row.score is not None and row.response is not None:
                by_case.setdefault((row.case_id, row.variant, row.trial), []).append(row)
        pairs = 0
        cases = {case.id: case for case in suite.cases}
        for (case_id, variant, trial), sample in sorted(by_case.items()):
            passing = [r for r in sample if r.score == 1]
            failing = [r for r in sample if r.score == 0]
            if not passing or not failing or pairs >= args.max_judge_pairs:
                continue
            first, second = passing[0], failing[0]
            if len(first.response or "") + len(second.response or "") > 12000:
                # Never silently truncate candidate evidence before judging it.
                continue
            for judge_model in models:
                if "judge" not in judge_model.roles or judge_model.id in {first.model_id, second.model_id}:
                    continue
                for reverse in (False, True):
                    jcase = judge_case(
                        f"{case_id}-{variant}-{trial}-{first.model_id}-{second.model_id}",
                        cases[case_id].task, first.response or "", second.response or "",
                        reference_winner="A", reverse=reverse,
                    )
                    judge_key = {"case_id": jcase.id, "model_id": judge_model.id,
                                 "variant": variant, "trial": trial}
                    if on_event:
                        on_event({"phase": "possibly_dispatched", **judge_key})
                    judged = observe_case(
                        model=judge_model, case=jcase, variant=variant, trial=trial,
                        generate=run_one, retain_response=args.store_text,
                    )
                    judge_rows.append(judged)
                    if on_event:
                        on_event({"phase": "terminal", "result": judged.to_dict()})
            pairs += 1
    all_rows = observations + judge_rows
    report = {
        "schema_version": 1, "run_id": run_id or str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite_sha256": suite.identity, "mode": "measured_requests",
        "admission_ceiling_microunits": args.max_cost_microunits,
        "reserved_ceiling_microunits": cost_reserved,
        "hosted_attempted_calls": paid_attempts,
        "results": [replace(row, response=row.response if args.store_text else None).to_dict()
                    for row in all_rows],
        "summary": summarise(all_rows, minimum_samples=args.min_samples),
        "judges": judge_agreement(all_rows),
        "qualified": False,
        "auto_promoted": False,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        group = command.add_parser(name)
        group.add_argument("--suite", type=Path, required=True)
        group.add_argument("--models", type=Path, required=True)
        group.add_argument("--variants", default="generic,family,model_role")
        group.add_argument("--repeat", type=int, default=1)
        if name == "run":
            group.add_argument("--output-dir", type=Path, required=True)
            group.add_argument("--provider-policy", type=Path)
            group.add_argument("--allow-paid", action="store_true")
            group.add_argument("--confirm-prompt-disclosure", action="store_true")
            group.add_argument("--max-paid-calls", type=int, default=0)
            group.add_argument("--max-cost-microunits", type=int, default=0)
            group.add_argument("--judge-derived", action="store_true")
            group.add_argument("--max-judge-pairs", type=int, default=8)
            group.add_argument("--shuffle-seed", type=int, default=42)
            group.add_argument("--min-samples", type=int, default=12)
            group.add_argument("--store-text", action="store_true")
    monitor = command.add_parser("monitor")
    monitor.add_argument("--history-dir", required=True, type=Path)
    monitor.add_argument("--output", required=True, type=Path)
    monitor.add_argument("--events", type=Path, help="Optional bounded workflow/assumption health JSON")
    try:
        args = parser.parse_args(argv)
        if args.command == "monitor":
            inputs = sorted(args.history_dir.glob("*/report.json"))
            if len(inputs) > 500:
                raise InvalidInputError("monitor has too many reports")
            documents = [_read(p, limit=4 * 1024 * 1024) for p in inputs]
            documents.sort(key=lambda d: d.get("created_at", ""))
            payload = monitor_history(documents)
            if args.events is not None:
                entries = _read(args.events, limit=2 * 1024 * 1024)
                if not isinstance(entries, dict) or set(entries) != {"schema_version", "events"} or entries["schema_version"] != 1 or not isinstance(entries["events"], list) or len(entries["events"]) > 10000:
                    raise InvalidInputError("monitor requires bounded versioned events")
                payload["workflow_and_assumptions"] = review_health_events(
                    [HealthEvent.from_dict(item) for item in entries["events"]]
                )
            _private_write(args.output, canonical_json(payload) + "\n")
            print(f"Audited {len(documents)} report(s); recommendations remain advisory.")
            return 0
        suite = RoleSuite.from_dict(_read(args.suite))
        models = _targets(args.models)
        variants = _parsed_variants(args.variants)
        if not 1 <= args.repeat <= 5:
            raise InvalidInputError("invalid repeat count")
        if args.command == "preflight":
            print(json.dumps(_preflight(models, suite, variants, args.repeat), indent=2))
            return 0
        if (not 1 <= args.max_judge_pairs <= 30 or not 1 <= args.min_samples <= 10000
                or not 0 <= args.max_paid_calls <= 2000
                or not 0 <= args.max_cost_microunits <= 10**12):
            raise InvalidInputError("invalid judge or evidence minimum")
        policy = HostedPolicy.read(args.provider_policy) if args.provider_policy else None
        run_id = str(uuid4())
        destination = args.output_dir / run_id
        _private_write(destination / "run.json", canonical_json({
            "schema_version": 1, "run_id": run_id,
            "suite_sha256": suite.identity,
            "status": "started", "no_automatic_replay": True,
        }) + "\n")
        journal = destination / "attempts.jsonl"
        # Durable per-attempt journal; incomplete calls are uncertain. A
        # restart must create a NEW run, never append or replay a paid request.
        journal_fd = os.open(str(journal), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(journal_fd, "w", encoding="utf-8") as handle:
            def on_event(event: dict[str, Any]) -> None:
                handle.write(canonical_json(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            report = _execute(args, suite, models, variants, policy,
                              run_id=run_id, on_event=on_event)
        _private_write(destination / "report.json", canonical_json(report) + "\n")
        _private_write(destination / "comparison.html", _report_html(report))
        print(json.dumps({"run_id": report["run_id"], "report_dir": str(destination),
                          "n_attempts": len(report["results"]),
                          "qualified": False}, indent=2))
        return 0
    except (InvalidInputError, ValueError, OSError, TypeError, UnicodeError, KeyError):
        # Do not print raw task/prompt/provider errors, credentials or file contents.
        parser.exit(2, "Benchmark configuration or execution failed; inspect safe local configuration.\n")


if __name__ == "__main__":
    raise SystemExit(main())
