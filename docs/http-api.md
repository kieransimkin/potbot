# REST, WebSocket and queue dashboard

The `serve` command exposes the existing task services and the durable job queue
through an ASGI application. The dashboard at `/` uses these same interfaces.
Workers run independently: a browser disconnect or server restart does not
discard submitted jobs or their retained events. See [jobs.md](jobs.md) for
claiming, cancellation, lease recovery and uncertain execution semantics.

## Start the service

Use the standard launcher to install the optional web dependencies through uv:

```powershell
.\start.ps1 credentials service-set --name api.access_token
.\start.ps1 serve --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
```

Use `start.bat` or `bash ./start.sh` with the same arguments. PostgreSQL settings
come from the existing credential flow. The configured application database
login must already be provisioned for the requested workspace and principal;
passing UUIDs does not create database authority.

The default listener is `http://127.0.0.1:8765`. Open this address and enter the
API access token. The server can also resolve its token from `POTBOT_API_TOKEN`.
Provider credentials are distinct from this access token and stay in the
server's credential resolver; the dashboard never asks for a provider API key.

Run the worker in a second terminal:

```powershell
.\start.ps1 jobs worker --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
```

The worker polls once per second when idle. `--once` attempts at most one queued
job and exits; `--worker-id` supplies a bounded operator identity, and
`--poll-interval` changes the idle interval within 0.1–30 seconds. Interrupting a
worker does not authorize replay of an interrupted provider call. Inspect its
job and expire an abandoned lease before reconciling known provider usage.

Create a task with the existing task-contract interface, then explicitly start
its hosted generation lifecycle before submitting jobs. Use
`POST /v1/tasks/{episode_id}/start-generation` with a v1 envelope whose payload is
`{"expected_token": 0, "reason": "Start budgeted generation."}` and a stable
idempotency key. The expected token must be the current task token. This requires
a submitted task with positive hosted-inference time and money allowances. It
does not capture a repository or authorize applying generated code. The existing
`task start-generation` CLI exposes the same operation; see
[hosted-jobs.md](hosted-jobs.md) for the complete setup example.

The dashboard provides queue counts, filtered job pages, detailed state and
progress, model selections, cancellation, live events, event history and explicit
provider catalogue discovery. Its form submits a model-generation job against an
existing active task. Advanced JSON accepts several reasoning/coding selections.
This release provides a hosted-model job runner; it does not represent the later
autonomous reasoning-graph controller as complete.

## Identity and transport

REST requests under `/v1/` require `Authorization: Bearer <token>`. The configured
bearer binding supplies a `WorkspaceContext`. PostgreSQL rechecks the login and
workspace membership in every job/task transaction; request bodies and query
strings cannot select a different workspace or principal. Resource denials use
the existing sanitized 404 response to avoid disclosing another workspace's IDs.

The default server is an explicitly configured workspace service using a static
bearer token. An externally deployed service needs TLS and an appropriate
authentication/proxy boundary. Browser origins must appear in the exact origin
allowlist; wildcard origins and URL credentials are not supported. Add an origin
with `--allowed-origin`, for example `http://localhost:8765` when using that name.
A loopback listener permits its bind origin, `127.0.0.1`, and `localhost` on the
configured port by default. Supplying `--allowed-origin` replaces those defaults;
repeat the flag to allow several exact origins. A non-loopback bind address
requires an explicit allowlist. The CLI accepts literal bind IP addresses or
`localhost`, disables access logs that could record accidental URL secrets, and
does not trust forwarded proxy headers implicitly.

The dashboard keeps its token in JavaScript memory, clears the password field
after connecting, and clears its state on disconnect/page exit. It does not use
cookies, browser storage, URL tokens, inline scripts, third-party scripts or a
CDN. Job content is inserted as text. The page's Content Security Policy limits
scripts/styles/connections to its own origin and prohibits framing.

`/health` is public liveness metadata, not a database or worker readiness claim.
`/openapi.json` is a public API definition containing no workspace records.

## REST operations

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | Listener version and liveness. |
| GET | `/openapi.json` | OpenAPI description, including bearer security and page parameters. |
| GET | `/v1/queue` | State counts, queue age, expired leases and a consistent event cursor. |
| GET | `/v1/jobs` | Newest-first jobs, with `state`, opaque `after` and `limit` filters. |
| POST | `/v1/jobs` | Idempotent job submission; returns 202 and the durable job record. |
| GET | `/v1/jobs/{job_id}` | Current public job state, model selections, progress and result metadata. |
| POST | `/v1/jobs/{job_id}/cancel` | Request cancellation; an empty payload is required. |
| GET | `/v1/jobs/events` | Workspace event replay using an integer `after` sequence. |
| GET | `/v1/jobs/{job_id}/events` | Event replay restricted to one accessible job. |
| GET | `/v1/jobs/{job_id}/invocations` | Find safe invocation records and accounting IDs, including after a worker crash left no final job result. |
| GET | `/v1/models` | Local model profiles, configured providers and cached discovered models. |
| POST | `/v1/providers/{provider}/models/discover` | Explicit bounded model discovery; requires an empty payload. |
| GET | `/v1/invocations/{invocation_id}` | Scoped hosted invocation state, accounting and policy identity, through the configured safe reader. |
| POST | `/v1/tasks` | Existing immutable task-contract submission. |
| GET | `/v1/tasks/{episode_id}` | Existing task state and current contract. |
| GET | `/v1/tasks/{episode_id}/contracts` | Immutable contract history. |
| GET | `/v1/tasks/{episode_id}/budget` | Task budget and reservation state. |
| POST | `/v1/tasks/{episode_id}/contracts` | Existing compare-and-swap contract revision. |
| POST | `/v1/tasks/{episode_id}/control` | Existing pause/resume/cancel task controls. |
| POST | `/v1/tasks/{episode_id}/start-generation` | Explicitly start a submitted task's budgeted hosted-generation lifecycle using its current update token. |

GET requests do not require a body. Job pages default to 50 entries; event pages
default to 100. Both HTTP page limits are at most 200. Unknown or duplicate query
parameters fail instead of silently changing meaning. Job-page cursors are opaque
and cannot be interchanged with integer event cursors.

Mutations use the established v1 request envelope:

```json
{
  "schema_version": 1,
  "request_id": "6adcc9dc-f727-4a41-823b-0bd70be2ff76",
  "idempotency_key": "submit-my-first-model-job",
  "payload": {
    "job_type": "model.generate",
    "episode_id": "8cdad074-dc7a-4a3c-97d1-fb0d6deef083",
    "selected_models": [
      {"provider": "openai", "model": "operator-configured-model-id", "role": "reasoning"},
      {"provider": "anthropic", "model": "operator-configured-model-id", "role": "coding"}
    ],
    "input": {
      "prompt": "Explain a safe implementation plan for the current task.",
      "max_output_tokens": 2048,
      "timeout_seconds": 60
    }
  }
}
```

The example UUIDs/model IDs are placeholders. Use a real existing task and model
identities allowed by the operator's provider policy. Submission retains the
input privately for the worker; listing jobs does not expose the prompt, provider
credentials or full internal lease payload. Provider output is available through
the bounded job event stream and the worker's authorized result interfaces.

To cancel, reuse `/v1/jobs/{id}/cancel` with a fresh request ID, a stable
idempotency key for that cancellation command, and `"payload": {}`. A queued job
can become `cancelled` immediately. A running job first becomes `cancelling`;
provider dispatch/usage may still require reconciliation. A successful HTTP
cancellation request does not claim that remote computation stopped instantly.

Responses preserve the existing envelope:

```json
{
  "schema_version": 1,
  "request_id": "6adcc9dc-f727-4a41-823b-0bd70be2ff76",
  "data": {"id": "the-job-id", "state": "queued"},
  "error": null
}
```

Failure responses put a stable `code`, safe `message` and `retryable` flag in
`error`; `data` is null. Database/provider exception text is not returned. Bodies
require `application/json`, are bounded to 1 MiB by default, and reject duplicate
JSON keys, unsupported envelopes and unbounded parsing. Keep a submission's
idempotency key if retrying after a lost HTTP response. A discovered catalogue
entry is metadata, not qualification to execute that model. Discovery itself
does not generate model output or share task contents.

The catalogue retains entries for image, audio, video and other non-text models,
but the dashboard excludes known non-text or configuration-disabled entries from
the generation form's suggestions. Unknown or opaque future model IDs still
require capability verification; name heuristics cannot fully enforce a
provider's changing media capabilities. Manual IDs and advanced JSON remain
subject to the server's provider policy and request validation.

## Command-line queue and recovery interfaces

Each `jobs` subcommand accepts `--workspace-id`/`--workspace`,
`--principal-id`/`--principal`, and optional `--schema`. Put those flags after the
subcommand, as in `jobs queue --workspace-id <UUID> --principal-id <UUID>`.
Commands print versioned JSON response envelopes, and each service operation
uses the same provisioned database authority as the HTTP API.

| Command after `jobs` | Purpose and required arguments |
| --- | --- |
| `queue` | Read workspace queue counts, age and lease state. |
| `list [--state running] [--after CURSOR] [--limit 50]` | Read an opaque-cursor job page. |
| `inspect JOB_UUID` | Read one public job record. |
| `submit --input job.json --key KEY` | Submit the four-field job payload shown above, without the HTTP envelope. |
| `cancel JOB_UUID --key KEY` | Request cancellation idempotently. |
| `events [--job-id JOB_UUID] [--after 0] [--limit 100]` | Read an integer-cursor event page. |
| `worker --provider-policy providers.json [--once]` | Run the independent hosted worker. |
| `expire [--limit 200]` | Mark abandoned expired worker leases `indeterminate`; never requeue them automatically. |
| `invocation INVOCATION_UUID` | Read safe invocation status, accounting and policy identity. |
| `invocations JOB_UUID` | Find a job's invocation IDs and safe accounting metadata even when its final result is absent. |
| `reconcile INVOCATION_UUID --actual usage.json` | Settle supported unresolved accounting for a terminal job using known usage. |

`invocation`, `invocations` and `reconcile` require neither a provider policy file
nor a provider request. After expiring an abandoned lease, use `invocations` to
find its retained accounting IDs. Reconciliation does not regenerate output, replay a call, or qualify
the job's code. Its usage file must contain all four nonnegative integer fields:

```json
{"calls":1,"tokens":450,"inference_ms":800,"money_microunits":50}
```

Use the actual accounting evidence for the invocation; `calls` must be 0 or 1.
Repeating reconciliation must agree with already settled usage. Provider policy
files and credentials remain operator-owned, outside submitted job JSON.

## WebSocket protocol

Connect to one of:

- `ws://127.0.0.1:8765/v1/jobs/stream` for all accessible workspace job events.
- `ws://127.0.0.1:8765/v1/jobs/{job_id}/stream` for one accessible job.

Use `wss://` with TLS. No query parameters are accepted. The first text message,
sent within five seconds, authenticates and supplies the last received cursor:

```json
{"type":"authenticate","token":"your-access-token","after":0}
```

The server validates the token and reads the scoped durable event service before
acknowledging the connection:

```json
{"schema_version":1,"type":"ready","cursor":0}
```

Each retained event arrives separately:

```json
{
  "schema_version": 1,
  "type": "event",
  "cursor": 42,
  "event": {
    "schema_version": 1,
    "sequence": 42,
    "job_id": "the-job-id",
    "event_type": "job.output",
    "data": {"text": "A streamed output segment", "provider": "openai", "model": "configured-id"},
    "recorded_at": "2026-10-22T00:00:00Z"
  }
}
```

The nested event also carries its durable event identity. State, progress and
heartbeat events contain a public job snapshot in `data.job`; output events
contain `data.text` and, when supplied by the worker, model/provider attribution.
See the event types in [jobs.md](jobs.md).

Remember the last fully processed `cursor`. After reconnection, send that value
as `after`: replay is strictly greater than it. Sequence numbers are
workspace-scoped, bounded to JavaScript's safe integer range and allocated in
commit order. Filtering by job can create gaps. Clients should deduplicate by
sequence if they repeat a previous cursor; the protocol does not claim that a
network send proves a client processed the event.

The server sends a heartbeat after 15 idle seconds and accepts only this optional
post-authentication control message:

```json
{"type":"ping"}
```

Its reply is `{"schema_version":1,"type":"pong","cursor":42}`. Heartbeats have
the same envelope with `type: "heartbeat"`. These are connection messages, not
new durable job events.

### Bounds, reconnects and multiple servers

Each stream holds one bounded page of up to 50 durable events. It polls the
database with a fresh scoped transaction every 0.5 seconds when caught up. The
implementation does not require an in-process subscriber list or Redis, and
separate web processes can replay the same PostgreSQL history. Database work runs
off the ASGI event loop so one synchronous service read does not block every
connection.

An authentication frame is at most 4 KiB, later control frames at most 64 bytes,
and outgoing frames at most 512 KiB by default. The outgoing allowance accounts
for JSON escaping of a bounded 64 KiB output delta. A send that takes over five
seconds closes with code `1013`; reconnect with the last received cursor.
Authentication/scope/protocol failures close with `1008`; other sanitized server
failures use `1011`. Disconnect cancels both stream tasks. These connection
timeouts do not mark the underlying job as failed or cancelled.

For a snapshot followed by updates, read the queue/job page's `event_cursor` and
then stream after that value. If combining separate snapshots, use the smallest
cursor and apply repeated updates idempotently. The bundled dashboard does this
on initial connection and retains its last streamed cursor during reconnects.

## Embedding the server

`potbot.api.server.create_app` takes explicit `task_service`, `job_service` and
`authenticate` arguments, plus optional scoped catalogue/discovery/invocation
callbacks and `ServerSettings`. This keeps database credentials and provider policies outside
the HTTP request document. The framework-independent task API remains available
for existing CLI and WSGI consumers.

The web stack is an optional dependency group. Domain imports and model catalogue
inspection remain independent of FastAPI, Uvicorn and network connections.

## Verification and primary references

Focused ASGI tests exercise header authentication, request and query bounds,
scope denials, response sanitization, job mutation envelopes, task compatibility,
catalogue discovery, WebSocket replay/live delivery and slow-consumer deadlines.
The durable queue's PostgreSQL tests separately establish transactional event
ordering, claims, cancellation and workspace isolation.

- [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/) describes
  socket lifecycle handling and the limitations of process-local broadcast state.
- [FastAPI WebSocket testing](https://fastapi.tiangolo.com/advanced/testing-websockets/)
  documents using the ASGI test client for WebSocket sessions.
- [Starlette WebSockets](https://www.starlette.io/websockets/) documents the
  underlying connection, send/receive and close interfaces.
