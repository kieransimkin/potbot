# Syn exchange contracts and safe export projection

23 October 2026. **Implemented first contract:** manually approved execution
metadata summaries and preview. No outbox, inbox, remote dispatch, production
configuration or full semantic/producer admission is introduced. See the
[architecture](09-syn-potbot-architecture.md) and [programme amendment](07-syn-integration-plan-impact.md).

![Potbot event ownership and the separate Syn delivery boundary](../media/syn-plan-boundaries.svg)

*Ownership illustration; outbox/inbox remain proposed. The summary is built at
the manual export boundary before append. This backend JSON feature has no new
GUI: contract tests and actual HTTP acceptance explain it without screenshots or
video of credential entry or unimplemented components.*

## Fixed v1 disclosure profile

Contract `urn:potbot:syn:execution-summary:v1` uses channel
`potbot.execution-events.v1`. It reports an occurrence without asserting that its
producer, semantic classification, input evidence or result passed admission.
`assertion` is always `source_report`.
The [published JSON Schema](../../vocab/syn-execution-summary.schema.json) describes
the closed wire shape; runtime checks additionally enforce exact integer values,
scope, causal consistency and canonical byte bounds.

| Fields | Meaning |
| --- | --- |
| `schema_version`, `contract`, `assertion` | Fixed schema 1, exact contract identity and explicit source-report status. |
| `source.installation_id`, `workspace_id`, `event_id`, `sequence` | Source Potbot installation/workspace, original event UUID and Potbot execution cursor. |
| `recipient.namespace_id`, `origin_sha256` | Operator-assigned destination-history namespace and SHA-256 of the canonical origin. This is not remote Syn installation attestation. |
| `execution.run_id`, `emitter_run_id`, `parent_event_id`, `parent_run_id` | Opaque local execution/causal identities. Parent IDs are both present or both null. |
| `execution.trigger_kind`, `stage`, `elapsed_ms` | Existing closed enums and nullable bounded nonnegative elapsed milliseconds. Null remains unknown/unavailable, not zero. |

The builder copies only these approved fields. Free-form triggers, artifact
references, content hashes, Type URIs, version strings, private data and future
local fields are omitted. Internal paths and digests can disclose private data.
Richer exchange requires a separately versioned profile with actual semantic/causal
admission and disclosure checks; extra fields in this v1 contract are rejected.

Canonical UUIDs and exact integers are required. Unknown fields/versions, invalid
enums, self-causation, incomplete child parents and numbers outside
0..9,007,199,254,740,991 fail. Sequence starts at 1. Policy files and canonical
wire payloads are limited to 4,096 bytes. Validate the whole page before any send.

This still discloses activity, linkage, timing and identifiers. It is minimized,
not anonymous or safe for every destination. Explicit local policy approves that
disclosure to the selected immutable history. It is not a signature, an access
grant or permission to share other workspace data.

## Explicit policy and preview

Keep the actual policy with private operator configuration, outside Git. It has
no credential. These synthetic example values approve no production recipient:

```json
{
  "schema_version": 1,
  "contract": "urn:potbot:syn:execution-summary:v1",
  "approval": "execution-metadata-to-immutable-ledger",
  "policy_id": "11111111-1111-4111-8111-111111111111",
  "installation_id": "22222222-2222-4222-8222-222222222222",
  "workspace_id": "33333333-3333-4333-8333-333333333333",
  "recipient_origin": "https://syn.example",
  "recipient_namespace_id": "44444444-4444-4444-8444-444444444444"
}
```

Retain the namespace for the intended destination history. Replacement of Syn's
history at the same URL requires explicit reconciliation and a new namespace;
the current service exposes no attested installation identity. Preserve
independent anchors. A new local reapproval `policy_id` does not alter existing
message bytes or delivery identity; it is returned only in the local result.

With usual installation/workspace/origin configuration and database credentials:

```sh
uv run potbot syn-service --workspace-id <UUID> sync-events \
  --principal-id <UUID> --export-policy <local-policy.json> --preview
```

Preview prints exact messages with `delivery: not_sent`. It reads local events
without resolving a Syn token or contacting Syn. Scope/policy mismatch fails
before database/vault credentials are resolved. Duplicate JSON keys fail.
Preview grants no approval and creates no acknowledgement or checkpoint.

Omit `--preview` only for the intended disclosure; retry the same source cursor
on uncertain acknowledgement. Scope must match the installation, authenticated
workspace and origin. No policy is generated automatically. Old `sync-events`
invocations without `--export-policy` fail at argument parsing.

## Identity and legacy migration

The old export used source event UUIDs as message IDs on
`potbot.execution-events`, carrying the full workspace-visible projection.
Reusing those IDs for smaller payloads would conflict with retained receipts.

V1 derives a standard UUIDv5/NAMESPACE_URL over canonical JSON containing exactly
`contract`, source `installation_id`, `workspace_id`, `event_id`, and the complete
`recipient` object. The original event UUID remains provenance. UUIDv5 is retry
identity, not authorship proof. Stage, sequence and local policy ID are not identity
inputs: altered content under the same identity raises conflict instead of hiding
the change under a fresh ID.

The fixed interoperability vector in `test_v1_published_retry_identity_vector`
uses the policy example above and source event
`55555555-5555-4555-8555-555555555555`; its message ID is
`609cdd2c-19ae-5961-b249-b5611ed5dc9d`. This protects the published canonical
identity recipe from accidental changes.

- Legacy receipts remain readable/verifiable; no source event or receipt is
  rewritten. The scoped adapter rejects new legacy-channel sends.
- Legacy and v1 reports may coexist for one occurrence. Consumers use contract
  and scoped source identity to avoid counting them as independent evidence.
- Future profiles need new contract/channel identities and explicit migration.
  Unsupported execution channel versions fail locally.
- Potbot source cursor and Syn receipt sequence remain separate.
- Generic deliberate messaging remains available. Reserved execution sends must
  validate exact shape, scope, sender and derived ID; that is not agent admission.

## Remaining gates

The summary makes no Type/release, artifact, input-digest or emitter-lease claims.
It does not repair pending producer admission in the local event ledger. Resolve
sealed snapshot membership, genuine causal input evidence, task/run binding and
fenced producer identity before richer payloads or universal monitoring claims.

Freeze the chosen profile/compatibility tuple, decide retention/outage policy and
qualify conflicts/recovery before the outbox. Automatic publication policy needs
durable ownership/revision rules; this manual local file is not that future store.
No original programme stage is completed by this increment.

## Evidence

`tests/unit/test_syn_exchange.py` covers omission, strict shape, scope denial,
versioned identity, stable retries, page preflight and credential-free preview.
`tests/integration/test_execution_events.py` exercises projection from real
PostgreSQL workspace events. Actual HTTP acceptance and current-head CI results
are retained in the private task handoff; diagrams do not establish outcomes.
