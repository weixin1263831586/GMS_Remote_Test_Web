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
# plugins ship fixes skills lacked), the sync FAILS (2026-09-08 audit §一:
# the old code only printed a Warning, which gated nothing).
if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    PREVIOUS_FILE="$(git -C "${REPO_ROOT}" show "HEAD:${TARGET#"${REPO_ROOT}/"}" 2>/dev/null || true)"
    if [[ -n "${PREVIOUS_FILE}" ]]; then
        PREVIOUS_VERSION="$(printf '%s\n' "${PREVIOUS_FILE}" | sed -n 's/^GMS_RT_VERSION="\(.*\)"/\1/p' | head -n 1)"
        PREVIOUS_HASH="$(printf '%s\n' "${PREVIOUS_FILE}" | sha256sum | cut -d' ' -f1)"
        SOURCE_HASH="$(sha256sum "${SOURCE}" | cut -d' ' -f1)"
        # Same version + different content = silent drift trap; a version
        # bump with new content is the normal release path.
        if [[ -n "${PREVIOUS_VERSION}" && "${PREVIOUS_VERSION}" = "${SOURCE_VERSION:-}" \
              && "${PREVIOUS_HASH}" != "${SOURCE_HASH}" ]]; then
            echo "Error: plugin CLI content changed at the same version (${SOURCE_VERSION:-unknown})." >&2
            echo "Bump GMS_RT_VERSION in skills/gms-remote-test/scripts/gms-remote-test.sh, then re-run this script." >&2
            exit 1
        fi
    fi
fi

# Version contract (2026-09-08 audit §一): kk.plugin.json, kimi.plugin.json,
# mcp_server.py SERVER_VERSION and the CLI GMS_RT_VERSION must move together.
PLUGIN_JSON="${PLUGIN_DIR}/kk.plugin.json"
KIMI_JSON="${PLUGIN_DIR}/kimi.plugin.json"
MCP_SERVER="${PLUGIN_DIR}/scripts/mcp_server.py"
CLI_SCRIPT="${PLUGIN_DIR}/scripts/gms-remote-test.sh"
manifest_version() {
    sed -n 's/^  "version": "\(.*\)",$/\1/p' "$1" | head -n 1
}
MANIFEST_VERSION="$(manifest_version "${PLUGIN_JSON}")"
KIMI_VERSION="$(manifest_version "${KIMI_JSON}")"
MCP_VERSION="$(sed -n 's/^SERVER_VERSION = "\(.*\)"$/\1/p' "${MCP_SERVER}" | head -n 1)"
CLI_VERSION="$(sed -n 's/^GMS_RT_VERSION="\(.*\)"/\1/p' "${CLI_SCRIPT}" | head -n 1)"
if [[ -z "${MANIFEST_VERSION}" || -z "${KIMI_VERSION}" || -z "${MCP_VERSION}" || -z "${CLI_VERSION}" ]]; then
    echo "Error: unable to read versions from ${PLUGIN_JSON} / ${KIMI_JSON} / ${MCP_SERVER} / ${CLI_SCRIPT}" >&2
    exit 1
fi
if [[ "${MANIFEST_VERSION}" != "${KIMI_VERSION}" || "${MANIFEST_VERSION}" != "${MCP_VERSION}" || "${MANIFEST_VERSION}" != "${CLI_VERSION}" ]]; then
    echo "Error: plugin version contract drifted:" >&2
    echo "  kk.plugin.json      = ${MANIFEST_VERSION}" >&2
    echo "  kimi.plugin.json    = ${KIMI_VERSION}" >&2
    echo "  mcp_server.py       = ${MCP_VERSION}" >&2
    echo "  gms-remote-test.sh  = ${CLI_VERSION}" >&2
    echo "All four must share one release version." >&2
    exit 1
fi
echo "Version contract OK: ${MANIFEST_VERSION}"
