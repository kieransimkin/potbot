# Potbot agent runtime modernization: research and integration plan

Status: **proposed**. Prepared 2026-11-01. This document is a design and qualification plan, not evidence that the described features are implemented. Reconcile it with the pinned main-branch commit and the existing 48-stage roadmap before changing stage statuses.

## Goals and architectural constraints

Evolve Potbot into a durable, policy-controlled, memory-aware multi-agent runtime without replacing the existing PostgreSQL, Syn semantic contracts, reasoning graph, sandbox operator, API, or UI. Preserve backward compatibility and avoid parallel authoritative stores. The operator, not a plugin or model, owns security decisions and side effects.

Key invariants:

1. PostgreSQL is the authoritative transactional state store; Markdown context files are controlled projections and editable inputs, never a competing database.
2. Every effect has a durable intent, authorization decision, idempotency/reconciliation strategy and causal evidence.
3. Workers execute only under operator-issued, expiring capabilities; retrieval applies scope filtering **before** model context construction.
4. No model response, plugin metadata, retrieved text, or signed publisher identity constitutes execution authority.
5. Every deployment claim is backed by a pinned revision, real PostgreSQL and sandbox qualification where applicable.
6. Existing `syn://` / `synny://` types, Ember contracts, telemetry, credential custody and API compatibility remain authoritative.

## Current baseline and integration points

The repository README describes implemented Phases 1–4, the Phase 5 foundation and hosted jobs/API at `0.5.0.dev8`, including durable tasks, model records, checkpointed patch pipeline, inference reservations, recipient-specific context packets and a PostgreSQL job queue. It explicitly marks live multi-model reasoning-tree selection, learned memory admission, live discovery, IBM/DanceFlow MCP and some qualification gates as future work. Verify these statements against the target commit and tests.

**Baseline reconciliation, 2026-11-01:** that README summary is historical. [R6](20-postgres-reasoning-and-model-providers.md) and [R6.1](21-reasoning-task-budgets-and-live-provider-qualification.md) document PostgreSQL-fenced bounded reasoning and optional task-bound accounting. [Stage 10D](50-model-md-runtime-integration.md) documents registered-object lifecycle and normal worker context integration, following authored skills and context RPC. Build on these foundations; keep constructor/protocol coverage, live deployment and qualification separate. The [recent-development reconciliation](recent-potbot-development-reconciliation.md) carries the original P/E stages, exact feedback/learning rules, semantic profiles and remaining feature gates into this programme.

Existing entry points to inspect before implementation:

- `src/potbot/controller/reasoning_runtime.py`: fenced checkpoints and event commits.
- `src/potbot/controller/reasoning_tree.py`: bounded graph execution.
- `context_contract/core.py` and the PostgreSQL MODEL.md stage documents: AGENTS/MODEL/MEMORY roles and durable context.
- `docs/implementation/plugin-discovery-strategy.md`: discovery, admission and qualification separation.
- `docs/implementation/12-canonical-tool-contracts.md`: typed semantic tool contracts.
- `docs/implementation/13-execution-provenance.md`: signed causal traces.
- `docs/implementation/09-tool-plan-gap-audit.md` and `scripts/check_tool_plan.py`: existing roadmap and acceptance gates.

Audit the current code, migrations, CI and tests first. Classify each requirement as implemented/qualified, implemented/unqualified, partial, planned or absent; do not equate a document with a shipped capability.

## Target architecture

`API/UI/Channels -> Operator/DurableRunEngine -> {ReasoningGraph, PromptCompiler, ContextEngine, MemoryEngine, PluginRuntime, EvaluationEngine} -> qualified sandboxed Cogs/model providers/PostgreSQL/artifacts/Syn events`.

All services communicate through versioned typed contracts, not unrestricted cross-imports or arbitrary plugin database writes. The operator enforces the security boundary and the durable ledger. Add OpenTelemetry export as a projection of Syn evidence, not a replacement.

## Workstream A: durable execution and side-effect safety (P0)

Research references: Temporal event histories and replay, LangGraph checkpoint/interrupt patterns, transactional outbox, idempotent consumers, leases and fencing tokens, event sourcing and saga compensation.

Extend the existing runtime rather than introducing a second scheduler. Model `runs`, `run_nodes`, `run_attempts`, `run_events`, `run_checkpoints`, `run_leases`, `tool_intents`, `tool_executions`, `run_interrupts` (names provisional; map to existing tables before migration). State transitions: created, queued, preparing, running, suspended, committing, completed, failed, cancelled, expired. Ensure transitions and outbox writes commit atomically; consumers tolerate duplicate delivery. Use monotonically increasing fencing epochs to reject stale workers. Checkpoints capture graph state, budget and pending intents, never raw secret material.

Tool protocol: propose -> validate capability -> persist intent -> execute -> persist receipt -> reconcile/compensate. Require idempotency keys where the target supports them. Where outcomes are uncertain, suspend for reconciliation rather than replaying a destructive call. Exactly-once external effects cannot be guaranteed without target cooperation. Support durable approval interrupts, cancellation and urgent/deferred/background steering.

Gates: process termination at every transition; expired leases; concurrent owners; DB outage/reconnect; duplicate event delivery; uncertain external outcomes; no duplicated irreversible action; replayed state equals committed state.

## Workstream B: prompt compilation and context engineering (P1)

Research: DSPy declarative prompt programs and GEPA/MIPROv2 optimization, retrieval-augmented generation, hybrid lexical/vector retrieval, reranking, hierarchical compression, lost-in-the-middle effects and prompt caching.

Create a `PromptCompiler` that preserves the existing instruction hierarchy: host policy > operator policy > authorized runtime contracts > workspace sandbox AGENTS.md > scoped object AGENTS.md > activated SKILL.md. Only authorized documents within their declared scope enter this hierarchy. MODEL.md, MEMORY.md, retrieved content and tool output remain data or evidence and confer no instruction or execution authority; assembly order and token priority do not change that boundary. Model-specific adapters format messages and tools but cannot weaken policy. Compile an immutable prompt manifest: run/node, sources and hashes, tokenizer/model revision, profile, tool permissions, token allocations, omissions, template version and cache key. Never log plaintext secrets.

Separate `ContextEngine`: scoped source discovery, PostgreSQL full-text + pgvector hybrid search, reranking, deduplication, relevance/diversity selection, token budgeting, provenance, compaction and retrieval diagnostics. Profiles: operator, reasoner, coder, researcher, validator and micro. Small models receive task-specific bounded contracts, not merely truncated operator prompts. Record why sources were excluded and distinguish untrusted evidence from instructions.

Gates: deterministic compilation for identical inputs; strict permission filtering; prompt injection does not elevate retrieved content; held-out task quality/cost comparisons; tokenizer and truncation regression tests.

## Workstream C: continual memory and verified experience (P1)

Research: write-manage-read memory architectures, episodic/semantic/procedural memory, reflection, LongMemEval, retrieval-augmented experience and access-controlled shared memory. Compare BM25, dense and hybrid retrieval on Potbot tasks instead of assuming vector-only superiority.

Memory kinds: working (run-local), episodic (verified trajectories), semantic (facts/decisions), procedural (skills/workflows), reflective (hypotheses requiring validation). Record id, kind, owner/scope, content hash, provenance/evidence, valid_from/until, confidence, supersedes, retention policy and embedding version. Keep PostgreSQL authoritative; render `/AGENTS.md`, `/MODEL.md`, `/MEMORY.md` via operator-mediated safe materialization.

Pipeline: capture -> extract -> verify -> deduplicate -> reconcile contradictions -> consolidate -> index -> retrieve -> expire/correct. Do not promote unverified model reflections to durable fact. Support time-bounded truth, supersession, correction propagation, retention/deletion rules and derived-index invalidation. Scope filtering happens before ranking and before any cross-agent sharing. Initial learning is retrieval of verified successful/failed trajectories; any LoRA or learned admission policy is offline, evaluated and separately approved.

Use the canonical [per-object memory programme](../object-memory/implementation-plan.md) and its unchanged [32-stage registry](../object-memory/plan.json) for MG01.1–MG08.4. Preserve original P085–P117: immutable proposals/evidence, accepted-head CAS, applicability/invalidation, correctable scoped feedback, successful/failed/unknown/flaky episodes, conditional procedures, temporary task guides and targeted delayed-defect revalidation. A context triple does not implement graph memory. Keep one existing admission lifecycle and PostgreSQL authority, require monitored Cogs, and qualify deletion, derivative cleanup and restore for every representation enabled in the shipped memory slice before storing real persistent user memory. See RD30–RD35 for exact inventory, seven retrieval profiles and standard-before-custom embedding requirements.

Gates: temporal contradiction tests, provenance round-trip, multi-session recall, deletion/expiry propagation, cross-workspace privacy, LongMemEval-style held-out tasks and retrieval precision/latency budgets.

## Workstream D: typed plugin lifecycle and zero-trust extensions (P0/P2)

Research: MCP interoperability and security, capability-based security, supply-chain provenance, sandboxed execution, OpenClaw lifecycle hooks and typed plugin contracts.

Extend canonical semantic tool contracts with versioned plugin manifests: identity, publisher/artifact digest, runtime ABI, entry point, dependency lock, declared tools, hook subscriptions, filesystem/network capabilities, secret scopes, CPU/memory limits and supported API versions. Discovery -> inspect -> verify -> quarantine install -> test -> operator approval -> activate -> monitor -> revoke/rollback. A signature establishes origin, not safety.

Typed hooks: before/after model, before/after tool, tool-result persistence, prompt contribution, memory read/write, session start/end, compaction, installation and shutdown. Define ordering, timeout, failure policy and trace emission. Security hooks fail closed and are operator-owned; advisory hooks cannot override security decisions. Plugins cannot mutate run ledgers, alter policy or read other workspaces except via authorized contracts. Support MCP servers as external capability providers with independent qualification and egress policy.

Gates: malicious plugin tests (symlink traversal, exfiltration, prompt injection, dependency substitution, stale signatures, policy escalation, forged receipts), rollback, version negotiation and concurrent tool invocation.

## Workstream E: graph orchestration and resource-aware scheduling (P1/P2)

Research: hierarchical/centralized/decentralized multi-agent coordination, DAG critical-path scheduling, bounded speculation, contextual-bandit routing, distributed cancellation and task contracts.

Preserve Graph of Thought/DFSDT/RadAgent experiments. Define node contracts with inputs, output schema, dependencies, required capabilities, model policy, budgets and validator. Operator schedules dependency-ready nodes; workers may propose subgraphs but cannot authorize them. Add cycle/deadlock checks, duplicate-work suppression, maximum depth/fanout, lock conflict handling, GPU/VRAM-aware admission, retry budgets and cancellation propagation. Start with deterministic scheduling and measured heuristics; learned routing remains opt-in after baseline evidence.

Gates: concurrent graph integrity, fair resource scheduling, no unbounded recursion, measurable throughput/cost improvement versus a sequential baseline and isolated validation of speculative branches.

## Workstream F: telemetry, inspection and replay (P0/P2)

Research: OpenTelemetry GenAI/agent semantic conventions, causal tracing, event-sourced replay and fault localization.

Trace `run -> node -> model invocation -> tool intent -> sandbox process -> artifact -> verification`. Link process/file/resource ledgers to Syn events. Export OTel spans without losing Syn type/version/evidence. Provide inspection-only replay, deterministic orchestration replay with recorded model/tool responses, and isolated experimental re-execution. The latter must not replay live external side effects without fresh authorization. UI: checkpoint timeline, prompt/context manifest, capability decisions, resource heat, causal failure explanation and run-to-run diff.

Gates: trace completeness, reproducible deterministic replay, no side effects during inspection, redaction tests, bounded storage and retention.

## Workstream G: adaptive model selection and prompt optimization (P2)

Research: DSPy GEPA/MIPROv2, contextual bandits, offline evaluation, calibration, model routing and selective prediction.

Model registry must separate listed from host-qualified models. Router considers task kind, model capability, privacy/disclosure, context length, VRAM, latency, cost, success history and budget. Compare simple rules against learned routing under frozen datasets. Offline prompt/policy optimization: collect verified traces -> split by repository/task/time -> optimize -> evaluate held-out -> security regression -> operator promote/rollback. Never allow learned policy to change security authority.

Retain original P149/P150/P152 as an **optional post-baseline** training path, distinct from prompt optimization: opt-in datasets with provenance, chronology, license and deletion handling; immutable unqualified candidate deployments; held-out/live adapter and privacy/security evaluation; explicit reversible activation. Keep fast-changing repository facts and user preferences in external memory rather than automatically treating them as training targets. This optional work must not block the baseline release or imply automatic weight training. Connect the existing R6.6/R7 role benchmarks to B00–B10 rather than creating another evaluation authority.

Gates: statistically supported task success or efficiency gains, no held-out leakage, bounded failure rate and rollback to prior policy.

## Workstream H: automation, channels and interoperability (P2/P3)

Research: durable timers, standing intents, webhook delivery semantics, MCP for tools and A2A for inter-agent tasks/artifacts.

Store triggers separately from runs: owner, predicate/schedule, timezone, expiry, deduplication key, scope, concurrency and budget. Event-driven waiting is preferred to continuously polling LLM loops. Add optional A2A adapter that exchanges filtered typed task contracts/artifacts, not raw private memory or unrestricted Syn graph state. Channel adapters must use the same operator policy and audit ledger.

Gates: missed/duplicate trigger recovery, permission revocation, replay-safe webhook ingestion, interoperability fixtures and external-agent isolation.

## Workstream I: integrated evaluation and release qualification (cross-cutting)

Benchmark reliability, quality, cost, latency, tokens, GPU/CPU/disk, tool calls, context relevance, memory accuracy, privacy and plugin security. Use SWE-bench-style coding tasks, LongMemEval-style memory tests, controlled graph tasks and Potbot's existing fixtures. Freeze baseline commit, dataset versions, model weights, prompts, tool versions and environment. Report p50/p95, confidence intervals where applicable and regression thresholds. Keep separate local-fixture, real-PostgreSQL, qualified-Linux-sandbox, remote-provider and end-to-end evidence. Do not label unrun gates as passed.

## Dependency-aware milestones

| Phase | Work | Prerequisites | Exit gate |
| --- | --- | --- | --- |
| M0 | Audit baseline and architecture map | Pinned main commit | Capability/test traceability matrix |
| M1 | Durable run and tool-effect hardening | M0 | Crash/duplicate-effect fault injection |
| M2 | PromptCompiler and ContextEngine | M0, M1 contracts | Reproducible prompt and retrieval benchmarks |
| M3 | Typed memory lifecycle | M2, context PostgreSQL | Provenance, temporal and privacy tests |
| M4 | Plugin manifest, hooks, qualification | M1, semantic tool contracts | Adversarial sandbox and permission tests |
| M5 | Graph scheduling and steering | M1, M2, M4 | Concurrent graph integrity and budget tests |
| M6 | Unified traces and replay UI | M1–M5 event contracts | Inspection/deterministic replay and redaction |
| M7 | Adaptive routing and optimization | M2, M3, M5, M6 | Held-out improvements with rollback |
| M8 | Automation and A2A/channels | M1, M4, M6 | Trigger recovery and interoperability tests |
| M9 | Production qualification | M0–M8 | Cross-platform security, performance and migration gates |

Indicative engineering effort: 23–39 workstream-weeks before accounting for parallelization, rework or existing implementation overlap. This is not a delivery commitment. Complete M0 before assigning exact file-level work or migration numbers.

## Patch delivery conventions

- Keep this plan in `docs/implementation/`; update the existing roadmap with links and statuses only after audit.
- Each implementation patch must be `git apply`-compatible, based on a pinned main commit, and include migrations, tests, docs and rollback notes.
- Prefer additive schemas and adapters; preserve OpenAI-compatible API, Syn types, model-provider contracts and sandbox layout.
- Run `python scripts/check_tool_plan.py`, existing `scripts/check.py`, real PostgreSQL tests and qualified sandbox tests appropriate to changed code.
- No screenshots, test claims or implementation-status changes without retained evidence.

## Research references and further reading

- OpenClaw agent loop: https://docs.openclaw.ai/concepts/agent-loop
- OpenClaw system prompts: https://docs.openclaw.ai/concepts/system-prompt
- OpenClaw memory: https://docs.openclaw.ai/concepts/memory
- OpenClaw plugins: https://docs.openclaw.ai/tools/plugin
- Temporal workflow execution: https://docs.temporal.io/workflow-execution
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- DSPy: https://dspy.ai/
- MCP specification: https://modelcontextprotocol.io/specification
- A2A protocol: https://a2a-protocol.org/latest/specification/
- OpenTelemetry GenAI conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- LongMemEval: https://arxiv.org/abs/2410.10813
- SWE-bench: https://www.swebench.com/

These references are starting points for implementation-specific review. Validate versions, APIs, licenses and claims when each milestone begins.
