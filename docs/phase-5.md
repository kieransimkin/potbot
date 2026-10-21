# Phase 5 foundation: model contexts, proposals and durable inference

Version **0.5.0.dev1** implements the next independently testable model layer on
top of the delivered Phase 4 source. It is a development milestone within Phase
5, not completion of all live deployment qualification steps P041–P056.

## Relationship to concurrent work

The base is GitHub commit `88de7ee06ca985015527618bf8ca7ffb8fdf3755` plus the
previously delivered Phase 4 patch. GitHub was fetched again while preparing this
update. Other chats are evolving the semantic hierarchy, AI primitive standards
and private-keystore design. This update adds model modules and tests without
editing their registries, SQL migrations, embedding identities, credential store,
cryptographic broker, startup scripts or MCP transports.

Existing `syn://` and `synny://` resolution remains authoritative. Context source
types use full URLs and preserve explicit versions and qualification references.
Caller-supplied declarations are labelled `semantic_validation: syntax_only`;
this renderer does not claim the later semantic validation and packet work in
P197. No new competing semantic type hierarchy is installed. Logical response
protocol IDs such as `coding-result-v1` are not semantic type identifiers.

The private-keystore runtime and the shared provider disclosure policy are
separate outstanding work. This patch does not create keys, store provider
credentials, change the PostgreSQL credential flow, or contact a model provider.

## Implemented flow

1. The controller chooses an existing search node and its immutable knowledge
   view, then selects mandatory task, repository, environment and handoff data.
2. A recipient-specific renderer applies the exact selected tokenizer/template,
   counts the resulting input tokens and reserves output space. Supplied optional
   memory and its qualifications travel together or are omitted together.
3. The inference gateway freezes the request and reserves calls, tokens,
   inference milliseconds, monetary micro-units and a concurrency slot through
   the existing task ledger before dispatch.
4. Complete output is assembled and parsed against a strict versioned envelope.
   Each schema-repair attempt gets its own packet, reservation, raw output and
   invocation. Repair allowance is fixed by the session identity.
5. Controller admission checks model identities, installed tools, references,
   scope and conditions. Structural parsing alone never authorizes execution.
6. An accepted coding response becomes an existing, unverified `coding_results`
   record. The Phase 4 patch application and exact-state verification services
   retain responsibility for applying and checking it.

The built-in transport is deliberately named `potbot-offline-fixture`. It emits
supplied finite events and performs no model inference. Its deployment identity
must identify a Potbot fixture; it cannot impersonate one of the five default
models. Reported fixture usage must be synthetic and agree with the rendered
input count. This allows deterministic CI without weights, network or secrets.

## Bounded proposals and conditions

`domain/model_proposals.py` adds model-facing framing around the existing
`ActionProposal`; the durable action's serialization is unchanged. A frame
contains controller identity echoes, a bounded installed-tool subset, evidence
and criterion references, resource classes, local dependencies, prior action
dependencies, expected observations and focused information needs.

`admit_action_frame` checks these against controller-owned `ProposalAuthority`
and `ToolRegistry`. Cycles and unknown tools/references fail. The predicate
language has explicit flat fields and operators, with depth and node bounds;
it never evaluates Python or shell expressions. Evaluation returns satisfied,
unsatisfied or unknown. Unknown facts cannot authorize a proposal, and a blocked
prerequisite blocks the whole frame. This foundation returns admitted proposals;
automatic graph scheduling is later controller work.

`ModelCodingResponse` requires a patch or a classified inability, exclusively.
The controller checks its exact handoff, task revision, base, declared paths,
check references and evidence inventory. A question can be recorded as an open
information need with an insufficient-context result. The model cannot claim a
patch, verification or task completion merely by asking a question.

## Exact recipient contexts

`ContextPacketService.create` selects mandatory authoritative data from the
existing search node. Coding recipients require an existing immutable handoff
and include its scope, behavior, compatibility requirements, required checks,
reuse receipts and relevant source files. Required source paths can also be
specified explicitly. This phase accepts supplied memory bundles; persistent
memory retrieval is not implemented here.

`LocalTokenizerRenderer` uses an already supplied, pinned tokenizer. It applies
that tokenizer's exact chat template and encodes the result without adding
special tokens twice. Both Google profiles explicitly place controller
instructions in the first user turn. The fixture renderer has a distinct
synthetic tokenizer identity.

The retained packet manifest includes the exact wire text, token count and token
ID digest, template and tokenizer identities, output reservation, ordered source
digests, task/knowledge-view/base/environment identities, included memory and
omission reasons. **The backend receives `wire_text`, not the manifest JSON.**

Mandatory requirements and required qualifications are never truncated.
Mandatory overflow raises `context_overflow` before inference. Optional memory
is selected as whole bundles. Declared `artifact:<UUID>` qualifiers must resolve
to distinct qualification artifacts in the same bundle. Reads rehash actual
retained source bytes and revalidate these relationships before dispatch.

## Durable dispatch and repair

The gateway reuses `command_results`, `model_invocations`, `context_packets` and
the task budget ledger; this update adds no SQL migration. Separate journal
stages record the frozen operation, admission, possible dispatch, completion
and explicit reconciliation. A per-operation PostgreSQL advisory lock prevents
simultaneous workers from dispatching the same key. Reusing a key with different
inputs, allowances or repair policy fails.

Cancelling a queued operation releases capacity only when dispatch is definitely
absent. A committed dispatch marker without a completion is indeterminate;
restarting does not call the backend again. Lost terminal usage retains the
reservation until explicit reconciliation. A normal completed attempt releases
unused capacity and retains its actual usage, including overruns. Duplicate
completion replay settles once. Actual usage that exceeds the task allowance
exhausts that task.

Malformed output, duplicate JSON keys, trailing prose, Markdown fences,
unregistered fields, invalid Unicode, oversized structures and incomplete
streams cannot publish usable proposals. A complete malformed envelope can be
repaired zero to four times according to the configured policy. A repair packet
must retain the preceding invocation's actual raw output and identity. An
interrupted or uncertain call is never treated as a format-repair retry.

Byte and event bounds, terminal reasons, deadlines and cooperative stop outcomes
are recorded. Ordinary transport failures retain partial output. A process lost
before completion can retain only its durable dispatch marker; this version
does not journal each individual stream chunk. Hard cancellation of arbitrary
blocking model runtimes requires the future supervised transport. It is not
claimed by this synchronous finite fixture adapter.

## Coding result admission

`ModelCodingService.record` checks the complete gateway journal, current
rendered packet, immutable handoff, raw-output reparse and settled reservation
before publishing a result. It rejects handwritten invocation rows without
gateway admission, unrelated reservations, stale contracts, unauthorized paths,
unknown checks and unsupplied evidence. Questions and results are committed
atomically; replay does not duplicate them.

The resulting patch remains `verification_status: unverified` until the existing
repository pipeline establishes its applicability and behavior. The acceptance
suite exercises the complete path through real PostgreSQL and Linux isolation:
failing baseline, fixture generation, result admission, exact-base application,
passing targeted tests and a resolved criterion assessment. The original source
repository remains unchanged.

## Commands

The existing BAT, PowerShell and Bash launchers all dispatch these commands:

```powershell
.\start.ps1 models catalog
.\start.ps1 models schema --schema action-proposal-v1
.\start.ps1 models schema --schema coding-result-v1
.\start.ps1 models preflight --profile local-host.json --project-root .
.\start.ps1 models validate-response --schema coding-result-v1 --input response.json
```

Use `start.bat` or `bash ./start.sh` with the same arguments. These commands do
not require a PostgreSQL connection, a credential vault or downloaded weights.
`validate-response` establishes structural validity only. Preflight returns exit
code 2 for a blocked host profile and 0 only for `ready_for_load_check`, which
still does not mean loading or generation was tested. Unreadable or oversized
input files and response-validation failures use exit code 1. Invalid or incomplete
host-profile JSON produces a sanitized blocked report with exit code 2.

See [model-adapters.md](model-adapters.md) for the five exact defaults, host
profile format, resource checks and official model references.

## Progress and remaining work

| Plan step | Status in this update |
| --- | --- |
| P041 | Bounded model proposal contracts, installed-tool admission and tri-state conditions implemented. |
| P042 | Existing handoff-to-output adaptation and durable coding admission implemented. Automatic reasoner creation of new implementation contracts remains pending. |
| P043 | Task-aware recipient context rendering, exact accounting, persistence and supplied memory interfaces implemented. |
| P044 | Strict envelopes, capability negotiation helper and bounded separately recorded repair implemented for the offline adapter. |
| P045 | Atomic reservations and durable admission implemented for generation, repair and reviewer request purposes. |
| P046 | Pinned local host profile and read-only feasibility checks implemented. Supervised loading/connectivity qualification remains pending. |
| P047 | Shared outbound destination/disclosure policy remains pending and must precede real provider transport. |
| P048 | Offline bounded event assembly, lifecycle classification and conservative restart behavior implemented. A live supervised backend remains pending. |
| P049 | Idempotent settlement and explicit unresolved-usage reconciliation implemented for the fixture gateway. Live/provider, loading and maintenance accounting remain pending. |
| P050 | Deterministic tests and conformance requirements are present. Opt-in budgeted live qualification runner remains pending. |
| P051–P055 | Five model profiles and exact-tokenizer renderer are present; all five live qualifications remain pending. |
| P056 | Physical multi-model loading, eviction and resource scheduling remain pending. |

The implementation-status JSON records these distinctions. No catalogue profile,
fake transport result, skipped run or preflight result can set a deployment's
qualification flag. Fixed weights remain the baseline; ongoing training and
learning components belong to later phases.
