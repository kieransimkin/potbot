# Durable jobs and streaming progress

The API queue stores work in PostgreSQL. It uses the same authenticated
`WorkspaceContext`, database-login binding and row-level security as tasks,
budgets, model invocations and repository evidence. A job belongs to an existing
task (`episode_id`); creating a job does not claim that its task is completed.
Submission freezes the task's current `contract_revision_id` in the job and its
worker lease. Revising a task does not silently update already queued prompts.
The provider gateway checks that frozen revision against the current task both
when reserving resources and immediately before allowing dispatch.

## Submission and selection

The versioned API submission payload has exactly four fields:

```json
{
  "job_type": "model.generate",
  "episode_id": "00000000-0000-0000-0000-000000000001",
  "input": {
    "prompt": "Compare two approaches to the repository task.",
    "max_output_tokens": 2048,
    "timeout_seconds": 60
  },
  "selected_models": [
    {"provider": "openai", "model": "operator-selected-model", "role": "reasoning"}
  ]
}
```

The UUID and model ID above are illustrative. Supply a real task identity and a
model from the configured provider's catalogue. The controller allows one to
sixteen distinct provider/model/role selections. Provider request validation,
disclosure permissions, credentials and task budgets remain worker admission
responsibilities; queue acceptance alone authorizes no external request.

The input is retained privately for the worker. Job records, queue snapshots and
state events expose explicit metadata fields only: task/job identity, selected
models, state, progress, counters, classified errors and timestamps. They do not
return the input, connection strings, credential material or exception text.
Model-generated output can be streamed to the authenticated workspace after the
worker redacts secrets. The initial worker executes selected models sequentially;
these jobs do not claim automatic construction of a complete reasoning graph.

## State and cancellation

| State | Meaning |
| --- | --- |
| `queued` | Committed and available for an eligible worker. |
| `running` | Claimed under a current worker lease. |
| `cancelling` | Cancellation requested; the worker has not confirmed stopping. |
| `succeeded` | Worker recorded successful completion. |
| `failed` | Worker recorded a known failure. |
| `cancelled` | Cancellation occurred before a claim or a worker confirmed stopping. |
| `indeterminate` | Worker was lost or a remote outcome remains uncertain. |

Cancelling a queued job immediately records `cancelled`. Cancelling running work
records `cancelling`; neither the API nor a closed WebSocket proves that an
external request stopped. The worker must select the final state according to its
observations. `dispatched_at` records the durable decision to allow dispatch, not
proof of a remote provider's receipt.

The worker's cancellation probe also checks the task's authoritative lifecycle
and contract revision. Pausing or cancelling the task, or revising its contract,
revokes permission to continue the old generation. The heartbeat propagates that
change to the provider stream. After possible remote dispatch, an interrupted
call with unknown usage remains `indeterminate` and retains its budget reservation
until explicit reconciliation; the task change cannot prove the provider stopped.

Workers claim jobs with `FOR UPDATE SKIP LOCKED`. Every lease carries a generation
and worker identity. Renewal, progress, output and completion require the same
current lease; an old worker cannot publish after its lease expires. Expired
active jobs become `indeterminate` and are never automatically requeued. This
avoids repeating paid work after a lost response. Reconciliation remains an
explicit controller/operator responsibility; inspection and retained output are
still available. A new submission requires its own deliberate command identity.

The internal `outbox_jobs` queue keeps its existing at-least-once semantics for
projection/maintenance handlers. It is separate from API jobs and is not silently
used to replay external inference calls.

## Replay and reconnect

`job_events` is append-only and covered by workspace row-level security. Each
event has a workspace-wide integer `sequence`, immutable event and job IDs,
`event_type`, `data`, `recorded_at`, and `schema_version: 1`.

State and progress events contain `data.job`, the public job projection. Output
events contain `data.text` and optional `provider`/`model`/`role` tags which must
match the job's selection. A role tag requires both provider and model tags. Use
the role tag to distinguish reasoning and coding calls when both use the same
provider/model pair. Output deltas are bounded to 64 KiB and total retained output
to 4 MiB per job. A streaming worker must redact before persisting output; the
queue never treats text produced by a model as instructions or tool authority.

Clients save the last received sequence and request subsequent events using
`after`. Pages return `items`, `next_cursor` and `has_more`; drain all pages before
waiting for more updates. Delivery on reconnect can repeat already received
events, so consumers deduplicate by sequence or event ID. Progress persists across
server restarts and multiple web-server processes because PostgreSQL is the
source of truth.

Cursor allocation locks a per-workspace counter until the event transaction
commits. A plain PostgreSQL sequence would allow a higher value to commit before
a lower value and could cause a client to miss an event after reconnecting.
Queue/job snapshots include a coherent `event_cursor`; state writes and their
events commit together. No cursor is advanced by an uncommitted event.

## Validation

The integration suite uses real PostgreSQL with restricted application logins.
It checks duplicate submissions, transactional rollback, competing claims,
expired leases, cancellation races, fenced completion, durable output replay,
immutable events, cross-workspace denial, pagination and overlapping event
transactions that attempt to commit out of order. Contract tests reject invalid
identities, non-finite numbers, excessive input, arbitrary result/error metadata
and malformed model selections.

Hosted integration tests combine these SQL boundaries with mocked provider HTTP.
They verify generation admission, explicit budgets, same-model role separation,
coordinator cancellation, task pause/revision during streaming, split-secret
redaction, usage overruns, unknown usage and crash recovery around reservation
and settlement commits. They do not invoke paid providers.
