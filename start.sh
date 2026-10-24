#!/usr/bin/env bash
# Bootstrap the pinned uv runtime, synchronize the lock, then enter Potbot.
set -euo pipefail

POTBOT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
POTBOT_UV_VERSION="0.12.23"
POTBOT_UV_DIRECTORY="$POTBOT_ROOT/.potbot/runtime/uv"
POTBOT_UV=""

progress() { printf '[Potbot startup] %s\n' "$1" >&2; }
fail() { printf '[Potbot startup] %s\n' "$1" >&2; exit 1; }

uv_matches() {
    local version
    version="$("$1" --version 2>/dev/null)" || return 1
    [[ "$version" == "uv $POTBOT_UV_VERSION" || "$version" == "uv $POTBOT_UV_VERSION "* ]]
}

export POTBOT_TEMP_ROOT="${POTBOT_TEMP_ROOT:-$POTBOT_ROOT/.potbot/temporary}"
mkdir -p -m 700 -- "$POTBOT_TEMP_ROOT"
export TMPDIR="$POTBOT_TEMP_ROOT" TEMP="$POTBOT_TEMP_ROOT" TMP="$POTBOT_TEMP_ROOT"

progress "1/4 Checking uv $POTBOT_UV_VERSION"
if [[ -x "$POTBOT_UV_DIRECTORY/uv" ]] && uv_matches "$POTBOT_UV_DIRECTORY/uv"; then
    POTBOT_UV="$POTBOT_UV_DIRECTORY/uv"
elif command -v uv >/dev/null 2>&1 && uv_matches "$(command -v uv)"; then
    POTBOT_UV="$(command -v uv)"
else
    progress "Downloading uv $POTBOT_UV_VERSION from Astral"
    mkdir -p -- "$POTBOT_UV_DIRECTORY"
    POTBOT_INSTALLER="$(mktemp "$POTBOT_TEMP_ROOT/potbot-uv.XXXXXXXX")"
    trap 'rm -f -- "$POTBOT_INSTALLER"' EXIT
    POTBOT_INSTALLER_URL="https://astral.sh/uv/$POTBOT_UV_VERSION/install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 --fail --location --show-error \
            --output "$POTBOT_INSTALLER" "$POTBOT_INSTALLER_URL"
    elif command -v wget >/dev/null 2>&1; then
        wget --https-only --output-document="$POTBOT_INSTALLER" "$POTBOT_INSTALLER_URL"
    else
        fail "Install curl or wget to bootstrap uv, then run this script again."
    fi
    UV_UNMANAGED_INSTALL="$POTBOT_UV_DIRECTORY" UV_NO_MODIFY_PATH=1 sh "$POTBOT_INSTALLER"
    POTBOT_UV="$POTBOT_UV_DIRECTORY/uv"
    uv_matches "$POTBOT_UV" || fail "The pinned uv installation could not be verified."
    rm -f -- "$POTBOT_INSTALLER"
    trap - EXIT
fi

export POTBOT_PROJECT_ROOT="$POTBOT_ROOT"
export UV_PROJECT_ENVIRONMENT="$POTBOT_ROOT/.venv"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$POTBOT_ROOT/.potbot/cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$POTBOT_ROOT/.potbot/runtime/python}"

progress "2/4 Ensuring managed Python 3.12 (download progress follows when needed)"
"$POTBOT_UV" python install 3.12 --no-bin --no-registry
progress "3/4 Installing locked Python dependencies and all runtime extras"
"$POTBOT_UV" sync --project "$POTBOT_ROOT" --locked --all-extras --no-dev \
    --python 3.12 --managed-python
progress "4/4 Starting Potbot"
exec "$POTBOT_UV" run --project "$POTBOT_ROOT" --no-sync python -m potbot.startup "$@"
