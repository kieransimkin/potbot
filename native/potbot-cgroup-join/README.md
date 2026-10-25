# Pinned Potbot cgroup launcher

`cgroup_join.c` is a minimal host-only trampoline. It is never mounted inside the
Bubblewrap guest. It joins the operator-allocated, delegated cgroup-v2 leaf
**before** `execve()` of the pinned host strace binary. A failed join never
executes the command. All descendants inherit this cgroup membership.

Build in the operator's trusted image and install outside all workspace mounts:

```sh
cc -std=c11 -O2 -Wall -Wextra -Werror -fstack-protector-strong \
  -D_FORTIFY_SOURCE=2 -o potbot-cgroup-join cgroup_join.c
sudo install -o root -g root -m 0755 potbot-cgroup-join /usr/local/libexec/potbot-cgroup-join
sha256sum /usr/local/libexec/potbot-cgroup-join "$(command -v strace)"
```

Pin the output SHA-256 digests into `POTBOT_OPERATOR_CGROUP_JOIN_SHA256` and
`POTBOT_OPERATOR_STRACE_SHA256`; configure the corresponding executable paths
with `POTBOT_OPERATOR_CGROUP_JOIN` and `POTBOT_OPERATOR_STRACE`.

The helper intentionally does not open a shell, search `PATH`, change UID/GID,
create cgroups, manage leases, or accept a guest-defined executable. The
trusted Python operator provisions the leaf and checks image/workspace identity
first; the child joins the leaf before tracing begins. The tiny `execv` wrapper
has no capabilities of its own and should not be installed setuid/setgid.
