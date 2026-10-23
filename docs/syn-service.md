# Syn communication and ledger integration

For how this changes the original 204-step implementation plan and the separate
48-stage tool programme, see the [planning amendment](implementation/07-syn-integration-plan-impact.md)
and its [complete stage impact map](potbot-tool-mcp-implementation-bundle/syn-integration-impact.json).
They distinguish current manual export from proposed reliable delivery/inbox
work and retain Potbot's existing task, semantic and memory authority.

Potbot pins Syn as the Git submodule `vendor/syn`. The Python `syn` extra installs
only the dependency-free `syn-client` package from that submodule. Syn runs as a
separate service; it owns durable communication and hash-chain receipts. Potbot's
existing database and signing/admission services continue to own job execution.

![Illustrated Potbot, Syn and PostgreSQL service boundaries with receipt verification](media/syn-service-flow.svg)

*Explanatory illustration maintained in the pinned Syn source. See its
[HTTP contract and database tests](https://github.com/Synsphere/syn/blob/57da1be8ee69e00375bb2756bb09839bf0d4bc06/docs/communication.md).* This
backend feature has no new graphical UI. Actual HTTP tests and the architecture
diagram explain it directly; a GUI screenshot or video would not add evidence.

The [recorded local acceptance trace](media/syn-service-acceptance.json) captures
real Potbot-to-Syn HTTP forwarding, retry conflicts, access denial, anchoring and
restart replay with synthetic data. Its SQLite fixture demonstrates the composed
HTTP path and persistence; PostgreSQL concurrency is qualified separately by the
Syn database tests. It is not evidence of a production deployment.

## Install and start

```sh
git clone --recurse-submodules https://github.com/kieransimkin/potbot.git
cd potbot
# For an existing checkout:
git submodule update --init --recursive
uv sync --locked --extra syn --extra web --extra postgres --extra credentials --dev
```

Git pins an exact Syn commit; `git submodule update --remote` is not part of setup.
Review and commit any future pin changes with corresponding lock and contract
checks. GitHub-generated source ZIPs omit submodule files. Use a recursive clone
or a complete distribution that includes the pinned source. The launchers detect
a missing client before attempting runtime downloads. CI resolves the exact Syn
gitlink and checks out that revision with a dedicated read-only key; see
[scoped credentials and CI access](implementation/08-syn-scoped-credentials.md).

Configure and run Syn separately using `vendor/syn/config.example.toml` as a
template, then run `uv run --project vendor/syn syn-server --config <config-path>`.
Its usual migration applies the ledger tables. Put a per-workspace random token
in Syn's secret `SYN_COMMUNICATION_TOKENS` JSON mapping. Set `POTBOT_SYN_URL` to
the HTTPS service origin (loopback HTTP is supported) and `POTBOT_SYN_TOKEN` to
that workspace's matching token, or use the scoped native-vault commands below.
Set `POTBOT_INSTALLATION_ID` to a retained installation UUID and
`POTBOT_SYN_WORKSPACE_ID` to the authenticated workspace UUID. A URL/token-only
configuration now fails closed until these explicit bindings are present.
Keep tokens in native storage or service environment injection;
never commit, log, embed in model prompts or reuse them for browser/API clients.
The integration is inactive when `POTBOT_SYN_URL` is absent. Syn's existing P2P
discovery is independent; this HTTP integration does not replicate via libp2p.

```sh
uv run potbot credentials syn-set \
  --installation-id <UUID> --workspace-id <UUID> --url https://<Syn-origin>
```

The hidden prompt stores a credential bound to this installation, workspace and
canonical service origin. Matching environment injection takes precedence and
is never autosaved. `syn-status` and `syn-delete` use the same scope arguments.

![Illustrated local checks before a Syn token can be used](media/syn-credential-scope.svg)

*Explanatory illustration of implemented scope checks and known-secret filters;
not a screenshot or evidence of remote-agent admission.*

## Read and send

```sh
uv run potbot syn-service --workspace-id <UUID> head
uv run potbot syn-service --workspace-id <UUID> messages --after 0 --limit 50
uv run potbot syn-service --workspace-id <UUID> send \
  --message-id <stable-UUID> --channel agents.progress --payload approved-message.json
uv run potbot syn-service --workspace-id <UUID> verify --expected-head retained-head.json
```

Retain a message UUID before sending, and reuse it on uncertain acknowledgement.
An identical retry returns the original receipt; changed contents conflict.
Save `head` output outside Syn's database as `retained-head.json` to pin a verified
prefix. `verify` without that file checks consistency against the current service
head, which cannot establish independent resistance to a fully rewritten service.
The shared client verifies every receipt's hash and workspace, checks replay
continuity, refuses redirects and bounds page sizes, responses and request time.

When the HTTP server is configured with Syn, its existing authentication and
origin checks also protect `GET /v1/syn/head`, `GET /v1/syn/messages?after=&limit=`
and `POST /v1/syn/messages`. POST uses the normal v1 request envelope: its
`idempotency_key` must be a canonical UUID, and `payload` contains exactly
`channel` and `payload`. Workspace and sender identity come from authenticated
context. The internal Syn token is never returned to browsers. Channels are
workspace-wide labels, not separate access-control boundaries. A receipt proves
persistence, not that another agent acted, a job succeeded or an action was allowed.

## Anchor existing execution events

```sh
uv run potbot syn-service --workspace-id <UUID> sync-events \
  --principal-id <UUID> --after 0 --limit 50
```

This explicit bounded command reads committed `ExecutionEventService` public
projections under the usual PostgreSQL workspace checks. It sends each unchanged
projection on `potbot.execution-events`, using the original event UUID as its
idempotent message ID. It returns the **Potbot source cursor**, which differs from
Syn's own communication sequence. Repeat with `next_cursor` until `has_more` is
false. If any send fails, retry the same source page; confirmed earlier messages
remain idempotent. Source events are never deleted or rewritten.

This is an opt-in replay/anchoring adapter, not an automatic outbox worker. It does
not claim the universal lifecycle instrumentation proposed in
[execution monitoring](implementation/05-syn-execution-monitoring.md) is complete.
It introduces no additional execution authority, raw model prompts or private
credentials into event payloads. Operators must approve other message contents
before intentionally sharing them with their configured Syn service.

The ledger provides one commit-ordered SHA-256 chain per workspace, plus durable
messages, replay and retry identity. Consensus, global finality, mining, automatic
peer replication and signed message authorship are outside this implementation.
Potbot's Agent/Ember signature checks remain a separate capability.
