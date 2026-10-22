# Linux sandbox host setup and CI

Potbot requires private user, mount, PID and network namespaces, a restricted
filesystem, dropped payload capabilities and its seccomp policy. Installing the
`bwrap` executable alone does not establish that the host permits this setup.

## Ubuntu 24.04 AppArmor profile

When `kernel.apparmor_restrict_unprivileged_userns` is enabled, Ubuntu mediates
capabilities inside unprivileged user namespaces. A missing suitable profile can
leave Bubblewrap unable to configure its private loopback interface, producing
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` before the workload
starts. Other host restrictions can produce similar setup failures; retain the
actual diagnostics instead of treating every such failure as an application bug.

Ubuntu provides `bwrap-userns-restrict` in the updated `apparmor-profiles` package.
The profile permits `/usr/bin/bwrap` to construct the sandbox, then stacks a child
profile that denies capabilities to the executed workload. Potbot's existing
network syscall restrictions and other isolation controls continue to apply.

On a dedicated Ubuntu 24.04 execution host:

```bash
sudo apt-get update
sudo apt-get install --yes bubblewrap libseccomp2 util-linux apparmor-profiles
sudo install -m 0644 \
  /usr/share/apparmor/extra-profiles/bwrap-userns-restrict \
  /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser --replace /etc/apparmor.d/bwrap-userns-restrict
```

Use the distribution's current profile and ABI. The original Noble package did
not contain this profile; the Noble updates repository does. Existing host policy
should be reviewed by the host administrator before replacing a local profile.

The GitHub Actions runner is disposable, so CI loads the packaged profile
directly from `/usr/share/apparmor/extra-profiles/` only when the kernel restriction
is enabled. It does not install a persistent override, change kernel sysctls,
disable AppArmor, run Potbot as root, share the host network, or skip failed tests.
Failure to read or load the required profile stops the job.

## Early qualification

After installing the locked development dependencies, run the existing focused
isolation suite as the ordinary execution user:

```bash
uv run --locked python -m pytest -q tests/integration/test_process_isolation.py
```

These six real-process checks cover normal execution, parent and child filesystem
and network confinement, cancellation, deadlines, retained output, and confirmed
process termination. CI now runs them before starting PostgreSQL. The complete
database and sandbox gate still runs afterward with its original test selection.

## Observed failure and validation scope

The first CI run for commit `2a451a906334f8f0654730939aa46d4f1e44444c` passed
962 core/Git tests, 191 PostgreSQL tests and the native Windows launcher job.
Its sandbox suite reported 43 failures and 27 passes, including Bubblewrap
loopback setup errors. All 248 delivered source files matched the committed
version, so the earlier Phase 4 prerequisite mismatch was already resolved.

This correction changes host provisioning and adds early qualification; no
runtime isolation rule or model code is changed. The development host lacks
Ubuntu AppArmor, so the profile transition must be confirmed by the subsequent
GitHub Actions run. A local sandbox test pass alone cannot establish that result.

## Primary references

- [Ubuntu AppArmor administration](https://ubuntu.com/server/docs/how-to/security/apparmor/)
  documents packaged profiles and explicit profile loading.
- [Ubuntu's description of its Bubblewrap profile](https://documentation.ubuntu.com/release-notes/25.04/#apparmor-profile-for-bwrap)
  describes setup permissions followed by restricted child capabilities.
- [Noble update package inspected for this fix](https://archive.ubuntu.com/ubuntu/pool/main/a/apparmor/apparmor-profiles_4.0.1really4.0.1-0ubuntu0.24.04.9_all.deb)
  contains the ABI 4.0 profile at the path used by CI. CI obtains its current
  distribution package through apt rather than downloading this reference URL.
- [Failed workflow run](https://github.com/kieransimkin/potbot/actions/runs/39686044019)
  records the original host failure.
