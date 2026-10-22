"""Refresh only Phase 7 and known baseline-lint checksum entries.

All unrelated source hashes (including other chats' security and semantic work)
remain byte-for-byte unchanged. Refuse paths outside the repository or symlinks.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "source-files.sha256"
CHANGED = (
    "src/potbot/persistence/schema.py",
    "src/potbot/persistence/phase7_schema.py",
    "src/potbot/controller/research_store.py",
    "src/potbot/evaluation/benchmark_ledger.py",
    "src/potbot/evaluation/benchmark_manifest.py",
    "src/potbot/research/__init__.py",
    "src/potbot/research/contracts.py",
    "src/potbot/research/repository.py",
    "src/potbot/research/http_transport.py",
    "src/potbot/research/cog.py",
    "src/potbot/research/source_policy.py",
    "scripts/benchmark_phase7.py",
    "scripts/refresh_phase7_hashes.py",
    "tests/unit/test_phase7_research.py",
    "tests/unit/test_phase7_benchmark_ledger.py",
    "tests/unit/test_phase7_benchmark_manifest.py",
    "tests/unit/test_phase7_http_transport.py",
    "tests/unit/test_phase7_research_cog.py",
    "tests/unit/test_phase7_schema.py",
    "tests/unit/test_phase7_source_policy.py",
    "tests/integration/test_phase7_research_store.py",
    "docs/phase-7.md",
    "docs/phase-7-increment-2.md",
    "docs/benchmarks/phase-7-methodology.md",
    "vocab/phase7-research-types.json",
    # Lint fixes are applied independently, but both patches may be checked
    # in before this script is run.
    "src/potbot/cogs/__init__.py",
    "src/potbot/cogs/builtin.py",
    "src/potbot/controller/solve_store.py",
    "src/potbot/tools/gateway.py",
    "src/potbot/workspace_sandbox.py",
    "tests/unit/test_cog_transformations.py",
    "tests/unit/test_phase6_search_scheduler.py",
    "src/potbot/persistence/phase7_worker_schema.py",
    "src/potbot/controller/research_worker.py",
    "src/potbot/research/worker_cli.py",
    "tests/unit/test_phase7_worker_schema.py",
    "tests/integration/test_phase7_research_worker.py",
    "docs/phase-7-increment-3.md",
)
LINE = re.compile(r"([0-9a-f]{64})  ([^\n]+)\Z")


def main() -> int:
    entries: dict[str, str] = {}
    for row in MANIFEST.read_text(encoding="utf-8").splitlines():
        match = LINE.fullmatch(row)
        if match is None or match[2] in entries:
            raise ValueError("invalid or duplicated source manifest entry")
        entries[match[2]] = match[1]
    repo_root = ROOT.resolve(strict=True)
    for relative in CHANGED:
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("invalid source path")
        target = ROOT / relative
        if (
            not target.is_file()
            or target.is_symlink()
            or not target.resolve(strict=True).is_relative_to(repo_root)
        ):
            raise ValueError(f"expected ordinary project file: {relative}")
        entries[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    MANIFEST.write_text(
        "".join(f"{entries[name]}  {name}\n" for name in sorted(entries)),
        encoding="utf-8",
    )
    print(f"Updated {len(CHANGED)} scoped source digests; retained other entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
