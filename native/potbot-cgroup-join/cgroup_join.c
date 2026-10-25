/* SPDX-License-Identifier: MIT
 * Potbot operator-only cgroup join + exec.
 *
 * The *first* program run by Popen joins its pre-created cgroup before
 * executing the operator-pinned strace. This avoids Python preexec_fn in a
 * multi-threaded controller and closes the parent-side PID migration race.
 * No shell, PATH lookup, namespaces, or privilege changes occur here.
 *
 * Usage: potbot-cgroup-join /absolute/cgroup/leaf -- /absolute/strace [args]
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static int fail(const char *reason, int code) {
    /* Operator log gets a short fixed error, never raw user-controlled paths. */
    (void)fprintf(stderr, "potbot-cgroup-join: %s\n", reason);
    return code;
}

int main(int argc, char *argv[]) {
    if (argc < 4 || argv[1][0] != '/' || argv[3][0] != '/' ||
        strcmp(argv[2], "--") != 0) {
        return fail("invalid invocation", 64);
    }
    /* Avoid truncation and any creation in the cgroup filesystem. */
    const char suffix[] = "/cgroup.procs";
    size_t n = strlen(argv[1]);
    if (n == 0 || n > PATH_MAX - sizeof(suffix)) {
        return fail("invalid cgroup path", 64);
    }
    char path[PATH_MAX];
    memcpy(path, argv[1], n);
    memcpy(path + n, suffix, sizeof(suffix));

    /* The operator has validated the delegated cgroup root and unique leaf.
     * Reject leaf and cgroup.procs symlinks, including testing environments.
     */
    struct stat leaf;
    if (lstat(argv[1], &leaf) != 0 || !S_ISDIR(leaf.st_mode)) {
        return fail("cgroup directory unavailable", 73);
    }
    int fd = open(path, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return fail("cgroup membership unavailable", 73);
    }
    /* "0" is the current process in cgroup v2. */
    const char join[] = "0\n";
    ssize_t written = write(fd, join, sizeof(join) - 1);
    int close_error = close(fd);
    if (written != (ssize_t)(sizeof(join) - 1) || close_error != 0) {
        return fail("cgroup join failed", 73);
    }
    /* The pre-launch trace files remain empty unless the join succeeded. */
    execv(argv[3], &argv[3]);
    return fail("pinned tracer unavailable", 69);
}
