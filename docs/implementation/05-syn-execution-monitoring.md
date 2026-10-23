# T01.3 / T03.4 / T10.2 / T10.3 / T11.2 / T12.2 — typed live execution monitoring

**Status:** proposed implementation contract; **not implemented or qualified** by this planning patch.  
**Plan baseline:** Potbot `main` commit `8341c6de7f73898f9b85d435499654fb497dacae`, inspected 23 October 2026.  
**Primary requirement:** the REST API and replayable WebSocket stream must expose **every agent-processed execution**—agent/sub-agent jobs, workflows, models, all tools (including MCP sessions, MCP tool calls and MCP-composed workflows), Cogs, scripts, browser/API/DB/CI and internal jobs—and every `syn://event/...` occurrence must disclose **what process or exact input triggered it**. An event without a proven cause is invalid; never synthesize a false trigger.

## 1. Reuse the canonical SYN and Potbot boundaries

- A `syn://event/<unique-id>` is a distinct occurrence **instance**; it is *not* its semantic type. Each instance has `event_type_ref` resolved to an exact `syn://type/...` or `synny://type/...` and to a pinned semantic release and type/schema version. The subject being executed has its own exact `subject_type_ref`, implementation/provider version and immutable deployment/configuration digest. Use the registered native Event primitive and existing inheritance/Ember contracts; do not create an alternate type root.
- The current hierarchy contains `synny://type/workflow-execution` and `synny://type/inference-execution`. These are **candidate type placements**, not proof that a given workspace's sealed semantic release has them activated. Select or add narrower start/progress/terminal event types only by a separately reviewed release migration; do not silently register new types.
- Extend the existing workspace-ordered `job_events` replay/cursor, immutable `evidence_events`, artifact store, `JobService`, API/WebSocket and controller-owned run/invocation/lease/budget stores. Define one canonical event identity/ordering contract and projections for legacy job events, not a disconnected telemetry database. If a migration is required for general (non-job) workflow events, the migration must retain coherent workspace cursor ordering and backwards-compatible `/v1/jobs/{id}/events` consumption.
- An execution occurrence, its model/workflow/tool definition, its run record, and its immutable outputs are **different objects**. Emitting an event never itself authorizes tool execution, proves a model output correct, settles unknown spend or completes an information need.

## 2. Mandatory provenance on **every** event

Persist the following even for progress, stdout/stderr, tool-call, heartbeat/status transition and completion events (some may be private to authorized viewers):

| Field | Required meaning |
|---|---|
| `event_ref`, `event_id`, `event_type_ref`, `event_type_version`, `schema_version` | Unique `syn://event/<id>` identity and exact registered/pinned semantic type and immutable schema version. Never use the type URI as the event's instance identifier. |
| `workspace_id`, `task_id`, `correlation_id`, `causation_id` | Authenticated tenant/task scope and causal trace identity; cross-workspace references denied. |
| `subject.kind`, `subject.subject_type_ref`, `subject.version`, `subject.run_id` | Executed **agent job, workflow, model, MCP server/tool/workflow, ordinary tool, Cog, subprocess, API/browser/database/CI or other registered unit of work**, with precise type, published version or pinned deployment revision, and a distinct UUID for each run. Child calls carry `parent_run_id`, workflow step/node ID and provider/implementation fingerprint when relevant. |
| `trigger.kind`, `trigger.source_ref`, `trigger.source_run_id`, `trigger.input_artifact_id`, `trigger.input_sha256` | **Direct initiating cause**. Exactly one normalized causal kind (`user_input`, `api_request`, `schedule`, `webhook`, `parent_event`, `workflow_step`, `tool_result`, `provider_callback`, `process_output`, `lifecycle_transition`). The identity required by that kind must be present and verifiable. A user-initiated root links to its immutable input receipt; a child links to its parent event and run. Optional supporting causes form an explicitly ordered, bounded list. |
| `emitter.kind`, `emitter.process_run_id`, `emitter.actor_ref`, `emitter.lease_generation` | **Who produced the event**, distinct from the trigger. A status/process output event identifies the worker/model/tool run that emitted it; a scheduler/API-initiated event identifies the authenticated scheduler/request/principal. A fenced lease must be current for worker-origin events. |
| `occurred_at`, `recorded_at`, `sequence` | UTC wall timestamps and strictly commit-ordered, workspace-scoped replay sequence. Provider-supplied time is **claimed** source time; the server's trusted recorded time is authoritative for ordering. |
| `input`, `output`, `metrics`, `links`, `visibility` | Typed metadata/retained artifacts, explicit measurement units and source, references to external tool runs and corresponding receipts, and a redacted workspace-visible projection. Field presence depends on phase; causal fields never do. |

**Failure rules:** reject missing `trigger`, empty/unknown causal source, wrong-workspace or stale source refs, self-causation and cycles, ambiguous parent run, inconsistent artifact digests, unregistered/wrong-release type refs, and events from lost lease generations. An unavailable third-party model revision must be recorded as unavailable with its *actual selected model ID plus pinned deployment/policy digest*; never invent a revision. A denied admission may record an `execution.denied` audit occurrence with its real attempted input and actor, but must not create a fictitious `started` event.

**Direct trigger examples:**

- User/API start: immutable request or input event, authenticated principal, input artifact ID and SHA-256.
- Scheduled start: schedule/config revision, fire occurrence ID and scheduled UTC time, with scheduler identity.
- Workflow-induced model start: workflow run ID, step/node ID, preceding `syn://event/...` and pinned input artifact.
- Tool invocation: caller's model/workflow run ID, triggering event and exact invocation/authorization receipt.
- Progress/output: producer's process/model/tool run ID, callback/stream update identity (or bounded sequence) and causal start/update event.
- Completion/failure: current run/lease state transition and the event/receipt causing settlement; original root input remains accessible through the ancestral chain.

## 3. Mandatory lifecycle and telemetry

For every actual run, publish durable occurrences at the appropriate transitions:

1. **Started:** `subject` exact type, implementation/deployment/model ID and version, unique run ID, parent run and workflow node, the **triggering input/process**, immutable typed input parameters/artifact SHA-256 (private original + authorized redacted projection), scope/authorization/policy revision, worker/deployment, start UTC time and counters initially unknown/not measured.
2. **Updated:** stage, bounded progress `{completed,total,unit}` or percent **only if measurable**, safe console/stdout/stderr segments with stream name/chunk sequence, heartbeats, resource snapshots, parent/child linkage, measured/estimated/unknown provenance, and the exact triggering callback, event or process. Preserve ordering and redaction before persistence; external text is untrusted evidence.
3. **Tool invoked/returned:** external tool's own unique run ID, pinned tool Type/implementation version, caller run ID, input digest, permission/lease IDs and its result/receipt/unknown state. Each nested tool call is independently traceable in its own typed start/progress/terminal events.
4. **Completed:** start/finish UTC timestamps and **elapsed clock/wall time** in milliseconds with clock basis; process CPU time, peak RSS, GPU compute/memory when supported, disk/network bytes, input/output tokens, inference time, financial cost/budget settlement, external tools called (type/version/run ID/receipt/outcome), typed output artifact IDs/MIME/SHA-256 plus a bounded redacted preview, state and verification/assessment separately. Unknown/unavailable values are explicit, never silently zero.
5. **Failed/cancelled/indeterminate/reconciled:** typed terminal or reconciliation events with a safe classified reason and causal trigger, but no fabricated successful completion or resource usage. Cancelling is an intent until the executor confirms stopping; an indeterminate external write remains unreplayed until authorized reconciliation.

Use monotonic timing within a surviving process; always also store trusted UTC start/end for cross-process reporting. After a crash, label UTC-derived elapsed time as reconstructed/estimated, not directly measured monotonic time. Record units and measurement provenance for every metric. Never conflate token/cost estimates with settled provider usage.

### Illustrative model start event (not a registered live payload)

```json
{
  "schema_version": 1,
  "event_ref": "syn://event/7c77e83e-4d2b-40e8-ac70-37db0c73d7c5",
  "event_type_ref": "synny://type/inference-execution",
  "event_type_version": "1.0.0",
  "semantic_release_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "phase": "started",
  "workspace_id": "d71530a8-d064-4518-b151-53de7711dbbd",
  "task_id": "3f89ff7f-68a9-4139-9e9b-0b18cf87928e",
  "correlation_id": "fbf2c6d0-938d-48c9-8f9b-a7f198b1c687",
  "causation_id": "07cba03a-1494-4226-bb3d-d0639f8ebed5",
  "subject": {
    "kind": "model", "subject_type_ref": "syn://type/machine-learning-model",
    "version": "operator-pinned-provider-model-revision", "run_id": "89e78c16-ad22-419d-bc0c-0c4fed185152",
    "parent_run_id": "3a576d18-1e75-42e9-9546-d449d01e811e", "workflow_node_id": "reasoner-02",
    "deployment_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "trigger": {
    "kind": "workflow_step", "source_ref": "syn://event/07cba03a-1494-4226-bb3d-d0639f8ebed5",
    "source_run_id": "3a576d18-1e75-42e9-9546-d449d01e811e",
    "input_artifact_id": "e1bd1286-869d-46db-8032-12d164b2fd5f",
    "input_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "emitter": {"kind": "controller", "process_run_id": "3a576d18-1e75-42e9-9546-d449d01e811e", "actor_ref": "syn://thing/potbot-controller", "lease_generation": 3},
  "input": {"type_ref": "syn://type/record", "parameters_redacted": {"temperature": 0.2}, "private_artifact_id": "e1bd1286-869d-46db-8032-12d164b2fd5f"},
  "occurred_at": "2026-10-23T00:00:00.000Z",
  "recorded_at": "2026-10-23T00:00:00.003Z",
  "sequence": 42
}
```

### Illustrative completion payload (specific child model execution)

The *terminal* event has a new `syn://event/...` identity but the **same subject run ID**. It carries its own `trigger` (the genuine run-state transition, with the triggering event/receipt) and the same mandatory emitter, scope, semantic release, timestamp and cursor fields as the start event. Its phase-specific data contains:

```json
{
  "phase": "completed",
  "run_id": "89e78c16-ad22-419d-bc0c-0c4fed185152",
  "trigger": {
    "kind": "lifecycle_transition",
    "source_ref": "syn://event/7c77e83e-4d2b-40e8-ac70-37db0c73d7c5",
    "source_run_id": "89e78c16-ad22-419d-bc0c-0c4fed185152",
    "input_artifact_id": "e1bd1286-869d-46db-8032-12d164b2fd5f",
    "input_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "started_at": "2026-10-23T00:00:00.000Z",
  "finished_at": "2026-10-23T00:00:03.200Z",
  "wall_elapsed_ms": 3200,
  "wall_elapsed_basis": "measured_monotonic",
  "resources": {
    "cpu_ms": {"value": 73, "basis": "measured"},
    "peak_rss_bytes": {"value": 12976128, "basis": "measured"},
    "gpu_ms": {"value": null, "basis": "unavailable"},
    "input_tokens": {"value": 540, "basis": "provider_reported"},
    "output_tokens": {"value": 214, "basis": "provider_reported"},
    "money_microunits": {"value": 500, "basis": "settled"}
  },
  "external_tool_runs": [
    {
      "run_id": "f314f34a-96fd-45ba-964d-f127c62bf087",
      "type_ref": "syn://type/tool",
      "version": "operator-pinned-version",
      "receipt_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "outcome": "succeeded"
    }
  ],
  "output": {
    "artifact_id": "39abbbfd-b403-41d6-a008-5c0ca543d74a",
    "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "media_type": "application/json"
  }
}
```

This is a *phase-specific excerpt*, not a complete event: the full canonical envelope and authenticated causal/emitter references are still required. Numeric amounts are illustrative, **not** observed Potbot or provider data.

The illustrative version and hashes are placeholders; production must use resolved/pinned values and the selected release's Type definitions. A start event's input parameters must be accessible to an appropriately authorized operator via the private artifact API, while public/job-wide event projections must redact credentials, secrets, sensitive prompts and unapproved content.

## 4. REST / WebSocket contract and read models

Extend (do not replace) the existing authenticated, workspace-scoped `JobService`/`/v1/jobs` REST/WebSocket infrastructure and its commit-ordered cursor with **proposed** interfaces:

| API | Required function |
|---|---|
| `GET /v1/executions?state=running&kind=workflow` | Paginated, filtered active executions, with type/version, run ID, parent, start UTC, elapsed wall-clock and progress. |
| `GET /v1/executions/{run_id}` | Typed run detail including immutable input/outputs, current state, resource/cost summary and causation chain. |
| `GET /v1/executions/{run_id}/events?after=<cursor>` | Durable replay of every event of this run (and optional descendant filter), preserving gap-safe workspace cursor semantics. |
| `GET /v1/executions/{run_id}/tree` | Parent-child DAG: workflow → steps → models → tools, event-to-event causal links, current/terminal state and costs. |
| `GET /v1/events/{event_id}/cause` | Verified initiating process/input, direct and bounded ancestral causes, source run/event IDs and permission-filtered artifact details. |
| `WS /v1/executions/stream` and `WS /v1/executions/{run_id}/stream` | Authenticated, resumable live event frames with stable run/event identity and replay cursor; preserve existing `/v1/jobs/stream` clients and event compatibility. |

The dashboard should expose an interactive live tree/timeline, workflow state, nested model/tool run states, exact Type/version, input/cause, progress, bounded console output, resource/time/cost meters, final outputs, and drill-down to each triggering event or input artifact. Read-only viewers may inspect authorized redacted projections but cannot approve, cancel or execute by sending an event. Filter by workspace/task/run/type/time and use bounded paging, back-pressure, reconnection and client deduplication. All reads enforce PostgreSQL RLS and authorization. Use immutable event and result receipts; do not infer output truth from an event labelled `completed`.

## 5. Mapping onto the existing 48-stage plan

- **T01.3:** stabilize typed `syn://event/...`/versioned event envelopes, mandatory direct trigger and emitter, nested process and input artifact identities, redaction, and immutable canonical hashes before adding transports.
- **T03.4:** transactionally emit admitted start, progress/tool-dispatch/terminal and denial records under the actual fenced worker/approval scope. Never permit untrusted callers to spoof a trigger or use an event as authorization.
- **T10.2:** connect workflow DAG nodes, model calls, tools, subtasks and nested events; survive restarts using the existing event cursor and no-repeat side effect semantics.
- **T10.3:** instrument each selected local/hosted model and independent critic call with its model Type, provider/model/deployment revision, unique invocation/run ID, triggering step/context packet and measured accounting.
- **T11.2:** verify causal DAG integrity and source-artifact hashes; reject fabricated parentage, orphan completion, missing/untrusted measurements, duplicate or cross-workspace refs.
- **T12.2:** implement live operator REST/WebSocket list/detail/tree/events/cause endpoints and dashboard projections, with strict auth, source redaction and backpressure.

The programme remains **48 stages**: these are mandatory additions to existing deliverables and tests, not declarations that production event telemetry exists yet.

## 6. Testable acceptance criteria / negative cases

1. **Event completeness:** 100% of persisted agent, workflow, model, MCP session/tool/workflow, Cog, adapter, internal-job, CLI/process, browser/API/database/CI start, update, output, tool-invocation and terminal events carry a valid `syn://event` instance ref, pinned registered event Type/version, unique run ID, emitter and **authenticated direct trigger**. No orphan event can appear in the live feed.
2. **Root / child causation:** user/API/scheduled roots point to a real input/request/schedule occurrence; child model/tool runs point to the originating workflow/process event and exact input digest. Both direct and full ancestry can be resolved, with bounded cycle detection.
3. **Crash/replay:** kill workers at start, progress, nested tool return, terminal commit and after network send. Replay yields the same event IDs and payload digests in commit order, no duplicates/phantom completion and no repeated unapproved side effects. Slow subscribers reconnect from their last acknowledged cursor.
4. **Timestamp/accounting integrity:** duration and resource units are explicit and tagged measured/estimated/unavailable; provider usage can reconcile later without rewriting append-only records. Tool call identifiers, output artifact digests and immutable receipts match persisted records.
5. **Privacy/security:** unauthorized subscribers never receive raw prompts, credentials, arbitrary console secrets or cross-workspace runs; stale lease, wrong type/release, forged parent event, wrong input hash and unscoped tool result all fail closed.
6. **UI/API parity:** running/finished state, progress, elapsed clock and parent/child links match API, WebSocket, evidence store and dashboard on concurrent workflows. Completion status does not imply verified implementation quality.

**Implementation order:** schema/semantic release contracts and integration migrations → controller event emitter and model/tool lifecycle hooks → transactional capture/receipt validation → REST/WebSocket read model → dashboard → interruption/chaos, RLS and security qualification. No unqualified external execution or model API access is introduced by this planning update.

## 7. Universal agent-work event coverage — mandatory scope expansion (v2)

**Scope expansion source baseline:** Potbot commit `5c3aa450d98b313d2a532d63d48819eebf334f4d`. This section broadens the earlier illustrative workflow/model/tool examples. It is a mandatory **planned** capability, not a claim that the live API or event producer already exists. Every actual unit of work performed, dispatched, scheduled or supervised by an agent participates in **the same typed, causally attributable execution ledger**, including agent-internal work that never invokes an external service.

### 7.1 First-class execution subjects

`subject.kind` is a **versioned, extensible classification** and must not be closed to the three example values `workflow`, `model` and `tool`. Independently of this label, **every event requires the exact registered `subject_type_ref`, pinned semantic release/type version, immutable implementation/configuration identity (when available), stable subject `run_id` and precise triggering input/process**. Categories include:

| Executed unit | Lifecycle / correlation details required |
|---|---|
| Agent and sub-agent runs | Parent agent/request ID; agent implementation, identity/policy and run ID; delegation, handoff, internal decision jobs and child-worker launches |
| Workflow and DAG jobs | Workflow definition digest/version, workflow run ID, step/node run ID, enqueue/dependency/approval trigger, fan-out/fan-in parents, retry/reconciliation attempts |
| Hosted/local model calls | Provider/model identifier and selected revision/deployment digest, inference invocation ID, model-stream chunk IDs, token/compute measurements and typed outputs |
| **MCP server/process sessions** | Approved server installation ID and executable/endpoint digest, transport (`stdio` or Streamable HTTP), negotiated protocol version, MCP session ID, process run ID, connected/disconnected/failure state |
| **MCP operations and MCP-composed workflows** | Tool/resource/prompt name and declared input/output Types, pinned discovery schema fingerprint, caller run ID, MCP JSON-RPC request ID, MCP session/server identity, unique **local** invocation run ID, server progress/notifications and returned artifacts. Model tool-calling through MCP must retain the complete outer-to-inner chain |
| Local process, CLI, scripts and tool adapters | Executable/version hash, arguments' private digest, sandbox identity, process PID/lease if trusted, start/output/exit/signal and subprocess descendants |
| Web/browser/retrieval/API/DB/tool connectors | Registered capability and operation, browser/action/HTTP/gRPC/SQL request identifier, provider attempt/response metadata, network policy, non-idempotent effect reservation, results and receipts |
| Pure Cogs and internal agent processing | Cog type, input/output Type, implementation hash, transformation invocation ID and parent dataflow node; include validation, scoring, reasoning routing, planning, artifact transformations, indexing and memory admission processing |
| Queue/maintenance/scheduled jobs and CI tasks | Durable queue/job ID, handler version, scheduler fire/event ID, service identity, cron/config revision, build/test run ID, work item/effect class, cancellation and retries |
| Other/future job kinds | Require an independently registered exact SYN subject Type/version plus the same lifecycle, causality, emitter, and outcome contract; unsupported kinds are not exempt from event emission |

**Scope rule:** Track **every admitted attempt**, including early rejections (`denied` events attributed to the attempted request), queued/blocked/awaiting-approval work, no-op or cache-hit completions, internal calls, repeated/reconciled attempts and unsuccessful executions. Discovery and MCP listing are tracked when performed as work; merely *having* a registry entry creates no fictitious execution event. A tool initiated by a model, an MCP server invoking a downstream adapter, or an agent spawning another agent creates a **distinct child run**, never silently folded into its parent.

### 7.2 Identity, direct cause and ancestry — for **every event**

- One `syn://event/<uuid>` **occurrence per emitted event**, with exact registered `event_type_ref`, `event_type_version`, immutable payload/hash and a workspace-ordered replay cursor. The `subject.run_id` is a separate UUID and stays constant for that invocation across started/progress/output/terminal events; each child invocation gets its **own** run ID.
- Every event has **both** `trigger` (what actually caused this event: a verified input, scheduler dispatch, parent event, workflow node, callback/stream chunk, tool result, state transition, etc.) **and** `emitter` (which authenticated controller, agent, worker, MCP server or process produced the occurrence). They are never implicitly the same field. A progress/stdout update identifies its exact process/stream/chunk sequence as well as the parent invocation.
- Root agent/API/scheduled jobs point to an authenticated initiating request/schedule fire and **immutable input receipt with content digest**; even service maintenance must identify its configured triggering schedule/queue or system maintenance request. An absent user task is represented as an authorized service-scoped root, not as an invented task ID or unidentified event.
- Each child execution identifies its `parent_run_id`, `parent_event_ref`, initiating operation/workflow node, and immutable input artifact/type/hash. Nested MCP sequences additionally identify their `server_run_id`, `session_id`, MCP method/tool name, MCP request/notification correlation (when supplied), and **locally minted unique invocation ID**. Never rely on remote, untrusted request IDs as sole identity.
- Fan-in work records one actual **dispatch trigger** plus an ordered list of all contributing upstream input event/run/artifact references. It must not invent a unique sole parent if multiple inputs participate. Graph edges must be validated for type, scope, cycles, current lease, and input digest.
- For a call that causes a downstream job, the downstream start/queued event points back to the initiating tool/model/agent event, and the upstream completion links to every observed child call (`child_run_id`, `kind`, exact Type/version, receipt and status). Unknown remote child outcomes remain indeterminate, not successful by inference.
- Preserve immutable cause chains even if a workflow is cancelled/retried or a worker crashes. Different attempts receive distinct attempt/run identities, link to the authorized retry/reconciliation event and never replay ambiguous non-idempotent effects automatically.

### 7.3 Canonical lifecycle / terminal measurement contract

**Lifecycle** — `queued` / `admission_denied` / `started` / `progress` / `console_output` / `child_dispatched` / `waiting` / `completed` / `failed` / `cancelled` / `indeterminate`; registered narrow Event Types may add statuses while preserving a versioned normalization. No synthetic `started` or `completed` record may be created for work that did not actually execute or whose result is unknown.

**Start event** — precise SYN subject/event Types and versions, run/attempt/parent IDs, initiator and emitter, pinned implementation or selected model/tool version, immutable typed **input parameters** (secured private artifact plus a redacted projection), budget/policy/authorization identity, UTC start timestamp and stage.

**Update event** — unique occurrence ID, stable run ID, direct causal event/stream chunk, stage/progress, optionally bounded stdout/stderr, telemetry/heartbeats, tool callback links, sequence and last observed resource sample. Retain output chunks as redacted bounded events or referenced artifacts; backpressure must never silently erase required durable state transitions.

**Terminal event** — known terminal result/status; start and finish UTC; monotonic elapsed wall clock **with measurement basis**; peak/total CPU time, RAM/GPU/VRAM, tokens, I/O, network, cost and sandbox/runtime consumption as measured, provider-reported, estimated or unavailable (never fabricated zero); all observed subordinate/external tool/MCP/model runs with **their unique run IDs, Types/versions, outcome and receipt hash**; precise typed output artifact refs, hashes and visibility rules. Provider accounting reconciliation creates another linked event rather than rewriting prior immutable events.

### 7.4 MCP end-to-end example: workflow → model → MCP → tools

Illustrative graph (example Type URIs must be resolved against the activated sealed SYN release):

```text
user/API input receipt                       ← authenticated root trigger
  └─ agent job run A  [queued, started, ...]
       └─ workflow run W [node=investigate; typed input hash]
            ├─ model invocation M [tool-call request and stream]
            │    └─ MCP tool invocation T [server run S, session, method, request ID]
            │         ├─ MCP progress/console/output events [T, S, stream chunk, callback]
            │         ├─ downstream API/DB/tool invocation U [distinct child run]
            │         └─ MCP result/terminal event [T; output/usage/child receipts]
            ├─ Cog transform C [input T artifact → output typed artifact]
            ├─ sub-agent run N [input C; independent worker/lease]
            └─ workflow completion W [duration, resources, children M/T/U/C/N]
```

The live API must support this complete nested event/causation tree **without requiring clients to know the job's implementation protocol in advance**. MCP workflows may themselves contain multiple sequential or parallel tool calls; each has its own run lifecycle. All MCP operations need method/transport/server metadata; any data or progress **pushed** by an untrusted MCP server is recorded as attributed source data, not forged controller observations or authorization.

### 7.5 Storage, streams, permissions and migration implications

- Extend the existing **authenticated workspace-scoped** API and durable cursor, **not** a second unrelated event system. The existing `job_events` table requires a `job_id` FK and cannot directly represent all standalone agent/Cog/MCP/process events. The implementation must explicitly design an **additive execution-event record/projection/migration**, with one transactional workspace-ordered cursor across all job and execution event reads; legacy `/v1/jobs` routes must continue working.
- Proposed `GET /v1/executions`, `/v1/executions/{run_id}`, `/v1/executions/{run_id}/tree`, `/v1/executions/{run_id}/events`, `/v1/events/{event_id}/cause` and `WS /v1/executions/stream` must include agent, MCP, tool, Cog, workflow, model, and service job kinds with filtering by Type/version, provider, run ancestry, task, time and status; paginate and replay after reconnect without cursor gaps.
- Keep private parameters, prompts, tool outputs and console data in policy-governed artifact storage. **Redact before publishing** event projections. Make provider telemetry clearly marked claimed vs observed. Enforce RLS, per-subscriber authorization and read-only event streaming. Events and MCP descriptions confer **no** ability to execute a tool or approve a write.
- Root/child/return relationships must remain consistent through concurrent dispatch, nested remote MCP sessions, retries and failover. Preserve immutable ledger history and signed/hashed evidence; reject unauthorized, cross-workspace, stale-fence, self-causal, orphaned or malformed events, even if a transport message superficially succeeds.

### 7.6 Mandatory cross-protocol acceptance matrix

1. **Coverage:** run representative fixtures for all of: agent/sub-agent, workflow with fan-out/fan-in, local and hosted model, MCP stdio and MCP Streamable HTTP tool workflow, MCP resource/prompt reads, local CLI/process, Cog transformation, HTTP/GraphQL/gRPC/SQL/browser, queue/scheduled job, maintenance/CI. Verify **100% of admitted work** has resolvable typed start/outcome events and each emitted update has a direct cause and emitter.
2. **MCP identity:** correlate MCP session/server process, JSON-RPC request IDs, local run IDs, nested tool calls and server progress callbacks without treating untrusted remote IDs as authenticated authority. A reused remote request ID in another session must not cause run collision; untrusted server events cannot invent their parentage.
3. **Nested replay:** restart API, workflow controller and MCP subprocesses mid-run. Replay the exact committed hierarchy/causal graph once, with no duplicate terminal states, dropped progress beyond bounded/acknowledged retention, or repeated side effects.
4. **Provenance integrity:** reject mismatched input artifact digest, missing root initiating input/schedule, unknown or stale parent run, invalid SYN type/version, cross-workspace child, forged emitter/lease and cycles (including fan-in). Each unit, even pure Cogs, has unique attempt identity.
5. **Usage and results:** terminal receipts enumerate child tool/model/MCP run IDs and output hashes; measured/estimated/unavailable units remain distinct. Unobserved remote completion, resource amounts and provider revisions are not invented. Reconciliation is explicit.
6. **Access and operators:** assert that list/tree/cause/stream queries expose all permitted execution kinds to an authorized operator and no foreign workspace or unredacted sensitive data to another client; one stable cursor works across job-only and general execution events. Preserve legacy consumers and request/response contracts.

**Implementation ownership:** T01.3 (semantic envelope), T02.1–T02.4 (MCP/local/HTTP/gRPC instrumentation), T03.4 (gateway/dispatch event admission), T10.1 (typed graph links), T10.2 (durable orchestration), T10.3 (model and sub-agent calls), T11.2 (causal and receipt verification), T12.2 (REST/WebSocket/dashboard). This scope does **not** change the existing 48-stage programme or assert that those stages are already implemented.
