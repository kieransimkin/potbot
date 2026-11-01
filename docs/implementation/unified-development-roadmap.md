# Potbot unified development roadmap

**Status:** Proposed consolidation, 2026-11-01. A full repository-wide document and issue audit remains necessary. This roadmap preserves existing stages and adds the agent-runtime modernization programme without asserting that planned features have shipped.

**Targeted reconciliation, 2026-11-01:** the plans recovered from the **Monitor Worker Resource Usage** conversation are explicitly mapped in [the worker monitoring and SYN reconciliation](monitor-worker-resource-usage-reconciliation.md), against `main` at `ef9a48a9c46a3ccfabc1d969665073f57224117e`. Its requirement rows and acceptance gates are part of this roadmap. This completes that targeted mapping, not the remaining repository-wide U00 audit or production qualification.

## Baseline and source-of-truth rules

The repository README reports Phases 1–4, the Phase 5 model foundation, hosted jobs/API and PostgreSQL queue at `0.5.0.dev8`. It describes durable patch checkpoints, model invocation records, provider adapters, signed SYN provenance and sandboxed candidate execution. It explicitly defers live multi-model reasoning-tree selection, learned memory admission, live web discovery, IBM/DanceFlow MCP and custom semantic embeddings. Existing `reasoning_runtime.py` contains fenced checkpoint commits, and `reasoning_tree.py` implements bounded graph execution. Those are integration points, not proof of complete production qualification.

Retain the synchronized **48-stage tool roadmap** and `scripts/check_tool_plan.py` as canonical for its existing stages. Do not renumber or reset it. Treat plans as requirements, not evidence. Status vocabulary: qualified; implemented but unqualified; partial; planned; blocked; unknown. PostgreSQL remains authoritative, Markdown context documents are controlled projections, and the operator retains security and side-effect authority.

## Existing plans to reconcile

| Source | Scope |
|---|---|
| `docs/implementation/09-tool-plan-gap-audit.md` | 18 additional requirement groups and independent core/Python/specialist qualification |
| `docs/implementation/10-syn-tool-cogs.md` | Typed SYN tool-Cog contract and debugger |
| `docs/implementation/11-tool-source-inventory.md` | T01.1 source inventory, ownership and hashes |
| `docs/implementation/12-canonical-tool-contracts.md` | T01.2 typed tools, capabilities, installations and evidence |
| `docs/implementation/13-execution-provenance.md` | T01.3 signed causal traces and production adoption |
| `docs/implementation/14-windows-temporary-storage.md` | Scratch ACLs and cleanup |
| `docs/implementation/15-artifact-range-cogs.md` | Range reads, leases, network and retention qualification |
| `docs/implementation/16-artifact-upload-cogs.md` | Resumable uploads, slots and durable qualification |
| `docs/implementation/plugin-discovery-strategy.md` | T04 discovery, admission, installation and qualification |
| `docs/implementation/02-mcp-transports.md`, `04-http-api-adapters.md` | Transport, egress, identity and adapter gaps |
| `docs/implementation/08-debugger-python-first.md` | Debugger operations and language backends |
| `docs/model-md-postgres-stage2-3.md`, `context_contract/` | PostgreSQL AGENTS/MODEL/MEMORY contract and materialization |
| `docs/agent-identity-and-ember-runtime.md` | Signing, Ember inheritance and custody |
| [Worker monitoring and SYN reconciliation](monitor-worker-resource-usage-reconciliation.md) | Requirement-level mapping of the named chat, source chronology, remaining gates and exact identity invariants |
| Monitoring documents `31-cog-process-resource-audit.md`, `33-agent-attribution-and-forensic-file-index.md`, `34-kernel-exit-accounting-and-isolated-cog-workers.md`, `35-worker-syn-nodes-and-qualification.md`, `38-operator-worker-qualification.md` | Mandatory sandbox dispatch, descendant/file/task-exit accounting, agent attribution, heartbeat lifecycle and real-host qualification; exact paths are linked in the reconciliation |
| SYN documents `39-syn-agent-inheritance-and-independence.md` through `48-syn-p2p-lan-discovery-and-encrypted-pledges.md` | Historical v1 and current hardware-based v2 contract, standalone Python service, HD identity/recovery, sealed adoption, confidential LAN intake and unresolved enrollment bridge; each source is linked separately in the reconciliation |
| `46-registered-worker-qualification.md`, `47-shared-sandbox-launch-and-audit.md`, `48-browser-controller-provenance.md` | Related worker/browser acceptance and provenance; these are distinct from similarly numbered SYN phases |
| `docs/implementation/agent-runtime-modernization.md` | M0–M9 modernization plan; present in the pinned main tree, with implementation/qualification assessed separately |
| Remaining sandbox, Syn, frontend, browser, model and provider documents | Must be exhaustively enumerated at milestone U00; this inventory is not exhaustive |

## Unified work packages

| ID | Workstream | Remaining / qualification scope | Modernization | Dependencies |
|---|---|---|---|---|
| U00 | Repository audit and roadmap reconciliation | Inventory all plans/issues/tests/CI; map legacy stage IDs and avoid duplicated work | M0 | Pinned main |
| U01 | Durable execution and provenance | Complete checkpoint recovery, fenced leases, tool intents, idempotency/reconciliation, T01.3 signed trace adoption | M1, M6 | U00 |
| U02 | Sandbox and shared filesystem | WSL+bubblewrap, versioned base images, `/usr/local/tools`, `/usr/local/skills`, `/var/ai`, `/var/logs/ai`, `/etc/ai`, `/etc/ai-past`, policy and cleanup | M1, M4 | U01 |
| U03 | Worker/Cog resource supervision | Every executable work Cog in its qualified workspace sandbox; complete descendant/access-mode/task-exit ledger; lifetime CPU/memory/I/O; system bottlenecks; operator-bound attribution; 60 BPM sprite with 180-second brightness and 2-second spike window; every-agent heat coverage with explicit metric semantics; independent sandbox SYN lifecycle | M5, M6 | U01–U02 |
| U04 | Semantic tool platform | 48-stage plan, T01–T04, typed Cogs, debugger, concurrency-aware tools, MCP/HTTP, live discovery and qualification | M4 | U01–U03 |
| U05 | Artifact transfer and GitHub concurrency | Range/upload Cogs, retention, restart/network recovery, artifact receipts, shared Git locks | M1, M4 | U01–U04 |
| U06 | Syn identity and security | Standalone Python SYN; trusted local discovery and separate agent installations; CSPRNG/BIP-39/CIP-3 identity; exact root/base/hardware-inheritance/eight-choice contracts; confidential pledge-to-selection-to-adoption flow; scoped activation/revocation; hourly signatures, Ember policy, OS custody, memory protection and TPM/HSM gates; reconcile legacy journal storage | M4, M6 | U01–U03 |
| U07 | Models, providers and credentials | Local low-VRAM Qwen/Gemma/Phi/LFM qualification, hosted Claude/OpenAI/Grok/Gemini/EvoLink, API setup page, usage accounting and failover | M2, M7 | U01, U06 |
| U08 | Reasoning graph and steering | Graph of Thought/DFSDT/RadAgent, recursive decomposition, validator nodes, urgent/deferred/background steering, scheduling, budgets | M1, M5, M7 | U01, U03, U07 |
| U09 | Prompt/context and MODEL.md | PostgreSQL AGENTS/MODEL/MEMORY projections, deterministic PromptCompiler, ContextEngine, token profiles and compaction | M2 | U01, U06, U07 |
| U10 | Memory and embeddings | pgvector, named semantic embeddings, custom embedding research, verified episodes, consolidation, temporal facts, trajectory learning | M3, M7 | U09 |
| U11 | Plugin and skills ecosystem | Capability manifests, lifecycle hooks, qualification, IBM/DanceFlow/WordPress MCP, shared skills | M4 | U04, U06, U09 |
| U12 | Browser automation | xpra, Playwright, CDP/devtools, accessibility, screenshots, DOM and sandboxed browser sessions | M4, M6 | U02–U04, U06 |
| U13 | Next.js operator frontend | Workspace orbs, dockable chat/terminal/browser, 3D reasoning graph, credentials, downloads, worker telemetry; central 60 BPM/three-minute glow and fast spike flash; access to every agent's attributed resource contribution | M6 | U01–U12 APIs |
| U14 | Evaluation and adaptation | Replay, run comparison, benchmark queue, prompt optimization, model routing, held-out quality/cost/security gates | M6, M7, M9 | U07–U10 |
| U15 | Automation and interoperability | Durable triggers, schedules, webhooks, A2A and channels | M8 | U01, U04, U06 |
| U16 | CI, release and deployment qualification | uv Bash/PowerShell/batch, CI image publication, real PostgreSQL, Linux/WSL sandbox, Windows ACLs, rollback, docs | M9 | Continuous |

## Delivery waves and gates

### Wave 0 — Inventory, reconcile and pin (U00)

Pin current main SHA. Enumerate every `docs/` plan, issue, PR, stage-status document and CI acceptance job. Build a requirement-level matrix with source path, legacy stage ID, U-ID, implementation location, test evidence, status, blockers and next action. Resolve contradictory or stale plans (especially superseded SQLite context designs). Preserve existing IDs and test checker semantics. **Exit:** no unmapped future-work requirement in inspected sources; remaining inaccessible sources explicitly recorded.

### Wave 1 — Secure durable foundation (U01–U03, U06)

Extend existing PostgreSQL/Syn execution rather than introducing a second job engine. Add fenced leases, transactional outbox, tool-effect intents, uncertain-outcome reconciliation, approval interrupts, cancellation and restart recovery. Complete sandbox mount/ownership/log/config-diff requirements, worker process/file/resource accounting, and Syn/Ember authority. **Exit:** fault-injection, concurrency, real PostgreSQL and qualified sandbox gates; no blind duplicate irreversible effect.

For the named monitoring chat, Wave 1 specifically includes the reconciliation's G1–G5 and G7 gates: complete audited worker dispatch, independently running sandbox SYN instances, exact identity/recovery contracts, the approved enrollment/adoption bridge, real custody and durable-state reconciliation. A pending LAN pledge, selected candidate, signed receipt or local heartbeat alone cannot qualify or activate an agent. Stage U03 accounting first, U06 service/identity integration next, then their joint sandbox lifecycle gate; the work-package dependencies do not require a circular implementation order.

### Wave 2 — Tools, plugins, transport and browser (U04–U05, U11–U12)

Complete existing T01–T04 milestones, typed Cog contracts, debugger, discovery, concurrency, artifact-range/upload transport and MCP/HTTP egress. Add versioned plugin manifests, capability approval, typed hooks and quarantine/rollback. Qualify IBM/DanceFlow/WordPress integrations against pinned APIs. Complete browser control with operator-mediated permissions. **Exit:** adversarial plugin, transport interruption, browser isolation and tool-specialist gates.

### Wave 3 — Intelligence and memory (U07–U10)

Qualify actual local/hosted models and inference costs. Integrate live multi-model reasoning graphs, scheduling, validation and steering. Build deterministic prompt/context compilation and finish PostgreSQL MODEL/MEMORY projections. Add scoped hybrid retrieval, temporal/consolidated memory, verified trajectories and named embeddings. Keep custom embedding spaces experimental until benchmarked. **Exit:** held-out graph, context, memory, model and privacy benchmarks.

### Wave 4 — Frontend and empirical optimization (U13–U14)

Finish real backend streaming, workspace controls, monitoring sprite, graph checkpoint timeline, prompt/context inspection, run replay and comparisons. Optimize prompts and model routing offline against frozen datasets, with promotion and rollback. **Exit:** UI integration/accessibility and measured improvement without security regression.

Retain the monitoring chat's exact display requirements and G6 acceptance gate: a 60 BPM pulse, brightness from the time-weighted 180-second system average, a separate flash from the short 2-second window, and access to all agents' contributions. The pinned implementation displays only the top eight agents' **host busy CPU** shares; that is partial coverage of the requested every-agent total-system-load view. Keep CPU labels accurate while qualifying additional resource measures and their denominators.

### Wave 5 — Automation and release (U15–U16)

Add durable event subscriptions, schedules, A2A and channels under the same operator policy. Qualify all supported platforms, real database, resource limits, identity, secrets, provider compatibility, migrations and recoverability. **Exit:** retained CI evidence and versioned release qualification.

## Critical engineering invariants

1. Do not create parallel authoritative PostgreSQL stores, schedulers, plugin registries or Syn event logs.
2. Preserve the OpenAI-compatible API, existing `syn://` and `synny://` contracts, Ember authority, credentials and artifact formats through additive migrations.
3. Models, plugins, tool descriptions and retrieved memory cannot grant permissions. Filter memory scope before retrieval and model context assembly.
4. Every effect needs a durable intent, authorization decision, receipt and reconciliation policy. Exactly-once external effects require external cooperation.
5. Distinguish documented, source-implemented, fixture-tested, real-PostgreSQL-tested, qualified-sandbox-tested and deployed states.
6. Every patch must include tests, migration/rollback notes and compatibility assessment. Run `python scripts/check_tool_plan.py`, `scripts/check.py` and relevant real-system suites.
7. Learning and policy optimization require frozen held-out evaluation, independent security regression and explicit promotion.

## First next milestone

**U00-A: full roadmap inventory and evidence reconciliation.** The present consolidation covers known plans and targeted GitHub evidence, but not an exhaustive traversal of the repository's documentation tree or issues. Produce a source-linked legacy-stage ↔ U-ID mapping before assigning new implementation stage numbers. After that, **U01-A: durable side-effect and crash recovery gap closure** is the first high-priority code milestone.

The **Monitor Worker Resource Usage** slice of U00-A is now recorded in the companion reconciliation. Carry its open items into U03/U06/U13/U16 rather than restarting the implemented stages. In the SYN sequence, Phase 48 provides confidential intake and explicitly defers its approved enrollment bridge. No SYN Phase 49 artifact was verified in the pinned tree or the recovered artifacts: retain the user's Phase 49 continuation request as **requested / unverified**, and use U06-E below to track the bridge without inventing a completed Phase 49.

## Required follow-through from the monitoring chat

| Work item | Existing U owner | Deliverable and gate |
|---|---|---|
| U03-Q | U03, U02, U16 | Qualify every executable worker route, complete process/file/task-exit evidence, loss detection, attribution and confirmed cancellation on the target Linux/WSL2 host; G1. |
| U03-S | U03, U06, U16 | Replace the local-only heartbeat completion assumption with independently installed, identity-bound Python SYN processes inside qualified agent/worker sandboxes; G2. |
| U06-I | U06 | Preserve and qualify exact entropy, recovery, root/base, hardware inheritance, eight choices, hourly signing and Ember contracts; G3. |
| U06-E | U06, U01, U02 | Connect private Phase 48 intake to approved/fenced candidate selection and Phase 47 sealed adoption; retain original signatures, reconcile uncertain effects and authorize scoped activation separately; G4. |
| U06-K | U06, U16 | Qualify native OS key custody, recovery/revocation/rotation and memory-exposure boundaries; retain TPM/HSM as a separate future profile until qualified; G5. |
| U03-V | U03, U13 | Verify the central 60 BPM/180-second glow and 2-second spike behavior; close top-eight and CPU-only attribution gaps with complete per-agent access and explicit resource semantics; G6. |
| U06-D | U00, U01, U06 | Reconcile existing private SYN SQLite journals with the PostgreSQL-authoritative policy, preserving signatures, uniqueness, replay reservations and custody boundaries; G7. |

These suffixes identify work within existing U packages. They do not renumber the canonical 48 tool stages, the SYN phase sequence, the browser S sequence, or the external-skill S00–S10 programme. See [the source and acceptance matrix](monitor-worker-resource-usage-reconciliation.md) before assigning completion status.

## References

- `docs/implementation/agent-runtime-modernization.md`
- `docs/implementation/09-tool-plan-gap-audit.md`
- `docs/implementation/plugin-discovery-strategy.md`
- https://docs.openclaw.ai/concepts/agent-loop
- https://docs.temporal.io/workflow-execution
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://dspy.ai/
- https://modelcontextprotocol.io/specification
- https://a2a-protocol.org/latest/specification/

## Cross-agent ecosystem compatibility (C00–C09)

Add the companion [cross-agent compatibility plan](cross-agent-compatibility.md) as a required cross-cutting workstream. It covers OpenClaw, OpenAI Codex and Anthropic Claude Code skills, instructions, plugin bundles, hooks, workflows, tool aliases, bidirectional MCP and browser-controller compatibility. C00–C01 start in Wave 0/1; C02–C04 are Wave 2 deliverables; C05–C06 join lifecycle/plugin work; C07–C09 are Waves 4–5 qualification. Keep one canonical typed SYN Cog/tool gateway and operator authority boundary. Compatibility claims require version-pinned behavioral tests; provider-model compatibility alone is insufficient.


## External skill ecosystem discovery and native execution (S00–S10)

The cross-agent C00–C09 programme is extended by [external skill ecosystem integration](potbot-skill-ecosystem-integration.md). Its new S00–S10 milestones cover official ClawHub, skills.sh/Git, Codex and Claude sources, plus independent directory discovery; immutable origin/version verification; native SKILL.md parsing and eligibility; sandbox dependency installation; secret brokering; Cog tool parity; slash commands, hooks and workflows; install/update/rollback; UI/CLI discovery; qualification on real pinned packages; and portable authoring/export.

Do **not** treat independent directory listings as installable/verified packages. Keep one canonical SYN Cog runtime and operator authority, with vendor aliases only at compatibility boundaries. Cross-map S milestones to T01–T48 and C00–C09 during U00 so no separate scheduler, authorization service or tool registry is created.

**New critical gates:** actual package fixtures from multiple sources; parser and readiness reports; browser/coding/connector/cloud workflow coverage; security denial tests; and explicit unsupported-feature reporting. No assertion of full ecosystem compatibility before these tests pass.


## Agent benchmarking and testing (B00–B10)

Add [Potbot agent benchmarking and testing](potbot-agent-benchmarking-plan.md) as the qualification specification for U14 and a cross-cutting requirement of all U/C/S/T work. Benchmark coding (SWE-bench Pro V2, Terminal-Bench), tools (BFCL, MCPMark), browser/desktop (BrowserGym, WebArena, OSWorld), research (GAIA, BrowseComp), memory (LongMemEval, LoCoMo), policy (tau2/tau3), multi-agent reasoning, security (AgentDojo), endurance and real pinned OpenClaw/Codex/Claude skill packages. Do not replace the existing SYN event ledger, Cog authority, PostgreSQL job queue or 48-stage test checker.

B00–B02 begin in Wave 0/1; B03–B05 qualify tools and browser in Wave 2; B06–B08 span Waves 3–5; B09 is continuous CI; B10 is a standing release gate. Record task, environment, model, prompt, tool/skill digests, seed, cost, oracle and source hashes. Report verified success, security violations, pass^k, cost, latency, resource use, and confidence intervals separately. Require independent state verification and clean-image grading. A vendor leaderboard, mock-only pass or unverified skill installation does not establish Potbot capability.
