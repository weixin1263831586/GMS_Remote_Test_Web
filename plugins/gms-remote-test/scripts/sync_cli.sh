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
