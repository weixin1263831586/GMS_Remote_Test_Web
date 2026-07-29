#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 ]]; then
    echo "Usage: $0 WORKER_ID CONTROLLER_URL TOKEN_FILE CONTROLLER_CERT WORKER_ADDRESS" >&2
    exit 2
fi

WORKER_ID="$1"
CONTROLLER_URL="$2"
TOKEN_FILE="$3"
CONTROLLER_CERT="$4"
WORKER_ADDRESS="$5"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${HOME}/gms-adb-proxy-source"
CONFIG_ROOT="${HOME}/.config/gms-worker"
UNIT_ROOT="${HOME}/.config/systemd/user"

if ! command -v adb >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y adb
fi

"${PROJECT_ROOT}/scripts/install_adbproxy_rs.sh"
mkdir -p \
    "${INSTALL_ROOT}" \
    "${CONFIG_ROOT}" \
    "${UNIT_ROOT}" \
    "${HOME}/.cache/gms-worker/pycache" \
    "${HOME}/gms-worker-data"
rm -rf "${INSTALL_ROOT}/worker_agent"
cp -a "${PROJECT_ROOT}/worker_agent" "${INSTALL_ROOT}/worker_agent"

[[ -f "${TOKEN_FILE}" ]] || {
    echo "Worker token file does not exist: ${TOKEN_FILE}" >&2
    exit 2
}
install -m 600 "${TOKEN_FILE}" "${CONFIG_ROOT}/token"
CONTROLLER_CA=""
if [[ "${CONTROLLER_CERT}" != "-" ]]; then
    install -m 644 "${CONTROLLER_CERT}" "${CONFIG_ROOT}/controller.crt"
    CONTROLLER_CA="${CONFIG_ROOT}/controller.crt"
fi

python3 - "${CONFIG_ROOT}/config.json" "${WORKER_ID}" "${CONTROLLER_URL}" \
    "${WORKER_ADDRESS}" "${CONTROLLER_CA}" "${CONFIG_ROOT}/token" \
    "${HOME}/gms-worker-data" <<'PY'
import json
import sys
from pathlib import Path

(
    config_path,
    worker_id,
    controller_url,
    worker_address,
    controller_ca,
    token_file,
    data_root,
) = sys.argv[1:]
payload = {
    "worker_id": worker_id,
    "name": f"Ubuntu ADB来源 · {worker_address}",
    "controller_url": controller_url,
    "address": worker_address,
    "controller_ca": controller_ca,
    "worker_token_file": token_file,
    "heartbeat_interval_seconds": 10,
    "suite_scan_interval_seconds": 3600,
    "max_jobs": 1,
    "source_only": True,
    "suite_roots": [],
    "data_root": data_root,
}
Path(config_path).write_text(
    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY

cat > "${UNIT_ROOT}/gms-worker-agent.service" <<EOF
[Unit]
Description=GMS ADB Proxy Source Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_ROOT}
Environment=GMS_WORKER_CONFIG=${CONFIG_ROOT}/config.json
Environment=GMS_ENV=production
Environment=PYTHONPYCACHEPREFIX=${HOME}/.cache/gms-worker/pycache
Environment=GMS_ADB_PROXY_BIN_DIR=${HOME}/.local/bin
Environment=PATH=${HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/python3 -m worker_agent.app
Restart=on-failure
RestartSec=5
KillMode=process
UMask=0077
LimitNOFILE=65536

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
sudo loginctl enable-linger "${USER}"
systemctl --user enable --now gms-worker-agent.service
systemctl --user --no-pager status gms-worker-agent.service
