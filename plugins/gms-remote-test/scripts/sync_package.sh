#!/usr/bin/env bash
# Build the self-contained plugin release payload (10.txt §二十一).
#
# skills/gms-remote-test/ is the single source of truth. Everything under
# plugins/gms-remote-test/ is a generated release copy:
#
#   scripts/gms-remote-test.sh   <- skills/.../scripts/gms-remote-test.sh
#   scripts/mcp_server.py        <- skills/.../scripts/mcp_server.py
#   scripts/mcp_launcher.sh      <- skills/.../scripts/mcp_launcher.sh
#   scripts/agent_mcp_config.py  <- skills/.../scripts/agent_mcp_config.py
#   scripts/gms-agent            <- skills/.../scripts/gms-agent
#   scripts/gms_agent/           <- skills/.../scripts/gms_agent/       (SDK)
#   skills/gms-remote-test/      <- skills/gms-remote-test/{SKILL.md,
#                                   references/,agents/}                (skill docs)
#
# The plugin therefore carries BOTH the MCP tools and the operational Skill
# (10.txt §七/§八): installing the plugin gives an agent "能操作 + 知道怎么
# 正确操作" as one unit. Only the three manifests (kk/kimi/.codex-plugin)
# and the tests stay hand-maintained under plugins/.
#
# Usage: plugins/gms-remote-test/scripts/sync_package.sh [repo_root]
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="${1:-$(cd "${PLUGIN_DIR}/../.." && pwd)}"
SKILL_SRC="${REPO_ROOT}/skills/gms-remote-test"

sync_one() {
    local source="$1" target="$2" label="$3"
    if [[ ! -f "${source}" ]]; then
        echo "Error: ${label} not found at ${source}" >&2
        exit 1
    fi
    mkdir -p "$(dirname "${target}")"
    if [[ -f "${target}" ]] && cmp -s "${source}" "${target}"; then
        return 0
    fi
    cp "${source}" "${target}"
    chmod 755 "${target}" 2>/dev/null || true
    echo "Synced ${label}"
}

sync_tree() {
    # Mirror every file of a directory subtree (shallow: no recursion into
    # nested dirs is needed for references/ and agents/, both are flat).
    local source_dir="$1" target_dir="$2" label="$3"
    if [[ ! -d "${source_dir}" ]]; then
        echo "Error: ${label} not found at ${source_dir}" >&2
        exit 1
    fi
    mkdir -p "${target_dir}"
    local file name
    for file in "${source_dir}"/*; do
        [[ -f "${file}" ]] || continue
        name="$(basename "${file}")"
        sync_one "${file}" "${target_dir}/${name}" "${label}/${name}"
    done
}

sync_dir_tree() {
    # Recursive mirror used for the gms_agent/ SDK package.
    local source_dir="$1" target_dir="$2" label="$3"
    if [[ ! -d "${source_dir}" ]]; then
        echo "Error: ${label} not found at ${source_dir}" >&2
        exit 1
    fi
    local file rel
    while IFS= read -r -d '' file; do
        rel="${file#"${source_dir}/"}"
        case "${rel}" in
            *__pycache__*) continue ;;
        esac
        sync_one "${file}" "${target_dir}/${rel}" "${label}/${rel}"
    done < <(find "${source_dir}" -type f -print0 | sort -z)
}

# --- scripts (CLI + MCP + runtime helpers) -------------------------------
for script in gms-remote-test.sh mcp_server.py mcp_launcher.sh agent_mcp_config.py gms-agent; do
    sync_one "${SKILL_SRC}/scripts/${script}" "${PLUGIN_DIR}/scripts/${script}" "${script}"
done
sync_dir_tree "${SKILL_SRC}/scripts/gms_agent" "${PLUGIN_DIR}/scripts/gms_agent" "gms_agent/"

# --- skill content (SKILL.md + references + agent metadata) --------------
sync_one "${SKILL_SRC}/SKILL.md" "${PLUGIN_DIR}/skills/gms-remote-test/SKILL.md" "SKILL.md"
sync_tree "${SKILL_SRC}/references" "${PLUGIN_DIR}/skills/gms-remote-test/references" "references"
sync_tree "${SKILL_SRC}/agents" "${PLUGIN_DIR}/skills/gms-remote-test/agents" "agents"

# --- version contract ----------------------------------------------------
# kk.plugin.json, kimi.plugin.json, .codex-plugin/plugin.json, MCP
# SERVER_VERSION and CLI GMS_RT_VERSION must share one release version.
manifest_version() {
    sed -n 's/^  "version": "\(.*\)",$/\1/p' "$1" | head -n 1
}
MCP_VERSION="$(sed -n 's/^SERVER_VERSION = "\(.*\)"$/\1/p' "${SKILL_SRC}/scripts/mcp_server.py" | head -n 1)"
CLI_VERSION="$(sed -n 's/^GMS_RT_VERSION="\(.*\)"/\1/p' "${SKILL_SRC}/scripts/gms-remote-test.sh" | head -n 1)"
PKG_VERSION="$(sed -n 's/^version: \(.*\)$/\1/p' "${REPO_ROOT}/agent/gms-remote-test/package.yaml" | head -n 1)"
KK_VERSION="$(manifest_version "${PLUGIN_DIR}/kk.plugin.json")"
KIMI_VERSION="$(manifest_version "${PLUGIN_DIR}/kimi.plugin.json")"
CODEX_VERSION="$(manifest_version "${PLUGIN_DIR}/.codex-plugin/plugin.json")"

for v in "${PKG_VERSION:-}" "${MCP_VERSION:-}" "${CLI_VERSION:-}" "${KK_VERSION:-}" "${KIMI_VERSION:-}" "${CODEX_VERSION:-}"; do
    if [[ -z "${v}" ]]; then
        echo "Error: unable to read one of the six version declarations" >&2
        exit 1
    fi
done
DRIFT=0
for v in "${MCP_VERSION}" "${CLI_VERSION}" "${KK_VERSION}" "${KIMI_VERSION}" "${CODEX_VERSION}"; do
    if [[ "${v}" != "${PKG_VERSION}" ]]; then
        echo "Version drift: ${v} != package.yaml ${PKG_VERSION}" >&2
        DRIFT=1
    fi
done
if [[ "${DRIFT}" = "1" ]]; then
    echo "  package.yaml          = ${PKG_VERSION}" >&2
    echo "  mcp_server.py         = ${MCP_VERSION}" >&2
    echo "  gms-remote-test.sh    = ${CLI_VERSION}" >&2
    echo "  kk.plugin.json        = ${KK_VERSION}" >&2
    echo "  kimi.plugin.json      = ${KIMI_VERSION}" >&2
    echo "  .codex-plugin/plugin  = ${CODEX_VERSION}" >&2
    echo "Fix: python tools/release_agent.py --version <new>" >&2
    exit 1
fi

# --- R16 drift guard: same version must mean identical content -----------
if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    check_no_same_version_content_change() {
        local target="$1" version="$2" version_re="$3" label="$4"
        local previous
        previous="$(git -C "${REPO_ROOT}" show "HEAD:${target#"${REPO_ROOT}/"}" 2>/dev/null || true)"
        [[ -n "${previous}" ]] || return 0
        local previous_version
        previous_version="$(printf '%s\n' "${previous}" | sed -n "${version_re}" | head -n 1)"
        if [[ -n "${previous_version}" && "${previous_version}" = "${version}" \
              && "$(printf '%s\n' "${previous}" | sha256sum | cut -d' ' -f1)" \
                  != "$(sha256sum "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "${target}" | cut -d' ' -f1)" ]]; then
            echo "Error: ${label} content changed at the same version (${version})." >&2
            echo "Bump the version with tools/release_agent.py, then re-run sync_package.sh." >&2
            exit 1
        fi
    }
    # Guard applies to the skill-side sources (the single source of truth);
    # plugin copies are regenerated from them, so they cannot drift alone.
    check_no_same_version_content_change \
        "${SKILL_SRC}/scripts/gms-remote-test.sh" "${CLI_VERSION}" \
        's/^GMS_RT_VERSION="\(.*\)"/\1/p' "gms-remote-test.sh"
    check_no_same_version_content_change \
        "${SKILL_SRC}/scripts/mcp_server.py" "${MCP_VERSION}" \
        's/^SERVER_VERSION = "\(.*\)"$/\1/p' "mcp_server.py"
fi

echo "Version contract OK: ${PKG_VERSION} (6 declarations, plugin payload synced)"
