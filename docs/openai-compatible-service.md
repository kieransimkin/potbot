# OpenAI-compatible API and one-command persistent service

This feature extends the existing Potbot hosted-provider system. It is an **opt-in,
operator-policy-bound interactive inference API**, not an alternative source of
PostgreSQL authority or a replacement for Potbot's durable task/job operations.

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

`model="potbot"` selects up to two configured, policy-enabled reasoning models.
It performs a **bounded three-call proposal → critique → synthesis** workflow,
using provider-native transports and output limits, and returns only the final
synthesis. It is suitable for interactive reasoning queries, not a claim that
the unfinished Phase-7 autonomous reasoning-tree controller is active. In
particular it does not submit a verified coding artifact, inspect repositories,
run tools, populate SYN graph records, or expose hidden chain-of-thought. The
separate durable job API retains the application's authoritative task budget,
invocation records, cancellation and reconciliation lifecycle. Interactive API
usage is **not durable task-budgeted**; operators must set conservative provider
rates, call limits and concurrency controls at their own deployment boundary.

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
real model calls. The existing Phase-7 controller/graph integration and durable
interactive-task budget accounting remain further work rather than hidden
implementation claims.

## Model-aware R3/R4 tree mode (new)

By default `serve` connects the virtual `potbot` model to two independent
structured hypothesis-generator calls, bounded evidence searches via registered
Cogs, and one final synthesis invocation. It records tree snapshots privately
under `.potbot/reasoning/<workspace-id>/<run-id>.json`. Disable with
`--reasoning-mode fixed` when qualifying providers unable to emit the strict
proposal JSON. `--reasoning-root` selects an operator-trusted storage root.
The standard server does not have live evidence-tool grants; absent channels are
explicitly unavailable. See [the R3/R4 guide](implementation/18-model-prompts-r3-r4.md)
for scope, model prompt profiles, admission boundaries and verification.
