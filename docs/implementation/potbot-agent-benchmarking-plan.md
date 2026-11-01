# Potbot agent benchmarking and testing programme

**Proposed: 2026-11-01.** This specification extends U14 and applies to U00–U16, C00–C09, S00–S10 and the existing 48-stage tool plan. It does not claim any benchmark has passed. Preserve PostgreSQL as the authoritative ledger, SYN as provenance, and the operator/Cog gateway as the only authorized side-effect path.

## Research findings and benchmark selection

| Domain | Benchmark candidates | Evaluation focus | Priority |
|---|---|---|---|
| Coding | SWE-bench Pro V2, SWE-bench Multilingual, internal repository fixtures | Patch correctness, regression avoidance, independent clean-image grading | P0 |
| Terminal | Terminal-Bench / Harbor | CLI, debugging, services, environments, filesystem state | P0 |
| Function calling | BFCL V4 | Schema, argument selection, parallel/multi-step calls | P0 |
| MCP and tools | MCPMark Verified, MCP-Atlas, internal SYN Cog fixtures | Real server state, typed contracts, policy and receipts | P0 |
| Browser | WebArena, BrowserGym, Online-Mind2Web | DOM, accessibility, screenshot, form, download, SPA and session behavior | P0 |
| Desktop | OSWorld 2.0 | Long multi-application workflows in disposable desktop environments | P1 |
| Research | GAIA, BrowseComp | Retrieval, evidence and correct sourced answers | P1 |
| Policy dialogue | tau2-bench, tau3 tracks | User interaction, policy compliance, final-state correctness, pass^k | P1 |
| Memory | LongMemEval, LoCoMo, custom temporal fixtures | Retrieval, temporal reasoning, contradictions, access isolation | P1 |
| Multi-agent | AgentBench and Potbot graph fixtures | Coordination, delegation, budgets, recovery and parallel efficiency | P1 |
| Security | AgentDojo and Potbot adversarial fixtures | Prompt injection, exfiltration, capability enforcement, benign completion | P0 |
| Skills | Pinned OpenClaw/ClawHub, Codex and Claude packages | Discovery, import, eligibility, native Cog execution and export | P0 |
| Endurance | METR-inspired horizon tests, custom long runs | Autonomous completion, checkpoint/restart, cost and intervention | P1 |

**Quality caveat:** A July 2026 audit reported substantial defects in the original SWE-bench Pro dataset; September 2026 Pro V2 introduced reviewed tasks and clean-image regrading. Potbot must independently validate chosen tasks: oracle patch passes, empty patch fails, prompt matches tests, and benchmark answers are inaccessible to agents. Freeze dataset, grader, sandbox image and network rules. Never mix versions or leaderboard results with different budgets/harnesses.

## Evaluation architecture

Add an `EvaluationService` on top of the existing PostgreSQL queue, SYN event/receipt ledger, Cog gateway, operator sandbox and artifact storage. Do not create a second privileged scheduler or parallel permissions registry.

Logical modules: `BenchmarkCatalog` (dataset/license/version/hash); `FixtureManager` (immutable setup and cleanup); `EvaluationPlanner` (model/harness/seed/budget matrix); `EvaluationRunner` (operator-authorized attempts); `Verifier` (isolated, outcome-based grading); `MetricEngine`; `RegressionGate`; `ReportExporter`.

Proposed additive tables (reconcile with current schema): `eval_suites`, `eval_cases`, `eval_campaigns`, `eval_variants`, `eval_attempts`, `eval_verdicts`, `eval_metrics`, `eval_baselines`, `eval_waivers`. Foreign-key to existing authoritative run, attempt and artifact IDs rather than duplicating state.

Each attempt captures: source SHA and dirty state, benchmark/grader hashes, model/provider/revision/quantization, inference parameters, prompt/compiler version, memory snapshot, installed skill and Cog digests, tool policy, sandbox image and host qualification, network mode, seed, resource/time/token/money budgets, events, outcome, verifier evidence, failure taxonomy, cost, wall time, peak CPU/RAM/VRAM, and human intervention. Missing measurements remain unknown, never zero.

## Native golden fixtures

Build a versioned `potbot-golden` suite with stable IDs, explicit expected state, negative controls and independent verifiers. Start with 10 cases in each family (90 minimum) and expand to 30+ per family after the vertical slice is proven.

- **G-CODE:** multi-file CRLF/BOM editing, stale base, symlink swap, interrupted journal, Git conflicts, debugging, CI.
- **G-TOOLS:** typed Cog inputs/outputs, MCP pagination/schema changes, denied effects, artifact ranges/uploads, cancellation and receipts.
- **G-BROWSER:** tabs, DOM/accessibility, screenshots, downloads, forms, auth stubs, SPA transitions, CDP reconnect, browser crash.
- **G-SKILLS:** OpenClaw/Codex/Claude skill parsing, eligibility, ClawHub provenance, dependency isolation, hooks, workflows, slash commands, subagents, rollback.
- **G-MEMORY:** temporal updates, contradictions, source verification, scope filtering, forgetting and episodic retrieval.
- **G-GRAPH:** cycle detection, duplicate work, delegation bounds, steering precedence, speculative branches, budgets, worker interruption.
- **G-SEC:** malicious SKILL.md/README/tool output, injection, cross-tenant data, credential attempts, package hooks and sandbox boundaries.
- **G-OPS:** PostgreSQL outage, lease fencing, SIGKILL at transaction boundaries, uncertain effects, replay, resource pressure and process ledger.
- **G-UX:** streaming reconnect, checkpoint graph, approvals, cancellation, accessibility and telemetry consistency.

Use synthetic accounts, disposable hosts and fake credentials. A successful tool response is not sufficient: independently inspect the final state. For cross-ecosystem compatibility, run real version-pinned skill packages where available and report unsupported features honestly.

## Metrics, comparisons and statistics

**Primary metrics:** verified task success, safety/authorization violations, reliability across repeated runs and cost per verified success. Never combine unsafe success with safe success into a single headline score.

**Secondary metrics:** pass@1, pass@k, pass^k (define separately), partial completion, median/p95 latency, input/output/cache tokens, model cost, tool calls, graph depth/width, duplicate work, memory recall precision, false memory admission, context utilization, prompt injection attack success, benign-task completion, false denials, retries, crash recovery time, CPU/RAM/VRAM/disk and human interventions.

Compare fixed single-model, model+fixed harness and full Potbot; use ablations for memory, context compaction, graph planning, model routing, skill retrieval and speculative execution. Use paired tasks and fixed budgets, randomized execution order, at least 3 repeated trials for exploratory stochastic comparisons and 5+ for release-critical results where feasible. Report denominator, confidence intervals (Wilson for proportions; paired bootstrap for differences), task-level failures, censored attempts and budget exhaustion. Pre-register the release metric and regression threshold; never cherry-pick best-of-N without accounting for N and total cost.

## Security, failure injection and release qualification

Inject crashes before/after tool intent, dispatch, receipt, checkpoint, event commit, model response and cancellation. Verify fencing, replay, reconciliation and no blind repeat of irreversible external effects. Include indirect browser/tool-output injection, poisoned memory, malicious package dependencies, cross-workspace access, symlink races and unauthorized network egress. Report both attack success and legitimate task completion: denying everything is not an acceptable defense. Keep red-team fixtures quarantined with no real credentials or unrestricted egress.

| Tier | Trigger | Coverage |
|---|---|---|
| E0 | Every PR | Fast deterministic unit, contract, schema, parser and policy checks; no paid models |
| E1 | PR / merge | Real PostgreSQL and qualified sandbox; selected golden fixtures; recovery and negative security |
| E2 | Nightly | Larger golden suites, BFCL/MCP/browser/coding subsets, stochastic repeats |
| E3 | Weekly / manual | Version-pinned external benchmarks, skill compatibility, long runs and ablations |
| E4 | Release candidate | Supported-platform, held-out, adversarial, reliability, cost and regression qualification |

Proposed gates: all deterministic authorization/security invariants pass; zero observed unauthorized side effects in release fixtures; no material statistically supported decline in verified task success; performance/cost within predeclared limits; each compatibility claim backed by a pinned real package and verified final state. Finite tests do not prove absolute safety; record residual risks. Waivers require scope, owner, reason, expiration and audit evidence.

## Implementation milestones B00–B10

| ID | Deliverable | Dependencies | Exit evidence |
|---|---|---|---|
| B00 | Inventory existing tests and benchmark candidates; pin versions/licenses/datasets; map U/C/S/T stages | U00 | Source-linked coverage matrix and benchmark manifest |
| B01 | Evaluation schemas and operator-mediated execution/receipt integration | U01, U14 | PostgreSQL migrations, durable restart tests |
| B02 | Golden fixture loader, independent oracle/verifier and negative controls | B01 | 90+ versioned cases, oracle-pass/empty-action-fail checks |
| B03 | BFCL, MCPMark and Cog tool adapters | B02, U04 | Typed call and final-state verifiers |
| B04 | SWE-bench/Terminal-Bench adapters | B02, U04–U05 | Clean-image patch grading, no answer leakage |
| B05 | BrowserGym/WebArena and desktop adapters | B02, U12 | Reproducible site/desktop snapshots and state checks |
| B06 | Memory, context, reasoning and multi-agent ablation harness | B02, U08–U10 | Paired multi-seed comparisons with budget accounting |
| B07 | OpenClaw, Codex, Claude and ClawHub skill compatibility benchmarks | B03, B05, C00–C09, S00–S10 | Real package fixtures, feature-by-feature support matrix |
| B08 | Adversarial, fault injection, endurance and isolation campaigns | B01–B07, U02–U06 | Attack/benign rates, restart and policy evidence |
| B09 | CI tiers, dashboard, regression baselines and signed reports | B02–B08, U13 | Reproducible PR/nightly/weekly/release jobs |
| B10 | Release qualification and ongoing benchmark refresh | B09, U16 | Auditable release report, dataset review and waiver register |

**Ordering:** Begin B00–B02 immediately with U00/U01. Parallelize B03–B05 once relevant Cog/browser surfaces qualify. B06–B08 track intelligence and compatibility milestones. B09 is incremental, B10 permanent. Do not block deterministic testing on paid models or inaccessible private datasets.

## First implementation slice

1. Inventory `scripts/check.py`, CI, existing benchmark queue, PostgreSQL and SYN run schemas.
2. Implement a versioned benchmark manifest and three initial cases: Cog authorization, coding patch, browser navigation.
3. Route one benchmark through the existing operator; verify state in an isolated process and retain signed SYN-linked evidence.
4. Compare two model/harness configurations at identical budgets with a task-level diff report.
5. Inject worker death and malicious tool output; confirm recovery, denial and correct scoring.
6. Add fast CI gates, then expand the catalog.

## Research sources (refresh at B00)

- SWE-bench Pro V2: https://labs.scale.com/leaderboard/swe_bench_pro_public_v2
- SWE-bench Pro audit: https://openai.com/index/separating-signal-from-noise-coding-evaluations/
- Terminal-Bench/Harbor: https://www.tbench.ai/ ; https://github.com/laude-institute/harbor
- BFCL: https://gorilla.cs.berkeley.edu/leaderboard.html
- MCPMark: https://mcpmark.ai/ ; MCP-Atlas: https://github.com/ScaleAI/mcp-atlas
- BrowserGym: https://github.com/ServiceNow/BrowserGym ; WebArena: https://webarena.dev/
- OSWorld: https://os-world.github.io/
- GAIA: https://huggingface.co/gaia-benchmark ; BrowseComp: https://openai.com/index/browsecomp/
- tau2-bench: https://github.com/sierra-research/tau2-bench
- LongMemEval: https://github.com/xiaowu0162/LongMemEval ; LoCoMo: https://github.com/snap-research/locomo
- AgentDojo: https://github.com/ethz-spylab/agentdojo ; METR: https://metr.org/
