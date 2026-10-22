# Potbot Phase 7 — Increment 5: audited research as solve-graph evidence

**Base:** `kieransimkin/potbot` `c6d44ddd8c9557e778177f25db81d67756736080` (22 October 2026). The package **includes the unmerged Increment 4 source-audit work**, so it is cumulative from this commit. No direct GitHub write, model call, network research request, or production database migration is performed by the installer.

## Delivered

- **Independent source audit** (`ResearchAuditStore.audit`): replays the original research query against immutable stored repository snapshot bytes; checks canonical result content, every observation's digest/excerpt/lines, the research receipt, and retained observation rows. A passing audit means only that immutable repository source bytes were inspected successfully, not that the inspected code is safe, correct, licensed, or a verified patch.
- **Source-to-graph bridge** (`ResearchSolveBridge.link`): controller-only operation that audits a *completed* local source-inspection run before opening an authenticated restricted PostgreSQL transaction. It rechecks the active task, information-need revision, exact source repository state, pinned semantic-release digest, source event and solve-policy revision under SQL locks. It then creates an immutable `KnowledgeView` with the preserved old evidence plus the checked source event digest.
- **Closed observation nodes:** Inserts a `NodeKind.OBSERVATION` under a selected existing solve node, records an `observes` edge, a source contribution and a **finished** frontier projection. No new model/patch execution is scheduled; the observed source cannot masquerade as a verification or coding candidate. A later controller can explicitly choose to use the verified source, subject to its own tool budget and task revision gates.
- **Migration 18, additive:** Append-only, workspace-scoped `research_solve_links` with foreign keys into existing research, solve-graph, evidence-event and view/node records. It links one research run once per solve run, keeps the parent node, stores a content identity, and prevents cross-run or cross-workspace pointer swaps. Previously published migration 1–17 statements and checksums are never rewritten.
- **Operator CLI:** `python -m potbot.research.worker_cli ... audit <research-run-id>` and `... link-source <research-run-id> <solve-run-id> <parent-node-id> --key <idempotency-key>`; authentication still comes from the configured restricted workspace database identity.
- **SYN type declaration** (`synny://type/potbot-research-solve-link`) is appended to the *pending*, not yet activated research type tree. No existing task release is changed or silently repinned; no Cog activation is implied.
- **Tests:** Pure view transformation and migration tests, plus disposable PostgreSQL integration checks for source bytes, graph links, idempotent replay, cross-workspace rejection, wrong parent, frozen frontier, unchanged open need, and blocked transitions after the task is paused.

## Correctness and authorization boundaries

Only a trusted controller is permitted to call the link operation. The pure knowledge-view transform accepts an existing checked event digest but does **not** attest it; the durable operation looks the event up in the authenticated workspace and checks its exact run/action/receipt/source/output bindings. Any source observation must first pass the independent byte-level replay audit.

The link uses a single database transaction to create the new view, node, finished frontier, edge, contribution and append-only research/solve link; application-level command idempotency and a locked solve run prevent double-publishing. It does **not** resolve the information need, run a coding tool, approve third-party licenses, or change the solve policy.

Model-generated identifiers, tools, source claims and JSON must not be accepted as signing/authorization capabilities. The trust boundary remains the existing `WorkspaceContext` bound to a restricted PostgreSQL login; this method is not a general HTTP endpoint. The `research_solve_links` type remains pending a separately approved SYN semantic-release import, ancestry/handler review and explicit task pin.

## Apply and verify

From the Potbot repository directory (or its parent), after extracting the ZIP:

```powershell
& "<extracted>\apply.ps1" -RepositoryPath .
```

The installer refuses unknown edits to its own new files, checks the expected Git ancestor, and only makes exact, guarded patches to the older CLI, migration list, checksum updater and two version-expectation tests. It leaves unrelated source untouched, and never commits or pushes. It can be re-run safely after a partial application.

Run the project verification after reviewing `git diff`:

```powershell
uv run --locked --extra postgres --extra providers --dev python -m ruff check .
uv run --locked --extra postgres --extra providers --dev python -m ruff format --check .
uv run --locked --extra postgres --extra providers --dev python -m mypy
uv run --locked --extra postgres --extra providers --dev python -m pytest -q tests/unit/test_phase7_research_audit.py tests/unit/test_phase7_research_solve_bridge.py tests/unit/test_phase7_graph_schema.py
# Run on a disposable pgvector-backed database with the project's test harness:
uv run --locked --extra postgres --extra providers --dev python scripts/check.py --with-postgres --with-sandbox
```

A green full CI is still required. The baseline's latest `verify` run was failing on **11 Ruff issues in the unrelated MCP/interactive tool tests and transport**, so a fresh CI run will only reach this work when those are also repaired. Do not claim a passing PostgreSQL integration gate or an activated Cog until it actually passes.

## Phase 7 remaining gates

1. Qualify migration 18 and source replay against real PostgreSQL with row-level security, multiple app roles, result tampering and concurrent callers in green latest-head CI.
2. Activate the research type tree and independently reviewed Cog binding via a new sealed SYN release; test release ancestry and explicit task opt-in.
3. Implement operator-owned external research execution with network namespace/proxy IP pinning, disclosure decisions, real budget reservations, provider schemas, immutable response artifacts, and ambiguous paid-call reconciliation. `ResearchSourceCatalog.preflight` alone is **not** authorization to send requests.
4. Add evaluated, sandboxed *example adaptations* and explicit information-need resolution only after independent patch/test receipts; do not treat source inspection as code correctness.
5. Perform true held-out model comparisons with measured invocation cost/time/tokens, provider revision, actual hardware identity and neutral verification receipts. Existing synthetic fixture `pass@k` is not evidence of model quality.
