#!/usr/bin/env bash
# Compile the reviewed x86-64 WSL kernel and prepare its release signing input.
set -euo pipefail
umask 022

configure_only=false
if [[ ${1:-} == --configure-only ]]; then
    configure_only=true
    shift
fi
if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo 'Usage: bash scripts/build_wsl_kernel.sh [--configure-only] NEW_WORKSPACE [SOURCE_ARCHIVE]' >&2
    exit 2
fi
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
    echo 'This kernel recipe requires x86-64 Linux.' >&2
    exit 2
}

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if ! potbot_revision=$(git -C "$repo" rev-parse --verify HEAD 2>/dev/null); then
    if [[ "$configure_only" != true ]]; then
        echo 'A full build requires a Git checkout with a readable Potbot revision.' >&2
        exit 2
    fi
    potbot_revision=unavailable
fi
inputs="$repo/sandbox/wsl-kernel/x86_64"
source_commit=d504d40ab83839cb61942a812a99d183c42227b9
source_sha=32c2610f46e9786acb64da0a18818849cd6fe4166612d2f522b9cc3be197656b
kernel_release=6.18.54.1-microsoft-standard-WSL2-potbot-ns-taskstats
source_url="https://codeload.github.com/microsoft/WSL2-Linux-Kernel/tar.gz/$source_commit"

mkdir -- "$1"
workspace=$(cd -- "$1" && pwd -P)
source="$workspace/source"
build="$workspace/build"
components="$workspace/components"
archive="$workspace/source.tar.gz"
mkdir -- "$source" "$build" "$components" "$workspace/tmp"
export TMPDIR="$workspace/tmp"
export LC_ALL=C TZ=UTC
export KBUILD_BUILD_USER=potbot KBUILD_BUILD_HOST=wsl-kernel-build
export KBUILD_BUILD_TIMESTAMP='Tue Oct 6 19:13:07 UTC 2026'
export SOURCE_DATE_EPOCH=1791313987

if [[ $# -eq 2 ]]; then
    cp -- "$2" "$archive"
else
    curl --silent --show-error --fail --location --max-time 240 --max-filesize 1073741824 \
        --retry 2 --proto '=https' --proto-redir '=https' --output "$archive" "$source_url"
fi
printf '%s  %s\n' "$source_sha" "$archive" | sha256sum --check --status
tar --extract --gzip --file "$archive" --directory "$source" --strip-components=1 \
    --no-same-owner --no-same-permissions

cp -- "$inputs/wsl-taskstats-pidns.patch" "$components/"
patchfile="$components/wsl-taskstats-pidns.patch"
patch --batch --forward --fuzz=0 --directory "$source" -p1 < "$patchfile"
(
    cd -- "$source"
    perl scripts/checkpatch.pl --no-tree --no-signoff --terse "$patchfile"
)
cp -- "$inputs/kernel.config" "$build/.config"

kmake() {
    make -s -C "$source" O="$build" ARCH=x86 CC=gcc-13 HOSTCC=gcc-13 RUSTC=false LOCALVERSION= "$@"
}

kmake olddefconfig
python3 "$source/scripts/diffconfig" "$inputs/kernel.config" "$build/.config" > "$workspace/config.diff"
# Kconfig records versions of the tools it detected. All functional changes
# must go back through review of the checked-in config.
awk '$1 !~ /^(CC_VERSION_TEXT|GCC_VERSION|AS_VERSION|LD_VERSION|PAHOLE_VERSION)$/' \
    "$workspace/config.diff" > "$workspace/unreviewed-config.diff"
if [[ -s "$workspace/unreviewed-config.diff" ]]; then
    echo 'Configured kernel settings differ from the reviewed recipe:' >&2
    cat "$workspace/unreviewed-config.diff" >&2
    exit 1
fi
for option in TASKSTATS TASK_XACCT TASK_IO_ACCOUNTING MODULES DEBUG_INFO_BTF IKCONFIG_PROC; do
    grep -Fx "CONFIG_$option=y" "$build/.config"
done
[[ $(kmake kernelrelease) == "$kernel_release" ]]
cp -- "$build/.config" "$components/kernel.config"
{
    printf 'potbot_revision=%s\nsource_commit=%s\nsource_sha256=%s\nkernel_release=%s\n' \
        "$potbot_revision" "$source_commit" "$source_sha" "$kernel_release"
    printf 'runner_image=%s\n' "${ImageVersion:-local}"
    gcc-13 --version | sed -n '1p'
    ld --version | sed -n '1p'
    pahole --version
    sha256sum "$inputs/kernel.config" "$patchfile" "$repo/scripts/build_wsl_kernel.sh" \
        | sed 's|  .*/|  |'
} > "$workspace/build-identity.txt"
printf 'Configured %s from %s\n' "$kernel_release" "$source_commit"
if [[ "$configure_only" == true ]]; then
    exit 0
fi

jobs=$(nproc)
kmake -j "$jobs" bzImage modules
kmake INSTALL_MOD_PATH="$workspace/modules" INSTALL_MOD_STRIP=1 modules_install
kmake INSTALL_HDR_PATH="$workspace/headers" headers_install
mkdir -- "$workspace/perf-build"
make -s -C "$source/tools/perf" OUTPUT="$workspace/perf-build/" -j "$jobs" \
    CC=gcc-13 HOSTCC=gcc-13 NO_JEVENTS=1 NO_JVMTI=1 NO_LIBTRACEEVENT=1 \
    NO_LIBPYTHON=1 NO_LIBPERL=1 DESTDIR="$workspace/perf" prefix=/ install
test -s "$workspace/perf/bin/perf"

cp -- "$build/arch/x86/boot/bzImage" "$components/"
bash "$source/Microsoft/scripts/gen_artifacts_vhdx.sh" \
    "$workspace/modules" "$workspace/headers" "$workspace/perf" \
    "$kernel_release" "$components/modules.vhdx"
qemu-img info --output=json "$components/modules.vhdx" > "$workspace/modules-image.json"
uv run --project "$repo" --locked --no-dev python "$repo/scripts/prepare_wsl_kernel_release.py" \
    prepare --components "$components" --source-archive "$archive" --source-commit "$source_commit" \
    --source-sha256 "$source_sha" --kernel-release "$kernel_release" --output "$workspace/prepared" \
    | tee "$workspace/preparation.json"
