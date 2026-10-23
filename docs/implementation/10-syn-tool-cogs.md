# Tools are SYN Cogs

Every agent-callable tool operation must be implemented and registered as a **SYN Cog**, with exactly one registered input data Type and one registered output data Type. CLI, MCP, HTTP, DAP, ADB and vendor SDKs are implementation transports beneath that Cog interface. They are not parallel agent-facing tool registries. This is a mandatory architecture requirement for all 48 planned stages, including the language-neutral debugger and its first Python backend.

This is a planned extension of the existing Cog runtime. It does not claim that the current transformation-only registry can already dispatch arbitrary IO. Reuse the current contracts and sealed semantic-release process; add a reviewed controller-mediated execution path rather than making pure transformations silently effectful.

![Generated planning illustration of the shared typed and admitted core](media/tool-plan-qualification.svg)

Planning illustration. The shared core includes Cog contracts, admission and receipts; independent target qualification follows. No runtime video is supplied because these execution Cogs remain planned. Actual Cog-driven debugging capture is a mandatory Python-first deliverable.

## Existing source authority

Reviewed baseline: `37e5e23cf0f69fd7f1bfb1266a033994d950767d`, 23 October 2026.

- [CogSignature, TypedCogValue, CogRunContext and CogReceipt](../../src/potbot/cogs/contracts.py) define explicit `syn://cog/...` instances, full Type references, handler revision, binding hash, controller scope and typed data/receipts.
- [Sealed installed Cog registry](../../src/potbot/cogs/registry.py) validates installed handler bindings against a SYN snapshot. Never import executable code from arbitrary model/server metadata.
- [Built-in pure transformations](../../src/potbot/cogs/builtin.py) remain reusable; parsing, conversion and scope checking confer no execution authority.
- [Tool gateway](../../src/potbot/tools/gateway.py), existing typed tool contracts and durable job/execution stores own admission, effect dispatch and evidence. Extend their boundaries rather than creating a second scheduler, permission engine or event stream.

The current `CogSignature.binding_hash` hashes the declared binding, **not all executed source bytes**. Retain independent adapter/binary/source digests, semantic release identity and executor qualification; do not describe a binding hash as code attestation.

## Required declaration for each tool operation

| Declaration | Required detail |
| --- | --- |
| Cog identity | A distinct installed `syn://cog/<id>` instance under the authoritative `syn://type/cog` hierarchy, exact semantic snapshot/release and inherited required/default/optional Ember contracts. Resolve existing Types before proposing additions. |
| One input | One registered request envelope Type. Compound arguments, session reference, operation parameters, expected source/target revisions and resource limits belong inside that typed value. Credentials and controller authority are never supplied by a model-controlled envelope. |
| One output | One registered result envelope Type, validated after dispatch. Use typed variants for success, denied, unsupported, pending, cancelled, failed and indeterminate with bounded artifact/evidence references. A transport success is not semantic verification. |
| Binding | Installed handler reference, implementation revision, binding hash, package/source/binary digests, transport adapter configuration and host requirements. Reject snapshot/handler mismatch. |
| Effects | Pure transformation versus controller-mediated execution, requested effect/scope, idempotency class, target ownership, timeout, cancellation, redaction and retry/reconciliation policy. Mark execution Cogs as requiring controller scope. |
| Routing | Input/output Types and capability metadata support keyword/semantic discovery and typed workflow edges. An incompatible edge requires an explicit installed converter Cog; lossy conversion is declared and checked. |
| Evidence | Cog transformation receipt plus authorization/dispatch and execution receipts, unique child run/attempt ID, causal SYN event links, typed input/output hashes and independent verification status. Do not overload a transformation receipt as dispatch authority. |

The installed native handler namespace is currently constrained by `CogSignature`; place wrappers under the allowed `potbot.cogs.*` namespace or change that contract through an explicitly tested semantic/runtime migration. An arbitrary dotted Python path in a manifest must never become executable.

## Invocation lifecycle

1. **Discover and inspect the Cog.** Resolve its installed identity, single input/output signature, exact semantic release, handler binding, target profile and declared effects. Listing an MCP tool or CLI is only a candidate until an admitted Cog binding exists.
2. **Validate the typed input.** Pure Cogs may parse, normalize or construct a proposal. Validate representation, inheritance, units, resource bounds and source/target preconditions against the pinned catalog. Multiple arguments stay inside the one input envelope.
3. **Obtain controller authority.** The controller loads authoritative actor/workspace grants and exact target leases; performs scope, effect, budget and approval checks; then mints a fenced dispatch context out of band. A serialized request, returned `approved=true`, valid Cog receipt or inherited Ember cannot mint this context.
4. **Execute through the Cog handler.** The registered execution Cog delegates only to its installed admitted adapter/executor. Bind the invocation to immutable typed input, actual transport attempt, exact target and controller lease. No agent path directly invokes a raw transport. Denial produces zero adapter dispatches.
5. **Persist and validate output.** Correlate progress/console/stop/terminal updates through the existing canonical execution ledger. Bound/redact output, validate the result Type and preserve full authorized artifacts independently of truncated projections. Record both Cog and execution receipts with their distinct meanings.
6. **Verify and compose.** An independent verifier checks the claimed outcome where required. Feed the typed output to compatible Cogs; explicit receipt/causal links connect each edge. Cache only where source, identity, policy and declared effects permit; never replay an effectful Cog as a memoized transformation.

For asynchronous tools, the one result may be a registered pending-result variant with an opaque operation reference. A separately registered await/status/cancel Cog accepts that reference and returns its own typed result. Streaming progress remains a causally linked event/artifact stream; it does not silently change the Cog's one-input/one-output signature. New requests are re-admitted, fenced and budgeted.

## Stateful debugger, terminal and device Cogs

Each operation is a separate typed Cog. Persistent state lives in the controller-owned session service and durable receipts, not a hidden global variable in a supposedly pure handler. Output session references are opaque, tenant/workspace/target/lease scoped, expiring and revision bound; possession alone grants no authority. Never serialize a live Python frame, socket, credential, OS handle or unrestricted device shell into a Cog value.

Illustrative proposed data Type URIs below require resolution/registration against the real catalog; they are not claims of existing Types. Adapter options use a versioned typed extension inside the request, while the outer debugger contract stays language neutral.

| Cog operation | Single input Type | Single output Type | Mandatory binding |
| --- | --- | --- | --- |
| `debug.launch` / `debug.attach` | `syn://type/data/debug-session-request` | `syn://type/data/debug-session-result` | Exact runtime, source snapshot, process-start identity and admitted adapter; launch and attach are distinct installed Cogs. |
| `debug.set_breakpoints` / exception configuration | `syn://type/data/debug-breakpoint-request` | `syn://type/data/debug-breakpoint-result` | Current session, requested source/hash and adapter-verified locations; conditions may execute code. |
| `debug.step_over` / `step_into` / `step_out` / continue / pause | `syn://type/data/debug-control-request` | `syn://type/data/debug-control-result` | Separate installed Cogs; selected thread and expected stop revision, fresh observed stop/exit or explicit indeterminate result. |
| `debug.threads` / `stack` / `scopes` / `variables` | `syn://type/data/debug-inspection-request` | `syn://type/data/debug-inspection-result` | Separate installed Cogs; bounded paging and current session/thread/stop handles, redaction and target-code rendering policy. |
| `debug.evaluate` | `syn://type/data/debug-evaluation-request` | `syn://type/data/debug-evaluation-result` | Separate effect grant, bounded execution and no assumption of purity. |
| `debug.detach` / `terminate` | `syn://type/data/debug-close-request` | `syn://type/data/debug-close-result` | Distinct ownership/effect policy; detach preserves an attached process; independently observe final target/session state. |
| Phone control / profiling | `syn://type/data/device-operation-request` | `syn://type/data/device-operation-result` | Per-operation installed Cogs and typed extensions; exact device/app/process/trace identity, effect and qualified host. |

Request envelopes carry parameters and expected revisions; the trusted controller context carries authority. Debug state, adapter protocol sequence numbers, session/frame handles and SYN run/event identities are distinct. Continuing invalidates stop-scoped handles across **all** inspection Cogs. Underlying Python/debugpy support must not leak Python object layouts into the common signature.

## Mandatory tool-Cog work monitoring

Every invoked tool Cog, including pure transformations, nested Cogs and transport-backed operations, must log its **start, progress and completion statistics** through the [already specified Cog/agent work-monitoring system](05-syn-execution-monitoring.md), particularly sections 2, 3 and 7. This is C07. Reuse its canonical durable execution ledger, workspace cursor, live REST/WebSocket read models and operator tree. A private logger, adapter transcript, parent-only summary or optional telemetry exporter does not satisfy this requirement.

| Lifecycle occurrence | Required observations |
| --- | --- |
| Start | Persist the real start time, exact Cog Type/instance/semantic release/handler revision, unique run/attempt ID, typed input hash/private artifact, source/target/host, workspace, actor/emitter, controller lease and direct causal input/event. A denied proposal logs denial and zero dispatch, never a fictitious start. |
| Progress | Persist actual phase/status updates, measurable counts/units where available, bounded redacted console chunks and resource observations with sequence, timestamp, trigger and emitter. Include a real executing/result-received transition for a short operation; never invent percentages, intermediate samples or elapsed work. Long and paused operations emit bounded status/heartbeat updates. Each nested tool has its own lifecycle and run ID. |
| Completion statistics | Persist terminal status, trusted UTC start/finish, elapsed wall milliseconds with monotonic/reconstructed basis, CPU time, peak RAM, GPU/VRAM, disk/network IO, input/output tokens, provider/runtime time, cost and budget settlement as applicable. Every metric carries units and measured/provider-reported/estimated/unavailable provenance. Record all observed child tool run IDs, Types/versions, outcomes/receipt hashes and exact typed output artifact IDs/MIME/hashes with redacted previews. |
| Failure/cancel/unknown | Persist a classified failure, independently acknowledged cancellation or explicit indeterminate state with available statistics. A stop request is not observed cleanup. Reconciliation and later provider accounting append linked events; never rewrite prior immutable history or report a fabricated successful completion. |

Every event occurrence has its own event identity and the invocation's stable subject run ID, plus the actual trigger **and** emitter. Parent correlation alone is insufficient. Bind state transitions and authoritative dispatch to the same transaction/fencing rules already specified. Missing measurements are unavailable, never zero. Profiler overhead and dropped samples remain visible. Bound progress rate and output without silently dropping required durable transitions; monitoring failure before dispatch blocks dispatch, and loss after a possible effect requires reconciliation rather than a successful unlogged result or automatic retry.

**Monitoring acceptance:** a Cog-driven Python fixture exposes launch, breakpoint, step and inspection Cogs as distinct live children with start/progress/terminal events and independently checked wall-time/output statistics. A nested fixture, a pure conversion, denial, crash, timeout and cancellation repeat these assertions. Inject reused adapter request IDs, forged causes/emitters, secret output, stale leases, a slow stream consumer and lost telemetry export; durable Cog work records remain attributable and bounded, with exactly one reconciled terminal state per attempt and no duplicate effect. A terminal-only adapter receipt or a parent summary fails qualification. Optional OTel export is tested separately.

## Python-first Cog acceptance

The authored Python fixture must be driven end to end **through registered SYN Cogs**, not only by calling adapter/session methods. The existing direct adapter harness may establish backend behavior but cannot qualify Cog integration.

1. Install a reviewed semantic snapshot containing the proposed common request/result Types and operation Cogs, with pinned handler/source bindings and inherited Ember checks.
2. Use a controller-admitted launch Cog to start the pinned Python fixture; independently exercise attach/detach on a separately started owned process.
3. Invoke breakpoint Cogs, confirm verified lines, run to a real stop, and inspect two frames and their locals through inspection Cogs.
4. Execute over/into/out Cogs, await fresh stop results, reject the previous stack/value handles, continue to an exception and inspect its causal stack.
5. Cancel/detach/terminate under the correct ownership policy, verify cleanup and rerun the original test outside the debugger through its test-runner Cog.
6. Retain typed input/output and transformation/dispatch/execution receipts for every operation, causal SYN event chains, exact source/runtime/catalog hashes and actual screenshots/recording. State separately which backend, sandbox and Cog gates passed.

Negative cases must reject unregistered/shorthand Types, wrong input/output Type, invalid required/inherited Embers, unsealed or changed handler revision, forged controller context, broadened child grant, missing admission, cross-workspace/expired session, stale stop handles and direct raw-transport bypass. Denial must prove zero tool dispatch. Two compatible Cogs compose through a validated typed edge; an incompatible edge is rejected before execution. Reconciliation must preserve an indeterminate external effect without executing it twice.

## Implementation allocation

- **T01.1–T01.4:** inventory actual Cog/runtime boundaries, review and seal Types/Embers, add signatures/result variants/controller extension and contract gates.
- **T02.1–T02.4:** install transport adapters as implementations behind Cog handlers; no alternate agent-facing tool dispatch.
- **T03.1–T03.4:** controller-only authority, credential custody, fenced effect dispatch and separate transformation/execution receipts.
- **T04.1–T04.4:** discover, admit, install, pin and revoke Cog-bound tool definitions, preserving active-run signatures.
- **T05–T09:** implement every selected tool operation as a registered typed Cog, including all debugger/device operations and explicit converters.
- **T10.1–T10.3:** compose input/output Types, admit each effectful node, record per-Cog causal execution and bounded asynchronous continuation.
- **T11.1–T11.4:** mandatory real Cog-driven Python workflow, semantic/authority/bypass negatives and independent evidence checks.
- **T12.1–T12.4:** ship semantic snapshots with the matching implementation, operator Cog/session views, SDK templates and migration/release gates.

The gap register's `cog_contract` and each stage's `cog_requirements` make C01–C07 machine-checkable. C07 makes the existing work-monitoring specification mandatory for every tool Cog. A plan-consistency pass does not establish implementation of these Cogs.
