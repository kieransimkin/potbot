# Potbot unified development roadmap

**U00-A completed, 2 November 2026:** the [closeout](u00-a-closeout.md) reconciles
the pinned main `95610037311c80cdf7163372998b08e1b8958926`, retained documents,
731 explicit source rows, narrative sections, code/test evidence, all 24 PRs and
32 CI workflows. The separately authored review and drift checks close the audit;
runtime qualification remains false. U01-A was the first implementation milestone from this audit.

**4 November 2026:** U03-Q starts with [worker audit completion evidence](38-operator-worker-qualification.md#u03-q-completion-evidence).
This gate requires terminal task traces and confirmed cgroup release before an audit can report success.
The remaining U02 and U03 deployment and route qualification gates still apply.

Plan **1.1** preserves P001–P153 and adds **P154–P204**; Architecture **1.1**
defines **G01–G35**. Older P153/G24 crosswalks below describe their historical
source scope. Preserve the additional requirements and independent ID namespaces.

**Targeted reconciliation, 2026-11-01:** the plans recovered from the **Monitor Worker Resource Usage** conversation are explicitly mapped in [the worker monitoring and SYN reconciliation](monitor-worker-resource-usage-reconciliation.md), against `main` at `ef9a48a9c46a3ccfabc1d969665073f57224117e`. Its requirement rows and acceptance gates are part of this roadmap. This completes that targeted mapping, not the remaining repository-wide U00 audit or production qualification.

**Broader conversation reconciliation, 2026-11-01:** [recent Potbot development requirements](recent-potbot-development-reconciliation.md) maps other recovered October–November conversations against `main` at `6bc2db628bc66995e1d5979881455378747b1bdf`, which already contains the monitoring reconciliation. Its RD01–RD39 requirements and gates are part of this roadmap. The review adds the original P001–P153/E01–E09 programmes, the 32-stage object-memory plan and detailed service, sandbox, tool, browser, UI, context and learning requirements. It distinguishes missing mappings, conflicting guarantees, partial integration and unverified prerequisites. It does not close the remaining binary-source, issue/CI or production audit. Delivery was checked against `6f6b16dce984398d939ce5d0360d7e708938a9dd`, including the new [browser verification closeout](browser-verification-closeout.md); that test-infrastructure increment preserves the real host, database, image and provenance-fixture gates.

## Baseline and source-of-truth rules

Older README phase summaries (`0.5.0.dev8`/`dev9`) are historical; the reviewed main reports `0.5.0.dev11`. They are not the final inventory of current capabilities. Retain its durable patch checkpoints, model records, PostgreSQL queue, signed SYN provenance and sandboxed execution. Later documents 20/21 describe migration 24 PostgreSQL-fenced bounded R6 reasoning and migration 25 optional task-bound budget accounting; context documents 42–50 describe application migrations 29–33, authored skills, registered-object lifecycle and normal worker context integration. Existing `reasoning_runtime.py` and `reasoning_tree.py` remain integration points. Preserve these documented foundations while qualifying live model/tool integration, host deployment, complete producer adoption and remaining memory/discovery work. A document or source implementation does not establish production qualification.

Retain the synchronized **48-stage tool roadmap** and `scripts/check_tool_plan.py` as canonical for its existing stages. Do not renumber or reset it. Treat plans as requirements, not evidence. Status vocabulary: qualified; implemented but unqualified; partial; planned; blocked; unknown. PostgreSQL remains authoritative, Markdown context documents are controlled projections, and the operator retains security and side-effect authority.

Also retain original **P001–P153**, embedding **E01–E09**, and object-memory **MG01.1–MG08.4** identities. Original architecture G01–G24, monitoring G1–G7, unified U IDs and the various S namespaces are distinct. The object-memory registry remains `not_started`/`not_run`; context-triple integration does not complete graph memory. The actual tool-stage range is T01.1–T12.4.

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
| [Recent development reconciliation](recent-potbot-development-reconciliation.md) | RD01–RD39 source-linked requirements, conflicts, remaining gates and legacy-stage crosswalks from the other recovered conversations |
| Original implementation-plan DOCX and local embedding addendum | Recovered saved P001–P153 and E01–E09 inventories, with historical artifact/version limitations recorded in the reconciliation; preserve feedback, deletion/restore and optional training scope |
| `docs/object-memory/implementation-plan.md`, `docs/object-memory/plan.json` | 32 MG stages, R01–R32/A01–A32; per-object memory, Types/Embers, source relationships, lifecycle, retrieval, monitored Cogs, deletion and federation |
| `docs/phase-6-controller-benchmark.md`, `docs/phase-7.md` through increment 5, `docs/phase7-closure-status.md`, `docs/reasoning-policy.md` | Shared search/ensemble, active research, immutable sources, information needs, reuse/adapt/build and universal event APIs; unavailable P6-001–P6-045 artifacts are not invented |
| Reasoning/API documents 17–24; `docs/hosted-providers.md`, `docs/startup.md`, `docs/openai-compatible-service.md` | Local/hosted models, prompt profiles, R2.5–R7, inbound/outbound compatibility, service/vault/logging, task budgets and role benchmarking |
| Shared-environment documents 26–41 and S15–S17; `33-remote-api-operator-setup.md` | Workspace identity, tools/models, concurrency, configuration history, upgrade/release gates, operator/GUI lifecycle, API setup and unresolved guest logging; exact filenames are linked in the reconciliation |
| Context documents `42-model-md-workspace-context.md` through `50-model-md-runtime-integration.md` | Application context storage/RLS, projections, inspection, runtime refresh, context RPC, authored skills and registered-object/normal-worker integration |
| `docs/ui/implementation-plan.md`, frontend/backend contract, UI9/UI10/UI11 and browser S37–S42 documents | Exact workspace/panel design, durable chat versus inference lifetime, PTY/history/graph gaps, transfer readiness and browser capabilities |
| `docs/syn-semantic-integration.md`, `docs/syn-primitive-semantics-and-ember-contracts.md`, `vocab/retrieval-profiles.json`, `docs/embeddings.md`, `docs/named-embeddings.md` | Complete type inventory, seven retrieval purposes, applicability, faithful typed encodings and standard-before-custom embedding qualification |
| Saved `tool-platform-audit.md` | Aggregate operation catalogue, readiness/acquisition/update states and shared invocation/accounting projection; historical proposed filenames remain unlocated in pinned main |
| Remaining sandbox, Syn, frontend, browser, model and provider documents | Enumerated and mapped in the U00-A source inventory/review at its pinned checkpoint; renew affected dispositions when they change |

## Unified work packages

| ID | Workstream | Remaining / qualification scope | Modernization | Dependencies |
|---|---|---|---|---|
| U00 | Repository audit and roadmap reconciliation | Inventory plans/issues/tests/CI; preserve P/E/MG/T/R/UI/M/C/S identities, map recovered requests, resolve conflicting guarantees and record inaccessible evidence | M0 | Pinned main |
| U01 | Durable execution and provenance | Checkpoint recovery, fenced leases, tool intents, idempotency/reconciliation, existing R6/R6.1 integration, all-producer causal trace adoption and replayable REST/WebSocket events | M1, M6 | U00 |
| U02 | Sandbox and shared filesystem | WSL2+bubblewrap, Docker-built pinned images, enforced agent ownership, shared mount/persistence matrix, read-only shared tools with operator-unlocked per-tool writes, `/usr/local/skills`, model/cache paths, guest `/var/logs/ai`, OS-native `/etc/ai` watcher with overflow/loss fencing and reconciliation, operator lifetime and cleanup | M1, M4 | U01 |
| U03 | Worker/Cog resource supervision | Every executable work Cog in its qualified workspace sandbox; complete descendant/access-mode/task-exit ledger; lifetime CPU/memory/I/O; system bottlenecks; operator-bound attribution; 60 BPM sprite with 180-second brightness and 2-second spike window; every-agent heat coverage with explicit metric semantics; independent sandbox SYN lifecycle | M5, M6 | U01–U02 |
| U04 | Semantic tool platform | Canonical 48 stages, typed Cogs, debugger, actual concurrency footprints, exact type inventory, semantic retrieval contracts, aggregate operation catalogue, staged acquisition/S12 release gate, MCP/HTTP and live qualification | M4 | U01–U03 |
| U05 | Artifact transfer and GitHub concurrency | Range/upload Cogs, retention, restart/network recovery, artifact receipts, shared Git locks and remote HEAD CAS/readback, coordinated verified model/tool downloads | M1, M4 | U01–U04 |
| U06 | Syn identity and security | Standalone Python SYN; trusted local discovery and separate agent installations; CSPRNG/BIP-39/CIP-3 identity; exact root/base/hardware-inheritance/eight-choice contracts; confidential pledge-to-selection-to-adoption flow; scoped activation/revocation; hourly signatures, Ember policy, OS custody, memory protection and TPM/HSM gates; reconcile legacy journal storage | M4, M6 | U01–U03 |
| U07 | Models, providers and credentials | Measured low-VRAM Qwen/Gemma/Phi/LFM qualification; Claude/OpenAI/Grok/Gemini/EvoLink discovery versus runnable capability; API setup; inbound compatibility matrix distinct from outbound adapters; task/interactive accounting boundaries and failover | M2, M7 | U01, U06 |
| U08 | Reasoning graph and steering | Existing bounded R6 plus independent candidate verification, research/reuse/adapt/build, atomic leaf contracts, urgent/deferred/background integration, parallel scheduling and budgets | M1, M5, M7 | U01, U03, U07 |
| U09 | Prompt/context and MODEL.md | Application PostgreSQL context/projections and Stage 10D integration; data-only MODEL/MEMORY, deterministic PromptCompiler, scoped ContextEngine, evidence reconciliation/conflicts, protocol coverage and per-launch snapshots | M2 | U01, U06, U07 |
| U10 | Memory and embeddings | MG01–MG08 per-object graphs; P085–P117 admission/applicability/feedback; exact/lexical/named-vector retrieval and seven semantic profiles; temporal correction, deletion-safe restore, standard embeddings before measured custom spaces | M3, M7 | U09 |
| U11 | Plugin and skills ecosystem | Capability manifests/hooks, IBM/DanceFlow/WordPress MCP and live KeywordMoves qualification, authored skill drafts/releases/resources, C/S ecosystem import/export and shared operation lifecycle | M4 | U04, U06, U09 |
| U12 | Browser automation | Same visible Xpra Chromium, Playwright/Patchright, CDP/DevTools/accessibility/rendered evidence; explicit S39 read-only limits, admitted network expansion, tab/window identity and source-map provenance | M4, M6 | U02–U04, U06 |
| U13 | Next.js operator frontend | Exact orb/panel behavior; durable job-backed chat, workspace discovery/history, resumable PTY, graph push/replay, credentials and model readiness; central 60 BPM/three-minute glow, fast spike flash and every-agent attributed resource access | M6 | U01–U12 APIs |
| U14 | Evaluation and adaptation | Existing R6.6/R7 plus B00–B10; exact returned-patch scoring, chronological independent evaluation, correctable feedback, role routing/prompt optimization and reversible promotion; optional post-baseline trained deployments | M6, M7, M9 | U07–U10 |
| U15 | Automation and interoperability | Durable triggers, schedules, webhooks, A2A and channels | M8 | U01, U04, U06 |
| U16 | CI, release and deployment qualification | UV launchers/native service/vault, CI images, real PostgreSQL/host/provider gates, Windows ACLs, capacity/outage profiles, backup with deletion journal, pending-work restore, upgrades/rollback and versioned release manifest | M9 | Continuous |

## Delivery waves and gates

### Wave 0 — Inventory, reconcile and pin (U00)

Pin current main SHA. Enumerate every `docs/` plan, issue, PR, stage-status document and CI acceptance job. Build a requirement-level matrix with source path, legacy stage ID, U-ID, implementation location, test evidence, status, blockers and next action. Resolve contradictory or stale plans (especially superseded SQLite context designs). Preserve existing IDs and test checker semantics. **Exit:** no unmapped future-work requirement in inspected sources; remaining inaccessible sources explicitly recorded.

Use both conversation reconciliations as completed mapping inputs. The recent-development crosswalk covers all 153 original P steps by phase range, E01–E09 and all 32 MG stages; it does not supply per-stage runtime evidence or recover inaccessible binary variants/P6 artifacts. Preserve RD13 shared-write policy, RD15 every-write history, RD14 S12 and RD16 S36/guest-log gaps as named open requirements.

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

Across these waves, follow the existing MG dependency cut rather than waiting for every broad U package: establish object identity/contracts, then admitted memory and exact recall; require monitored Cog dispatch and deletion/restore before persistent user-memory use. Retain correctable feedback and failed/unknown/flaky experience. Original optional P149/P150/P152 training follows baseline qualification and cannot become a baseline release blocker.

## Critical engineering invariants

1. Do not create parallel authoritative PostgreSQL stores, schedulers, plugin registries or Syn event logs.
2. Preserve the OpenAI-compatible API, existing `syn://` and `synny://` contracts, Ember authority, credentials and artifact formats through additive migrations.
3. Models, plugins, tool descriptions and retrieved memory cannot grant permissions. Filter memory scope before retrieval and model context assembly.
4. Every effect needs a durable intent, authorization decision, receipt and reconciliation policy. Exactly-once external effects require external cooperation.
5. Distinguish documented, source-implemented, fixture-tested, real-PostgreSQL-tested, qualified-sandbox-tested and deployed states.
6. Every patch must include tests, migration/rollback notes and compatibility assessment. Run `python scripts/check_tool_plan.py`, `scripts/check.py` and relevant real-system suites.
7. Learning and policy optimization require frozen held-out evaluation, independent security regression and explicit promotion.

## First next milestone

**U01-A: durable side-effect and crash recovery gap closure.** U00-A's
[reviewed closeout](u00-a-closeout.md) is complete. Preserve the existing
ExecutionStore, ToolGateway, Outbox, PostgresReasoningStore, steering inbox,
BudgetLedger and pinned execution journal.

The [sandbox transport recovery implementation](u01-a-sandbox-transport-recovery.md)
connects a durable local queue through the existing worker. The target records
stable effect IDs and can resolve lost acknowledgements with a receipt or a
cancellation. The existing PostgreSQL budget ledger settles each resolved
dispatch in the same transaction as its outcome.

The controlled queue provides the U01-A reference transport. Additional
producers and open PR13's factories need equivalent target evidence and
qualification against released-Syn grants. Live native worker qualification
remains under the existing host gates.

The monitoring and recent-development reconciliations remain historical mapping
inputs. Their U03/U06/U13/U16 gates survive audit closure. The old requested
Phase 49 artifact remains unverified; later official SYN-R04/R05 supply actual
native adoption/heartbeat integration under their own release contracts. Positive
audited Cog dispatch, upgrade/recovery and general rollout remain independent.

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

## Required follow-through from the other recent development conversations

| Work item | Existing U owner | Deliverable and gate in the recent-development reconciliation |
| --- | --- | --- |
| U02-P | U01/U02/U06 | Mount/identity matrix; read-only ordinary shared-tool access with operator-unlocked scoped maintenance writes; enforced concurrency; and OS-native `/etc/ai` watcher loss/overflow fencing plus reconciliation; RD11–RD13/RD15. |
| U02-L | U02/U03/U16 | Agent `/var/logs/ai` mount, creator-only writes, actual tool aliases/destinations and operator watch/archive/recovery; RD16, distinct from host Xpra S17. |
| U04-R | U02/U04/U16 | Locate or implement and qualify S12 post-activation repin/release enforcement; S11 approval or S13 health is insufficient; RD14. |
| U04-C | U04/U05/U11/U13 | Aggregate operation/readiness/accounting catalogue, acquisition lifecycle, concurrency and actual connector/KeywordMoves coverage; RD12/RD18/RD19. |
| U07-Q | U02/U05/U07/U13/U16 | Native service/vault, provider and inbound API matrix, model downloads/cache paths and installed/runnable/qualified readiness; RD01–RD04. |
| U08-T | U01/U04/U08/U14 | Existing fenced reasoning/accounting plus ensemble verification, temporal steering, atomic leaf scheduling and active research; RD05–RD09. |
| U09-C | U06/U09/U11/U13 | Preserve data-only state/history and Stage 10D integration; close constructor/protocol, semantic-reconciliation and authored-skill gaps; RD26–RD29. |
| U10-G | U01/U04/U06/U09/U10/U14/U16 | Preserve MG01.1–MG08.4, exact inventory, seven retrieval profiles, embedding addendum, applicability and deletion-safe recovery; RD30–RD35. |
| U12-C | U04/U12/U13/U16 | Same-session browser control, explicitly admitted network capabilities, tab/window selection and source-map provenance; RD20–RD22. |
| U13-D | U01/U07/U13/U16 | Exact UI contract, job-backed inference, principal-aware discovery/history, resumable PTY, graph replay and integrated host readiness; RD23–RD25. |
| U14-L | U08/U09/U10/U14 | Correctable feedback, two-task learning, failed/unknown/flaky lessons and R/B/P benchmark linkage; optional training remains post-baseline; RD34/RD36/RD37. |
| U16-R | U01/U04/U06/U11/U12/U16 | All-producer events, durable operator state, ecosystem behavior and original operational/backup/restore/release gates; RD10/RD17/RD38/RD39. |

See [RD01–RD39 and the legacy-stage crosswalks](recent-potbot-development-reconciliation.md) for exact sources, current boundaries and evidence limitations. These work items preserve existing programmes and do not assert implementation completion.

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

Do **not** treat independent directory listings as installable/verified packages. Keep one canonical SYN Cog runtime and operator authority, with vendor aliases only at compatibility boundaries. Cross-map S milestones to the 48 canonical tool stages (T01.1–T12.4) and C00–C09 during U00 so no separate scheduler, authorization service or tool registry is created.

**New critical gates:** actual package fixtures from multiple sources; parser and readiness reports; browser/coding/connector/cloud workflow coverage; security denial tests; and explicit unsupported-feature reporting. No assertion of full ecosystem compatibility before these tests pass.


## Agent benchmarking and testing (B00–B10)

Add [Potbot agent benchmarking and testing](potbot-agent-benchmarking-plan.md) as the qualification specification for U14 and a cross-cutting requirement of all U/C/S/T work. Benchmark coding (SWE-bench Pro V2, Terminal-Bench), tools (BFCL, MCPMark), browser/desktop (BrowserGym, WebArena, OSWorld), research (GAIA, BrowseComp), memory (LongMemEval, LoCoMo), policy (tau2/tau3), multi-agent reasoning, security (AgentDojo), endurance and real pinned OpenClaw/Codex/Claude skill packages. Do not replace the existing SYN event ledger, Cog authority, PostgreSQL job queue or 48-stage test checker.

B00–B02 begin in Wave 0/1; B03–B05 qualify tools and browser in Wave 2; B06–B08 span Waves 3–5; B09 is continuous CI; B10 is a standing release gate. Record task, environment, model, prompt, tool/skill digests, seed, cost, oracle and source hashes. Report verified success, security violations, pass^k, cost, latency, resource use, and confidence intervals separately. Require independent state verification and clean-image grading. A vendor leaderboard, mock-only pass or unverified skill installation does not establish Potbot capability.
