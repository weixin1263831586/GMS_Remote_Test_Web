#!/usr/bin/env bash
# GMS Agent Runtime MCP launcher (10.txt §十九).
#
# Agent manifests should launch the MCP server through this launcher instead
# of python3 mcp_server.py directly. The launcher loads the per-client env
# profile written by install.sh (GMS_REMOTE_TEST_SERVER, GMS_RT_PROFILE,
# GMS_AUTH_TOKEN_FILE, GMS_CURL_CA_CERT) from
#
#     ${XDG_DATA_HOME:-~/.local/share}/gms-remote-test/mcp/<client>.env
#
# so the user never has to `source` anything before starting the agent —
# the env requirement becomes an implementation detail of the runtime.
#
# Selection order for the client name (first wins):
#   1. $GMS_AGENT_CLIENT (set by the plugin manifest env block)
#   2. first existing <client>.env among kimi, codex, kkagent
#
# Security: the profile file is 0600 and only ever exports the four runtime
# variables above; it never contains platform passwords (agents authenticate
# via Agent Service Tokens only, GMS_AGENT_AUTH_MODE=service-token).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_ENV_DIR="${GMS_MCP_ENV_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/gms-remote-test/mcp}"

CLIENT="${GMS_AGENT_CLIENT:-}"
if [ -z "${CLIENT}" ]; then
    for candidate in kimi codex kkagent; do
        if [ -f "${MCP_ENV_DIR}/${candidate}.env" ]; then
            CLIENT="${candidate}"
            break
        fi
    done
fi

if [ -n "${CLIENT}" ] && [ -f "${MCP_ENV_DIR}/${CLIENT}.env" ]; then
    # shellcheck disable=SC1090
    set -a
    . "${MCP_ENV_DIR}/${CLIENT}.env"
    set +a
fi

# Agents must never run in password mode even if the env profile is missing:
# the MCP server itself refuses to register password tools without this flag.
export GMS_AGENT_AUTH_MODE="${GMS_AGENT_AUTH_MODE:-service-token}"

exec python3 "${SCRIPT_DIR}/mcp_server.py" "$@"
