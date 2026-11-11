# Stage 10B: agent context RPC and PostgreSQL operation leases

Base: `ed1aae6fa189e6a521ede996c92892aaa41d140f` (after the CI/temporary-cleanup
fix). This increment leaves those CI fixes intact. It adds an opt-in context
worker transport; it is not a claim that every existing worker or shared-tool
lifecycle has automatically acquired this profile.

The [Syn RPC transport decision](syn-release-dependency-and-node-deployment.md#route-application-rpc-through-syn)
now governs cutover. This stage records the original dedicated stdio
implementation and its PostgreSQL session guarantees. Its test results
establish local RPC behavior after admission.

## Implemented path

A trusted dispatcher admits a registered agent/run under its existing process
lease and starts its qualified interactive sandbox process. It then binds the
process's dedicated stdout/stdin pipes to `ContextWorkerChannel`. The channel
checks the operator, live process lease, registered home, workspace, agent UID/GID,
image pin, telemetry identity, run UUID and isolation receipt before issuing a
PostgreSQL context session. The process remains under its existing supervisor.

The host keeps the session capability. The guest receives neither a PostgreSQL
DSN nor that capability, and cannot select an actor, agent ID, workspace, object
name, token, session ID, path, SQL, or generation in JSON. The only guest selectors
are `scope` (`agent` or `sandbox`) and the exact document basename.

The exposed methods are `context.read` and `context.update`. An agent can read
its own triple and the sandbox triple, update its own triple, and update shared
MODEL/MEMORY only when its PostgreSQL principal has an explicit per-role grant.
A guest cannot change shared AGENTS.md, issue sessions, grant rights, renew
leases, access another agent, or run arbitrary tools through this channel.

This adds no network listener, Unix socket pathname, writable mount or general
host RPC endpoint. Stdout is reserved for frames; ordinary logs use stderr.
Do not silently attach this protocol to a worker which already uses stdout for
MCP or another protocol. Use the dedicated context-capable worker profile.

## Database and atomicity

Migration **31**, `context_session_schema`, introduces:

- `workspace_context_sessions`: principal-owned agent/run bindings, a digest of
  a random 256-bit host-only capability, expiry, absolute lifetime, generation,
  revocation state and bounded operation count.
- `workspace_context_session_receipts`: immutable request hashes and results,
  linked to the exact context revision and the immutable agent/run session.

Both tables use the existing workspace RLS/membership harness. The session
policy additionally requires a private agent object owned by the principal.
The transition trigger prevents changing agent/run ownership, changing the
capability or budget, reviving revoked/expired leases, and decreasing counters.
The migration owner remains separate from restricted runtime credentials.

`ContextSessionStore.execute()` locks the session row with `FOR UPDATE`, verifies
the host capability and live generation, and uses database wall-clock time
**after acquiring the lock**. It reuses the existing context service through a
transaction-only adapter: there is no second connection or nested independent
commit. The document CAS, immutable history, event, command idempotency record,
RPC receipt and request count commit or roll back together.

After any document-lock wait, expiry is checked again before commit. Revocation
locks that same session row, so it has a serial order with an in-flight update.
A successful revocation does not retroactively undo an earlier committed write;
subsequent operations and receipt replays are denied. A renewal never revives an
expired lease. Shared writer grants are checked and share-locked even before a
receipt replay, so removing a grant blocks later replay through the agent API.

Identical update retries reuse the canonical request UUID and return the same
receipt, even after newer document revisions. A reused UUID with different
content, scope, revision or reason is rejected. A dropped reply does not prove
rollback. No automatic retry with a new UUID is performed. If a session has
already been revoked, the trusted operator can inspect its receipt in PostgreSQL;
the guest is not allowed to resurrect the session to fetch it.

Revision `actor` fields retain the existing principal attribution. The new
immutable receipt/session relationship supplies the additional agent and run
attribution; this patch does not rewrite historical actor fields.

## Bounds and lifecycle

Frames use a four-byte big-endian length followed by UTF-8 JSON. Duplicate keys,
nonfinite JSON, unknown fields, invalid UUID spelling, booleans as revisions,
invalid UTF-8 and out-of-range revisions are rejected. Frame bodies are limited
to 2 MiB, document contents to 1 MiB, and each direction of a channel to 8 MiB.
JSON escaping can therefore impose a lower practical content limit. A channel
accepts at most 256 requests. Sessions default to 256 successful new operations
and have a hard maximum of 1,024. Identical receipt recovery does not consume a
second database operation budget entry, but still consumes the channel budget.

The default context lease is 60 seconds, renewed by the host every 15 seconds,
with a one-hour absolute deadline. Renewal requires the independently checked
operator and process lease. Each partial frame/read/write has a deadline.
Unexpected errors are replaced with fixed error codes/messages, never raw SQL,
paths, stack traces, request contents or credentials.

On cancellation, a synchronous database operation is allowed to settle before
the session is revoked; cancellation cannot undo a commit. The channel closes
the process and records its existing supervisor exit receipt. The caller must
not release the process lease unless shutdown is confirmed. Cleanup failure is
reported as indeterminate, not a clean shutdown. An unreachable database leaves
the last committed session expiry as the fail-closed upper bound on authority.

## Host integration

`ContextWorkerChannel` is a host-only adapter, not a public admission API. Use
it only after the ordinary controller task/budget/image/process admission. The
existing worker dispatcher remains responsible for selecting approved argv.
For a context-capable worker, set `telemetry_identity_kind='agent'`, the correct
workspace/agent/run UUIDs, its registered UID/GID, and its registered home CWD.
Use an empty `ProcessRequest.stdin` because the pipes belong to RPC.

Illustrative wiring within that already-authorized dispatcher:

```python
from potbot.context.sessions import ContextSessionStore
from potbot.context.worker_channel import ContextWorkerChannel
from potbot.domain.serialization import content_hash
from potbot.tools.interactive_supervisor import InteractiveProcessSupervisor

supervisor = InteractiveProcessSupervisor(operator.backend)
channel = ContextWorkerChannel(
    store=ContextSessionStore(restricted_database),
    principal=trusted_workspace_context,
    agent_id=registered_agent_id,
    guard=admitted_process_guard,
    registry=registered_agent_homes,
    check_operator=operator.monitor.require_active,
)
admitted_process_guard.start()
try:
    process = await supervisor.open(
        admitted_request, cancelled=lambda: admitted_process_guard.fenced
    )
    result = await channel.serve(process)
    # channel.serve refuses a successful result without confirmed shutdown.
    admitted_process_guard.finish(
        evidence_sha256=content_hash(
            {
                "context_session_id": str(result.session_id),
                "isolation_identity": result.exit.isolation_identity,
                "requests": result.channel.requests,
            }
        )
    )
except BaseException:
    admitted_process_guard.abandon()  # retain the existing reconciliation boundary
    raise
```

The interactive supervisor's stray uninitialized status-limit expression is
removed because it prevents importing this dependency. Its worker-monitor call
now forwards the already-trusted identity kind and run UUID, matching the
one-shot supervisor's attribution. No isolation policy is relaxed.

## Guest client and rollout

The image includes `/opt/potbot/context_client.py`, a standard-library-only
client. Rebuild and qualify the image before using that path. A worker may use:

```python
import sys
from uuid import uuid4

sys.path.insert(0, "/opt/potbot")
from context_client import ContextClient

client = ContextClient()
current = client.read("agent", "MODEL.md")
request_id = str(uuid4())  # retain before sending; reuse only for an identical retry
receipt = client.update(
    "agent",
    "MODEL.md",
    "# Working model\n\nVerified environment observations.\n",
    expected_revision=current["revision"],
    request_id=request_id,
    reason="verified observation",
)
print(f"Committed revision {receipt['revision']}", file=sys.stderr)
```

Apply the patch, then run `uv run --locked --extra postgres potbot admin migrate`
using the existing host-only `POTBOT_ADMIN_DSN`/admin credential profile. The
running service continues to use restricted `POTBOT_DSN` credentials. There is
no guest-facing session-issuance HTTP route. Existing agents without this worker
profile retain their prior behavior.

To roll back deployment, stop selecting this profile and stop its workers,
revoke their sessions where possible, and retain migration 31 and its history.
Do not drop the receipt tables or revert the schema registry after applying the
migration: older code intentionally rejects unknown future schema versions.

## Tests and outstanding boundaries

`tests/unit/test_context_agent_rpc.py` exercises forged fields, frame bounds,
real stream/socketpair and subprocess-pipe exchanges, cancellation, session
validation, worker/run binding and shutdown/revocation ordering. These are
unit/transport tests, not proof of Bubblewrap isolation.

`tests/integration/test_context_sessions.py` uses the existing disposable
`app_env` PostgreSQL fixture for real row locks, concurrent writers, duplicate
request recovery, revocation races, same-workspace peer RLS, immutable identities,
shared grant revocation and rollback after a lock wait crosses the lease expiry.
The dedicated `context-agent-rpc.yml` workflow runs it without removing any
existing repository gate.

Local preparation used complete, Git-blob-verified copies of every changed
existing file and a selected-file dependency snapshot, not a full checkout.
Live PostgreSQL, the repository's pinned Python 3.12 toolchain, complete repository
lint/type checks and real Linux/WSL2 worker isolation were not available locally;
CI and the qualified host must run those gates before production enablement.

This stage moves **context-operation session coordination** into PostgreSQL.
It does not replace the existing host-local workspace/process lease ledger,
provide distributed filesystem ownership, automatically enable every worker,
provision every primitive lifecycle, publish Syn releases externally, or add
skill-authoring RPCs. Do not describe the entire sandbox runtime as multi-host
safe on the strength of these database sessions alone.

## Design references

- PostgreSQL row locks: https://www.postgresql.org/docs/18/explicit-locking.html
- Psycopg transaction scopes: https://www.psycopg.org/psycopg3/docs/basic/transactions.html
- Python framed stream primitives: https://docs.python.org/3.12/library/asyncio-stream.html
