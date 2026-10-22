#!/usr/bin/env python3
"""Inspect Phase 7 benchmark observations without asserting model qualification.

Usage: python scripts/benchmark_phase7.py --input records.json --output report.json

The read-only CLI has no trusted receipt checker and *always* withholds verified
success rates. Live scores require the authorized PostgreSQL verification store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json
from potbot.evaluation.benchmark_ledger import BenchmarkSample, evaluate_benchmark
from potbot.evaluation.benchmark_manifest import BenchmarkSuite, check_suite_samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--suite", type=Path, help="Pinned held-out suite manifest")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline")
    parser.add_argument("--treatment")
    args = parser.parse_args()
    try:
        raw = args.input.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise InvalidInputError("benchmark input exceeds eight MiB")
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict) or set(document) != {"schema_version", "samples"}:
            raise InvalidInputError("expected a versioned sample collection")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise InvalidInputError("unsupported benchmark input schema")
        if not isinstance(document["samples"], list):
            raise InvalidInputError("expected benchmark sample list")
        samples = tuple(BenchmarkSample.from_dict(item) for item in document["samples"])
        suite_digest = None
        if args.suite is not None:
            suite_raw = args.suite.read_bytes()
            if len(suite_raw) > 2 * 1024 * 1024:
                raise InvalidInputError("benchmark suite exceeds two MiB")
            suite = BenchmarkSuite.from_dict(json.loads(suite_raw.decode("utf-8")))
            suite_digest = check_suite_samples(suite, samples)
        result = evaluate_benchmark(
            samples, baseline_cohort=args.baseline, treatment_cohort=args.treatment
        )
        if suite_digest is not None:
            result["suite_digest"] = suite_digest
            result["suite_manifest_checked"] = True
        destination = args.output.absolute()
        if destination.is_symlink():
            raise InvalidInputError("benchmark output cannot overwrite a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(canonical_json(result) + "\n", encoding="utf-8")
        print(f"Wrote unqualified audit report: {destination}")
        return 0
    except (InvalidInputError, ValueError, UnicodeDecodeError, OSError) as error:
        parser.exit(2, f"Benchmark inputs unavailable or invalid: {type(error).__name__}\n")


if __name__ == "__main__":
    raise SystemExit(main())
