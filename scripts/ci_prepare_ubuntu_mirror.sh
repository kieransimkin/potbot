#!/usr/bin/env bash
set -euo pipefail

sources=(
  /etc/apt/apt-mirrors.txt
  /etc/apt/sources.list.d/ubuntu.sources
  /etc/apt/sources.list
)
azure_sources=()
for source in "${sources[@]}"; do
  if [[ -f "$source" ]] && grep -Eq 'https?://azure\.archive\.ubuntu\.com/' "$source"; then
    azure_sources+=("$source")
  fi
done

if (( ${#azure_sources[@]} == 0 )) || timeout 5 getent ahosts azure.archive.ubuntu.com >/dev/null; then
  exit 0
fi

timeout 5 getent ahosts archive.ubuntu.com >/dev/null
echo 'Azure Ubuntu mirror did not resolve. Using the official Ubuntu archive.'
sudo sed -Ei 's#https?://azure\.archive\.ubuntu\.com/#https://archive.ubuntu.com/#g' "${azure_sources[@]}"
