# Syn integration changes to the Potbot implementation plans

Review date: **23 October 2026**. This is a planning amendment to the existing
programme. It does not implement the proposed relay, inbox, federation or security
work, advance stage completion, or qualify the integration for production.

Subsequent implementation: [scoped credentials and CI access](08-syn-scoped-credentials.md)
records the first completed code increment. The review below retains its original
source baselines; this is not a claim that all proposed gates are now complete.
The [architecture](09-syn-potbot-architecture.md) and
[exchange contract increment](10-syn-exchange-contracts.md) add manually approved
metadata summaries, preview and explicit legacy identity migration. Full
semantic/producer admission, automatic delivery and receiving remain pending.

**Recommendation:** use Syn as Potbot's communication and externally checkable
receipt service. Keep Potbot's task decisions and durable execution transactions
under their existing owners. Build reliable event delivery before remote-agent
dispatch, and settle data-retention rules before exporting memory or source data.

![Proposed Potbot event commit, Syn delivery and incoming-message admission boundaries](../media/syn-plan-boundaries.svg)

*Generated planning illustration. Solid boxes identify existing components;
dashed boxes identify proposed integration work. It is not a screenshot or a
demonstration of an implemented relay or inbox. The current HTTP fixture is in
the [service guide](../syn-service.md). A recording of these proposed components
would not establish implementation; capture one when their acceptance runs exist.*

## Plans and source precedence

There are two different programmes, with different meanings for their phase
numbers. Neither should be renumbered to accommodate Syn:

| Source | Meaning and treatment |
| --- | --- |
| [Implementation Plan 1.1](../Multi_Model_Coding_Agent_Implementation_Plan%20%281%29.docx) | **204 steps, P001–P204, 15 phases**. It preserves the 153 original step IDs and adds semantic, embedding and MCP work. Use its dependency order rather than numeric ID order. |
| [Implementation Plan 1.0](../Multi_Model_Coding_Agent_Implementation_Plan.docx) | Historical 153-step predecessor, not a second independent backlog. Its completed foundations are extended rather than reopened. |
| [Architecture specification](../Multi_Model_Coding_Agent_Architecture_Specification.docx) | Potbot owns authorization, memory admission, budgets, evidence and verification. Its transaction/outbox and remote-effect recovery rules also apply to Syn. |
| [Semantic addendum](../Agent_Semantic_Vocabulary_and_Semantics_v0_1.docx) | Immutable semantic releases, exact identity/coverage, source lineage and separate admission remain required. A transport receipt cannot replace them. |
| [Tool master plan](../potbot-tool-mcp-implementation-bundle/potbot-universal-tool-mcp-implementation-plan.md) | **48 stages, T01.1–T12.4**. Its Phase 7 means repository/CI engineering; the original plan's Phase 7 means active investigation. Use explicit P/T identifiers. |
| [Phase 6 controller](../phase-6-controller-benchmark.md), [Phase 7 increment](../phase-7-increment-5.md), [current closure](../phase7-closure-status.md) | Existing graph/research code and remaining qualification gates. Their historical observations remain historical; Syn delivery does not complete those features. |
| [Execution monitoring](05-syn-execution-monitoring.md) and [initial event ledger](06-t013-event-ledger.md) | The canonical causal-event requirements and current migration/API foundation. Reconcile the stale statement that read endpoints are still missing. |

Reviewed integration baselines: Potbot `833612ba8b60df6b0cc86e29b9d5ad3ff4d91aad`,
Syn `57da1be8ee69e00375bb2756bb09839bf0d4bc06`; fetched Potbot `main` remains
`e2f7b2e423ceb7360df76b18b076de09bf39936a`. These are source baselines, not release
certificates. [Potbot PR #1](https://github.com/kieransimkin/potbot/pull/1) and
[Syn PR #10](https://github.com/Synsphere/syn/pull/10) are drafts.

The unmerged [plugin discovery strategy branch](https://github.com/kieransimkin/potbot/tree/f015276b22eab6a29339928550653251352bdc24)
was also reviewed. It changes the master plan and the JSON/CSV tracker rows for
T04.1–T04.4. Preserve its provenance, ownership-neutral ranking, admission and
qualification requirements when merging. This amendment adds separate sections
and an [additive machine-readable impact map](../potbot-tool-mcp-implementation-bundle/syn-integration-impact.json)
instead of rewriting those concurrent tracker rows. All 48 existing IDs,
prerequisites and recorded statuses remain unchanged.

## What the current integration supplies

The submodule and lightweight client supply authenticated HTTP messages,
workspace-ordered replay, idempotent message IDs and hash-chain receipts. Potbot
has configured message/head REST forwarding and an explicit `sync-events` CLI
that exports one page of approved execution metadata summaries. The original
full-projection adapter is superseded by the [v1 contract](10-syn-exchange-contracts.md),
which has explicit destination approval, preview and a separate delivery identity.

There is **no automatic delivery worker, receiving-agent inbox, distributed
scheduler, consensus, signed message authorship or libp2p message replication**.
The Syn node's peer discovery does not implement those protocols. Syn's
workspace token authenticates access to a whole workspace; `sender` is a client
assertion, and channels do not create private per-user mailboxes.

Syn receipts establish storage/integrity relative to the selected service and
anchor. They do not establish a truthful event, an authorized action, a verified
patch, independent corroboration, memory acceptance or provider completion.

## Ownership after integration

| Concern | Owner and planned boundary |
| --- | --- |
| Task state, contract revisions, approvals, budgets, leases and candidates | Potbot's controller and restricted PostgreSQL transactions. Syn messages may propose work; they cannot mutate these owners directly. |
| Causal execution events and UI replay | Potbot's `ExecutionEventService`, migration 19 and shared `job_event_cursors`. Keep existing REST/WebSocket clients. |
| Shared messages and receipt-chain append/replay | Syn service and its own migration `20261023_0012`; reuse `syn-client` for protocol/verification rather than copying it into Potbot adapters. |
| Reliable export/import | Proposed Potbot outbox relay and admitted inbox, using existing fencing/idempotent-command primitives. No second scheduler is introduced through Syn's separate `/jobs` API. |
| Sealed Types, Cog bindings and semantic validation | Potbot's selected immutable semantic release and admission service. Syn's mutable Type API can be an explicitly imported source; receipt validation is not semantic validation. |
| Agent signatures and secret custody | Existing Potbot Agent/Ember broker. A communication token, `sender` field or Syn Credential record is not that signature or a grant. |
| Evidence, private artifacts, memory, vectors and deletion | Existing Potbot stores, visibility rules, derivation lineage and deletion owners. Export only the permitted, minimized projection. |
| Peer protocols and any future consensus | Syn project. Require a separately reviewed threat model, identities, protocol and qualification work before Potbot relies on them. |

Potbot's numeric migrations and Syn's Alembic revisions remain independent. Use
separate service roles/schema ownership even if they share a PostgreSQL host.
Do not replace Potbot RLS with a workspace-wide Syn bearer token or let either
service apply the other's migrations at startup.

## Changes to the original 204-step plan

These are **extensions** of the named steps; completed historical work keeps its
recorded status. No P205 or new phase is being invented by this review.

| Original steps | Required change |
| --- | --- |
| P010–P011, P026, P034, P060 | Add a fenced Syn delivery handler behind the existing outbox pattern. Commit the local event and delivery intent together; send after commit; reconcile lost acknowledgements without replaying the underlying tool/model action. |
| P020, P029, P047, P156 | Resolve Syn installation/workspace/recipient credentials through scoped broker references; include Syn secrets in capture redaction. Freeze disclosure policy and selected semantic release independently of service configuration. |
| P057–P071 | Preserve immutable knowledge views, frontier leases, graph contributions, cancellation and candidate selection. Communication can carry references to a run; it is not another solve graph or worker claim. |
| P072–P084, P158–P175, P203–P204 | Record the actual MCP/provider/DanceFlow route and remote operation handle. A Syn delivery retry only retries delivery; it cannot replay a paid API call, assume a cancelled job stopped or substitute for the component's own status evidence. |
| P085–P094, P177–P183 | Received facts/procedures remain proposals. Apply admission/conflict rules; do not append private or deletable memory text to an immutable communication history. Define minimized records, reference visibility and deletion behaviour before export. |
| P095–P110, P184–P197 | Retain exact source snapshots, named encoding generations, retrieval eligibility and coverage. Anchoring a result does not improve completeness, authorize a result or make repeated records independent sources. |
| P111–P117 | Shared feedback/lessons are candidate input to existing learning admission. No automatic preference, procedure or routing-policy promotion from a message. |
| P118–P123, P198 | Extend current task/run inspection with delivery state, receipt identity, retained anchors and admitted inbox provenance; keep execution state separate. Include a two-service controlled integration scenario. |
| P124–P148, P199–P201 | Add service compatibility, upgrade/restore, interrupted delivery, token rotation, scope, retention and adversarial-message gates. Existing real-model and task-quality benchmarks remain necessary. |
| P149–P153, P202 | Optional training/UI work stays optional. Neither an immutable receipt nor shared storage grants permission to train on the payload or activate a learned deployment. |

## Changes across the 48-stage tool programme

Every stage has an explicit disposition in the impact map. Grouped changes:

| Stages | Plan consequence |
| --- | --- |
| T01.1–T01.4 | Inventory both revisions and service identity; bind causal envelopes to selected releases; establish separate Potbot/Syn migration and compatibility checks. Existing partial code is reused, not rebuilt or marked qualified. |
| T02.1–T02.4 | Keep protocol-specific MCP/CLI/HTTP transports. Emit normalized events through the controller boundary; Syn is the communication/receipt route, not a replacement MCP transport or arbitrary tool relay. |
| T03.1–T03.4 | Add disclosure policy, recipient-scoped credentials, secret filtering, outbox/inbox fencing and cross-service recovery. This is the main new implementation work. |
| T04.1–T04.4 | Syn can carry untrusted catalog observations/change hints after admission. Revalidate exact publisher/schema/installation evidence through the plugin strategy; do not adopt a remote catalog automatically. |
| T05.1–T05.4 | Keep source acquisition and independent source comparison. Export only permitted references/digests; a copied chain receipt is not another independent source. |
| T06.1–T06.4 | Browser engines, isolation and benchmarks stay the same. Their normalized events may use the common relay; profiles, cookies, raw private captures and browser authority do not travel with them. |
| T07.1–T07.4 | Qualify private-submodule checkout, exact cross-repository revisions, clean source distributions and CI attribution. Submodule availability is separate from application test success. |
| T08.1–T08.4 | Preserve Potbot RLS and retrieval stores. Register an explicit Syn service boundary; Syn storage is not an unrestricted SQL/graph or cross-workspace query endpoint. |
| T09.1–T09.4 | Solver/proof implementations and correctness gates remain unchanged. Receipts can be anchored; proof validity still comes from independent checking. |
| T10.1–T10.4 | Add typed agent-message steps only after secure delivery/admission. Potbot's DAG scheduler remains authoritative; remote claimed completion is input to reconciliation, not a terminal transition. |
| T11.1–T11.4 | Separate integrity, authorship, authorization and factual verification. Add service-outage, replay, forged sender, chain truncation/fork, restore and deletion tests, plus bounded latency/backlog measurements. |
| T12.1–T12.4 | Treat deployment as a pinned two-service compatibility profile: source access, client/protocol/DB versions, credential injection, health, backup/restore, rollback and independently retained anchors. |

## Revised implementation order

1. **Qualify the current dependency boundary** — T01.1/T01.4/T07.3/T12.1.
   Resolve authorized CI access to private Syn, preserve a credential-free local
   contract lane, and run exact-revision Linux/PostgreSQL/sandbox/packaging checks.
   Move this dependency-specific work forward; do not wait for all of Phase 12.
2. **Complete causal/semantic admission and the safe export projection** —
   T01.2/T01.3/T03.1/T03.3. Add sealed-release membership, actual trigger/input
   evidence, issuer/lease checks, bounded payloads, field classification and
   secret/reference filtering before unattended publication. Current URI syntax
   checks and manual `sync-events` do not complete this gate.
3. **Implement transactionally durable outbound delivery** — T03.4 and the
   existing P010/P011 primitives. Extend the local transaction with an export
   intent; claim it with a fenced worker; append the unchanged approved event
   using its versioned stable delivery UUID while retaining the source event UUID;
   verify the matching receipt; commit the acknowledgement. The v1 summary identity
   is defined in [the exchange guide](10-syn-exchange-contracts.md).
   Keep all network waits outside the database transaction.
4. **Implement authenticated, typed receiving-agent admission** — T03.4/T10.1/
   T10.2. Persist message identity and consumer progress atomically with the
   admitted local proposal/event. Recheck audience, task revision, signature or
   grant, lease, visibility and budget before dispatch. Duplicate delivery must
   not duplicate a proposal or an external action.
5. **Qualify operations and evidence** — T11.2/T11.4/T12.2–T12.4. Exercise outages,
   lost acknowledgements, restart/restore, backlog limits, rotation, correction,
   deletion, retained-anchor checks and operator inspection. Distributed agents
   are enabled only after these gates and the existing execution prerequisites.

These work packages fit the existing stage gates. They do not increase the 48
stages, lower an existing acceptance threshold or reopen completed foundation
deliverables. Independent browser/solver/model work can continue through its
existing offline contracts while Syn access or delivery qualification is blocked.

## Required delivery and recovery contract

- Preserve **two cursor domains**. Potbot's workspace execution cursor can have
  gaps across job/standalone event families; Syn's receipt sequence orders all
  communication channels in one workspace. Retain both. Never resume a local
  event scan from a Syn sequence or claim a global order across workspaces.
- Use a versioned projection and stable `(Syn installation, workspace, message
  UUID)` identity. An instance reference or matching UUID is not globally unique
  across independently installed Syn services. Switching endpoint/installations
  requires explicit migration/reconciliation and retained anchors.
- Delivery states should distinguish pending, attempted/acknowledgement unknown,
  acknowledged, verified-against-anchor, conflict and quarantined. These are
  **proposed delivery states**, separate from job state and factual verification.
  An acknowledgement can be lost after Syn committed; retry only the identical
  message ID/content. Conflicting content is quarantined, not assigned a fresh
  ID to conceal the conflict.
- Admission and the local outbox intent are atomic locally. Sending and storing
  the remote receipt are not a distributed transaction. Test each crash window;
  idempotency prevents duplicate appends but is not exactly-once execution.
- Keep a declared outage policy. Deferred anchoring may queue bounded exports
  without changing an otherwise permitted local execution. A task requiring an
  external receipt stays blocked/unqualified at that gate until it is verified.
  Never convert a Syn outage into a repeated provider/tool action or a false pass.
- Use existing artifact/reference owners for large or private content. The current
  protocol's 65,536-canonical-byte payload limit is not permission to upload raw
  prompts, secrets, private source, memory bodies or unrestricted console output.
  Even a digest/reference needs disclosure and retention review.
- Apply deletion/visibility at retrieval and context delivery. Removing off-chain
  material does not erase a receipt already appended to Syn. Plan explicit
  correction/revocation records and access denial; do not promise that a tombstone
  purges previously shared bytes or that a hash is automatically non-sensitive.
- Validate receipts with the canonical client. Retain checkpoints outside Syn's
  database. A service capable of rewriting both history and its new head can
  produce an internally consistent replacement; a previously retained anchor is
  needed to detect that changed prefix. One anchor does not prove consensus or
  detect every possible fork without a separate comparison protocol.

## Qualification and unresolved decisions

The [current Potbot CI failure](https://github.com/kieransimkin/potbot/actions/runs/39910979031)
is a confirmed access failure: all jobs stopped while cloning private
`Synsphere/syn`. Remote application tests did not run. Granting CI access is an
operator credential/permission decision; it is not supplied by this document.
Syn PR #10 has no verification checks. A package install or populated submodule
therefore remains distinct from an accepted deployment.

Before a cross-service release, record both source commits, client/protocol
version, independent DB revisions, deployed service/config identity, permitted
workspace/audience, retained anchor, required versus deferred delivery policy and
the exact acceptance runs. Test the selected compatibility tuple, including a
controlled unsupported-version failure and backup/restore. Do not claim all
possible version combinations from testing one tuple.

Three decisions remain explicit future work: the authority model for messages
between independently operated agents; the approved projection/retention rules
for sharing source or memory references; and whether Syn will provide federation
or consensus. The current useful milestone is one managed service. Future peer
work belongs in Syn's own reviewed protocol design, including persistent peer
identity, signed envelopes, discovery trust, deduplication, reconciliation and
failure/partition handling. It does not silently become a new Potbot scheduler,
wallet, permission system or license to make the private repository public.

## Review evidence and completion

Verified from source: submodule pin and client boundary; explicit one-page export;
Potbot migration 19/shared cursor and public projections; Syn workspace token
scope, append/replay API and independent migration; existing scoped credential
resolver; original Word-plan precedence and ownership rules. Proposed work is
labelled throughout. Original DOCX sources, existing tracker statuses and runtime
code are not changed by this planning amendment.

**Blogworthiness: FAIL** — this is a planning reconciliation for the already
prepared integration story, with no new implemented runtime or measured result.
Evidence is the source/plan comparison and the known private-submodule CI failure.
Retain useful architecture findings in that existing draft; do not create a second
post solely about updating implementation plans.
