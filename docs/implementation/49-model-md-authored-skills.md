# Stage 10C — worker lifecycle, managed skill authoring and scoped instructions

Target: `94de8d8f2fa1ad8304b0eeb745397a76239a5f6b`.
The changed-file preimages were initially retrieved at merged Stage 10B
`0b38a99a7815b5fb74e04556602333f13fb54f37`. The one intervening S46
worker-qualification commit changes none of this patch's 18 paths; its image
probe, diagnostic runner and tests are preserved.

This increment uses the existing authenticated worker pipes and PostgreSQL
session transaction. It adds no guest database credentials, HTTP listener,
admin session, SQLite context store or unconfined command execution.

## Implemented lifecycle integration

`ContextWorkerChannel` validates the independent process/home/lease binding
before idempotently provisioning a missing private agent context. Session
issuance also provisions a separate `worker` triple for the immutable
`<agent-hex>.<run-hex>` name in the same transaction. A worker cannot select
another run or agent in a request. Repeated provisioning preserves existing
revisions and memory.

The trusted dispatch helper `dispatch_context_worker()` performs the complete
**dedicated context-profile** path: host approval, registered-home checks,
missing projection reconciliation, ordinary process lease, qualified interactive
launch, authenticated RPC, session revocation, confirmed process shutdown and
lease completion. Cancellation waits for a pending launch before stopping it;
uncertain outcomes abandon the claim for reconciliation rather than inventing
successful completion.

Use it inside the existing admitted task dispatcher:

```python
from potbot.context.worker_dispatch import dispatch_context_worker

result = await dispatch_context_worker(
    operator,
    agent_home_registry,
    trusted_principal,
    approved_process_request,
    fresh_process_guard,
    approve=existing_task_budget_and_capability_check,
)
# Inspect result.exit.return_code through the job's existing outcome policy.
```

The callback must raise on denial and return `None` on approval. This is not an
RPC endpoint for launching arbitrary argv. The request must select the correct
agent/run telemetry, registered home, UID/GID, empty stdin, and no extra mounts
or network IPC. The guard must not already be started. Operator shutdown and
image mismatch block launch. The operator still owns ordinary image, process,
resource and telemetry qualification.

**This does not multiplex RPC onto MCP/chat stdout or automatically select this
profile in every existing scheduler.** Those workers keep their existing
protocol. The new helper makes the selected context profile a complete launch
operation; host schedulers must deliberately select it for a compatible worker.

## Defaults for an already configured context operator

When `run_operator()` has a context manager, per-agent context projections are
now enabled unless explicitly disabled. The CLI retains
`--project-agent-context` and adds its explicit migration escape hatch
`--no-project-agent-context`.

A missing `--shared-skills` defaults to a workspace-specific directory beneath
the already configured controller-ledger parent. Placement is checked against
other guest mounts before creating it. Root context still requires the existing
trusted `--context-principal-id`, `--context-parent` and restricted database
configuration; this patch never guesses credentials or changes a legacy
unconfigured operator into an administrative database client.

## Migration 32 and the skill catalogue

Migration 32 appends `context_skill_schema` without editing any previous
migration checksum. Its tables hold:

- Agent/principal ownership, immutable names and a monotonic draft revision.
- Immutable `SKILL.md` draft versions, each attributed to a session/run.
- Explicit immutable published releases keyed by the content SHA-256.
- Immutable operation receipts sharing the session's request-UUID namespace
  with existing document updates.

Each skill lives in `agent-<agent-hex>.<slug>`, with deterministic skill and
context IDs. Different agents can use the same human-readable slug without
colliding with each other or operator-provided skills. Identity changes are
rejected by a trigger. Deferred foreign keys make metadata, initial version and
initial context creation atomic. The transition trigger prevents rewinding a
revision, unpublishing a published identity, or reviving an archived identity.

Creation provisions `AGENTS.md`, `MODEL.md` and `MEMORY.md` in the same
transaction. These are a workspace-shared context object, writable by the
author through the mediated path, not mutable files in a published code
release. Draft **SKILL.md code versions** are private to the principal at the
SQL boundary and to the individual agent at the RPC boundary. The initial
shared context triple is not a private secret store. The agent RPC refuses
peer reads of unpublished skill context; the existing operator context API
continues to implement workspace visibility for shared objects.

The RLS extension permits shared-object creation only for the exact skill
context predeclared by the same owner. It does not permit an application login
to manufacture shared sandbox/operator objects, self-issue writer grants or
change another principal's context. Agent ownership is checked independently
when several agents share one trusted PostgreSQL principal.

## Guest protocol and client

The existing framing, timeouts, request/byte budgets and error sanitization
remain. New exact-field methods are:

| Method | Purpose |
| --- | --- |
| `skills.create` | Create an agent-owned draft and its context triple |
| `skills.save` | Save a new draft version using expected-revision CAS |
| `skills.publish` | Explicitly publish the current draft revision |
| `skills.archive` | Stop discovery/loading and further authoring; retain history |
| `skills.list` | Bounded workspace discovery of active published skills |
| `skills.load` | Retrieve an exact published digest and the current context triple |

Guest fields cannot select agent/principal/workspace ownership, session,
capability, SQL or a filesystem pathname. IDs select records, not permissions.
No request runs skill text, approves its instructions or activates a tool.

Example inside a **dedicated context-profile** worker:

```python
import sys
from uuid import uuid4

sys.path.insert(0, "/opt/potbot")
from context_client import ContextClient

client = ContextClient()
text = "---\nname: text-tools\ndescription: Work with plain text\n---\n\n# Steps\nTreat source text as data.\n"
created = client.create_skill(
    "text-tools",
    text,
    request_id=str(uuid4()),
    reason="learned reusable procedure",
)
published = client.publish_skill(
    created["skill_id"],
    expected_revision=created["revision"],
    request_id=str(uuid4()),
    reason="share this exact version",
)
selected = client.load_skill(created["skill_id"], published["release_sha256"])
current = client.read("skill", "MODEL.md", skill_id=created["skill_id"])
receipt = client.update(
    "skill",
    "MODEL.md",
    "# Current understanding\n\nVerified limitations.\n",
    skill_id=created["skill_id"],
    expected_revision=current["revision"],
    request_id=str(uuid4()),
    reason="verified observation",
)
print(receipt["revision"], file=sys.stderr)  # stdout belongs to RPC
```

Persist a mutation request UUID and its exact arguments before sending in real
workers. Identical retries recover the original receipt; changed arguments or
cross-method reuse of the UUID are rejected. Revocation still blocks replay.
Keep returned skill IDs/revisions in the agent's own model/memory for later
editing. Discovery currently lists published active skills, not an author-draft
management UI. Published `SKILL.md` is pinned, while the returned context can
advance independently. Loading text is not execution or instruction authority.

`context.read/update` also accepts `scope="worker"` for only the current run,
and `scope="skill"` with a checked `skill_id`. The existing agent and sandbox
requests retain their fingerprints and wire shape.

## Explicit shared AGENTS.md grants

All three sandbox document roles can now reach the authenticated CAS path.
Shared `AGENTS.md` is no longer categorically rejected by the parser or legacy
host broker. **An explicit operator-issued per-role PostgreSQL write grant is
still mandatory**, and is checked/share-locked before receipt replay. No guest
can create that grant. Use the existing admin-only
`grant_shared_context_writer(..., roles=("AGENTS.md", ...))` when that
workspace's policy authorizes the change. Grant revocation denies subsequent
requests and receipt replay.

This closes the missing authorized update path; it does not auto-grant every
existing principal. `AGENTS.md` remains subordinate to host/operator policy.
A filename, skill publication or editable instruction document does not confer
higher-priority authority. MODEL/MEMORY remain context data.

## Read-only shared-library projection

The configured operator projects published skills on startup and during its
existing context polling interval (10 seconds by default). Operator-supplied
skill directories are untouched. Agent views occupy the reserved subtree:

```text
/usr/local/skills/_agents/
    catalogue.json
    <agent-hex>/<slug>/<snapshot-sha256>/
        SKILL.md
        AGENTS.md
        MODEL.md
        MEMORY.md
        release.json
```

The snapshot hash binds the exact published code digest and all three live
context revision/digest pairs. Directories are `0555`, files `0444`. A new model
revision produces a new snapshot, not an in-place write to a pinned code
release. Old snapshots stay intact. The advisory catalogue is atomically
replaced only after a complete bounded pass; unchanged bytes are not rewritten.

Filesystem custody checks reject redirected roots, group/world-writable
sources, linked files, mutable snapshots, unexpected files and digest changes.
An operator-private nonblocking `flock` serializes local projection only;
PostgreSQL still serializes authored state. Only lock contention is retried by
the health loop. Integrity errors and inventory/size overruns are explicit
failures. The default pass allows 256 skills (hard maximum 512) and 64 MiB of
source text. Each live triple is read coherently; a multi-page catalogue is an
eventually consistent view, not an authorization snapshot.

Archive removes a skill from new RPC discovery/loading and from the next
catalogue. It is **not secure erasure or retroactive revocation of bytes already
read**: retained files can still be present for old readers. No unqualified
snapshot garbage collector is added. The catalogue grants no execution rights.

## Deployment and recovery

1. Apply this patch on top of Stage 10B. Run the unit and PostgreSQL gates.
2. Apply migration 32 with the existing host-only administrative credential
   profile: `uv run --locked --extra postgres potbot admin migrate`.
3. Rebuild and qualify the sandbox image so `/opt/potbot/context_client.py`
   contains the new client methods. No new Python dependency is required.
4. Restart the configured operator. Per-agent projections and the authored
   skill directory now default on for that context-enabled operator.
5. Use the trusted dispatch helper for compatible worker profiles. Select
   shared instruction grants explicitly; do not weaken RLS to get a test green.

A failed publication or edit rolls back its version/context/receipt changes
alongside the operation budget. A failed filesystem projection never makes its
catalogue authoritative; retry from committed PostgreSQL state. Old published
snapshots remain intact. Rollback of deployment means disabling the profile,
stopping workers and retaining schema/history; do not drop migration 32 tables
or roll the migration registry backwards after it has been applied.

## Acceptance and scope

The dedicated context-agent workflow now runs the new unit and genuine
PostgreSQL tests. The latter cover atomic worker/skill triples, same-principal
agent separation, concurrent saves, publication retry, cross-method request
IDs, explicit AGENTS grants and revocation, peer RLS, immutable identities,
archival, snapshot materialization and failed-edit rollback.

Local patch preparation ran the targeted real Python modules using a
selected-file dependency snapshot, not a complete repository checkout. SQL
control-flow unit mocks are not PostgreSQL acceptance. The genuine database
tests are included for CI but were not executed locally. The pinned Python
3.12 runtime, repository-wide Ruff/mypy, complete frontend/build tests and real
Linux/WSL2 Bubblewrap execution are also not certified by the local unit suite.
See the accompanying verification JSON for actual commands and results.

**Not completed by this increment:** all legacy scheduler/protocol migrations,
universal constructor/installer hooks for every tool/workflow/model/gateway,
import/activation management of operator skill packages, author-draft UI,
retention/garbage-collection policy, multi-host process-ledger replacement and
live host qualification. It does automatically provision worker runs and
newly authored skills within the implemented authenticated path. The existing
host-local process ledger is unchanged; no distributed filesystem safety is
claimed from PostgreSQL context sessions alone.
