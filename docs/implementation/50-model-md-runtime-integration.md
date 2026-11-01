# Stage 10D — registered object lifecycle and ordinary worker integration

## Source and scope

Target: `375055a1041b20ac3f718a61510c9bd6fd9a1243`.
The implementation began at merged Stage 10C `b9f0199210ee4a68c38529320091fa5a4423d4d0`.
The intervening browser-controller changes in `execution_cogs.py` are retained;
its complete new preimage was verified against GitHub's blob digest.

This is the normal execution/lifecycle integration, not another unused dispatch
helper. `ExecutionCogRuntime` binds its existing authenticated database to its
existing `SandboxedCogExecutor` after validating installed semantic ports and
policy. The runner supplies per-launch immutable root/agent contexts to ordinary
workers. An explicitly installed `context-rpc/v1` worker additionally receives
context/skill/object operations and its typed work input/result on its existing
pipes. No HTTP listener, socket mount, guest DSN or host callback is introduced.

The default remains the existing `typed-json/v1` worker wire format. Its input
and result envelopes do not change. Unknown protocols are rejected rather than
guessed from stdout. Existing model, MCP and browser protocols are not silently
multiplexed; their worker installation must explicitly implement the context
profile before receiving it. Read-only installed work cannot use context or
skill mutations as a hidden write channel.

## Automatic object coverage

Migration **33** appends `context_lifecycle_schema`. It does not edit migrations
1–32 or their checksums. Registered source creation and initial context triples
commit together, including rollback of both on failure.

| Authoritative registration | Context lifecycle |
| --- | --- |
| Repository | Stable repository context |
| Episode/task | Stable task context |
| Model catalogue | Model context preserving history through metadata changes |
| Tool action | Stable context for the tool kind, not a replacement on every call |
| Execution-run contract | Principal-private run context |
| Syn namespace | Namespace context |
| Sealed Syn snapshot | Context for each native object and type, keyed by namespace and canonical reference |
| Registered workspace sandbox | Operator and gateway context alongside the existing sandbox triple |
| Host execution installations | Versioned tool/workflow/model/connector and execution-gateway context |
| Existing shared-tool registry | Bounded operator reconciliation, including removed-installation observations |
| Authored skills, agents, dedicated worker sessions | Existing Stage 10B/10C provisioning remains in use |

The source coverage is explicit in `SOURCE_TABLES`, not a heuristic over every
SQL relation. Backfill includes existing registered rows and only **sealed** Syn
snapshots. It does not duplicate context on each snapshot/version, and it does
not copy source payloads, raw code, credentials or historical events into policy.
Removing an action is not uninstalling its tool. Missing members in a newer Syn
snapshot are not inferred to be globally retired, because multiple pinned
snapshots may remain in use. Registration is not execution authorization.

Object IDs use a namespaced SHA-256-derived identity. Source names/definitions
can evolve without resetting MODEL.md or MEMORY.md. Lifecycle observations and
runtime definition fingerprints are stored separately from those learned
Markdown documents. Retirement removes new discovery and managed access while
retaining document history. Runtime installations use principal-owned context
views shared by that principal's agents, not by unrelated principals.

Migration-owned, tightly scoped `SECURITY DEFINER` trigger functions seed shared
objects. Their search paths are pinned to `pg_catalog`, the installation schema,
and `pg_temp` last. PUBLIC execute is revoked, runtime source-index writes are
blocked by restrictive policies, and the trigger checks its exact source table.
The migration requires the existing RLS-bypassing installation owner; a
restricted runtime login must never be promoted to satisfy that requirement.

## Registered-object reads and edits

The client gains `list_objects()` with bounded keyset pagination. An opaque
`object_id` can then select `scope="object"` for context read/update. It cannot
select an actor, namespace path, SQL statement or execution capability.

Agent, worker, skill and sandbox objects are excluded from the generic selector:
their existing stricter scope-specific checks cannot be bypassed with an ID.
Shared object writes require the existing explicit per-document operator grant,
including AGENTS.md. Grant revocation and object retirement serialize against
managed requests and block receipt replay after the change takes effect.

```python
from uuid import uuid4

retained_request_uuid = str(uuid4())
objects = client.list_objects(limit=25)
# Select an object from its metadata; an ID does not confer rights.
selected = objects["objects"][0]
current = client.read("object", "MODEL.md", object_id=selected["id"])
receipt = client.update(
    "object", "MODEL.md", "# Working model\n\nVerified observation.\n",
    object_id=selected["id"], expected_revision=current["revision"],
    request_id=retained_request_uuid, reason="verified observation",
)
```

Shared registered-object files are projected separately from authored skills:

```text
/usr/local/skills/_objects/
    catalogue.json
    <object-id-hex>/<snapshot-digest>/
        AGENTS.md
        MODEL.md
        MEMORY.md
        object.json
```

Principal-private runtime contexts remain RPC-only and are **never copied into
this shared directory**. Every file/directory snapshot remains immutable. The
advisory catalogue can advance, but does not grant write/execute privileges or
retroactively erase files already read. The projector keeps bounded inventory,
byte, ownership, symlink/hardlink and digest checks, with the existing local
projection lock. The default pass is limited to 4,096 visible registered objects
and 64 MiB of text; an overrun fails explicitly rather than silently publishing
an incomplete inventory. There is no automatic snapshot garbage collection.

## Normal operator defaults

`potbot operator run --manifest ...` now enables context by default. It resolves
the **restricted application** credential through the existing credential
resolver, verifies `session_user`, and derives the principal from the exact
workspace membership. A managed service child retains its configured workspace,
principal, credential profile and schema. A standalone operator may specify
`--context-schema` or `POTBOT_CONTEXT_SCHEMA` (otherwise `potbot`).

An explicit principal or service schema mismatch fails before filesystem
allocation. The default snapshot parent is a private workspace-specific folder
under the existing controller-ledger parent. It must be operator-owned, outside
all guest mounts and mode 0700. Existing unsafe permissions are rejected, not
repaired. The existing root/agent contexts and migration-33 source relation must
be available before the operator exposes its health endpoint.

`--context-principal-id`, `--context-parent`, `--shared-skills` and the explicit
agent-projection flags remain supported. The new `--without-context` escape
hatch is for deliberate legacy migration only; it is not a qualified completion
of this feature and is never a silent fallback after a database/auth failure.

The operator reconciles the existing trusted tool inventory and both shared
catalogues on startup and on the existing polling interval. A per-launch worker
also prepares its own immutable context snapshots and the same workspace skill
library, so it does not depend on a Python object shared with a separate
operator process. Existing operator health checks, image qualification,
registered UID/GID, worker monitoring and process leases remain mandatory.

## Typed work on the context profile

Select `protocol="context-rpc/v1"` on an operator-installed
`SandboxedCogOperation`. The normal execution runtime chooses the existing
interactive audited supervisor automatically; the caller does not invoke the
Stage 10C helper separately.

A compatible guest worker uses the rebuilt image's stdlib client:

```python
import sys
from uuid import uuid4
sys.path.insert(0, "/opt/potbot")
from context_client import ContextClient

client = ContextClient()
work = client.work_input()  # fixed by the controller, not selected by the guest
model = client.read("agent", "MODEL.md")
# Perform only the installed worker's approved operation here.
value = {"status": "observed"}
request_id = str(uuid4())  # retain before sending; reuse only for the same result
client.finish_work("syn://type/the-installed-output-port", value, request_id=request_id)
# Exit normally after the receipt; log to stderr, never stdout.
```

The typed work envelope is bounded at 16 KiB and retains the installed input,
output type and invocation UUID. This is not a new arbitrary-code runner or a
child-work dispatch API. MODEL/MEMORY remain untrusted context data; AGENTS.md
never supersedes host policy.

A final work result and its session fence commit atomically. Identical result
retries recover the same receipt; a new/different result or cross-method reuse
of a mutation UUID is rejected. After a final result, further reads/edits/skill
operations are fenced, although the host can renew briefly for shutdown and
revoke the session. A stored result does **not** prove successful execution.
The controller also demands the correct semantic port, a zero exit code,
confirmed process-tree/audit shutdown and a live ordinary process claim.

Cancellation settles an in-progress preparation/launch before cleanup. Failed
or ambiguous process outcomes retain the existing claim for reconciliation.
Projection files are removed only when no process was dispatched or termination
is independently confirmed. Unconfirmed launches do not lose mounted source
files. The existing SYN worker heartbeat still encloses both execution profiles.

## Apply, deploy, and qualify

Apply this patch to the merged Stage 10C tree, then run the listed unit and real
PostgreSQL gates. Apply migration 33 with the **existing administrative**
credential profile:

```powershell
uv run --locked --extra postgres potbot admin migrate
```

Runtime continues to use restricted credentials. Rebuild and qualify the image
for the updated `/opt/potbot/context_client.py` and restart the ordinary operator.
Do not edit a qualified image in place or manufacture a qualification digest.
For large preexisting catalogues, run the atomic backfill within the installation's
approved migration maintenance window. A failed migration is not partial success.
Retain migration 33 and history during application rollback; old code intentionally
rejects unknown future schema revisions.

The dedicated `context-agent-rpc.yml` workflow adds the new tests without
removing existing gates. A successful PostgreSQL report requires at least all
11 lifecycle cases and no skipped context acceptance tests; it records the
checked-out commit and explicitly does not claim live sandbox qualification. Required source tests are:

```powershell
uv run --locked python -m pytest -q `
  tests/unit/test_context_agent_rpc.py `
  tests/unit/test_context_skill_lifecycle.py `
  tests/unit/test_context_worker_dispatch.py `
  tests/unit/test_context_integration.py `
  tests/unit/test_context_execution_path.py `
  tests/unit/test_context_operator_defaults.py `
  tests/test_workspace_context_contract.py `
  tests/test_context_inspection.py
```

Required disposable-PostgreSQL tests include
`tests/integration/test_context_lifecycle.py` and the existing context session,
skill, permissions, inspection and refresh suites. The lifecycle tests exercise
real source triggers, atomic registration/rollback, idempotent native-source
seeding, runtime version/retirement, principal isolation, grant checks,
final-result contention, replay fencing and transaction rollback.

On a qualified Linux/WSL2 host, acceptance must additionally create a workspace
and two agents through normal registration, observe automatic triples, run a
pinned context-capable Cog through `ExecutionCogRuntime`, test competing CAS
updates and denied cross-agent access, publish/load a skill, restart the
operator, and verify retained histories and new-launch snapshots. Repeat failure
cases for operator loss and unconfirmed process shutdown. A passing protocol
unit test is not evidence for those kernel and deployment boundaries.

## Delivery evidence and explicit limits

The delivery report records exact upstream preimage hashes, patch checks,
Python syntax checks and the selected-source test commands. Local tests exercise
real changed context modules, immutable filesystem operations, subprocess pipes
and runner methods; unavailable full-application/database/OS dependencies are
explicit unit doubles in the external preparation harness. That harness is not
shipped as a repository test bypass. The genuine PostgreSQL tests remain enabled
in CI and are not labelled as locally executed.

This patch completes the registered-object and normal Cog-runner integration
specified above. It does not replace the older host-local process/agent-home/tool
ledger, create distributed filesystem ownership, implement the separate Syn
object-memory graph proposal, auto-execute discovered skills, or certify arbitrary
third-party protocols. All new document, source-observation and work-result state
is PostgreSQL-backed. Multi-host process fencing and live host qualification
must not be inferred from successful context transactions.
