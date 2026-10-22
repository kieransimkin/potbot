# Potbot Phase 6 — controller and preliminary benchmarks

**Baseline:** GitHub `e5c9b35f8db138852ec6696fdff131f9e8253c5a` (22 October 2026). This is a proposed, unmerged increment. The repository's separate security, workspace sandbox and Syn catalog are authoritative and are not replaced by this patch.

## Delivered in this increment

- Migration 15: `solve_runs`, immutable branch views and graph nodes/edges, fenced mutable frontier with separate event history, candidate contributor attribution, and Cog receipts; all workspace-scoped with existing RLS/role machinery.
- `SolveStore`: pinned run creation, explicit evidence-source validation, idempotent graph operations, leases, state-bound result artifacts, no automatic replay on expired claims, strict task revision/lifecycle rechecks, and scoped inspection.
- `SolveCogBridge`: route a **completed, settled** hosted invocation's retained output through registered Type-to-Type Cogs, create new output artifacts and linked Cog receipts; transformation does **not** authorize execution. The original model context/usage, handoff and exact-state verification still apply.
- `DepthFirstSearch`: deterministic finite LIFO frontier, explicit claim/generation, backtracking, verified-stop and conservative indeterminate stop. The root context is an inert anchor, not a billed model call. This is a *pure policy projection*, not a new authority to dispatch remote calls.
- Offline programming benchmark: six authored Python fixtures with failing baselines, two intentionally distinct synthetic patch candidates, strict Cog chains, fresh candidate copies, unchanged check definitions, executed `unittest` checks, graph observations, and search traces. Results are included below.

## Reproduction

From the repository after applying the update and installing project dependencies:

```bash
uv run --locked python scripts/benchmark_phase6.py --max-candidates 1 --output benchmarks/k1.json
uv run --locked python scripts/benchmark_phase6.py --max-candidates 2 --output benchmarks/k2.json
uv run --locked python -m pytest -q tests/unit/test_phase6_search_scheduler.py tests/unit/test_phase6_benchmark.py
```

On Windows, use `start.ps1` for ordinary app commands and `uv run --locked` for the standalone benchmark script. No credentials or paid model API calls are required.

Run the unverified PostgreSQL integration gate in a proper Linux/WSL dev environment with `POTBOT_TEST_DSN`:

```bash
uv run --locked python scripts/check.py --with-postgres --with-sandbox
```

The next GitHub CI run must qualify migrations and OS isolation; this local environment has neither a PostgreSQL server nor the pinned Python 3.12 runtime.

## Observed results — synthetic fixture baseline only

| Measure | k=1 | k=2 |
|---|---:|---:|
| Authored programming tasks | 6 | 6 |
| Baselines reproduced as failing | 6 | 6 |
| Candidate patches executed | 6 | 12 |
| Verified passes of supplied checks | 0 | 5 |
| Empirical success@k | 0% | 83.33% |
| Correct solution-or-stop against fixture labels | 1/6 | 6/6 |
| Backtracks observed | 0 | 6 |
| False verified successes on deliberately unresolved task | 0 | 0 |
| Live model deployments evaluated | 0 | 0 |

The five passing fixes cover arithmetic, inclusive boundary, normalization, reuse of an existing clamp helper, and regression-safe parity behavior. Both candidates fail the `unresolved` exponentiation case, and Potbot reports no verified patch. The first candidate is deliberately incorrect on every case; a large `k=2` improvement is expected by construction, not evidence of realistic model effectiveness or independence. This is an **orchestration smoke benchmark**, not a model quality comparison. Inputs and test assertions are authored and visible. Verification checks only what those tests assert; it does not prove all possible behavior correct. Durations are environment-dependent and are not used for performance claims.

### Recorded limitations

- The benchmark's fixture-only subprocess execution uses separate temporary directories but **not** Potbot's production Bubblewrap sandbox. It is **not** intended for arbitrary downloaded code or live model output.
- No actual Claude, OpenAI, Grok/xAI, Gemini, EvoLink, or local LLM call is in this run. Provider capability/qualification, monetary cost, tokens and inference throughput are not measured.
- Live PostgreSQL integration tests have been authored and collected but **not executed** here. `SolveStore` and `SolveCogBridge` remain *integration-pending* until real database, workspace isolation, and fault-injection gates pass.
- The REST/WS monitoring layer has not yet been wired to the `SolveStore` graph projections; no claim is made of production autonomous solving or completed Phase 6.
- Phase 6's full independent multi-reasoner proposals, active external investigation, patch composition, adaptive selection and live-model benchmarking are further work. Later long-term memory admission, learned utility and generalized MCP remain separately gated by the implementation plan.

## Next qualification and benchmarking gates

1. On a disposable PostgreSQL installation, run the new tests for create/replay, cross-workspace scope, contributor attribution, live-lease fencing, quarantined expiry, paused task, and stale contract rejection. Verify migration ledger compatibility from v14 to v15 and both historical lineages.
2. Connect job runner to `SolveStore.claim` / `SolveCogBridge.transform_hosted` and the existing implementation-contract admission, resource reservation, candidate application, check assessment and exported verification manifest; ensure all model calls have matching invocation evidence.
3. Extend the REST API with bounded, workspace-scoped run/graph/need/candidate projections. Use existing durable events and WebSocket cursor replay instead of in-process broadcasts.
4. Run an end-to-end fault-injection fixture that kills a worker before/after provider dispatch, after artifact creation, and after patch effect publication; require zero unsafe reruns and correct indeterminate reconciliation.
5. Run actual model qualification under explicit user-configured credentials, budgets, policy and sandbox. Split data into training/dev vs held-out tasks and report pass@1, pass@k, true verified rate, false-positive rate, cost, input/output tokens, total time, context consumption, reuse rate, repair/backtracks, and variance across repeated runs. Never combine synthetic results with live benchmark scores.

No request to hosted providers should happen implicitly from running this offline benchmark.
