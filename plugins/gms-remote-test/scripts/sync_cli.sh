#!/usr/bin/env bash
# Sync the bundled gms-rt CLI into the plugin from the repository skill.
#
# The plugin is self-contained: scripts/gms-remote-test.sh is a copy of
# skills/gms-remote-test/scripts/gms-remote-test.sh. Run this script after
# the CLI changes in the repo so the plugin ships the same contract.
#
# Usage: plugins/gms-remote-test/scripts/sync_cli.sh [repo_root]
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="${1:-$(cd "${PLUGIN_DIR}/../.." && pwd)}"
SOURCE="${REPO_ROOT}/skills/gms-remote-test/scripts/gms-remote-test.sh"
TARGET="${PLUGIN_DIR}/scripts/gms-remote-test.sh"

if [[ ! -f "${SOURCE}" ]]; then
    echo "Error: CLI not found at ${SOURCE}" >&2
    exit 1
fi

cp "${SOURCE}" "${TARGET}"
chmod +x "${TARGET}"

SOURCE_VERSION="$(sed -n 's/^GMS_RT_VERSION="\(.*\)"/\1/p' "${SOURCE}")"
echo "Synced gms-remote-test.sh (CLI version ${SOURCE_VERSION:-unknown}) into plugins/gms-remote-test/scripts/"

# R16 drift guard: same version must always mean identical content.  If the
# previous plugin copy already carried this version but differed from the
# source (content drift with an unchanged version — the exact trap that let
# plugins ship fixes skills lacked), refuse to sync silently.
if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    PREVIOUS_HASH="$(git -C "${REPO_ROOT}" show "HEAD:${TARGET#"${REPO_ROOT}/"}" 2>/dev/null | sha256sum | cut -d' ' -f1 || true)"
    SOURCE_HASH="$(sha256sum "${SOURCE}" | cut -d' ' -f1)"
    if [[ -n "${PREVIOUS_HASH}" && "${PREVIOUS_HASH}" != "${SOURCE_HASH}" ]]; then
        echo "Warning: plugin CLI content changed at the same version (${SOURCE_VERSION});" >&2
        echo "bump GMS_RT_VERSION in skills/gms-remote-test/scripts/gms-remote-test.sh before shipping." >&2
    fi
fi
