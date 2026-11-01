# MODEL.md release-hardening gate (increment after Stage 9)

This increment repairs confirmed CI regressions and adds a dedicated context
qualification step to the PostgreSQL workflow. It is intentionally **not** a
claim of production qualification on a host where the tests were not executed.

## Confirmed fixes

- Next.js `WorkspaceOrbit` duplicate, incomplete declaration blocked TypeScript.
- Shared tool mount referenced `_TO` instead of its declared `_TOOL` pattern.
- Context projections and immutable skill publishing now use Potbot's managed
  `TemporaryDirectory` allocator, retaining the no-follow validation and
  create-only publication workflow rather than importing `tempfile.mkdtemp`.
- Historic S9 tests now use real lease claims and reconciliation rather than a
  deleted `active_guards` test double. This does not add unsafe compatibility
  state to the host ledger.
- Offline S40 display fixture initializes its optional audit factory; S38/S41
  Linux GitHub Actions use short, isolated 0700 scratch roots under `/tmp` to
  respect the AF_UNIX `sun_path` limit. On ordinary hosts Potbot keeps its
  existing project-local scratch root unless explicitly overridden.
- The Xpra status fixture includes the two explicitly published capability flags.
- Import order and unused import failures in S10/S11 are corrected.

## Release checks

Run on your current full Git checkout (the GitHub CI workflow does the
PostgreSQL checks with a disposable fixture):

```powershell
uv run --locked ruff check src/potbot/api/ui_tool_upgrades.py src/potbot/tools/tool_approval_worker_s11.py
uv run --locked pytest -q tests/unit/test_context_release_hardening.py tests/test_context_refresh.py tests/test_context_syn_release.py
uv run --locked pytest -q tests/unit/test_shared_tool_upgrades_s9.py tests/unit/test_shared_tool_mounts_s5.py tests/unit/test_temporary.py
uv run --locked pytest -q tests/unit/test_agent_xpra_displays_s40.py tests/integration/test_xpra_native_bridge.py
```

Then run the full Linux/WSL2 sandbox and PostgreSQL suite with the approved
`POTBOT_TEST_DSN`. The new CI step runs all context integration tests *before*
artifact-Cog checks to make failures visible as a separate stage.

## Remaining security gates before production release

1. Run a qualified Linux/WSL2 host's real Bubblewrap bind-mount and restart
   tests; unit tests only simulate the process boundary.
2. Authenticate sandbox-to-operator mutation RPCs using real agent session
   identity and an independently verified lease; never expose raw DSNs.
3. For multi-host operation, replace or explicitly pin host-local SQLite
   *execution admission leases* with a PostgreSQL-backed distributed fence.
   Document context itself already has PostgreSQL authority.
4. Verify creation, rename, removal, retention and restore of context on all
   primitive lifecycles; provisioning helpers are not universal hooks yet.
5. Opt in and publish the new Syn context type release with explicit version
   approval. The preexisting immutable registry must remain unchanged.

Do not mark the feature distributed-safe or fully production-ready until these
checks are run and recorded.
