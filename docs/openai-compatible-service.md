# OpenAI-compatible API and one-command persistent service

This feature extends the existing Potbot hosted-provider system. It is an **opt-in,
operator-policy-bound interactive inference API**, not an alternative source of
PostgreSQL authority or a replacement for Potbot's durable task/job operations.

**Current integration reading guide, 2026-11-01:** R3/R4 tree mode, R5 critique
and [R6 PostgreSQL coordination](implementation/20-postgres-reasoning-and-model-providers.md)
extend the original fixed proposal/critique/synthesis path. [R6.1 accounting](implementation/21-reasoning-task-budgets-and-live-provider-qualification.md)
adds an explicit task-bound mode. Read these increments together; the initial
fixed-mode implementation and its former integration gaps do not describe every
current server configuration. These are documented source capabilities, with
deployment qualification still required.

## Features and capability boundaries

- `GET /v1/models`: OpenAI-style `object=list` with policy-enabled text models,
  qualified as `<provider>/<model>`, plus the virtual `potbot` entry. The known
  local Qwen embedding model is listed **only after installing its local files**;
  the actual supervised encoder verifies pinned hashes before generation.
- `GET /v1/models/{id}`: OpenAI model object.
- `POST /v1/chat/completions`: text-only OpenAI messages, text completions and
  SSE `chat.completion.chunk` streaming, including optional `include_usage`.
- `POST /v1/responses`: text-only Responses API, including text-delta SSE.
- `POST /v1/embeddings`: local Qwen3 0.6B supervised encoder, float vectors only;
  requests never implicitly download model weights.
- `GET /v1/potbot/models`: the original Potbot model-directory envelope;
  existing dashboard clients use this route. Other native `/v1/...` routes keep
  their original schema and authorization rules.
- `/health` remains a public **liveness** route, not provider readiness.

Namespaced provider IDs avoid collisions between model IDs reused by different
providers. Catalogue discovery remains explicitly controlled by the existing
`POST /v1/providers/{provider}/models/discover` route. A model without an allowed
operator policy rule or usable configured credential is not advertised as
available for hosted text inference. Local default reasoning/coding profiles are
*not* advertised for live generation until a working local backend exists.

## The virtual `potbot` model

`model="potbot"` uses only configured, policy-enabled reasoning models. The
standard server's **tree mode** builds on independent structured hypotheses,
registered evidence Cogs, R5 critique and bounded R6 adaptive expansion. With
the provisioned PostgreSQL backend, migration 24 stores workspace-scoped runs,
fenced checkpoints, steering and events; a local JSON snapshot is supplemental
diagnostic data. The normal bounded configuration does not establish distributed
parallel branch execution or a fully qualified autonomous coding workflow.

`--reasoning-mode fixed` retains the **bounded three-call proposal → critique →
synthesis** fallback for providers unable to meet structured tree contracts.
Neither mode exposes hidden chain-of-thought. Standard `serve` does not install
live evidence-tool grants: absent channels are reported unavailable, and model
proposals cannot authorize tools. A final text synthesis does not by itself
constitute a verified coding artifact.

Ordinary interactive requests remain **outside durable task-budget accounting**.
R6.1 permits a trusted caller to bind a virtual-tree request to an existing
running task using `potbot_task_id`, a Potbot extension. Migration 25 then binds
predispatch reservations and invocation receipts to the task/run/node and
settles observed usage through the existing budget ledger. It does not turn
arbitrary direct-model calls into jobs. Workspace authentication/RLS, task
contract and run-lease checks remain mandatory. Expired or uncertain charged
calls require reconciliation; they must not be silently reissued.

The API rejects unsupported tool/function calling, multimodal input, file
attachments, unsupported sampling/JSON modes, and unknown parameters rather
than pretending the feature worked. Model access and prompt disclosure are
controlled entirely by the existing operator policy. Outgoing inference is
allowed only through configured provider adapters with pinned endpoint origins.

## Install / start / stop

All platform launchers already install the pinned uv release, managed Python
3.12 and **locked extras**. They now expose the same persistent service command:

```powershell
# From the repository root on Windows
.\start.ps1 service install --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json --migrate
.\start.ps1 service status
.\start.ps1 credentials api-show
.\start.ps1 service logs
.\start.ps1 service stop
.\start.ps1 service start
```

```cmd
start.bat service install --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
start.bat credentials api-show
start.bat service status
```

```bash
./start.sh service install --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json --migrate
./start.sh service status
./start.sh credentials api-show
./start.sh service logs
./start.sh service stop
./start.sh service start
```

The workspace and restricted application principal **must already be provisioned**
using the existing `admin workspace-create`, `login-create`, `principal-bind`
workflow. Installation deliberately does not silently create a privileged database
principal or bypass a workspace's mandatory sandbox root. `--migrate` optionally
runs migrations with the separately configured admin connection. The installer
checks the configured principal's PostgreSQL membership before registering the
service. PostgreSQL remains an external prerequisite.

The install command creates a native-vault API token when missing and shows it
once **only on an interactive terminal**. `credentials api-show` explicitly
retrieves it later, and refuses redirected output/CI terminals. The public
service configuration contains workspace/principal UUIDs, policy path, listener,
credential profile and log settings **but no key, DB password or provider secret**.
Do not expose the service listener publicly without TLS, a reverse proxy,
additional user authentication and rate limits.

The installer registers an unprivileged **user-owned** service:

| Host | Service manager | Startup and lifetime |
| --- | --- | --- |
| Linux | systemd `--user` | Enabled for user sessions; optional `loginctl enable-linger USER` for unattended boot |
| macOS | `launchd` LaunchAgent | Starts at user login; uses that user's Keychain |
| Windows | Task Scheduler ONLOGON | Starts when the installing user logs in, not as SYSTEM |

A small service supervisor starts **both** the API and the existing worker as
independent children and restarts failed children. `service run` operates in the
foreground for troubleshooting; `service uninstall` unregisters the manager but
**retains workspace data and credentials**. `service install --replace` updates an
existing configuration. A service is not claimed to be running until `/health`
responds. Changing deployment users can make native-vault credentials unavailable.

Use `--download-embeddings` to explicitly download and SHA-256 verify the pinned
Qwen embedding files before starting. Neither installing the service nor
listing the model triggers an unattended multi-GB download. The console displays
progress, bytes, speed and ETA. Existing `dependencies download` is retained.

## Client example

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="<TOKEN>")
print([model.id for model in client.models.list().data])
response = client.chat.completions.create(
    model="potbot",
    messages=[{"role": "user", "content": "Explain the trade-offs."}],
)
print(response.choices[0].message.content)
```

Hosted direct calls can be made using e.g. `model="openai/<allowed-id>"`.
The default service listener is loopback-only (`127.0.0.1:8765`). Every API
request still requires a bearer token and a currently provisioned workspace
binding. By design, the OpenAI wire shape does not use Potbot's command envelope.

## Observable console, privacy, retained logs

Console events include `api.query`, `model.query`, `reasoning.stage`,
`model.response`, `api.response`, `api.failure`, `model.start`, `model.delta`,
`job.claimed`, `job.finished`, `model.download`, `model.install.start`,
`model.install.completed` and supervisor start/restart/stop. Each inference event
contains a correlation/run ID, model, elapsed time, usage where reported and
bounded content size. JSONL logs rotate after 10 MiB and retain five backups.

**Default mode logs metadata only.** `service install --log-content` or
`serve --log-content` explicitly enables raw prompt/response text logging,
including the synthetic workflow's provider-visible intermediate messages.
Logging never includes bearer authorization headers or known configured secrets;
secrets are redacted from content, and content is bounded. Do not opt into text
logging on a shared host without appropriate data retention, access-control
and privacy settings. Provider *hidden* reasoning tokens/traces are never
requested or exposed.

When running as a service, inspect `.potbot/service/{api,worker,supervisor}.jsonl`
or call `service logs`. These are diagnostic streams, not signed/durable SYN
ledger receipts. Existing durable job and SYN execution events remain the
authoritative sources for those operations. The compatibility API emits
correlation IDs but deliberately does not forge SYN execution records.

## Tests and known limitations

Run `uv run --locked pytest -q tests/unit/test_openai_compat.py
 tests/unit/test_persistent_service.py tests/unit/test_api_server.py`.
The pure endpoint tests use an injected fake provider: they do **not** make paid
provider calls. The service-manager tests isolate native manager registration.
To qualify a deployment, separately check a real PostgreSQL principal, a usable
OS vault, systemd/launchd/Task Scheduler lifecycle, provider credentials and
real model calls. Also run the applicable R6/R6.1 migration/RLS/lease, task-budget
and provider qualification suites. Remaining work includes parallel branch
workers, admitted live evidence tools, complete API conformance and target-host
qualification. Preserve the distinction between ordinary interactive use and
the explicitly task-bound accounting path described above.

## Reasoning increment history and current authority

The [R3/R4 guide](implementation/18-model-prompts-r3-r4.md) defines model-specific
prompt profiles, independent hypotheses and registered evidence admission.
[R2.5/R5](implementation/19-live-steering-r5-api-capabilities.md) adds steering
decisions and critique. [R6](implementation/20-postgres-reasoning-and-model-providers.md)
adds PostgreSQL coordination and bounded adaptive expansion; [R6.1](implementation/21-reasoning-task-budgets-and-live-provider-qualification.md)
adds task-bound accounting. Later increments supersede earlier statements that
these particular integrations remain future work.

`--reasoning-root` selects the operator-trusted diagnostic snapshot location,
including `.potbot/reasoning/<workspace-id>/<run-id>.json` under the default
layout. It does not replace configured PostgreSQL authority. Native outbound
OpenAI/Gemini transports do not establish full inbound OpenAI/Gemini server
compatibility; retain the explicit [capability matrix](implementation/19-live-steering-r5-api-capabilities.md)
and [remaining conformance requirements](implementation/recent-potbot-development-reconciliation.md).
