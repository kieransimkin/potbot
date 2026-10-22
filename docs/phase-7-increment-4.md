# Phase 7 — increment 4: qualified repository-source replay

**Source baseline:** `kieransimkin/potbot` main commit `84d220fe2fb29d1c001987654c30a75d25048492`, with compatible already-applied mypy fixes at `c6d44ddd8c9557e778177f25db81d67756736080` (22 October 2026). This increment is an additive patch; it does not rewrite migrations 1–17 or activate external network research.

## Included

- Carry forward the three mypy repairs from Actions run `39755676660`. The installer recognises the already-committed improved fixes at `c6d44dd` and leaves them untouched; older eligible baselines receive exact-anchored equivalents.
- `ResearchAuditStore.audit(context, run_id)` independently re-reads **both** immutable source and result artifacts, reconstructs the query using the original time-qualified source observations, and checks the stored batch envelope, source and action scopes, observation fingerprints, receipt hash, generation, output SHA-256 and referenced PostgreSQL observation rows.
- Audit reports `verified_immutable_repository_source_only`, with `need_resolved=false`, `patch_verified=false`, and `external_source_license_verified=false`. It **never** admits patch execution, approves copyrighted examples, or makes model-performance claims.
- `python -m potbot.research.worker_cli ... audit <run-id>` for operator inspection, preserving the existing workspace/principal authentication and restricted DB login.
- Pure offline anti-fabrication and real PostgreSQL positive/negative integration tests.

## Verification

Run from a configured Potbot root:

```powershell
uv run --locked --extra postgres --dev python -m ruff check .
uv run --locked --extra postgres --dev python -m ruff format --check .
uv run --locked --extra postgres --dev python -m mypy
uv run --locked --extra postgres --dev python -m pytest -q tests/unit/test_phase7_research_audit.py
uv run --locked --extra postgres --dev python scripts/check.py --with-postgres --with-sandbox
```

The final command requires an **explicit disposable** PostgreSQL fixture, pgvector and an enabled Linux Bubblewrap sandbox. Nothing in this update calls a paid model or fetches from external HTTP/MCP sources. The qualification remains unverified until CI completes.

## Next gate

Integrate independent external-source approval (operator-controlled egress with DNS/IP pinning and disclosure approvals), budget reservations, immutable remote fetch blobs and settlement/reconciliation. Only then use source queries to propose code adaptations, with separate tests and a held-out live-model benchmark.

**Concurrent CI note:** at `c6d44dd`, latest-head CI `39758141227` fails on 10 Ruff diagnostics in the independently developed MCP transport and its tests. This Phase 7 increment does not automatically rewrite concurrent MCP modules. Clear those scoped imports/unused-loop-variable diagnostics in that workstream before expecting a green repository-wide CI.
