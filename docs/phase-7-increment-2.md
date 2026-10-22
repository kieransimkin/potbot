# Phase 7 — increment 2: exact external-source preflight and CI repair

**Baseline:** `kieransimkin/potbot` `bbd2ec78165bac7f402effa520d410ea222ef064` (22 October 2026). This increment is additive to Phase 7 increment 1. It is applied locally; no GitHub branch, commit, merge or push is performed by this package.

## Delivered

- `potbot.research.source_policy.ResearchSource`: operator-configured external documentation, example-code, or MCP resource with an exact HTTPS URL, declared dependency/source revision, maximum response length and an egress-policy identity.
- `ResearchSourceCatalog`: immutable order-independent fingerprint for a bounded, uniquely named resource set.
- `ResearchFetchGrant`: *references* to independently admitted action, source catalog, reservation, disclosure decision and deployed egress policy. The grant dataclass **is not a capability**: controllers must verify all referenced identities in authoritative durable tables. This type must never be accepted directly from the model or an HTTP client.
- `ResearchSourceCatalog.preflight`: binds one previously admitted `ResearchAction` to an exact configured resource, declared revision, byte ceiling and grant. Returns the existing constrained `FetchPermit` without performing network I/O.
- `ResearchSourceCatalog.observe`: turns only a matching `FetchResult` into a typed `ResearchObservation`. It preserves source digest and content hash; distinguishes fetched UTF-8, empty, non-text, timeout, rate-limit, redirect-denied and oversized results. The license stays explicitly **unknown**. A failed fetch does not resolve an information need or count as verified source evidence.
- New isolated unit tests cover URL restrictions, catalog uniqueness/digests, stale or forged grants, query version mismatch, budgets, observation identity and non-successful fetch classification.
- Scoped automatic Ruff lint/import and formatting repair for the existing Phase 7 files, including the six files implicated in GitHub Actions run 39748553676; the source SHA-256 manifest is then refreshed using the existing *scoped* hash updater.
- A guarded launcher bootstrap for an **independent concurrent regression**: commit `fcede56c0390fa478acc79bf416468508fc0afc1` deleted the project root `README.md`. Hatchling then fails with `OSError: Readme file does not exist: README.md` during editable dependency installation in Actions run 39749990292. The launchers use `git restore --source=bbd2ec7 --worktree -- README.md` **only when README.md is absent**, so no existing user file is replaced. Commit the restored file along with other intended changes to repair that build failure.

## This is NOT a network-research or budget-execution release

An attacker-controlled `ResearchFetchGrant` can be constructed with arbitrary UUIDs, so preflight **alone does not authorize an HTTP request**. A production controller must independently confirm the action is still open, its workspace/task/need and SYN release match, the budget reservation is active and settled/chargeable according to existing accounting rules, the disclosure policy permits sending the request, and the proxy enforces pinned hostname/IP/DNS policies. The existing `http_transport` does not defeat DNS rebinding without an enforced egress boundary. Sources are configured by the operator and fetched only by an authorized worker; never trust LLM text to create URLs or hostnames.

Similarly, interpreting an in-memory `FetchResult` is not evidence that remote bytes have actually been fetched. Future execution work must persist the exact immutable response artifact, reconcile usage, and link controller-verified invocation and source receipts before promoting a source or resolving the information need.

This increment has **no** arbitrary web search, third-party SDK, paid model invocation, execution of downloaded examples, production MCP calls, unsigned source promotion, or benchmark performance claims. SYN research type/Cog definitions still require a separately versioned, pinned semantic release.

## Applying and verifying

From inside the `potbot` checkout or the directory containing `potbot`:

```powershell
# Extract the ZIP, then run this file from within the extracted package.
.\apply.ps1
```

Or on Linux/macOS, with `uv` installed:

```bash
bash ./apply.sh
```

The installer checks that this commit is part of the checkout's ancestry and that **all existing files it will format** have no local or committed changes since that baseline; unrelated worktrees and changes are left alone. It never writes to git history or initiates remote actions. After applying, inspect `git diff` and run:

```bash
uv run --locked python scripts/check.py --with-postgres --with-sandbox
```

The full qualification gate requires PostgreSQL, sandbox support, Python/dependencies specified in the lockfile, and the latest GitHub Actions result. The installer attempts Ruff lint/format and the focused source-policy tests, but its success is not a substitute for real PostgreSQL/RLS, an egress-network-policy test, an authenticated controller grant, or a true held-out model benchmark.

## Next increment

Wire a *controller-owned* research worker to `ResearchStore` and `RepositoryStateStore` using immutable source artifacts, atomic action/receipt publication, restart-fenced execution with no duplicate network effects, actual budget/disclosure records, and a typed Cog output bound to a pinned SYN release. Add a source-provider qualification harness and tests for stale needs, job cancellation, budget exhaustion, SSRF/rebinding, tampered source snapshots and cross-workspace replay. Only after this should external-search, documentation, examples and MCP adapters be admitted for production.
