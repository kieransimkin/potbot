# S45: Artifact-controller acceptance after mandatory worker isolation

Base reviewed: `ed1aae6fa189e6a521ede996c92892aaa41d140f`.

## Observed CI result

The S44 merge passed the 42 temporary-storage tests, the six process-isolation
tests, and all 64 MODEL.md/context tests on the actual GitHub runner. The next
failure was the bounded artifact-read integration test. It still installed a
Python `HostOperation` as a workflow, which `require_isolated_work()` correctly
rejects. The upload integration test contained the same obsolete wrapper.

## Boundary preserved

The artifact open/read/close and upload open/chunk/inspect/commit/abort Cogs are
fixed, registered controller state machines. The runtime already installs these
through `artifact_ranges=` and `artifact_uploads=`. They remain subject to the
sealed catalog, typed ports, independent admission, scoped database transactions,
private artifact evidence, and the pinned execution journal.

These acceptance tests now use `operations=()` and invoke the registered Cogs
through `BoundCogRegistry.invoke`. The fixture performs its sequencing outside
any worker installation. It does not monkeypatch admission, substitute an
executor, permit an arbitrary host callback, or execute a workflow on the host.
The existing required-worker-isolation implementation is unchanged.

The fixtures retain bounded byte reconstruction, real PostgreSQL lease state,
controller-instance restart, concurrency, policy/scope checks, corruption,
revocation during I/O, terminal write fences, and REST/WebSocket event replay.
The WebSocket probe replays an actual completed artifact OPEN invocation instead
of an obsolete workflow run. All 11 read negatives and 22 upload negatives are
preserved. Nested sandbox-workflow causation is **not** claimed by these tests.

## Stronger evidence checking

`potbot.evaluation.artifact_control_plane` checks that:

- All ledger entries belong to allowed artifact Cogs and have valid pinned IDs.
- Event IDs are unique and the shared workspace cursor increases; concurrent
  runs may interleave and unrelated cursor gaps are allowed.
- Each invocation starts as a root. Subsequent records refer to the immediately
  previous event in that same run, never a different run or a fictitious worker.
- Started invocations include progress and exactly one terminal observation.
  A standalone failure is permitted only for an explicit admission denial.
- Required Cogs have successful completions with retained output identities.
- Terminal elapsed/CPU metrics are measured, finite, bounded and nonnegative.
- No child-worker executions are smuggled into this controller-only result.

The report explicitly sets `sandbox_worker_qualified: false`. The exact-source
read/upload checkers recompute the report; missing, altered and broader-scope
claims fail. Validation uses explicit exceptions, not removable assertions.
The source manifest now also pins the fixture, checker, controller-evidence
validator and worker-isolation implementation. Reports describe the checked
artifact services; they are not cryptographic attestations or proof of live
browser, network, process-isolation or PostgreSQL execution on their own.

## Dedicated CI

`.github/workflows/artifact-controller-s45.yml` runs the new offline validator
suite and the existing worker-isolation unit tests, starts the repository's
explicitly disposable PostgreSQL fixture, and runs both artifact integration
checks and their exact-source evidence gates. The upload step runs even if the
read step fails, without converting either failure to success. Missing evidence
(for example, an accidentally skipped test) makes its gate fail.

This job never deploys Potbot and never uses a production database. It uses a
job-specific Compose project name and destroys only that disposable project.
The ordinary `ci.yml` gates and all existing security restrictions remain enabled.

## Local verification and remaining work

Offline tests exercise the evidence validator and fixture/checker wiring. They
are **not** substitutes for the real PostgreSQL runs. Run the following against a
disposable database in an environment with the locked project dependencies:

```bash
uv run --locked python -m pytest -q tests/unit/test_artifact_control_plane.py tests/unit/test_sandbox_cog_worker.py
POTBOT_TEST_DSN=postgresql://potbot_test:potbot_test_only@127.0.0.1:55432/potbot_test \
  uv run --locked python -m pytest -q tests/integration/test_artifact_range_cogs.py tests/integration/test_artifact_upload_cogs.py
```

Commit the patch before generating exact-source gate artifacts. Local test runs
without `POTBOT_RANGE_EVIDENCE_DIR` / `POTBOT_UPLOAD_EVIDENCE_DIR` do not write gate
reports. CI sets these variables and evaluates the committed sources.

The older `tests/integration/test_execution_provenance.py` also contains
`HostOperation` work fixtures. Its actual-worker tests require a separate
sandbox-executable migration, not removal of `require_isolated_work()`. This patch
does not hide, skip or mark that broader gate as passed. Live two-agent Xpra,
network allowlist enforcement, and WSL2 operator-failure recovery likewise remain
separate host-qualification requirements.
