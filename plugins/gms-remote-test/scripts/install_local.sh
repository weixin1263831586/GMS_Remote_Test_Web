#!/usr/bin/env bash
# Install ("clone") this plugin into kkagent as a local plugin and keep the
# registry version in sync. Safe to re-run; refuses partial overwrites.
#
# Usage:
#   scripts/install_local.sh              # install to ~/.kkagent/plugins/local/gms-remote-test
#   scripts/install_local.sh /other/dir   # install to a custom directory
#
# After installing, restart kkagent (or reconnect plugins) so the new MCP
# server process picks up the changes.
#
# 2026-09-10: also reconciles the kkagent MCP registration so an existing
# hand-written `[mcp_servers.gms]` (direct `python3 mcp_server.py` launch,
# possibly without GMS_AUTH_TOKEN_FILE in env) is upgraded to the launcher
# entrypoint — the same reconciliation `gms-agent install` performs for
# registry installs, which local "翻版" installs previously skipped. The
# 0.12→0.15 upgrade hit exactly this gap: files updated, but the client kept
# launching the old entrypoint without a token file, so every authenticated
# tool failed with "Authentication required".
set -euo pipefail

PLUGIN_ID="gms-remote-test"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${1:-${HOME}/.kkagent/plugins/local/${PLUGIN_ID}}"
REGISTRY="${HOME}/.kkagent/plugins/installed.json"
KKAGENT_CONFIG="${KKAGENT_CONFIG:-${HOME}/.kkagent/config.toml}"

[ -f "${SOURCE_DIR}/kk.plugin.json" ] || {
    echo "error: kk.plugin.json not found in ${SOURCE_DIR}" >&2
    exit 1
}

VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
    "${SOURCE_DIR}/kk.plugin.json")

mkdir -p "$(dirname "${TARGET_DIR}")"

# Copy the plugin payload; skip caches and this installer's own temp files.
rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
    "${SOURCE_DIR}/" "${TARGET_DIR}/"
echo "installed ${PLUGIN_ID} ${VERSION} -> ${TARGET_DIR}"

# Keep the registry version current so /plugins shows the real version and
# updates are detected. Local plugins keep their existing "source": "local".
if [ -f "${REGISTRY}" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "${REGISTRY}" "${PLUGIN_ID}" "${VERSION}" "${TARGET_DIR}" <<'PY'
import json, sys, os
from datetime import datetime, timezone

registry_path, plugin_id, version, root = sys.argv[1:5]
data = json.load(open(registry_path))
plugins = data.setdefault("plugins", [])
now = datetime.now(timezone.utc).isoformat()
entry = next((p for p in plugins if p.get("id") == plugin_id), None)
if entry is None:
    entry = {"id": plugin_id, "source": "local", "enabled": True}
    plugins.append(entry)
    print(f"registered new local plugin {plugin_id}")
entry.update({
    "root": root,
    "source": entry.get("source", "local"),
    "enabled": entry.get("enabled", True),
    "updatedAt": now,
    "version": version,
})
if "installedAt" not in entry:
    entry["installedAt"] = now
json.dump(data, open(registry_path, "w"), indent=2)
print(f"registry updated: {plugin_id} -> {version}")
PY
fi

# Reconcile the kkagent MCP registration (2026-09-10). If the user's
# config.toml still points [mcp_servers.gms] directly at mcp_server.py,
# repoint it at mcp_launcher.py (which loads the per-client profile and
# forces service-token auth mode) and back the config up first. A launcher
# registration is left untouched. When the launch is (or stays) direct, warn
# if GMS_AUTH_TOKEN_FILE is absent so the failure surfaces at install time
# instead of as "Authentication required" on every tool call afterwards.
reconcile_mcp_registration() {
    [ -f "${KKAGENT_CONFIG}" ] || {
        echo "note: ${KKAGENT_CONFIG} not found; skipping MCP registration check"
        return 0
    }
    python3 - "${KKAGENT_CONFIG}" <<'PY'
import re
import shutil
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
text = config_path.read_text(encoding="utf-8")

# TOML section = the header line plus every line up to the next line-initial
# "[table]" header (NOT merely the next "[": args arrays contain brackets).
m = re.search(r"(^\[mcp_servers\.gms\]$)(.*?)(?=^\[|\Z)", text, re.M | re.S)
if m is None:
    print("note: no [mcp_servers.gms] section in config.toml; nothing to reconcile")
    raise SystemExit(0)

section = m.group(0)
if "mcp_launcher.py" in section:
    print("mcp registration already uses mcp_launcher.py; ok")
    raise SystemExit(0)

if "mcp_server.py" not in section:
    print("warning: [mcp_servers.gms] uses a custom command; leaving it untouched")
    raise SystemExit(0)

new_section = section.replace(
    "mcp_server.py", "mcp_launcher.py",
)
# The launcher needs to know which client profile to load.
if "GMS_AGENT_CLIENT" not in new_section:
    new_section = new_section.replace(
        "env = {",
        'env = { GMS_AGENT_CLIENT = "kkagent",',
        1,
    )
backup = config_path.with_suffix(".toml.bak-gms-launcher")
shutil.copyfile(config_path, backup)
config_path.write_text(text[: m.start()] + new_section + text[m.end():],
                       encoding="utf-8")
print(f"reconciled [mcp_servers.gms] -> mcp_launcher.py (backup: {backup})")
PY

    # Direct-launch registrations still miss the token env; warn loudly.
    if grep -q '^\[mcp_servers\.gms\]' "${KKAGENT_CONFIG}" && \
       ! grep -A6 '^\[mcp_servers\.gms\]' "${KKAGENT_CONFIG}" | grep -q 'GMS_AUTH_TOKEN_FILE' && \
       ! grep -A6 '^\[mcp_servers\.gms\]' "${KKAGENT_CONFIG}" | grep -q 'mcp_launcher.py'; then
        default_token="${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/default.token"
        echo "warning: [mcp_servers.gms] env lacks GMS_AUTH_TOKEN_FILE." >&2
        if [ -r "${default_token}" ]; then
            echo "warning: add it to the env line, e.g. GMS_AUTH_TOKEN_FILE = \"${default_token}\"" >&2
        else
            echo "warning: no enrolled token found at ${default_token}; run gms-rt-agent-enroll first" >&2
        fi
    fi
}

if command -v python3 >/dev/null 2>&1; then
    reconcile_mcp_registration
fi

echo "done. restart kkagent to activate the new plugin version."
