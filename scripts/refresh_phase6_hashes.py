"""Refresh only source hashes modified by the Phase 6 controller patch.

Existing security/semantic/workspace checksums remain unchanged. Reject malformed
manifests and missing or symlinked new files; never walk arbitrary host paths.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "source-files.sha256"
UPDATED = (
    "src/potbot/persistence/schema.py",
    "src/potbot/persistence/phase6_schema.py",
    "src/potbot/controller/solve_store.py",
    "src/potbot/controller/cog_bridge.py",
    "src/potbot/controller/search_scheduler.py",
    "src/potbot/evaluation/phase6_bench.py",
    "scripts/benchmark_phase6.py",
    "scripts/refresh_phase6_hashes.py",
    "tests/unit/test_phase6_search_scheduler.py",
    "tests/unit/test_phase6_benchmark.py",
    "tests/integration/test_phase6_search_store.py",
    "docs/phase-6-controller-benchmark.md",
    "docs/benchmarks/offline-fixtures-k1.json",
    "docs/benchmarks/offline-fixtures-k2.json",
    "docs/benchmarks/offline-fixture-summary.csv",
)
_PATTERN = re.compile(r"([0-9a-f]{64})  ([^\n]+)\Z")


def main() -> int:
    entries: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        match = _PATTERN.fullmatch(line)
        if match is None or match[2] in entries:
            raise ValueError("Invalid or repeated source checksum entry")
        entries[match[2]] = match[1]
    root = ROOT.resolve(strict=True)
    for rel in UPDATED:
        path = ROOT / rel
        if not path.is_file() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Expected source file is missing or not a normal project file")
        entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    MANIFEST.write_text(
        "".join(f"{entries[name]}  {name}\n" for name in sorted(entries)), encoding="utf-8"
    )
    print(f"Updated {len(UPDATED)} known files; preserved {len(entries) - len(UPDATED)} others")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
