# Phase 6 — Increment 1: controller identity and stream lifecycle

**Rebased against GitHub `main`:** `03dd92b69013e31b6b0552972904264e2ec3e516` (WebSocket fix, 22 October 2026). The original Phase 6 increment was prepared against `f02c7ba` before the separate security, sandbox and WebSocket commits.

## Changes

- `domain/search_graph.py`: pure run-policy, knowledge-view, node, edge and graph projection contracts. Every node is scoped to the workspace, task revision and semantic-release digest. Nodes deduplicate only by their same state and evidence identity. Cross-state relation admission is restricted to explicit continuation or verification and all cycles are rejected.
- `tests/unit/test_phase6_graph.py`: scope, budgets, identity, stale revisions, graph duplication and cycle tests.
- The WebSocket fix originally proposed by this increment has **already been superseded** by commit `03dd92b` on `main`. This rebased patch **does not modify `src/potbot/api/server.py`** and preserves the newer fix, security features and sandbox setup.

## Boundaries

This is **not** the complete Phase 6 controller. The new graph structures are in-memory admission contracts, not yet PostgreSQL-backed with fencing. They do not confer execution authority; semantic type validation remains the responsibility of the registered SYN service. This package does not change migrations, credentials, local/hosted provider routing, or secret custody.

## Verification

- Focused pure graph tests: **5 passed** under Python 3.11-compatible syntax in the local Python environment.
- Full repository checks and live GitHub Actions are **not verified** for this increment. The exact `uv.lock` environment was not installed, and a real Postgres/sandbox suite was not executed.
- WebSocket lifecycle changes are intentionally excluded from this patch. Their current verification status must be established independently from the latest GitHub Actions run; this package cannot certify it.

## Next increment

1. Validate the existing `03dd92b` WebSocket lifecycle fix in CI and do not reapply the obsolete server hunk.
2. Add PostgreSQL run policy and graph tables with workspace row-level policies, idempotent commands, CAS transitions and worker-generation fencing.
3. Connect actual hosted model response receipts to typed reasoning/coding admission.
4. Add a deterministic single-branch controller fixture through existing sandboxed patch verification.
