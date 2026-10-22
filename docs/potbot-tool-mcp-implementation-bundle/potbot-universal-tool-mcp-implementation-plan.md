# Potbot Universal Tool, MCP and Reasoning Integration — Master Implementation Plan

**Document version:** 1.0 (planning baseline)  
**Prepared:** 22 October 2026  
**Target:** `kieransimkin/potbot` / Syn-Synny semantic ecosystem  
**Observed GitHub baseline:** `main` at `74dac45d5535595975e10f545fd6d9cb1652e6a8` (read-only inspection, 22 October 2026)  
**Scope:** 12 phases × 4 incremental stages = **48 independently testable deliverables**.  
**Status:** Design document only; the 48 stages are **not represented as implemented** merely by this plan.

> Integration rule: build on the existing Potbot jobs/API/WebSocket, workspace sandbox, hosted model gateway, Ed25519 identity and locking Cogs, semantic Type catalogs, inherited Embers, typed Cog transformations, artifact and receipt mechanisms, and Phase 6 graph search. Do not recreate these as parallel systems. An existing feature should be marked **reuse**, **extend**, **qualify**, or **missing** before any implementation ticket is started.

## 0. Existing system and constraints

Repository references inspected: [README](https://github.com/kieransimkin/potbot/blob/main/README.md), [Cog transformations](https://github.com/kieransimkin/potbot/blob/main/docs/cog-transformations.md), [hosted jobs](https://github.com/kieransimkin/potbot/blob/main/docs/hosted-jobs.md), [agent identity](https://github.com/kieransimkin/potbot/blob/main/docs/agent-identity-and-ember-runtime.md), [workspace sandboxing](https://github.com/kieransimkin/potbot/blob/main/docs/workspace-sandboxing.md), and [Phase 6 benchmark notes](https://github.com/kieransimkin/potbot/blob/main/docs/phase-6-controller-benchmark.md). Existing claims of implementation are subject to checkout, migration and integration verification. The Phase 6 document records synthetic smoke benchmarks and explicitly distinguishes them from unqualified live-model performance.

| Existing component | Reuse/extension rule |
|---|---|
| Python 3.12 / uv / start scripts / CI | Extend pinned extras and existing scripts/check.py; preserve lightweight core. |
| PostgreSQL durable tasks/jobs and owner separation | Add migrations without rewriting historical ledgers; use existing RLS, leases, budgets and event cursor. |
| REST / WebSocket / dashboard | Add tool capabilities and projections to existing service boundaries. |
| Agent identity, Ed25519 verification, Credential and locking Cog | Publish verification keys as designed; private keys never exposed; respect installed custody tiers and runtime qualification. |
| Syn/Synny catalog / inherited Ember contracts | Use full syn:// or synny:// Type URLs; no shorthand or duplicate ontology. |
| Cog transformations | Each Cog maps exactly one registered input Type to one output Type, with pinned implementation binding; transformations confer no execution authority. |
| Workspace registered sandbox roots | All downloaded dependencies, binaries, model weights, caches, copies, tests and outputs within workspace root; qualified Linux isolation or fail closed. |
| Phase 6 controller/benchmarks | Extend search and Cog receipts; do not infer live qualification from synthetic fixtures. |

## 1. Non-negotiable architectural invariants

1. **Canonical semantic identity:** every Type uses an explicit `syn://type/...` or `synny://type/...` URI; instances are distinct from Types. All installed cogs retain required/default/optional Ember semantics.
2. **Separate the stages of authority:** discovery ≠ installation ≠ registration ≠ authorization ≠ execution ≠ verification. Metadata or model-generated text cannot silently grant access.
3. **Cog purity of authorization:** a Cog may transform typed data; even trusted transformation results never automatically authorize tool execution.
4. **Explicit workspaces:** one immutable chosen sandbox root per workspace; host paths outside it are not readable/writable by untrusted workloads. No disguised global downloads.
5. **Custody:** only public verification keys leave the broker; private keys, access tokens and sensitive environment variables never enter model-visible responses, receipts or logs.
6. **Side-effect semantics:** classify operations as read-only, idempotent write, non-idempotent write, destructive; reconcile uncertain outcomes rather than blindly retrying.
7. **Evidence versus assertion:** a syntactically valid response, an MCP success envelope and a passing synthetic test are not sufficient grounds to claim semantic correctness.
8. **Reproducible source:** tool binaries, schema revisions, host qualification, provider versions, input hashes, environment and artifact hashes must be recorded.
9. **Untrusted input:** fetched pages, repository text, issues, tool descriptions, MCP manifests and prompts are untrusted content, never privileged instructions.
10. **Budgeted boundedness:** limits on runtime, tokens, spend, redirects, pages, recursion, concurrency, output size, retries and network access.
11. **Portable but honest:** Windows and macOS launchers may manage dependencies, but untrusted code executes only under a demonstrably qualified containment backend.
12. **Data minimality:** external queries use least disclosure and scoped credentials; every outbound disclosure is assessed by policy.

## 2. Canonical capability ontology and interface contracts

Example semantic hierarchy (extend the installed hierarchy, do **not** add parallel roots if matching types already exist):

```text
syn://type/tool
syn://type/tool/research/web-search
syn://type/tool/browser/automation
syn://type/tool/code/repository
syn://type/tool/data/sql-query
syn://type/tool/reasoning/smt-solver
syn://type/tool/verification/formal-proof
syn://type/tool/implementation
syn://type/tool/installed-instance
syn://type/tool/invocation
syn://type/tool/result
syn://type/tool/evidence
syn://type/tool/approval
syn://type/workflow/tool-graph
```

### Core entities and contracts

| Concept | Required identity and contract |
|---|---|
| Capability Type | Semantic URI, inherited Embers, input/output Type references, risk class and discovery examples. |
| Implementation | Publisher, protocol, immutable version/digest, compatible capability Type, transport and manifest provenance. |
| Installed Instance | Workspace, configuration, permitted scopes, binding hash, health, trust/admission state and operator policy. |
| Invocation | Immutable input digest, workspace, Agent signature/authorization decision, budget, idempotency class, run ID. |
| Result | Typed output, status (success/error/unknown), content hash, external resource refs, failure class and bounded data. |
| Cog receipt | Explicit input/output Types, handler binding/revision, data hashes, trace and authority-independent transformation evidence. |
| Source evidence | Canonical locator, retrieved and published timestamps, snippet/span offset, hash, extractor version, trust and permission. |
| Approval | Principal, scope, exact action/argument hash, expiry, one-shot or bounded-use grant, audit event. |
| Workflow | Typed nodes/edges, dependencies, budget, concurrency, rollback plan, policy revision and immutable execution snapshot. |

Recommended normalized invocation envelope (illustrative mapping onto existing schemas, not a replacement schema):

```json
{
  "schema_version": "tool-invocation-v1",
  "tool_type_ref": "syn://type/tool/research/web-search",
  "implementation_ref": "syn://tool-implementation/brave-search/pinned-build",
  "input_type_ref": "syn://type/data/search-query",
  "output_type_ref": "syn://type/data/search-results",
  "workspace_id": "<uuid>",
  "agent_identity_ref": "syn://agent/<id>",
  "input_artifact_sha256": "<sha256>",
  "policy_revision_sha256": "<sha256>",
  "effect_class": "read-only",
  "authorization_receipt_ref": "<immutable-ref>",
  "execution_budget": {
    "wall_time_ms": 30000,
    "max_output_bytes": 262144
  },
  "correlation_id": "<uuid>"
}
```

Real Type URIs, identities and field contracts must be checked against the authoritative Syn catalog and existing code before committing any schema. Multiple input arguments can be wrapped in a single registered **request envelope Type**, consistent with the current one-in/one-out Cog rule.

## 3. Release slices and dependencies

| Milestone | Scope | First useful capability | Hard gate |
|---|---|---|---|
| M0: foundation | P01–P02 | Typed standard adapters | Contract and protocol tests |
| M1: safe runner | P03–P04 | Authorized, discoverable tool invocation | Sandbox, identity and replay security |
| M2: useful agent | P05–P07 | Web/browser/GitHub research and coding | Evidence+CI+isolated browser |
| M3: structured integration | P08–P09 | SQL/APIs, mathematics and solver verification | RLS + formal/solver correctness |
| M4: autonomous composition | P10 | Resumable multi-tool workflows | No repeat side effects under crashes |
| M5: qualified release | P11–P12 | Measured and managed tool ecosystem | Reproducible performance and security dossier |

Dependencies are logical prerequisites, not calendar dates. Develop independent provider adapters in parallel only after contracts, policy and test harness are stable. The full programme is not blocked on availability of paid APIs: every integration requires a credential-free local/mock contract suite.

## 4. Granular stage-by-stage implementation

### Phase 01 — Foundations and non-destructive reconciliation

#### T01.1 — Current-state inventory and gap analysis
- **Prerequisites:** none.
- **Deliverable:** A version-pinned capability inventory and migration-safe extension map.
- **Implementation:** Inspect actual Potbot APIs, PostgreSQL migrations, Cog registry, Syn catalog, sandbox profiles, credentials, jobs, REST, WebSocket, models and CI; record implemented vs unqualified vs missing; identify safe extension points.
- **Automated acceptance:** Inventory matches pinned main commit; each existing service has an integration owner; no duplicate database or identity subsystem proposed; undocumented assumptions flagged.
- **Demonstration:** Generate an inventory JSON and cross-reference report from the checked-out repository.
- **Evidence required:** `tests/` report for `T01.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T01.2 — Canonical semantic type and Cog contracts
- **Prerequisites:** T01.1.
- **Deliverable:** Versioned semantic schemas for tool, capability, implementation, installation, invocation and evidence.
- **Implementation:** Extend authoritative Syn/Synny Types using full syn:// or synny:// URIs; preserve inherited/default/required/optional Ember contracts; distinguish discovery descriptor from installed executable instance; represent typed input/output via one-input/one-output Cog or typed envelope; bind handler revisions and hashes.
- **Automated acceptance:** Reject shorthand Type identifiers; reject unknown/unregistered data Type refs, invalid inheritance and Ember violations; detect incompatible schema evolution; serialize/deserialize deterministically.
- **Demonstration:** Round-trip a SearchQuery → SearchResults Cog and a tool invocation receipt.
- **Evidence required:** `tests/` report for `T01.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T01.3 — Execution envelope and provenance contracts
- **Prerequisites:** T01.2.
- **Deliverable:** Signed, immutable invocation/result/receipt envelope and lifecycle schema.
- **Implementation:** Define job, run, invocation, input/output artifact hash, policy revision, executor version, causal parent, status, units, budgets, approval, redaction and external source provenance; use Potbot immutable receipts and event conventions.
- **Automated acceptance:** Canonical hashes stable on repeat; altered inputs or policy revision invalidate reuse; partial execution never appears verified; payloads over size limits are rejected.
- **Demonstration:** Replay an immutable read-only invocation and inspect the linked receipt chain.
- **Evidence required:** `tests/` report for `T01.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T01.4 — Build matrix and stage acceptance harness
- **Prerequisites:** T01.1–T01.3.
- **Deliverable:** Stage-aware CI, disposable integration services and machine-readable gate reports.
- **Implementation:** Use existing uv lock, Python 3.12, scripts/check.py and GitHub CI; add fast offline tests, real PostgreSQL, qualified Linux Bubblewrap/seccomp, contract/fuzz and optional live-provider gates; publish JUnit/JSON and skipped-gate reasons.
- **Automated acceptance:** Fail if mandated DB/sandbox suite skipped; never treat synthetic fixtures as live qualification; prove migration compatibility; preserve all existing test suites.
- **Demonstration:** One command produces a P01 gate report and a reproducible failing negative test.
- **Evidence required:** `tests/` report for `T01.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 01 exit gate:** Catalog existing implementation with no collisions. Validate exact Type URIs, inherited Ember contracts and immutable receipt round-trips; CI has explicit, non-skippable security/DB gates.

### Phase 02 — Universal protocol and process adapters

#### T02.1 — MCP stdio adapter
- **Prerequisites:** T01.2,T01.3,T01.4.
- **Deliverable:** Fully tested stdio MCP client for curated servers.
- **Implementation:** Implement protocol negotiation, tool/resource/prompt enumeration, argument validation, results/content blocks, cancellation, progress, logging, pagination and failure normalization using an official SDK rather than hand-coded wire protocols; pin server packages.
- **Automated acceptance:** Fixture MCP server passes discovery, calls, tool errors, pagination, server crash, oversized payload and cancellation tests; blocked tools cannot be called by guessing IDs.
- **Demonstration:** Invoke a local calculator MCP server in a confined workspace.
- **Evidence required:** `tests/` report for `T02.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T02.2 — MCP Streamable HTTP and authorization
- **Prerequisites:** T02.1,T01.3; security design review before remote calls.
- **Deliverable:** Remote MCP transport with authenticated resource-scoped access.
- **Implementation:** Support target protocol version 2026-07-28 and negotiate compatibility for earlier supported versions; implement stateless request flow, multi-round-trip where applicable, transport timeouts, header routing, server identity, OAuth protected-resource metadata, scope checks and token audience validation.
- **Automated acceptance:** Mismatched protocol fails clearly; expired/wrong-audience OAuth token rejected; untrusted redirects blocked; 401/403 and disconnect produce correct statuses; reconnect does not repeat writes.
- **Demonstration:** Call a controlled remote read-only MCP endpoint using scoped OAuth test credentials.
- **Evidence required:** `tests/` report for `T02.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T02.3 — CLI and local process adapters
- **Prerequisites:** T01.3,T01.4.
- **Deliverable:** Typed CLI/process adapter running only through approved executors.
- **Implementation:** Add allowlisted executable identities, argument schemas, stdin/stdout/stderr caps, workdir mapping, exit status and resource limits; reuse qualified Potbot Bubblewrap rather than unrestricted shell access.
- **Automated acceptance:** Reject arbitrary host executable, paths outside registered workspace, shell injection and environment leakage; kill child process trees at timeout; stream bounded progress.
- **Demonstration:** Run git status, python -V and a controlled failing command from workspace tooling.
- **Evidence required:** `tests/` report for `T02.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T02.4 — HTTP, SDK, GraphQL and gRPC adapters
- **Prerequisites:** T02.1,T02.2.
- **Deliverable:** Common schema-checked API adapter framework.
- **Implementation:** OpenAPI request/response validation, HTTP pagination and rate limits, JSON Schema, streaming responses; optional GraphQL introspection and gRPC reflection; map API operations into precise semantic Types and side-effect categories.
- **Automated acceptance:** Malformed schemas, hostile redirects, unbounded pagination, malformed response bodies and timeout retry paths fail safely; idempotent reads cache correctly; writes are never blindly retried.
- **Demonstration:** Use a mocked versioned REST service and a real local GraphQL/gRPC fixture.
- **Evidence required:** `tests/` report for `T02.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 02 exit gate:** Local and remote MCP fixtures and typed CLI/HTTP calls work end-to-end; malformed/unavailable server behavior is correct, with no uncontrolled write retry.

### Phase 03 — Security boundaries and durable tool execution

#### T03.1 — Capability permission and risk policy
- **Prerequisites:** T01.2,T01.3.
- **Deliverable:** Policy decision service with explainable authorization.
- **Implementation:** Define read, external disclosure, file mutation, network, DB write, code execution, money and admin capability tiers; bind policy to agent identity, wallet hierarchy, workspace, tool instance and user/operator grants; integrate existing security-lock Cog semantics.
- **Automated acceptance:** Default deny; parent grant cannot be broadened by child; privilege escalation by modifying tool metadata rejected; deny decisions logged without leaking secrets.
- **Demonstration:** Show authorized search and denied cloud-delete operations for same agent.
- **Evidence required:** `tests/` report for `T03.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T03.2 — Workspace and network confinement
- **Prerequisites:** T02.3,T03.1.
- **Deliverable:** Enforced filesystem/network boundary for local and remote tool tasks.
- **Implementation:** Reuse immutable registered sandbox root; all models/tools/dependencies/artifacts/cache inside root; Linux Bubblewrap/seccomp for local untrusted execution; broker egress by hostname/IP with DNS pinning and SSRF protection; Windows/macOS require separately qualified VM/container backend or fail closed.
- **Automated acceptance:** Traversal, symlink, junction, mount, /proc leakage, localhost metadata probing and redirect-to-internal-IP are denied; host files cannot be read/written; unqualified platform cannot run untrusted workloads.
- **Demonstration:** Adversarial fixture attempts workspace escape and forbidden network destinations.
- **Evidence required:** `tests/` report for `T03.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T03.3 — Credential broker and identity integration
- **Prerequisites:** T03.1,T03.2.
- **Deliverable:** Non-exportable scoped credential acquisition and usage.
- **Implementation:** Extend current OS credential/native broker; preserve public Ed25519 verification keys and private-key custody; use scoped OAuth where available and short-lived provider credentials; never put bearer secrets in model context, tool receipts or logs; categorize passkeys/TPM/hardware as future qualified backends.
- **Automated acceptance:** Secret scanner finds no credentials in traces/artifacts; incorrect identity/recipient rejected; child tool cannot read broker storage; key rotation invalidates stale rights; no unsupported custody profile claims.
- **Demonstration:** Call an authenticated test API while the agent sees only an opaque credential handle.
- **Evidence required:** `tests/` report for `T03.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T03.4 — Durable tool gateway and approvals
- **Prerequisites:** T02.*,T03.1–T03.3.
- **Deliverable:** First end-to-end authorized tool runner.
- **Implementation:** Add invocation admission, budget reservation, durable queue claim/lease fencing, immutable result, approval handshake, cancellation, reconciliation and side-effect-aware retries via existing PostgreSQL jobs; preserve typed Cog separation: transforming data does not authorize external execution.
- **Automated acceptance:** 100 simulated worker crashes result in no unauthorized repeat of side-effecting call; concurrent claims do not double execute; denied approval means zero side effects; read-only invocations replay safely.
- **Demonstration:** An operator approves a restricted tool invocation from CLI/API; job/receipt appears in WebSocket replay.
- **Evidence required:** `tests/` report for `T03.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 03 exit gate:** Agent can execute a permitted tool in a qualified sandbox through existing jobs, while every denied filesystem, network, credential and destructive action fails closed.

### Phase 04 — Capability registry, discovery and routing

#### T04.1 — Curated registry and trusted package catalog
- **Prerequisites:** T02.*,T03.*.
- **Deliverable:** Local signed/version-pinned capability and adapter catalog.
- **Implementation:** Provide declarative manifest for name, versions, protocol, origin, trust tier, scopes, schemas, SemVer, hashes/SBOM, update policy and dependencies; separate catalog listing from installation/execution.
- **Automated acceptance:** Unpinned or unsigned high-risk package rejected by policy; stale manifest flagged; malicious package cannot gain network/filesystem grants by self-description.
- **Demonstration:** List and inspect 20 curated adapters without installing all dependencies.
- **Evidence required:** `tests/` report for `T04.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T04.2 — MCP discovery, rescan and schema reconciliation
- **Prerequisites:** T04.1,T02.1,T02.2.
- **Deliverable:** Incremental MCP search and dynamic catalog updates.
- **Implementation:** Discover enabled server toolsets, tools/resources/prompts, listChanged events, pagination, schemas and supported versions; cache with TTL/revision; ingest third-party MCP registry metadata as untrusted candidates pending operator admission.
- **Automated acceptance:** Discovery is deterministic across reconnects; tool removal invalidates planner references; changed schema requires revalidation; list pagination returns every permitted tool.
- **Demonstration:** Add a fake MCP tool during an active session; see tool appear without restarting the agent.
- **Evidence required:** `tests/` report for `T04.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T04.3 — Capability search and routing
- **Prerequisites:** T04.1,T04.2,T01.2.
- **Deliverable:** Semantic + symbolic selector with cost/security filtering.
- **Implementation:** Build keyword, graph and vector search over type hierarchy, metadata and examples; rank by required data types, trust, scopes, observed quality, latency and cost; expose inspect/select and alternative suggestions; lazy-load only relevant tool definitions.
- **Automated acceptance:** A golden set of 40 requests routes to expected capability class; permission-disallowed option never ranks as executable; cold discovery bounded by token/tool budget.
- **Demonstration:** Ask for screenshot inspection, satisfiability solving and commit inspection without naming vendors.
- **Evidence required:** `tests/` report for `T04.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T04.4 — Versioning, health, revocation and install lifecycle
- **Prerequisites:** T04.1–T04.3.
- **Deliverable:** Operator lifecycle APIs and circuit breakers.
- **Implementation:** Install/uninstall/pin/up/downgrade adapters inside workspace tools/; healthcheck schema/runtime compatibility; quarantine compromised packages, recall capability revisions and invalidate stale invocations.
- **Automated acceptance:** Rolling update preserves old active runs; revoked instance is denied immediately to new calls; rollback restores previous manifest without corrupting receipts; health state is visible.
- **Demonstration:** Disable a known-bad test tool during queued invocations and verify safe behavior.
- **Evidence required:** `tests/` report for `T04.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 04 exit gate:** Operator discovers, inspects, pins, health-checks and revokes 20 capability definitions, and agent routes golden capability requests without installing everything.

### Phase 05 — Search, retrieval and evidence research

#### T05.1 — Search provider adapters and result normalization
- **Prerequisites:** T03.4,T04.3.
- **Deliverable:** Multi-provider search capability with source timestamps.
- **Implementation:** Integrate Google via supported licensed API/provider, Brave, Bing where supported, and Tavily/Exa or comparable providers; credentials brokered; normalized query, ranked results, snippet, retrieval time, canonical URL, source engine and quota; do not assume Google public website is a stable API.
- **Automated acceptance:** Mocked quota failure falls back when policy allows; duplicates deduplicated but source lineage retained; restricted result providers cannot silently exfiltrate sensitive queries.
- **Demonstration:** Compare two providers for a technical research query and save independent result sets.
- **Evidence required:** `tests/` report for `T05.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T05.2 — Fetch, extraction and canonical source artifacts
- **Prerequisites:** T05.1.
- **Deliverable:** Web fetch/crawler with robust content provenance.
- **Implementation:** HTTP fetch with robots/terms/rate constraints; structured HTML/JSON extraction; PDF/document parser, tables, code and metadata; redirects canonicalized; content-addressed copies with extraction versions; optional OCR only on scanned material.
- **Automated acceptance:** Correctly extracts authored text from HTML, PDF and JSON fixtures; preserves citation spans and source hashes; rejects zip bombs, huge pages and private-network redirects.
- **Demonstration:** Follow search results and answer with verifiable document-location citations.
- **Evidence required:** `tests/` report for `T05.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T05.3 — Specialist scientific and archival retrieval
- **Prerequisites:** T05.2.
- **Deliverable:** Domain source connectors with query contracts.
- **Implementation:** Add Crossref, OpenAlex, arXiv, PubMed, Semantic Scholar (subject to API availability), Wikidata/SPARQL and Internet Archive; record primary vs secondary source and publication/event date separately.
- **Automated acceptance:** DOI resolution and duplicate author/title matching work; retractions/corrections represented; unsupported endpoints degrade explicitly; rate-limits respected.
- **Demonstration:** Retrieve a paper and its linked metadata from two independent sources.
- **Evidence required:** `tests/` report for `T05.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T05.4 — Evidence comparison and research workflows
- **Prerequisites:** T05.1–T05.3,T09.1 later enhancement.
- **Deliverable:** Reproducible research workflow and contradiction matrix.
- **Implementation:** Build claim/source/evidence triples, corroboration, temporal validity, independence, contradiction flags and cited answer assembly; stop with uncertainty rather than inventing verification.
- **Automated acceptance:** Planted conflicting claims produce an unresolved state; old news not mislabeled current; every quoted claim has traceable source offsets; zero fabricated citations in gold fixture.
- **Demonstration:** Research a contested technical specification and show supported vs unresolved claims.
- **Evidence required:** `tests/` report for `T05.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 05 exit gate:** A multi-source research question completes with immutable extracted documents, cited claims, contradiction handling and source freshness annotations.

### Phase 06 — Browser control and visual verification

#### T06.1 — Playwright controlled browser service
- **Prerequisites:** T03.2,T03.4,T04.3.
- **Deliverable:** Managed isolated browser sessions via Playwright MCP/CLI.
- **Implementation:** Install browser runtimes within allowed workspace tool assets; use isolated profiles, accessibility snapshots, navigation, screenshots, forms, controlled downloads/uploads and deterministic session replay; persistent profiles opt-in and separately partitioned.
- **Automated acceptance:** Two workspaces cannot read each others cookies; malicious webpage instructions cannot authorize new tool calls; downloads remain under workspace; session recovery does not invent form submission status.
- **Demonstration:** Automate an internal test application from login through read-only dashboard export.
- **Evidence required:** `tests/` report for `T06.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T06.2 — Chrome DevTools and network debugger
- **Prerequisites:** T06.1.
- **Deliverable:** CDP-based debugging and performance capability.
- **Implementation:** Integrate Chrome DevTools MCP for console errors, network waterfall, trace, coverage, heap/performance and DOM/AX inspection; bind traces to run receipts; disable unintended data sharing/telemetry when required.
- **Automated acceptance:** Captures and attributes a deliberately failing request; rejects debugger connection outside permitted sessions; large traces redacted and bounded.
- **Demonstration:** Locate a simulated CORS, 404 or frontend JavaScript failure.
- **Evidence required:** `tests/` report for `T06.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T06.3 — Visual regression, accessibility and desktop fallback
- **Prerequisites:** T06.1,T06.2.
- **Deliverable:** Multimodal browser result and UI test adapters.
- **Implementation:** Create image diff, screenshots, DOM/AX cross-check, semantic interaction and layout regression snapshots; optional native desktop automation only in qualified isolated VMs; prioritize structured UI before coordinate clicks.
- **Automated acceptance:** Detect known 1-pixel/large-layout regressions with tuned thresholds; keyboard-only path tested; false-positive baseline reported; no raw browser profile credentials exported.
- **Demonstration:** Compare UI screenshots and explain visually detected vs DOM-detected changes.
- **Evidence required:** `tests/` report for `T06.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T06.4 — Browser reliability benchmark and test fixtures
- **Prerequisites:** T06.1–T06.3.
- **Deliverable:** Browser action benchmark with deterministic replay.
- **Implementation:** Build local fixture site for auth, forms, redirects, pagination, downloads, popups, slow response and malicious prompt content; run Playwright, CLI and optional alternative engines on identical tasks; report outcome, context tokens, elapsed time and costs.
- **Automated acceptance:** >=95% deterministic success on own stable fixture at pinned environment after agreed retries; zero unauthorized form submissions; reproducible traces for every failed case.
- **Demonstration:** Dashboard of browser methods and sample CI artifact.
- **Evidence required:** `tests/` report for `T06.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 06 exit gate:** A browser debugging/research task runs in an isolated profile with screenshots, network traces and zero cross-workspace session bleed.

### Phase 07 — Repository, code and CI engineering

#### T07.1 — GitHub and GitLab integration
- **Prerequisites:** T03.4,T04.3.
- **Deliverable:** Repository-host adapters with scoped tools.
- **Implementation:** Integrate GitHub MCP official server, REST fallback for missing operations and optional GitLab/Gitea; repo/PR/issue/commit/actions read APIs; write scopes and approval requests explicit; webhook and polling options.
- **Automated acceptance:** Read-only PAT cannot create PR; pagination complete; webhook replays deduplicated; pull request operations bound to exact branch and current revision.
- **Demonstration:** Read failing GitHub Actions log and create a proposed issue draft in test repo.
- **Evidence required:** `tests/` report for `T07.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T07.2 — Local Git, AST and LSP intelligence
- **Prerequisites:** T07.1,T02.3.
- **Deliverable:** Source-aware repository exploration.
- **Implementation:** Integrate git status/diff/log/blame, ripgrep, Tree-sitter, ast-grep, LSP symbol/ref/diagnostics; cache against immutable source snapshot; never mutate original host repository.
- **Automated acceptance:** Rename/function reference lookup correct in fixtures; stale index invalidated on git commit; path outside workspace denied; binary and malformed files handled safely.
- **Demonstration:** Locate definition, usages and tests of a method and produce a navigation graph.
- **Evidence required:** `tests/` report for `T07.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T07.3 — Build/test/static/security toolchain
- **Prerequisites:** T07.2,T03.2.
- **Deliverable:** Normalized verification runner.
- **Implementation:** Use uv-managed pytest and language-specific tests, lint, types, formatting, Semgrep/CodeQL where available, OSV/Trivy/Syft and configurable SBOM; record exit codes, commands, tool versions, environment digests and logs.
- **Automated acceptance:** A planted vulnerability/test failure is detected; changed test definition invalidates prior success; absence of mandatory checker fails; no secret in logs; no automatic network dependency download unless allowed.
- **Demonstration:** Run verified code quality profile against a controlled faulty sample project.
- **Evidence required:** `tests/` report for `T07.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T07.4 — Safe patch/PR/CI closure workflow
- **Prerequisites:** T07.1–T07.3,T03.4.
- **Deliverable:** Artifact-bound proposed changes with CI feedback loop.
- **Implementation:** Re-use Potbot existing baseline→candidate→patch→exact-state-check→manifest pipeline; commit on isolated branch, open draft PR when approved, poll CI/replay events and assemble results; no autonomous merge by default.
- **Automated acceptance:** Wrong base hash or stale contract blocks write; required checks cannot be deleted to pass; fake CI success rejected; branch creation/write requires explicit scope; output includes patch provenance.
- **Demonstration:** Repair a failing fixture, open draft PR in test repository and record CI outcome.
- **Evidence required:** `tests/` report for `T07.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 07 exit gate:** A coding task reproduces a failing baseline, produces an isolated patch, runs exact-state checks, and records approved PR/CI evidence.

### Phase 08 — Databases, APIs and specialist connectors

#### T08.1 — Safe SQL and analytical data adapters
- **Prerequisites:** T02.4,T03.1–T03.4.
- **Deliverable:** PostgreSQL, SQLite and DuckDB read/query interfaces.
- **Implementation:** Use existing owner-separated PostgreSQL store; separate user/workspace query connection from internal persistence; prepared statements, schema introspection, read-only transactions by default, row/output caps, explain plan, cost guardrails; DuckDB loads workspace files only.
- **Automated acceptance:** RLS cross-workspace query fails; SQL injection and multi-statement mutation rejected; explain cost-limit works; raw DSN never exposed; queries interrupted on budget.
- **Demonstration:** Query workspace-owned fixture and join CSV/Parquet with DuckDB.
- **Evidence required:** `tests/` report for `T08.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T08.2 — Vector, full-text and graph retrieval
- **Prerequisites:** T08.1,T04.3.
- **Deliverable:** Hybrid retrieval and semantic graph capability.
- **Implementation:** Extend current pgvector/named embeddings, full-text and relationship types; plug in Neo4j/Kuzu/Qdrant only when justified; implement citations and source snapshot scoping, identity and revision filters.
- **Automated acceptance:** Cross-tenant leakage absent under 100 adversarial queries; wrong embedding model revision cannot silently mix vectors; graph traversal respects max depth; search results reproduce from snapshot.
- **Demonstration:** Return semantic and structural matches for a coding question.
- **Evidence required:** `tests/` report for `T08.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T08.3 — Schema-aware external API integration
- **Prerequisites:** T02.4,T08.1.
- **Deliverable:** API catalog generated from OpenAPI/GraphQL/gRPC schemas.
- **Implementation:** Map API methods to capability and cog contracts, generate typed request models, validate response, paginate/resume and classify read vs write vs destructive operations; store provider schema hash and refresh diff.
- **Automated acceptance:** Breaking API schema change blocks invocation until requalified; rate-limit fallback emits status; idempotency-key write behavior validated; provider errors correctly mapped.
- **Demonstration:** Import OpenAPI spec and perform safe request using generated capability.
- **Evidence required:** `tests/` report for `T08.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T08.4 — Cloud, files, collaboration and optional services
- **Prerequisites:** T08.3,T03.3.
- **Deliverable:** Policy-controlled specialist connector profile.
- **Implementation:** Provide adapters/connector templates for S3-compatible object stores, Google Drive/SharePoint, Slack/Teams, GitHub Releases, Docker/Kubernetes/cloud control planes, Unity/Blender and domain tools; prioritize need rather than auto-install; privileged operations behind approval.
- **Automated acceptance:** Each added connector passes standard contract suite; unavailable authorization reported accurately; cloud destructive action denied by default; cross-workspace secrets isolated.
- **Demonstration:** Enable one chosen optional connector from registry and run qualification script.
- **Evidence required:** `tests/` report for `T08.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 08 exit gate:** The agent queries a workspace-scoped SQL dataset and a typed API, with no leakage or unauthorized writes; graph/vector retrieval remains scoped and versioned.

### Phase 09 — Computational reasoning and formal verification

#### T09.1 — Python, statistics and symbolic computation
- **Prerequisites:** T03.2,T03.4.
- **Deliverable:** Reasoning compute profile with Python/NumPy/SciPy/SymPy.
- **Implementation:** Provide typed operations for exact algebra, calculus, numerical experiments, statistics, plots and reproducible notebooks/scripts; include input dataset hashes, versions, numerical tolerances and execution environment; external code strictly sandboxed.
- **Automated acceptance:** Curated numeric/symbolic gold tasks meet exact/tolerance targets; NaN/overflow/ill-conditioned cases flagged; independent recomputation matches receipts; no untrusted wheel network install.
- **Demonstration:** Solve sample analytical task both symbolically and numerically.
- **Evidence required:** `tests/` report for `T09.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T09.2 — SMT, optimization and counterexamples
- **Prerequisites:** T09.1,T01.2.
- **Deliverable:** Z3/cvc5 and OR-Tools reasoning capabilities.
- **Implementation:** Translate typed constraints and problem instances into bounded SMT/CP-SAT jobs; expose satisfiable, unsatisfiable, unknown and timeout distinctly; request models/counterexamples, unsat cores and objective bounds when available.
- **Automated acceptance:** Known SAT/UNSAT suites return correct outcome; timeout never recast as UNSAT; no untrusted solver expressions executed as Python; constraint model provenance retained.
- **Demonstration:** Identify contradiction in cryptographic key-rotation rules and show counterexample.
- **Evidence required:** `tests/` report for `T09.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T09.3 — Wolfram, domain mathematics and simulation
- **Prerequisites:** T09.1,T09.2.
- **Deliverable:** Advanced computational backend with fallbacks.
- **Implementation:** Integrate Wolfram via supported API/MCP where authorized, local SageMath/Julia or domain simulation as optional profiles; compare answer consistency with SymPy/numerical solver; distinguish computational result from tool-generated natural-language explanation.
- **Automated acceptance:** No paid API calls without budget authorization; inconsistent symbolic results trigger verification; units/dimensional checks pass; reproducibility report notes external provider version.
- **Demonstration:** Compute same integral or optimization using two backends and compare.
- **Evidence required:** `tests/` report for `T09.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T09.4 — Formal proof and model checking
- **Prerequisites:** T09.2,T01.2.
- **Deliverable:** Proof-checking and state-machine analysis profile.
- **Implementation:** Integrate Lean 4, TLA+/Apalache/Alloy (qualified tools) for invariants, temporal behavior and counterexample traces; encode specs, preserve assumptions, run kernel/checker separately; no claim that arbitrary code is proven by example tests.
- **Automated acceptance:** One true theorem checked, one deliberately false theorem rejected; flawed state-machine exposes counterexample; missing proof treated unverified; proof artifact retains checker revision.
- **Demonstration:** Check no-double-dispatch invariant on a simplified tool queue state machine.
- **Evidence required:** `tests/` report for `T09.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 09 exit gate:** Agent automatically checks numerical, symbolic, SMT and model-checking claims, exposing counterexamples and inconclusive outcomes appropriately.

### Phase 10 — Workflow composition and agent intelligence

#### T10.1 — Composable typed Cog workflow planner
- **Prerequisites:** T04.3,T05.*,T07.*,T08.*,T09.*.
- **Deliverable:** Type-directed workflow graph assembler.
- **Implementation:** Infer typed routes from capability input/output contracts, selecting explicitly installed Cogs and data converters; support fan-out/fan-in via typed envelope nodes; reject cycles unless loop bounds and termination guard explicitly supplied; explain plan and required rights before executing.
- **Automated acceptance:** 50 golden dataflow cases compile; incompatible types rejected; cycles without bounds rejected; every edge carries immutable type/version; unauthorized transform cannot trigger a tool.
- **Demonstration:** Compose SearchQuery → Pages → Claims → VerifiedReport without naming specific adapters.
- **Evidence required:** `tests/` report for `T10.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T10.2 — Durable parallel workflow runtime
- **Prerequisites:** T10.1,T03.4.
- **Deliverable:** Resumable DAG executor and event stream.
- **Implementation:** Extend existing PostgreSQL queue/REST/WebSocket jobs with graph node scheduling, deduplication, lease fences, bounded concurrency, pause/resume, approval handoff, checkpointing and compensation proposals; avoid replaying uncertain non-idempotent effects.
- **Automated acceptance:** Kill workers at 5 fault points and require no double side effects; event cursor replay identical after restart; fan-in waits for all required verified inputs; budget exhaustion halts safely.
- **Demonstration:** Run fan-out multi-source research workflow with injected worker crash.
- **Evidence required:** `tests/` report for `T10.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T10.3 — Model reasoning controller and independent critics
- **Prerequisites:** T10.1,T10.2,T09.*.
- **Deliverable:** Tool-aware reasoning and independent challenge workflow.
- **Implementation:** Extend existing multi-model selection, provider gateway and Phase 6 graph/search instead of creating competing planner; add tool proposals with confidence/uncertainty, bounded alternative plan search, independent verifier role, contradiction tests and grounded action selection.
- **Automated acceptance:** Planted false claims challenged, unsupported conclusions labeled; reviewer cannot self-approve writes; no added model call bypasses budget; role context isolation passes.
- **Demonstration:** Run same coding problem with and without tool/critic loop and compare verified outcomes.
- **Evidence required:** `tests/` report for `T10.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T10.4 — Adaptive selection, caching and capability utility
- **Prerequisites:** T10.3; offline utility fixture (expanded by T11.1).
- **Deliverable:** Policy-safe empirically learned router.
- **Implementation:** Collect per-task-family success, cost, latency, failure and context usage; choose by permissions and expected utility, back off on poor health, allow manual override and deterministic baseline; cached responses must respect exact source/credential/revision scope.
- **Automated acceptance:** Forced override honored; no unqualified tool promoted automatically; wrong-scope cache never served; with evidence insufficient route remains baseline rather than inventing performance gain.
- **Demonstration:** Display a tool-selection decision explanation and its evidence.
- **Evidence required:** `tests/` report for `T10.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 10 exit gate:** A typed multi-tool DAG survives worker failure and restart without repeating uncertain side effects, and independent verification can reject an incorrect result.

### Phase 11 — Benchmarks, trust and adversarial evaluation

#### T11.1 — Representative task benchmark suite
- **Prerequisites:** T05.*,T06.*,T07.*,T08.*,T09.*.
- **Deliverable:** Versioned baseline/gold dataset and blind task splits.
- **Implementation:** Assemble Potbot/DanceFlow real tasks plus web, browser, API, SQL, math, formal proof and adverse cases; keep authored fixtures distinct from held-out and actual provider runs; immutable baseline and scorer revisions.
- **Automated acceptance:** At least 20 tasks per target domain to start with explicit difficulty and expected observable outcome; benchmark contamination checked; each outcome independently scored.
- **Demonstration:** Produce model-only versus single-tool versus composite-tool comparison report.
- **Evidence required:** `tests/` report for `T11.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T11.2 — Evidence integrity and factual verification
- **Prerequisites:** T11.1,T01.3,T05.4.
- **Deliverable:** Evidence checker and provenance audit.
- **Implementation:** Implement immutable citations, external source hashes, assertion→support mapping, independent execution checks, source freshness, contradictory evidence and replay audit; integrate with patch verification and solver result retention.
- **Automated acceptance:** Tampered citation/patch/proof hash detected; unsupported claims fail; stale docs flagged; reproducible receipts for offline tasks; partial tool success not promoted to verified claim.
- **Demonstration:** Show accepted claim, challenged claim and unresolved claim with source chain.
- **Evidence required:** `tests/` report for `T11.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T11.3 — Statistical evaluation and regression policies
- **Prerequisites:** T11.1,T11.2,T10.4.
- **Deliverable:** Comparable experiments and release regression gates.
- **Implementation:** Compare model-only, retrieval-only, calculation-only, solver, critic and adaptive configurations; paired task sets, repeated seeds, confidence intervals, pre-registered metrics: verified success, false-pass, tool selection, cost/success, tokens, latency, reliability; budget-adjusted quality.
- **Automated acceptance:** No aggregate metric mixes synthetic with live; baseline regressions fail gate at configured threshold; confidence uncertainty reported; all runs include pinned configs and costs.
- **Demonstration:** Generate a CI report showing whether each tool meaningfully improves a target task family.
- **Evidence required:** `tests/` report for `T11.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T11.4 — Security, chaos and adversarial qualification
- **Prerequisites:** T03.*,T10.2,T11.2.
- **Deliverable:** Red-team and failure-injection release suite.
- **Implementation:** Test prompt injection, tool-result laundering, malicious MCP manifests, poisoned document retrieval, SSRF, symlink races, quota exhaustion, replay after crash, dependency compromise, OAuth confusion and cross-tenant leakage.
- **Automated acceptance:** Zero critical successful boundary escapes in gated corpus; kill-switch works during incident; unqualified host fails closed; secrets absent in logs; data destruction requires explicit authorization.
- **Demonstration:** Execute attack corpus in disposable CI environment and retain audit evidence.
- **Evidence required:** `tests/` report for `T11.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 11 exit gate:** Reproducible paired benchmarks show verified success, costs, uncertainty and security regressions on separated synthetic/held-out/live runs.

### Phase 12 — Packaging, operations and release certification

#### T12.1 — Installation and dependency qualification
- **Prerequisites:** T01–T11.
- **Deliverable:** Reproducible cross-platform deployment profiles.
- **Implementation:** Use existing start.bat, start.ps1, start.sh and uv lock; provision adapters/models to workspace-local directories; pin all tool binaries, hashes, browsers and packages; optional profiles avoid bloating default; report host sandbox qualification separately.
- **Automated acceptance:** Fresh Linux install passes standard suite; Windows/macOS untrusted execution either passes qualified isolation or fails closed; offline smoke mode works without paid credentials; rollback documented.
- **Demonstration:** Run fresh install and capability discovery from clean checkout.
- **Evidence required:** `tests/` report for `T12.1`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T12.2 — Operator UI, monitoring and controls
- **Prerequisites:** T10.*,T04.4,T11.*.
- **Deliverable:** Unified tool dashboard and API surfaces.
- **Implementation:** Extend current web dashboard/REST/WebSocket with catalog, permissions, health, active DAG, tool calls, prompts/resources, evidence, cost budgets, approvals, audit and diagnostic outputs; filter by workspace.
- **Automated acceptance:** Read-only users cannot approve; replay cursor shows same state after reconnect; all secret fields redacted; API and UI agree on execution status and verification state.
- **Demonstration:** Operator inspects running workflow, approves a safe write and sees immutable result.
- **Evidence required:** `tests/` report for `T12.2`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T12.3 — Developer docs, SDK, examples and migration guides
- **Prerequisites:** T12.1,T12.2.
- **Deliverable:** Stable extension SDK and adoption documentation.
- **Implementation:** Publish semantic Type reference, adapter contract, sample MCP server/client, custom CLI/REST/Cog plug-in templates, version/migration guide, isolation requirements, threat model, troubleshooting and tests; keep existing docs linked and authoritative.
- **Automated acceptance:** External contributor can build a sample adapter following docs; examples run in CI; old project migrations preserve receipts and types; no unpinned download instructions.
- **Demonstration:** Create sample custom tool using SDK and pass conformance tests.
- **Evidence required:** `tests/` report for `T12.3`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

#### T12.4 — Release certification and maintenance policy
- **Prerequisites:** T12.1–T12.3,T11.4.
- **Deliverable:** Signed release with acceptance dossier and ongoing change control.
- **Implementation:** Pin commit, migrations, tools, model configs, execution host qualification, SBOM, security scans and benchmark reports; publish release, downgrade/rollback, compatibility table, deprecation and routine requalification policy; label limitations prominently.
- **Automated acceptance:** All required tests pass on claimed hosts; no known critical security escape; release can be restored and receipts validated; metrics vs prior qualified version reported with no fabricated score.
- **Demonstration:** Reproduce full gated demonstration from clean tagged checkout.
- **Evidence required:** `tests/` report for `T12.4`, pinned source/config digest, execution receipt(s), demo command/transcript or screenshot where appropriate, security assessment, and stage-gate JSON.

**Phase 12 exit gate:** A clean, qualified host installs a signed tagged release, runs demonstrations, validates rollback, and exposes operator controls and complete acceptance dossier.

## 5. Curated external integration backlog

A named product below is a **candidate implementation**, not a declaration that a maintained official MCP server exists or that all its functions are safe by default. Prefer official maintained MCP integrations; otherwise use SDK/API/CLI wrappers with the same contract tests. Pin versions after technical qualification. P0/P1/P2 are **programme priorities**, not blanket installation permission.

| Capability | Candidates | Phase | Priority / strategy |
|---|---|---|---|
| MCP client/server | MCP Python/TypeScript SDK, MCP Inspector, MCP Registry | 2,4 | P0, full protocol conformance |
| Search | Google licensed Search API, Brave, Bing, Tavily, Exa, Serper/SerpAPI | 5 | P0, two independent providers minimum |
| Fetch/extraction | Crawl4AI, Trafilatura, Firecrawl, Docling, Apache Tika | 5 | P0, workspace/parser isolation |
| Academic | OpenAlex, Crossref, arXiv, PubMed, Semantic Scholar | 5 | P1, evidence-specialist profile |
| Historical/semantic | Internet Archive, Wikidata/SPARQL, RSS/GDELT | 5 | P1, source timestamps |
| Browser actions | Microsoft Playwright MCP / CLI | 6 | P0, isolated sessions |
| Chrome debugging | Chrome DevTools MCP, CDP | 6 | P0, controlled sessions |
| Browser comparisons | WebdriverIO, Stagehand, Selenium, Puppeteer | 6 | P1, benchmark alternatives |
| Repository hosting | GitHub MCP, GitLab, Gitea, Sourcegraph | 7 | P0 GitHub, others demand-led |
| Code intelligence | git, ripgrep, Tree-sitter, ast-grep, LSP | 7 | P0, local read semantics |
| Tests/quality | pytest, Hypothesis, Vitest/Jest, Ruff, Pyright, Semgrep | 7 | P0, language profile-based |
| Supply chain | OSV, Syft, Trivy, Renovate/Dependabot | 7 | P1, scans and SBOM |
| Relational/analytics | PostgreSQL, SQLite, DuckDB | 8 | P0, workspace-aware access |
| Retrieval | pgvector, OpenSearch, Kuzu/Neo4j, Qdrant | 8 | P0 pgvector; P1 others |
| REST/API | OpenAPI/HTTP, GraphQL, gRPC, Bruno/Postman, Schemathesis | 8 | P0 OpenAPI, P1 specialist |
| Cloud/platform | S3, Docker, Kubernetes, Terraform/OpenTofu, AWS/Azure/GCP | 8 | P1/P2 scoped operational profiles |
| Productivity | Google Drive, SharePoint, Slack, Teams, Jira, Linear | 8 | P2, user-connected only |
| Symbolic/numeric | Python, NumPy, SciPy, SymPy, SageMath, Julia | 9 | P0 Python/SymPy, others on demand |
| Logic/optimization | Z3, cvc5, OR-Tools, MiniZinc, Clingo | 9 | P0 Z3, P1 others |
| Scientific computation | Wolfram Language/Alpha, NetworkX, PyMC, Stan | 9 | P1; P2 specialist |
| Formal methods | Lean 4, TLA+/Apalache, Alloy, Dafny, Rocq/Isabelle | 9 | P1 Lean/TLA+, P2 remainder |
| Orchestration | Existing Potbot durable jobs; Temporal/Prefect as later candidates | 10 | P0 reuse existing; avoid duplicate scheduler |
| Model orchestration | Existing model gateway; LangGraph/AutoGen/PydanticAI selectively | 10 | P0 reuse existing contracts |
| Evaluation/observability | OpenTelemetry, Phoenix/Langfuse, promptfoo, Ragas, MLflow | 11 | P1 external integration when justified |
| Benchmarks | SWE-bench, Terminal-Bench, GAIA, BrowseComp, WebArena, OSWorld, BFCL, tau-bench | 11 | P1 curated benchmark profiles |
| Domain-specific | Unity, Blender, DanceFlow MCP, Stemlab, FrameRudiments | 8,12 | P1 for user-specific demos; lazy-loaded |

### Profile selection defaults

| Profile | Initially active (minimal tool descriptions) | Lazy-loaded specialists |
|---|---|---|
| core | Tool search, inspect, request-approval, execute, verify, retrieve-receipt | All provider-specific tools |
| research | Search, fetch, citations, evidence comparator | Academic, archive, scholarly metadata |
| software | Git, GitHub read, code search, tests, patch verifier | CI write, PR write, security scan, build tools |
| web | Playwright read-only snapshots, nav, DevTools inspector | Forms/writes, network mocking, video, desktop |
| data | Postgres read-only, DuckDB, vector search | Graph DB, data transformation writes |
| reasoning | Python/SymPy, Z3, numerical checker | Wolfram, Lean, TLA+, optimization |
| operations | Logs, metrics, deploy inspection | Cloud infra writes, Terraform apply, credential admin |

## 6. Concrete workflow acceptance scenarios

**WF01 — Investigate failing CI:** Given repository/run ID, fetch logs, identify change/exception, inspect source, propose patch in candidate tree, run invariant tests, produce no-merge draft PR. **Pass:** No patch marked verified without unchanged/check-executed manifest; no unapproved GitHub write.

**WF02 — Research uncertain specification:** Search two engines, open original docs, compare publication and retrieval dates, cross-reference, cite exact excerpts, mark unresolved disagreements. **Pass:** Zero fabricated evidence references; contradicted claim remains unresolved.

**WF03 — Browser bug reproduction:** Open isolated browser, capture AX tree, console/network, screenshot and performance trace; local repair proposal and regression test. **Pass:** No session crosses workspace; exact screenshot/trace attached to failure.

**WF04 — Database/API schema reconciliation:** Inspect external API schema and local data schema; compile typed Cog conversions; run read-only join; request approval for any writes. **Pass:** Type incompatibility detected before call; RLS and authorization enforced.

**WF05 — Security design verification:** Translate security invariants into SMT and state-machine models; query Z3, model-check queue transitions; retain counterexamples and unknown states. **Pass:** Deliberately broken dispatch invariant produces counterexample; timeout not called proof.

**WF06 — Resumable parallel research:** Schedule search tasks in parallel; introduce crash after read result and before fan-in; replay run from checkpoint and compare artifact hashes. **Pass:** Consistent events/receipts and no duplicate external write.

**WF07 — Tool package compromise:** Discover malicious MCP manifest with hidden privilege request and tool-injected prompt; attempt install/call; inspect denial and rollback. **Pass:** No access beyond operator-approved scope and no credential leakage.

**WF08 — DanceFlow domain task:** Discover Stemlab/DanceFlow MCP tools and inspect source/fixtures; choose relevant capabilities; generate evidence-backed test/report. **Pass:** Lazy-load song-specific resources, no site-wide asset bloat; one complete receipt chain.

## 7. CI, test tiers and measurable exit criteria

| Tier | Mandatory frequency | Scope | Pass requirement |
|---|---|---|---|
| Unit & schema | Every PR | Types, Embers, canonical URLs, policy, pure adapters, data shapes | 100% critical invariant cases; no unexpected failures |
| Contract | Every PR | Mock MCP/API/CLI/provider/solver endpoints, schema drift, errors, pagination | All required contract cases pass |
| PostgreSQL & migration | Every PR in DB-enabled CI | Fresh+upgrade migrations, RLS, leases, receipts, event replay | Real database suite runs and passes; no silent skip |
| Linux sandbox | Every PR in security CI | Bubblewrap/seccomp, filesystem, network broker, process kills | Host preflight qualified, then all negative cases denied |
| Windows/macOS launcher | Every PR/profile change | uv, bootstrap, local assets, process-isolation qualification or fail-closed | No unqualified execution of untrusted tools |
| E2E offline | Every PR/nightly according to cost | Local controlled Git/SQL/MCP/browser/solver workflows | Deterministic golden workflows pass |
| Adversarial/chaos | Nightly and release | Prompt injection, SSRF, symlink races, replay, credential theft | Zero known critical escape, signed report |
| Live vendor smoke | Opt-in scheduled and release | Approved providers, capped spend, scoped tokens, reliable API availability | Report pass/fail/unavailable; no automatic false qualification |
| Reasoning A/B | Nightly/release | Same tasks model-only vs tool/critic/solver profiles | Comparable metrics with confidence intervals, no synthetic/live mixing |

### Release-wide acceptance targets

- **Security:** 0 successful critical cross-workspace read/write, host filesystem escape, credential exfiltration or unauthorized destructive calls in a pinned adversarial corpus. These are pass/fail gates; absence of known failures is not proof of universal security.
- **Execution:** 0 unapproved duplicate side effects in deliberately injected crash windows, with ambiguous external outcomes represented as `unknown`/`needs-reconciliation` rather than `success`.
- **Integrity:** 100% of claims marked `verified` have acceptable evidence receipts under the versioned scoring policy; mutated receipts and source hashes are rejected.
- **MCP:** ≥95% conformance pass on agreed deterministic fixtures, with all critical protocol/auth cases passing; unsupported optional capabilities explicitly declared.
- **Browser:** ≥95% fixture-task success in qualified environment and zero cross-profile cookies/session leaks; provider-specific reliability is separately reported.
- **Routing:** ≥90% class-level recall@3 on a versioned 40-task capability-matching golden suite initially, raised after baseline measurements; 0 policy-forbidden executable selections.
- **Reasoning:** report paired verified success, false positive, failure, token+money cost and latency against model-only, per task family; improvement is an empirical decision gate rather than an assumed result.
- **Durability:** every workflow node transitions through valid states; event replay and immutable artifacts survive abrupt worker termination.
- **Deployment:** fresh qualified host setup and rollback validated; unqualified hosts fail closed for dangerous operations.

Targets are proposed gates to calibrate against realistic task difficulty and reproducibility, not measurements of the current repository.

### Benchmark methodology

Freeze task and grader versions; report pass@1/pass@k, confidence intervals (paired/bootstrap or exact binomial as appropriate), false-verified rate, clarification/unknown rate, wall-clock time, input/output tokens, paid API cost, egress bytes, retry count, success per unit cost and unsafe-call counts. Compare only runs with the same evaluator and pinned task split. Track model/provider/temperature/seed when supported, context packet hash, tool versions, external service availability and host isolation state. Benchmarks with intentionally constructed patches are **orchestration tests** rather than estimates of reasoning quality.

## 8. Standard stage-gate report JSON schema

Store a stage outcome and a traceable link to reports under `docs/implementation/tool-integration/gates/`. Suggested form:

```json
{
  "stage_id": "T03.4",
  "source_commit": "<full sha>",
  "schema_revision": "tool-gate-v1",
  "host_qualification": "linux-bwrap-qualified",
  "state": "passed|failed|blocked|not-run",
  "checks": [
    {
      "name": "no-double-dispatch",
      "status": "passed",
      "evidence": "artifact://...",
      "suite": "integration"
    }
  ],
  "migration_revision": "<revision>",
  "policy_digest": "<sha256>",
  "evidence_manifest_sha256": "<sha256>",
  "blocking_defects": [],
  "authorizations": {
    "credentialed_live_tests": false
  }
}
```

A stage only passes when all mandatory checks ran and passed. An absent host prerequisite yields `blocked`, **never** `passed` or a silent skip. Gates are immutable once published; subsequent requalification creates a new report.

## 9. Pull-request and implementation slicing

Prefer **one PR per stage**, subdividing risky stages into narrow implementation PRs but preserving a single explicit acceptance gate. Each PR description references exactly one primary stage and can identify prerequisites. Commit generated adapters, tests and documentation together; freeze new interfaces before expanding provider sets. Run offline contracts first, then SQL+sandbox, then optional credentialed live runs. Do not merge implementation-specific features from concurrent chats without rebasing and re-running relevant gates on a single pinned commit.

**Recommended first 10 PRs:** T01.1 inventory; T01.2 semantic contracts; T01.3 envelopes and receipts; T01.4 CI gates; T02.1 MCP stdio; T02.3 CLI adapter; T03.1 authorization; T03.2 confinement; T03.3 broker integration; T03.4 gateway. T02.2/T02.4 can be developed concurrently in a separate branch after Type interfaces stabilize. No P05+ live integrations should bypass the P03 security gate.

## 10. Suggested file additions (additive to current Potbot layout)

```text
docs/implementation/tool-integration/00-master-plan.md
docs/implementation/tool-integration/architecture.md
docs/implementation/tool-integration/threat-model.md
docs/implementation/tool-integration/protocol-matrix.md
docs/implementation/tool-integration/gates/Txx.y.json
src/potbot/tools/adapters/{mcp,cli,http,browser,database,solver}/
src/potbot/tools/catalog/
src/potbot/tools/policy/                 # facade over existing authority
src/potbot/controller/tool_planner.py    # extend existing controller
tests/contract/tools/
tests/integration/tools/
tests/security/tools/
tests/benchmarks/tool_selection/
examples/tool-workflows/
```

This is a **proposed** additive layout. T01.1 must reconcile actual modules, avoiding new packages when existing ones already implement the function.

## 11. Registry evaluation and supply-chain admission checklist

- [ ] Official maintainer or independently verified publisher, package/build hash and license.
- [ ] Supported transport and exact protocol version, initialization/auth and renewal behavior.
- [ ] Complete set of tools/resources/prompts; typed input/output and side-effect classification.
- [ ] Network destinations, DNS/redirect behavior and sensitive data processing.
- [ ] Ability to run without privileged host filesystem or global package installation.
- [ ] Availability of local/offline test fixture; CI pinning and signed artifact provenance.
- [ ] Failure behavior, cancellation, limits, pagination, rate limit, retries, version upgrades.
- [ ] Independent evidence of quality or empirical benchmark on representative tasks.
- [ ] Kill switch, revocation method, audit trail and uninstall/rollback path.

## 12. Sources and living reference links

- [Potbot repository](https://github.com/kieransimkin/potbot)
- [Potbot Cog contracts](https://github.com/kieransimkin/potbot/blob/main/docs/cog-transformations.md)
- [Potbot security and Ember implementation](https://github.com/kieransimkin/potbot/blob/main/docs/agent-identity-and-ember-runtime.md)
- [Potbot workspace sandbox](https://github.com/kieransimkin/potbot/blob/main/docs/workspace-sandboxing.md)
- [Potbot jobs](https://github.com/kieransimkin/potbot/blob/main/docs/hosted-jobs.md)
- [Potbot Phase 6 benchmark scope](https://github.com/kieransimkin/potbot/blob/main/docs/phase-6-controller-benchmark.md)
- [MCP specification July 2026](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP transport authorization](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/index.mdx)
- [MCP Registry](https://registry.modelcontextprotocol.io/)
- [Official GitHub MCP Server](https://github.com/github/github-mcp-server)
- [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)
- [Z3 API](https://z3prover.github.io/api/html/)
- [Google OR-Tools](https://developers.google.com/optimization)
- [Lean theorem prover](https://lean-lang.org/)

## 13. Programme risk register and stage estimates

Estimates below are rough **engineering-week ranges for focused workstreams**, not delivery dates or promises. Several streams can run in parallel after the P03 security boundary has been qualified. Reuse/extension findings from T01.1 may lower effort significantly; credentialed vendor qualification and Windows/macOS isolation may increase it.

| Phase | Rough engineering weeks | Primary uncertainty | Mitigation |
|---|---:|---|---|
| P01 | 2–4 | Existing scheme collisions | Inventory-first and codeowner review |
| P02 | 3–6 | MCP protocol/version compatibility | Official SDK, fixture matrix and negotiated versions |
| P03 | 4–8 | Containment, credentials, crash/replay semantics | Fail closed, qualified Linux host, fault injection |
| P04 | 3–5 | Untrusted registry metadata and churn | Quarantine/discovery separation and pinned manifests |
| P05 | 3–6 | Search quota, parsing quality, rights | Multiple provider adapters and source-artifact hashes |
| P06 | 3–6 | Browser session isolation and flaky tasks | Local fixture site and browser profile partitioning |
| P07 | 3–5 | Language diversity and PR state races | Language profiles and immutable repository snapshots |
| P08 | 3–6 | RLS, provider schemas and external auth | Read-only defaults and generated contract tests |
| P09 | 4–7 | Formalization mistakes and solver timeouts | Typed results, independent checkers and unknown status |
| P10 | 4–8 | Durable side effects and scheduler integration | Reuse existing job/store and state machine testing |
| P11 | 3–6 | Evaluator bias and benchmark contamination | Blind hold-out, preregistered tasks and paired runs |
| P12 | 2–5 | Packaging and multi-platform qualification | Pinned install, explicit host support matrix |

| Risk | Severity | Trigger | Mandatory response |
|---|---|---|---|
| Unsafe MCP server or compromised package | Critical | Manifest adds scopes or executable binary changes | Quarantine instance, revoke install, run forensic receipt audit |
| Workspace escape / host access | Critical | Any successful boundary-crossing fixture | Stop release; fix isolation; rerun full adversarial suite |
| Private credential leakage | Critical | Token/private key appears in model context/log/artifact | Disable credential, rotate, incident-review, add regression |
| Duplicate non-idempotent action | Critical | Crash/restart repeats external write | Quarantine uncertain outcome; add reconciliation; fix state machine |
| Misleading benchmark improvement | High | Synthetic/live mixed or scorer changes | Invalidate comparison; rerun with frozen task+grader |
| Tool discovery overload | Medium | Planning latency/token use exceeds cap | Lazy tool metadata and cost-aware capability ranking |
| Unbounded provider costs | High | Retry/parallel plan exceeds budget | Enforce budget before reservation/dispatch; halt run |
| Drifting external APIs | Medium | Schema/endpoint differs from pinned contract | Quarantine adapter revision and require qualification |
| Concurrent changes in Potbot | High | Overlapping migrations/contracts/phase work | Rebase pinned SHA and execute upgrade/full CI |

**Suggested ownership:** one core semantics/DB owner, one execution/security owner, one integration/adapters owner, one evaluation/CI owner, and reviewers familiar with the existing Cog and identity architecture. External API cost and policy approval remains an operator responsibility. Resist adding a separate workflow scheduler or second authority engine simply for convenience.

## 14. Definition of done for the complete programme

The programme is complete only when: (a) all 48 stage-gate reports are passing or have explicitly approved documented scope reductions; (b) real qualified-host PostgreSQL, sandbox, API, browser, tool, reasoning and adversarial tests pass; (c) the agent autonomously discovers and composes authorized tools through typed cogs while preserving the boundaries between discovery, transformation, execution and verification; (d) benchmark data establishes real rather than assumed reasoning benefits; and (e) a reproducible tagged release with upgrade/rollback instructions and known limitations is published.

**Next executable task:** Implement `T01.1` on a fresh pinned repository branch: emit the current-state inventory JSON, produce a code-to-stage traceability map, identify missing/duplicate components, and add an automated consistency test. No production code changes should be required in that first PR.

