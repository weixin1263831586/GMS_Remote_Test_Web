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
set -euo pipefail

PLUGIN_ID="gms-remote-test"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${1:-${HOME}/.kkagent/plugins/local/${PLUGIN_ID}}"
REGISTRY="${HOME}/.kkagent/plugins/installed.json"

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

echo "done. restart kkagent to activate the new plugin version."
