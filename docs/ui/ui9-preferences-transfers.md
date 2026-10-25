# UI-9 — Real workspace preferences, downloads and model installation receipts

This increment connects the existing Next.js Preferences, Downloads, and Model
Installations panes to **separate authenticated workspace APIs**. Xpra remains
the sole X application transport. No fake transfer bytes, model downloads or
user credentials are returned to browser JavaScript.

## Routes and authority

| Potbot Python endpoint | Next.js same-origin proxy | Behavior |
| --- | --- | --- |
| `GET /v1/ui/workspace` | future multi-workspace discovery | Returns the *current authorized* workspace identity only |
| `GET/PUT /v1/ui/preferences` | `GET/PUT /api/ui/preferences` | Optimistic revision, strict non-secret UI setting whitelist |
| `GET /v1/ui/downloads` | `GET /api/ui/downloads` | Real per-workspace transfer receipts and byte counters |
| `POST /v1/ui/downloads` | `POST /api/ui/downloads` | Enqueue an explicitly approved, immutable artifact manifest |
| `POST /v1/ui/downloads/{id}/cancel` | `POST /api/ui/downloads/{id}/cancel` | Request cancellation or cancel an unstarted transfer |

Browser requests require the existing HttpOnly operator session. Every write
requires a same-origin check and tight JSON bounds. The backend still verifies
its bearer principal against PostgreSQL RLS on **every transaction**. Read
projections exclude local paths, arbitrary download URLs, credentials and raw
worker exceptions. Preference updates use a revision to reject stale writes.

The backend migration is **26, `ui_resources_schema`**. Do not replace or
reorder any earlier migration. Apply it through Potbot's existing restricted
admin migration workflow; the API process cannot perform schema creation.

## Dedicated transfer worker

The operator provisions a storage directory, e.g. a workspace-owned `/var/ai`
volume or a host-controlled shared model cache (according to existing sandbox
mount/ownership rules), and runs the worker separately from the HTTP service:

```bash
PYTHONPATH=src python -m potbot.api.ui_transfer_worker \
  --workspace-id YOUR-WORKSPACE-UUID \
  --principal-id YOUR-PRINCIPAL-UUID \
  --model-root /path/to/operator-provisioned/models \
  --once
```

Omit `--once` for a polling daemon and configure a supervisor restart policy.
**A worker does not launch automatically from a browser request**. If no
worker runs, the queued transfer remains queued. Restricted login scope is
checked by the existing `database_context` implementation.

Only specifically approved Hugging Face repository IDs, immutable commit SHAs,
base filenames, known quantization labels, exact SHA-256 and expected size are
accepted. Actual outbound HTTP uses HTTPS, no ambient proxies, a restricted
redirect destination list, and an exact byte bound. A dedicated worker writes
an exclusive temporary file, updates durable byte progress, verifies size and
SHA-256, and hard-links the complete verified file into the operator-provisioned
model cache. Temporary files are removed on failure. The installed state means
**verified file present**, not a functional or licensed model deployment.

The admission API serializes workspace requests and rejects more than 12 active
transfers or a cumulative 256 GiB admitted byte budget. Gated or inaccessible
artifacts fail with a generic reason; this worker does not bypass vendor
authorization. Cancellation is cooperative at chunk boundaries; a crash leaves
its lease requiring a separate indeterminate-reconciliation pass, never a
silently claimed success.

Example artifact payload (values must come from a *real* upstream file listing):

```json
{
  "model_id": "Qwen/Qwen3-0.6B",
  "artifact_repository": "TRUSTED-PUBLISHER/ACTUAL-GGUF-REPOSITORY",
  "revision": "EXACT-40-HEX-COMMIT-SHA",
  "filename": "EXACT-FILENAME.Q4_K_M.gguf",
  "quantization": "Q4_K_M",
  "sha256": "EXACT-64-HEX-SHA256",
  "size_bytes": 123456789
}
```

These are **placeholders**, not a verified artifact or an instruction to run a
specific installer. When the publication is by a third party, the operator
must independently validate its licensing, provenance, revision and digest.

## Frontend settings and effects

Preferences now persist by workspace **and principal**, with per-workspace
optimistic revisions. Dark/light/system theme selection, reduced-motion mode,
client Xpra frame-rate budget and selected graph settings are applied to the
UI. Dockview coordinates and the optional compact layout remain browser-local.
The Downloads pane polls at bounded intervals while visible and shows actual
queued/downloading/verifying/installed/failed/cancelled/indeterminate states;
verified bytes remain zero until SHA-256 completion.

## Remaining cross-project integration and honest acceptance limits

- Chat history still lives in the browser tab, not a durable multi-device
  backend conversation service. A separate authenticated session ledger is
  needed to preserve chat turn provenance and privacy.
- The current frontend is connected to one explicit, authorized workspace.
  Multi-workspace discovery requires principal-level multi-workspace identity
  rather than granting a scoped token broader capabilities.
- Bash and controlled-browser windows can already be displayed via Xpra when
  launched in the workspace. Starting those processes securely from the UI
  requires a **qualified workspace launcher / PTY session service**, with
  workspace-sandbox boundaries and explicit user approval.
- A verified downloaded model is not yet installed into inference routing or
  benchmark-qualified. Model load health and quantization-runtime telemetry
  should be separate from download progress.
- The SHA-256 worker is tested with deterministic local byte fixtures;
  real Hugging Face CDN download, PostgreSQL RLS integration, Xpra/XBill
  browser acceptance, and production Next.js build must pass on the equipped
  host before treating this end-to-end as production qualified.

## Checks

```bash
PYTHONPATH=src python -m pytest -q tests/unit/test_ui_resources.py
PYTHONPATH=src python -m pytest -q tests/integration/test_ui_resources_postgres.py
cd frontend && npm test && npm run typecheck && npm run build
```
