# U01-A sandbox transport recovery

The local queue uses JSON-RPC over subprocess pipes. The existing sandbox worker
starts the target process inside its sandbox. The target commits the effect ID,
request digest and queue message to SQLite before replying. PostgreSQL retains
the attempt, the existing task budget reservation and the reconciled outcome.

```mermaid
flowchart LR
    A[Record intent and reserve budget] --> B[Commit dispatch marker]
    B --> C[Existing sandbox worker]
    C --> D[Local queue target over stdio]
    D --> E[Record receipt and settle budget]
    D -->|Reply lost| F[Operator admits recovery worker]
    F --> G[Target reads result or records cancellation]
    G --> E
    A -->|Stopped before dispatch| H[Release reservation]
```

The diagram shows where durable state changes. The acceptance case exercises
actual subprocess pipes inside Bubblewrap with PostgreSQL and SQLite. This
command-line feature has no UI to capture. Hosted CI uses a development Syn
fixture and the standard Bubblewrap launcher. Live native Syn and audited S46
host qualification are separate deployment gates.

The operator provisions a dedicated registered agent home and a new target UUID
for each queue. Use the pinned executable from the sandbox image and run
`/usr/local/libexec/potbot-queue-transport init TARGET_UUID` in that agent's home
as the agent identity. Preserve the resulting private database for the target's
lifetime. The same trust boundary owns its backups and must prevent unrelated
work from rewriting its state. A reset requires a fresh target identity.

The controller seals `BoundCogRegistry.provenance(queue_transport=True)` and
installs `install_queue_transport(target_id, executable_sha256, policy)` as the
single operation in the runtime. The typed request carries a stable effect UUID
and a message of up to 512 characters. The host pins the executable, target and
worker identity. Execution still passes the existing sandbox admission and Syn
grant checks. The released Syn and operator audit requirements apply to live
deployments.

The workspace commit-order lock serializes lane admission and the durable
dispatch marker. PostgreSQL also enforces uniqueness of each workspace, target
and effect ID across task revisions and installations. An unresolved attempt
keeps its reservation and blocks another write in that lane.

A trusted controller supplies `admit_transport_recovery` and calls
`reconcile_transport_effect(run_id)`. For a prepared attempt the controller
atomically cancels the attempt and releases the reservation before the original
controller can dispatch. For a dispatched attempt the controller obtains a new
Syn grant and enters the sandbox worker with the original pinned input. The
target transaction returns the committed receipt or records a cancellation that
fences a late arrival. A missing or mismatched target leaves the attempt held.
The existing host lease and process audit govern recovery when worker shutdown
is uncertain.

The budget reserves one call, the configured tool deadline and one concurrency
slot. Confirmed pre-dispatch cancellation releases all three. A resolved
dispatch consumes one call and the reserved deadline as a conservative charge
and releases concurrency. The outcome and settlement share a PostgreSQL
transaction. Recovery repeats return the same result and charge, including
when the controller lost the original response.

Reproduce with the existing disposable PostgreSQL fixture and Linux Bubblewrap
configuration.

```sh
uv run --locked python -m pytest -q tests/integration/test_sandbox_transport_recovery.py
```

The tests cover lost intent and dispatch acknowledgements, a lost receipt after
the target committed, repeated reconciliation, and normal settlement. The
transport profile covers the dedicated local queue. Additional target adapters
need equivalent authoritative outcome evidence and their own qualification.
