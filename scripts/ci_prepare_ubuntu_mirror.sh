#!/usr/bin/env bash
set -euo pipefail

sources=(
  /etc/apt/apt-mirrors.txt
  /etc/apt/sources.list.d/ubuntu.sources
  /etc/apt/sources.list
)
azure_pattern='https?://azure\.archive\.ubuntu\.com/ubuntu(/|[[:space:]]|$)'
azure_sources=()
for source in "${sources[@]}"; do
  if [[ -f "$source" ]] && grep -Eq "$azure_pattern" "$source"; then
    azure_sources+=("$source")
  fi
done

if (( ${#azure_sources[@]} == 0 )); then
  exit 0
fi

# Probe the same suite the runner needs. APT still verifies Ubuntu's signed metadata.
mirrors=(
  https://azure.archive.ubuntu.com/ubuntu
  https://archive.ubuntu.com/ubuntu
  https://ubuntu.osuosl.org/ubuntu
  https://mirror.clarkson.edu/ubuntu
)
for mirror in "${mirrors[@]}"; do
  if ! curl --fail --silent --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 4 --max-time 8 --output /dev/null "$mirror/dists/noble-updates/InRelease"; then
    continue
  fi
  if [[ "$mirror" != "${mirrors[0]}" ]]; then
    echo "Using reachable Ubuntu archive mirror $mirror."
    sudo sed -Ei "s#$azure_pattern#$mirror\\1#g" "${azure_sources[@]}"
  fi
  exit 0
done

echo 'No configured Ubuntu archive mirror served noble-updates.' >&2
exit 1
