# Phase 7 — Increment 3: durable repository research worker (cumulative)

**Baseline:** `kieransimkin/potbot` commit `bbd2ec78165bac7f402effa520d410ea222ef064`, ancestor of current checked head `8c2399d337c1703fb296752292e051770fb49324` (22 October 2026); increment 2 is now already committed at that head. This is a proposed source package, not a GitHub merge. **It includes increment 2**; do not apply increment 2 separately.

## What this increment delivers

1. **README/Hatchling fix.** The earlier tool-plan commit removed the root `README.md` (still missing at the newer head); `uv sync --locked` now fails before executing project checks. The installer restores the original Phase 7 README *only when absent*. Existing READMEs and unrelated tool-plan documents are untouched.
2. **Ruff cleanup.** Format and import-sort only the existing/new Phase 7 source and test files. The baseline `bbd2ec7` Linux verify failed on seven Phase 7 Ruff diagnostics. Do not run blanket `ruff --fix .` over concurrent work.
3. **Typed external source catalogue (from increment 2).** `ResearchSourceCatalog` binds one operator-configured exact HTTPS URL, category, declared source revision and egress-policy digest to an immutable query plus reservation/disclosure identities. It has a pure, mockable observation projection; no production egress is enabled.
4. **Migration 17, additive.** `research_runs` implements a workspace-scoped queue, one action per run, claimed generation and bounded lease; terminal runs are not automatically retried. `research_run_receipts` is append-only and binds source/output artifacts and a content digest. Historical migration versions 1–16 remain unchanged.
5. **Executable repository-only research.** `ResearchRunStore` admits a repository research action that has already been approved under migration 16, rechecks its task, need, semantic release and repository state, then queues it. `LocalResearchWorker` consumes exactly one queued action from an authenticated restricted PostgreSQL login. It reads only the retained `RepositorySnapshot` bytes, with no mutable filesystem access and no network calls. Workers do not execute repository files or generated code.
6. **Evidence integrity fix.** The previous `ResearchStore.record_snapshot_observation()` merely checked the claimed file SHA against a manifest: a caller could forge an excerpt. It now requires `ArtifactStore` and recomputes the observation from the immutable source bytes within the publication transaction. The new worker independently recomputes its entire observation batch before committing its output artifact, observation rows, event and receipt in one transaction.
7. **Explicit operator CLI.** `python -m potbot.research.worker_cli`, using the same principal/workspace Postgres protection and credential resolver as other Potbot tools. It provides `queue`, `run-once`, `inspect`, and `quarantine-expired`; no model calls.
8. **Focused tests.** Pure unit/schema/permit checks, plus real PostgreSQL integration tests for successful immutable-source reads, forged-content rejection, idempotent queueing, stopped/stale tasks, lost leases, cross-workspace denial, receipt integrity and network-source blocking.

## Critical authority boundaries

- Only a real, scoped, restricted PostgreSQL connection authorizes queue/claim/publication. An arbitrary model-generated `ResearchLease` cannot bypass the current row state, lease owner, generation, expiry, active task contract, current information-need revision, repository state or sealed task semantic release.
- A `research_runs.state='succeeded'` means **source inspection finished**. It is not a verified fix, an accepted patch, a resolved need, verified attribution, a licensed reuse decision or authorization to run code.
- Observations are recreated from the immutable stored source bytes and compared for exact contents, excerpt line range, digest, completeness and status. No source text is trusted merely because it includes an apparently correct hash or claims to pass tests.
- Expired running leases become `indeterminate`, never `queued`. Rerunning a quarantined effect would require an explicit future reconciliation/admission design.
- External documentation/example/MCP URLs remain **non-executable** through this worker. The source catalogue is a preflight contract, not proof that a public host resolves to a safe address. A pinned-egress proxy/network namespace, disclosure decision, rate limits, reservation settlement, provider-specific response schema and immutable source artifact must exist before enabling external research.
- SYN research types/Cog still require validation/import into a new sealed release and actual task pinning. The local worker produces typed `ResearchObservationBatch` data but intentionally marks its receipt `cog_activation: pending_pinned_syn_registration`. It does not silently activate a Cog in old tasks.
- The worker's repository read has no monetary provider cost. It does not claim cost accounting, budget settlement or real-model benchmark qualification for paid calls.

## Apply and acceptance

From the Potbot repository root or its parent, unzip the package and run its `apply.ps1` on Windows or `apply.sh` on Linux. `uv` and Git are prerequisites. The launcher repairs the absent root README before invoking `uv`; the Python installer checks the Phase 7 ancestor, safely handles a previously applied increment 2, writes only scoped files, applies Ruff formatting and source checksums, and runs focused unit tests.

For full acceptance in a trusted Linux environment with a disposable PostgreSQL fixture, pgvector and Bubblewrap:

```bash
uv run --locked --extra postgres --extra providers --dev python scripts/check.py --with-postgres --with-sandbox
```

Focus the new integration gate if necessary:

```bash
uv run --locked --extra postgres --extra providers --dev python -m pytest -q tests/integration/test_phase7_research_worker.py tests/integration/test_phase7_research_store.py -m postgres
```

Standalone operator example (with an already registered, admitted repository research action and configured Postgres credentials):

```powershell
uv run --locked --extra postgres --dev python -m potbot.research.worker_cli --workspace-id <workspace-uuid> --principal-id <principal-uuid> --artifact-root <workspace-artifact-root> queue <research-action-uuid> --key research-first
uv run --locked --extra postgres --dev python -m potbot.research.worker_cli --workspace-id <workspace-uuid> --principal-id <principal-uuid> --artifact-root <workspace-artifact-root> run-once --worker-id local-research-01
```

`<...>` arguments are placeholders; use your existing workspace identities and trusted artifact root, and never paste production credentials into logs. Local model calls, remote HTTP, source compilation and patch application are not triggered by the example.

## Follow-on Phase 7 gates, still outstanding

1. Qualify migration 17, RLS, job expiry, errors and publication on disposable PostgreSQL/pgvector in **green latest-head CI**; verify clean-wheel builds and strict mypy. Do not call a CI failure green merely because the earlier Ruff errors were repaired.
2. Wire research execution into existing resource reservation/usage ledgers, and place *qualified external-source* workers behind operator-pinned egress and credential injection with explicit non-replayable paid-call reconciliation.
3. Activate research types, Cogs and versioned handler bindings through a new SYN release; verify ancestry/type contracts before running the Cog handler.
4. Add immutable actual remote source response artifacts (not just projected text), attribution/license metadata, freshness, error typing and per-provider MCP documentation/example adapters.
5. Bind research evidence to solve graph nodes, evaluated local adaptations and information-need resolution; require independent verification for patch effectiveness.
6. Run a held-out, actual-model comparative benchmark with legitimate provider revisions, deployment/hardware identity, money/tokens/time measured, genuine verification receipts and a neutral evaluator. Fixture `k=2` numbers must never be presented as live-model win rates.
