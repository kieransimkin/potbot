# Potbot Phase 7 — integration repair and SYN event read API

**Patch baseline:** `33909e624a8d30c3aed8731cef051f66283fbae9`.

This increment corrects test and authority-boundary regressions observed in GitHub Actions and introduces an **optional, read-only** universal SYN execution event API. It does **not** claim that unrestricted network research, paid model benchmarking, or sealed type activation is qualified.

## Fixes

- The authenticated research controllers lock the mutable `need_status` row, not the immutable `information_needs` row. The old `FOR SHARE OF n,s` attempts a row lock on an append-only table for which the restricted application account deliberately has no `UPDATE` permission (SQLSTATE `42501`). Application permissions are not widened.
- The integration app-environment fixture registers a separate explicit workspace sandbox for each authenticated workspace, keeping the CLI artifact path inside the selected root.
- A Phase 6 controller test reads the documented `lifecycle` field rather than a nonexistent `status` field.
- The Linux subprocess shutdown test tolerates `/proc/PID/status` disappearing between two operations; it still fails if a descendant remains runnable.
- The newly committed T01.3 event implementation is formatted using the project's pinned Ruff tool before CI.

## Monitoring API (read-only)

The normal `potbot serve` launcher now supplies `ExecutionEventService(database)` to `create_app`, activating the read-only routes. Direct library callers can still omit the dependency:

- `GET /v1/executions/events?after=N&limit=M` — workspace-scoped event replay.
- `GET /v1/executions/{run_id}/events?after=N&limit=M` — scoped single-run replay.
- `WS /v1/executions/stream` and `WS /v1/executions/{run_id}/stream` — authenticated cursor replay and live polling using the same bounded job streaming machinery.

No supplied event service means no new routes. Existing `/v1/jobs` contracts remain intact. Neither endpoint can append events or execute work, and persistence continues to be controlled by the trusted `ExecutionEventService`. A WebSocket `cursor` is commit-ordered; it can have gaps due to filtered event families. Clients must retain the cursor and deduplicate after reconnect.

## Still required for Phase 7 release qualification

1. Green newest-head PostgreSQL tests (including RLS, tenant separation, migration 19, research worker and event cursor concurrency), unit/mypy, sandbox and packaging CI.
2. Research/Cog semantic types imported and pinned to a sealed SYN release with explicit task revision opt-in. The committed T01.3 envelope currently validates type URI syntax **only**, not sealed release membership.
3. Authority-checked external source worker backed by enforcing DNS/IP egress isolation, disclosure checks, real budgets, immutable third-party source artifacts and non-replay reconciliation. Existing preflight must not be mistaken for authorisation.
4. Evaluated example adaptations and need resolution verified independently against a qualified sandbox.
5. Truly held-out live model comparisons with complete invocation and verification receipts, actual costs and hardware/provider revisions. Existing synthetic fixture scores are not model performance claims.
6. Instrumenting model/MCP/tools and terminal metrics is a later execution-monitoring increment; this patch exposes persisted events but does not create them automatically for every job.

## Syn integration consequence

The [Syn planning amendment](implementation/07-syn-integration-plan-impact.md)
adds two-service qualification, scoped credentials, safe event export and fenced
delivery/inbox recovery to the existing programme. Source inspection/research
events, semantic release activation, graph admission and actual task verification
retain their current owners and gates. A remote receipt cannot resolve a need,
authorize a tool, activate a Cog or qualify a model.

![Proposed delivery and incoming-message admission between Potbot and Syn](media/syn-plan-boundaries.svg)

*Generated planning illustration, not a demonstration of the proposed relay/inbox.*

The integration draft's first remote checks failed before application tests at
private Syn submodule checkout. Authorized read-only source access now succeeds;
the restored run exposed three historical migration assertions, which were
corrected without changing migration SQL. Continue to require all exact-head
gates. The [first credential increment](implementation/08-syn-scoped-credentials.md)
adds installation/workspace/origin binding and Syn token capture filters.
Existing local Syn/client fixtures do not replace Phase 7 PostgreSQL, sandbox
or live-model qualification. The initial manual `sync-events` command is not
the automatic outbox delivery planned here.
