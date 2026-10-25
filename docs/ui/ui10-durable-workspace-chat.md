# UI-10 — Authorized workspace connections and durable conversation sessions

UI-10 adds real, separately authenticated **workspace connections** and
PostgreSQL-backed **chat sessions**. It does not yet replace the existing
single-workspace backend token model with cross-workspace database discovery.
The workspace ring shows only the explicit allowlist configured on the Next.js
operator server. Each entry is verified against its own `/v1/ui/workspace`
identity using its own bearer credential before appearing in the browser.

## Server-side workspace connections

The existing single-workspace `POTBOT_UI_BACKEND_URL`,
`POTBOT_UI_WORKSPACE_ID`, `POTBOT_UI_BACKEND_TOKEN`, and
`POTBOT_UI_MODEL` environment variables continue to work.

For more than one workspace, use the server-only environment variable
`POTBOT_UI_WORKSPACES_JSON` containing 1–12 independently credentialed
entries:

```json
[
  {
    "id": "11111111-2222-4333-8444-111111111111",
    "name": "Engineering",
    "url": "http://127.0.0.1:8765",
    "token": "REPLACE_WITH_SCOPED_BACKEND_TOKEN",
    "model": "potbot"
  },
  {
    "id": "22222222-3333-4333-8444-222222222222",
    "name": "Studio",
    "url": "http://127.0.0.1:8766",
    "token": "REPLACE_WITH_A_DIFFERENT_SCOPED_BACKEND_TOKEN",
    "model": "potbot"
  }
]
```

Only explicit HTTP loopback endpoints may be used; no arbitrary URL supplied
by a browser or model becomes an upstream connection. **Treat the JSON value
as a secret.** Do not commit it, send it to the client or log the configuration.
Each workspace entry must point to an existing backend service whose bearer
authenticates as a workspace-bound principal. The BFF checks backend workspace
identity before showing any orb. `X-Potbot-Workspace` is an *untrusted selector
into this allowlist*, not an authorization token and never a direct PostgreSQL
RLS override. Every resource route uses the selected server-side credential.

Xpra remains a separate workspace-bound session; set
`POTBOT_UI_XPRA_WORKSPACE_ID` to the workspace it belongs to when multiple
connections are configured. Until multiple bridge instances are configured,
Xpra windows are available only in that selected workspace—not in others.

## Durable chat APIs

Migration **27**, `ui_chat_schema`, adds RLS-scoped `ui_chat_sessions` and
`ui_chat_turns` owned by `(workspace_id, principal_id)`. The backend supports:

| Backend route | Purpose |
| --- | --- |
| `GET /v1/ui/sessions` | List 50 newest session summaries |
| `POST /v1/ui/sessions` | Idempotently create a session with a canonical UUID and title |
| `GET /v1/ui/sessions/{id}` | Read the latest 40 conversation turns |
| `POST /v1/ui/sessions/{id}/turns` | Atomically persist a bounded user request and rebuild trusted model context |
| `POST /v1/ui/sessions/{id}/turns/{turnId}/finish` | Persist an actual terminal model result or mark an uncertain request |
| `POST /v1/ui/sessions/{id}/turns/{turnId}/reconcile` | After six minutes, explicitly acknowledge a possibly-dispatched request as indeterminate |

The BFF adds authenticated same-origin equivalents under `/api/ui/sessions`.
The browser cannot submit unverified old assistant messages to the inference
context: the backend constructs that context from committed completed turns.
Messages are sensitive and are stored as plain application data in PostgreSQL;
use database encryption, backup and retention controls appropriate to your
installation. This increment does **not** implement application-level content
encryption, chat search, pagination beyond the latest 40 turns, deletion,
privacy-policy retention workflows or cross-device window-layout sync.

Each chat is limited to 200 turns and 100 sessions per principal per workspace.
The current UI displays the most recent 40 turns; older turns remain in the
database for a future history paging/export feature. Model inference still
runs under the existing BFF streaming request. **It does not continue
independently if that BFF process crashes or cancels its request.** A crash
leaves a pending receipt. The BFF withholds its `[DONE]` marker until the
completion has been durably acknowledged; if persistence or the provider fails,
the outcome is indeterminate. No inference is automatically replayed.

## UI behavior

The selected workspace orb restores its own last opened conversation after
reload. A chat session selector and New button are present above the timeline.
Switching workspace changes the credential, session list and message history;
other workspaces retain their own panel layouts. An abandoned pending chat may
be explicitly reconciled after six minutes; the original model run is not
restarted. The footer distinguishes this from autonomous persistent background
inference, which remains future work.

## Acceptance and remaining back end

- Unit/route tests verify scope allowlisting, token non-disclosure, request
  validation, streaming persistence ordering and indeterminate outcomes.
- `tests/integration/test_ui_chats_postgres.py` verifies real RLS, request
  idempotency, pending fencing and turn history when PostgreSQL is configured.
- Full frontend production typecheck, Playwright workspace switching, actual
  PostgreSQL migrations and restart recovery must run on the configured host.
- The next backend milestone is controller-owned, detached durable chat
  execution with polling/replayable SSE—not merely browser-owned inference.
- Native, principal-aware multi-workspace discovery, PTY creation and new Xpra
  browser/application launch APIs are still open; this work does not claim them.
