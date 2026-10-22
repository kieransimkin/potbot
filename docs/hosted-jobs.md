# Hosted models and observable jobs: architecture and implementation increment

Version **0.5.0.dev2** adds hosted generation, a durable PostgreSQL queue, a REST
API, WebSocket replay and a browser dashboard. This is an increment to the
model foundation. Automatic reasoning-tree search, learned memory admission,
MCP execution and live qualification of the five local deployments remain
separate implementation work. The historical Phase 5 report describes dev1;
this document and `hosted-jobs-verification.json` describe this increment.

## Architecture

The API authenticates a provisioned workspace principal and calls the existing
task service or the new job service. PostgreSQL revalidates that identity in
every transaction. Request fields never supply database authority, a provider
endpoint, a credential reference or a pricing policy.

Jobs retain a private input, the active immutable task revision and up to 16
distinct provider/model/role selections. Independent workers claim jobs using
row locks and fenced leases. A worker invokes selected models sequentially and
tags output by provider, model and reasoning/coding role. Both roles receive
the supplied input in this increment: a later controller will construct
role-specific context packets, compare alternatives and build the reasoning
graph. A successful generation job records successful text generation; it does
not mark the task or a proposed patch verified.

Before each call, the hosted gateway freezes the request and operator-policy
hash, checks the current task revision and reserves the existing task budget.
Its final authorization check and possible-dispatch marker commit together.
Provider responses pass through incremental secret redaction before output
events enter PostgreSQL. A terminal provider observation is retained before
budget settlement, so a crash between observation and settlement can finish
accounting without generating again.

The hosted request identifies a provider, model and API protocol. It does not
invent local weights or tokenizer hashes. The existing local/offline gateway,
typed proposal admission and exact-state patch verification retain their
separate contracts.

The browser uses the same REST operations exposed to other clients. WebSockets
read durable events using a workspace sequence, with bounded batches and send
deadlines. A transactional cursor avoids skipping a lower event whose
transaction committed after a higher sequence was allocated. API processes
share no essential in-memory queue state. Model-discovery caches are bounded
and expendable; jobs and events survive process restarts.

## Provider selection and credentials

| Provider ID | Service | Credential name | Environment override |
| --- | --- | --- | --- |
| `anthropic` | Claude | `providers.anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI | `providers.openai` | `OPENAI_API_KEY` |
| `xai` | Grok | `providers.xai` | `XAI_API_KEY` |
| `gemini` | Gemini | `providers.gemini` | `GEMINI_API_KEY` |
| `evolink` | EvoLink | `providers.evolink` | `EVOLINK_API_KEY` |

Use `credentials service-set --name NAME` to enter a value without echo and
save it in the existing native OS vault. `service-status` lists configuration
without revealing values. The dashboard's separate `api.access_token` needs
at least 32 visible ASCII characters; `POTBOT_API_TOKEN` overrides it. Workers
need provider credentials and PostgreSQL access, not the browser token.

Provider model lists are discovered explicitly using the provider's metadata
API. A catalogue read alone never triggers inference. EvoLink inventory retains
all reported model entries, including media-only models; those entries are not
eligible for this text-generation job. Unknown capabilities remain unqualified
and require an operator rule and capability check. New text model IDs need no
hard-coded release of Potbot. See [provider protocols](hosted-providers.md) for
native routes, EvoLink family routing, output limits and documentation sources.

The five local profiles remain in the catalogue: Phi-4-mini-instruct and Gemma
3 4B IT for reasoning; Qwen2.5-Coder-7B-Instruct, CodeGemma 7B IT and DeepSeek
Coder V2 Lite Instruct for coding. Their existing preflight remains available.
Hosted availability does not qualify those local deployments.

## Operator policy and accounting

Copy `examples/hosted-provider-policy.json` to an operator-managed file. It
defaults to disabled disclosure with no allowed models. After deciding which
task contents may be sent, set `allow_prompt_disclosure` and add explicit rules.
An illustrative rule is:

```json
{
  "provider": "openai",
  "model": "replace-with-discovered-model-id",
  "roles": ["reasoning", "coding"],
  "input_microunits_per_million": 1000000,
  "output_microunits_per_million": 2000000,
  "protocol": "responses"
}
```

These numbers are arithmetic examples, **not current prices**. One million
micro-units is one unit of the operator's common accounting currency. Obtain
rates for the chosen account/model and set ceilings that cover cache-write
premiums and other relevant token charges. Provider-wide `"model": "*"` rules
are supported; exact-model rules take precedence. Omitting `protocol` uses the
adapter's documented default. Rules and disclosure policy come from the worker
configuration, never a job payload. Restart the worker when changing policy.

Admission reserves calls, tokens, inference milliseconds, monetary micro-units
and concurrency. Hosted tokenizers and hidden provider templates are unavailable;
input admission therefore uses UTF-8 bytes plus explicit overhead, labelled a
conservative estimate. `input_token_allowance` can raise that bound. Final token
usage comes from the provider; calculated cost remains a conservative estimate
under the frozen operator rates, not an invoice. Reported overruns are charged
and can exhaust the task. The configured per-call ceiling is an admission bound,
not a guarantee about unknown provider billing behavior.

No request is automatically retried after a possible send. Missing terminal
usage, a lost worker or an interrupted call leaves an indeterminate result and
retains the reservation. `jobs invocations JOB_ID` finds the retained calls even
when the worker never published its result. `jobs invocation ID` shows accounting state;
`jobs reconcile ID --actual usage.json` records explicit usage after the job
is terminal. The usage file must contain `calls`, `tokens`, `inference_ms` and
`money_microunits`. Reconciliation never calls a provider or accepts a patch.
Cancellation closes local processing; it cannot prove a provider stopped
computing or stopped billing.

## Start and use the service

1. Use the existing [provisioning workflow](phase-2.md) to migrate the application
   schema and create a workspace, principal and repository. Use the restricted
   application PostgreSQL login for the server and workers. Migrations 11 and
   12 add job/event and hosted-invocation records; earlier checksums are preserved.
2. Save the needed provider keys and the separate API access token. Configure
   the operator policy described above.
3. Submit `examples/hosted-task-contract.json` through `task submit`, editing
   its objective, context, requirements and budget for the real task. The
   contract includes the extra `inference_ms` and `money_microunits` dimensions.
4. Start that task with `task start-generation TASK_ID --expected-token TOKEN
   --reason "Start hosted generation" --key START_KEY`, using the existing
   task CLI's `--dev-auth --workspace UUID --principal UUID` global options.
   The REST equivalent is `POST /v1/tasks/{id}/start-generation` with an
   idempotency envelope and `expected_token`/`reason` payload. A submitted task
   is initialized and started atomically; stale tokens or missing budgets fail.
5. Start the web service and an independent worker, then open the dashboard.

```powershell
.\start.ps1 serve --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
# Run in a second terminal:
.\start.ps1 jobs worker --workspace-id <UUID> --principal-id <UUID> --provider-policy providers.json
```

Use `start.bat` or `bash ./start.sh` with the same command arguments. All three
launchers already install runtime extras with `uv`; the new web/provider extras
are included. The default dashboard is `http://127.0.0.1:8765`. Enter only the
API access token, discover the relevant provider's models, and submit a job
against the running task. The advanced form accepts several model selections.
`examples/hosted-job.json` shows the raw payload; replace its task and model IDs.

The [HTTP guide](http-api.md) covers routes, envelopes, bearer authentication,
origin configuration, WebSocket authentication and reconnect cursors. The
[queue guide](jobs.md) covers states, cancellation, fencing and recovery.
`jobs worker --once` is useful for a controlled single claim; `jobs queue`,
`jobs list`, `jobs inspect`, `jobs events` and `jobs expire` support operation
without a browser.

## Implementation plan update

| Increment | Testable deliverable | Status |
| --- | --- | --- |
| H01 | Five provider identities, native protocols and bounded streamed responses | Implemented; HTTP transport mocked in tests |
| H02 | Dynamic inventory, native-vault credentials and operator disclosure/rate rules | Implemented; no live account qualification claimed |
| H03 | Scoped PostgreSQL jobs, idempotency, fenced claims and commit-ordered events | Implemented; real PostgreSQL tests |
| H04 | Durable hosted admission, crash-safe accounting and streamed secret redaction | Implemented; real PostgreSQL plus mocked provider tests |
| H05 | Authenticated REST routes, OpenAPI and cursor-based WebSocket replay | Implemented; request/stream and PostgreSQL integration tests |
| H06 | Queue dashboard, worker/server CLIs and platform-launcher integration | Implemented; browser QA and CLI/package checks |
| H07 | Account-specific live smoke tests and exact-model capability qualification | Requires configured operator accounts; no paid calls made during development |
| H08 | Bind hosted results into typed reasoning proposals and verified coding handoffs | Next controller increment |
| H09 | Ensemble graph search, model comparison and feedback-driven selection | Remains in the original controller/learning plan |

Future controller work should consume these job and invocation identities,
construct bounded role-specific contexts, retain source/reuse evidence and
apply the existing proposal validators. Generated text must not bypass that
admission layer or the repository's compile/test verification.
