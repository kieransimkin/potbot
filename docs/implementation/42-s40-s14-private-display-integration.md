# S40/S14 agent private Xpra integration (rebased)

## Status and prerequisites

This stage integrates the **already committed** `AgentXpraDisplayPool` with the
existing `XpraApplicationSupervisor`, while preserving S14 operator-owner
fencing. The baseline is Potbot `main` at `c70abe46e5582b36c1dc3dda2f4d5e4baa7d3aa5`.
The installer matches the syntax tree and verified method-level contracts,
refusing unknown variants. It never executes `git reset` or discards local work.

This is **source-level integration**, not evidence that an unqualified machine
can isolate a hostile browser. Qualified Linux/WSL2, Bubblewrap, seccomp, native
Xpra/Xdummy and the independently pinned rootfs are still required.

## Trust boundaries

- **Existing workspace Xpra service** remains the operator-visible display.
  The Next.js workspace selector and `/v1/xpra/apps` API still operate on the
  original operator applications. The public API supplies `kind` only; it
  cannot supply `agent_id`, arbitrary argv, sandbox paths, or Xauthority.
- **Agent private Xpra pool** is constructed only by the host-owned, mode-0600
  operator policy. One registered agent identity gets one private Xpra/Xdummy
  display with its own authority and one browser process. The guest mount plan
  hides all other agent home directories and the shared workspace X11 socket.
- The private browser control surface is a *host-only* method:
  `apps.start("controlled-browser", agent_id=registered_uuid)` followed by
  `apps.agent_browser_reservation(registered_uuid, session_id)`. The host
  controller creates the UUID from authenticated agent registration; it MUST
  NOT deserialize `agent_id` or reservation tokens from model outputs.
- Each machine command uses
  `apps.browser_command(session_id, command, lease_token=token,
  actor_agent_id=registered_uuid)`; the coordinator checks both the reservation
  and owning agent. The S37 browser Cog service supplies the same agent ID from
  its controller-issued `BrowserLease`, never from page content.
- The host can query `apps.agent_sessions(registered_uuid)` or perform a clean
  `apps.terminate_agent_browser(registered_uuid, session_id)` without making
  those controls publicly callable. An uncertain stop fences the session.
- Agent browsers do not appear in the ordinary Xpra session inventory and
  cannot be attached to, minimized or terminated through the public operator
  application routes using their leaked session ID. Host fencing remains able
  to stop them.
- The S14 operator attachment is required for the private-display pool.
  An operator epoch change fences the supervisor, attempts to reap workers,
  and only retires private display resources after their process termination
  has been confirmed. Unconfirmed processes require operator reconciliation.

## Enable the feature

Add the **fragment** below to the existing *host-owned* application policy.
Do not replace your full policy with this fragment:

```json
{
  "browserLeases": true,
  "strictXpraInput": true,
  "agentDisplays": {
    "root": "/var/lib/potbot/operators/private-displays/workspace-id",
    "workspaceName": "your-registered-workspace-name",
    "maxDisplays": 4
  }
}
```

The `root` directory must **already exist**, be an actual directory with
mode 0700 owned by the Xpra host operator, and must not overlap the guest
rootfs, `/home`, model staging, shared tools, `/etc/ai` or the operator log
store. `workspaceName` must match the AgentHomeRegistry workspace-group name
that was used when provisioning agents. A qualified S13 operator attachment
must be available. When absent, private displays remain disabled; ordinary
operator Xpra launching continues unchanged.

S39 egress remains an opt-in policy. When enabled, private Chromium workers
use the existing operator-owned AF_UNIX CONNECT broker; no general guest TCP
capability is granted. Native Xpra viewers remain read-only; interaction goes
through the agent-reserved Playwright/CDP command interface.

## Review and acceptance

Run in the repo after installation:

```bash
uv sync --locked --extra web --dev
uv run --locked python -m pytest -q \
  tests/unit/test_s40_rebased_supervisor.py \
  tests/unit/test_agent_xpra_displays_s40.py \
  tests/unit/test_xpra_application_sessions.py \
  tests/unit/test_xpra_operator_fence_s14.py \
  tests/unit/test_xpra_owner_recovery_s14.py \
  tests/unit/test_browser_cogs_s37.py \
  tests/unit/test_browser_s38.py
uv run --locked python -m compileall -q src/potbot
```

**Mandatory production host acceptance (not satisfied by offline tests):**

1. Provision two independent, registered agents, each with its own private
   display. Confirm the Xpra display number, Unix-socket inode and Xauthority
   cookie differ. One agent's mount namespace must not contain the other's
   home or X socket.
2. Attempt cross-agent X11 connections, browser RPC calls and reservations.
   All must be refused, including after a lease expires or a process exits.
3. Trigger S14 owner loss during a browser action. Confirm no automatic
   replay, process-group termination and uncertain-leases accounting.
4. Test operator UI app endpoints with a leaked agent session ID; agent
   browsers must never appear in inventory or be publicly terminated.
5. Run the existing real Xpra/Xdummy and Bubblewrap acceptance suite and
   examine operator telemetry. Agent GUI sessions still use the long-lived
   application launcher; do not claim complete per-process accounting until
   that process path has its own live telemetry acceptance.

**Next stage:** session-scoped Xpra tickets and per-agent Dockview streams.
This integration does **not** expose private Xpra protocol sockets through the
public workspace WebSocket relay.
