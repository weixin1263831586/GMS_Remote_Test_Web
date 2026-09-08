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

# 4.txt P0-4: the skill directory is the single source of truth for both the
# CLI and the MCP server. The Controller ZIPs skills/gms-remote-test/ as-is,
# so skills/gms-remote-test/scripts/ MUST contain mcp_server.py or remote
# --client codex/kimi installs register an MCP server that does not exist.
sync_one() {
    local source="$1" target="$2" label="$3"
    if [[ ! -f "${source}" ]]; then
        echo "Error: ${label} not found at ${source}" >&2
        exit 1
    fi
    if [[ -f "${target}" ]] && cmp -s "${source}" "${target}"; then
        return 0
    fi
    cp "${source}" "${target}"
    chmod +x "${target}"
    echo "Synced ${label} into plugins/gms-remote-test/scripts/"
}

SOURCE_CLI="${REPO_ROOT}/skills/gms-remote-test/scripts/gms-remote-test.sh"
TARGET_CLI="${PLUGIN_DIR}/scripts/gms-remote-test.sh"
SOURCE_MCP="${REPO_ROOT}/skills/gms-remote-test/scripts/mcp_server.py"
TARGET_MCP="${PLUGIN_DIR}/scripts/mcp_server.py"
sync_one "${SOURCE_CLI}" "${TARGET_CLI}" "gms-remote-test.sh"
sync_one "${SOURCE_MCP}" "${TARGET_MCP}" "mcp_server.py"

SOURCE_VERSION="$(sed -n 's/^GMS_RT_VERSION="\(.*\)"/\1/p' "${SOURCE_CLI}")"
SOURCE_MCP_VERSION="$(sed -n 's/^SERVER_VERSION = "\(.*\)"$/\1/p' "${SOURCE_MCP}" | head -n 1)"
echo "Synced gms-remote-test.sh (CLI version ${SOURCE_VERSION:-unknown}) into plugins/gms-remote-test/scripts/"

# R16 drift guard: same version must always mean identical content.  If the
# previous plugin copy already carried this version but differed from the
# source (content drift with an unchanged version — the exact trap that let
# plugins ship fixes skills lacked), the sync FAILS (2026-09-08 audit §一:
# the old code only printed a Warning, which gated nothing).
if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    PREVIOUS_FILE="$(git -C "${REPO_ROOT}" show "HEAD:${TARGET_CLI#"${REPO_ROOT}/"}" 2>/dev/null || true)"
    if [[ -n "${PREVIOUS_FILE}" ]]; then
        PREVIOUS_VERSION="$(printf '%s\n' "${PREVIOUS_FILE}" | sed -n 's/^GMS_RT_VERSION="\(.*\)"/\1/p' | head -n 1)"
        PREVIOUS_HASH="$(printf '%s\n' "${PREVIOUS_FILE}" | sha256sum | cut -d' ' -f1)"
        SOURCE_HASH="$(sha256sum "${SOURCE_CLI}" | cut -d' ' -f1)"
        # Same version + different content = silent drift trap; a version
        # bump with new content is the normal release path.
        if [[ -n "${PREVIOUS_VERSION}" && "${PREVIOUS_VERSION}" = "${SOURCE_VERSION:-}" \
              && "${PREVIOUS_HASH}" != "${SOURCE_HASH}" ]]; then
            echo "Error: plugin CLI content changed at the same version (${SOURCE_VERSION:-unknown})." >&2
            echo "Bump GMS_RT_VERSION in skills/gms-remote-test/scripts/gms-remote-test.sh, then re-run this script." >&2
            exit 1
        fi
    fi
    # 4.txt P0-4: mcp_server.py drift guard — SERVER_VERSION pins the release,
    # so same-version content drift between skill source and plugin is also a
    # silent ship-the-wrong-server trap.
    PREVIOUS_MCP="$(git -C "${REPO_ROOT}" show "HEAD:${TARGET_MCP#"${REPO_ROOT}/"}" 2>/dev/null || true)"
    if [[ -n "${PREVIOUS_MCP}" ]]; then
        PREVIOUS_MCP_VERSION="$(printf '%s\n' "${PREVIOUS_MCP}" | sed -n 's/^SERVER_VERSION = "\(.*\)"$/\1/p' | head -n 1)"
        if [[ -n "${PREVIOUS_MCP_VERSION}" && "${PREVIOUS_MCP_VERSION}" = "${SOURCE_MCP_VERSION:-}" \
              && "$(printf '%s\n' "${PREVIOUS_MCP}" | sha256sum | cut -d' ' -f1)" != "$(sha256sum "${SOURCE_MCP}" | cut -d' ' -f1)" ]]; then
            echo "Error: plugin mcp_server.py content changed at the same version (${SOURCE_MCP_VERSION:-unknown})." >&2
            echo "Bump SERVER_VERSION in skills/gms-remote-test/scripts/mcp_server.py, then re-run this script." >&2
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
