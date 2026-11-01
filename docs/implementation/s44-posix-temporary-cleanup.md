# S44: owned POSIX scratch cleanup after immutable skill-release tests

## Failure and scope

Base: `fd2ff733c94e6cceb34513abffbddef3fd2bca0c` (`fixCI`). The failing
Potbot verification job reported 64 successful MODEL.md/context assertions,
then returned exit code 1 from pytest's registered session cleanup. The
shared temporary-directory helper could not unlink a read-only release's
`release.json` because its containing directory was mode `0555`.

`SharedSkillReleaseStore` intentionally publishes directories as `0555` and
files as `0444`. Those production settings are correct and remain unchanged.
S44 fixes disposal of the enclosing, explicitly allocated scratch tree. It does
not change the skill publisher, workspace mounts, Bubblewrap, seccomp, AppArmor,
credential custody, Windows ACL inheritance, or database behavior.

## Cleanup contract

The allocation records its directory device/inode identity. On supported POSIX
systems, cleanup opens the parent and exact allocated directory, checks that
identity, and uses descriptor-relative operations for traversal and deletion.
Directory opens use `O_DIRECTORY | O_NOFOLLOW`; permission repair uses `fchmod`
on the opened descriptor rather than a pathname. Directory identities are
checked before descending and before removing directory entries.

Only a directory owned by the effective user may have missing owner read,
write, or search bits restored. Group/other mode bits are preserved. Regular
files are never chmodded by the POSIX path: unlinking needs permissions on the
containing directory, not write permission on the file. This also preserves
permissions on externally hardlinked files. Symlinks are unlinked as entries;
their targets are not traversed or chmodded. The selected parent directory is
never made writable by cleanup.

A replaced root, foreign-owned directory requiring repair, cross-device
subtree, unreadable directory, or permanent permission denial remains an
explicit cleanup failure. Missing entries are tolerated. Descriptors close
on success and failure. `ignore_cleanup_errors=True` still warns and retains
an unsuccessful allocation; it does not falsely set `_cleaned`. An existing
exception from the context body remains the primary exception.

Callers must stop writers before cleanup. This is not a replacement for the
sandbox's mount/process isolation or a guarantee against arbitrary concurrent
mount/rename operations by a privileged attacker. In particular, the
cross-device guard is not detection of every same-device bind mount. No
path-based permission-repair fallback is added on platforms without the
required descriptor APIs. Native Windows read-only-file handling and bounded
sharing-lock retries remain in place.

## Regression coverage and CI

`tests/unit/test_temporary_cleanup.py` covers read-only nested and top-level
directories, six permission modes, real skill-store publishing/idempotence,
external links, hardlinks, root substitution, injected directory/symlink races,
foreign-owner repair rejection, permanent denials, unchanged parent modes,
missing entries, descriptor lifetime, warning/retry behavior, and a child
pytest process which must exit zero after its session cleanup.

The existing temporary-storage fault-injection tests now forward the private
cleanup helper's expected-identity argument. The main Linux verification job
runs the storage and pytest-teardown tests before sandbox/database setup:

```bash
uv run --locked python -m pytest -q \
  tests/unit/test_temporary.py \
  tests/unit/test_temporary_cleanup.py \
  tests/unit/test_pytest_temp_isolation.py
```

Run this gate as an ordinary user, not solely as root. Root bypasses two of the
real permission-denial probes. Native Windows will skip the POSIX-specific
module; its existing temporary-storage job still exercises the Windows path.
No database acceptance gate or required evidence upload has been disabled.

## Evidence at patch preparation

The changed existing files and the dependency files used by local tests were
retrieved at the base commit, and their Git blob hashes were checked. Local
execution used a selected-file snapshot, not a full checkout, because archive
and Git transport were unavailable in the execution container.

On Linux, Python 3.13.5 / pytest 9.0.2, uid/gid 65534:

- Original immutable-release cleanup reproduction: exit 1.
- The same reproduction with S44: exit 0.
- Original nested pytest probe: `1 passed`, then teardown error, exit 1.
- The same nested pytest probe with S44: `1 passed`, exit 0, scratch removed.
- Targeted tests: **41 passed, 1 deselected**. The deselected case is the
  repository-wide source-allocation inventory, which cannot be assessed from
  a selected-file snapshot. It remains enabled in the full-repository CI gate.

Python 3.12 syntax parsing and patch applicability were also checked. The
pinned Python 3.12 CI runtime, full PostgreSQL suite, repository-wide lint/type
checks, native Windows execution and live WSL2/Xpra host qualification were
not run locally. This document does not certify overall CI or browser runtime
qualification. The next pushed commit must pass the existing verification
workflow; live browser acceptance remains a separate host requirement.

## Reference

Python's `shutil.rmtree` documentation describes descriptor-based symlink-attack
resistance and `onexc`; its pathname callback is not an appropriate place to
perform unrestricted directory permission repair:
https://docs.python.org/3.12/library/shutil.html#shutil.rmtree
