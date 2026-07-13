#!/bin/bash
set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "Usage: $0 WORKER_ID CONTROLLER_URL TOKEN CONTROLLER_CERT [SUITE_ROOT]" >&2
    exit 2
fi

WORKER_ID="$1"
CONTROLLER_URL="$2"
TOKEN="$3"
CONTROLLER_CERT="$4"
SUITE_ROOT="${5:-${HOME}/GMS-Suite}"
WORKER_ADDRESS="${6:-}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${HOME}/gms-worker-agent"
CONFIG_ROOT="${HOME}/.config/gms-worker"
UNIT_ROOT="${HOME}/.config/systemd/user"

mkdir -p "${INSTALL_ROOT}" "${CONFIG_ROOT}" "${UNIT_ROOT}" \
    "${HOME}/.cache/gms-worker/pycache" "${HOME}/gms-worker-data"

# Desktop dependencies are installed together with the Worker so the host is
# immediately usable from the Controller's 主机桌面 page. Existing packages
# are left untouched. sudo may prompt once on a newly provisioned host.
if ! command -v x11vnc >/dev/null 2>&1 || ! command -v websockify >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y x11vnc novnc websockify
fi
rsync -a --delete "${PROJECT_ROOT}/worker_agent/" "${INSTALL_ROOT}/worker_agent/"
install -m 600 /dev/null "${CONFIG_ROOT}/token"
printf '%s\n' "${TOKEN}" > "${CONFIG_ROOT}/token"
CONTROLLER_CA=""
if [[ "${CONTROLLER_CERT}" != "-" ]]; then
    install -m 644 "${CONTROLLER_CERT}" "${CONFIG_ROOT}/controller.crt"
    CONTROLLER_CA="${CONFIG_ROOT}/controller.crt"
fi

cat > "${CONFIG_ROOT}/config.json" <<EOF
{
  "worker_id": "${WORKER_ID}",
  "name": "${WORKER_ID}",
  "controller_url": "${CONTROLLER_URL}",
  "address": "${WORKER_ADDRESS}",
  "controller_ca": "${CONTROLLER_CA}",
  "worker_token_file": "${CONFIG_ROOT}/token",
  "heartbeat_interval_seconds": 15,
  "suite_scan_interval_seconds": 300,
  "max_jobs": 1,
  "suite_roots": ["${SUITE_ROOT}", "/opt/GMS-Suite"],
  "data_root": "${HOME}/gms-worker-data"
}
EOF

cat > "${UNIT_ROOT}/gms-worker-agent.service" <<EOF
[Unit]
Description=GMS Remote Test Worker Agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_ROOT}
Environment=GMS_WORKER_CONFIG=${CONFIG_ROOT}/config.json
Environment=PYTHONPYCACHEPREFIX=${HOME}/.cache/gms-worker/pycache
ExecStart=/usr/bin/python3 -m worker_agent.app
Restart=on-failure
RestartSec=5
KillMode=process

[Install]
WantedBy=default.target
EOF

NOVNC_WEB_ROOT="$(python3 - <<'PY'
from pathlib import Path
for item in ('/usr/share/novnc', '/usr/share/novnc/'): 
    if Path(item).is_dir():
        print(item)
        break
PY
)"
if [[ -n "${NOVNC_WEB_ROOT}" ]]; then
cat > "${UNIT_ROOT}/gms-worker-novnc.service" <<EOF
[Unit]
Description=GMS Worker noVNC Desktop
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/websockify --web=${NOVNC_WEB_ROOT} 6080 localhost:5900
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

cat > "${INSTALL_ROOT}/start-x11vnc.sh" <<'EOF'
#!/bin/bash
set -euo pipefail

# SSH sessions commonly expose DISPLAY=localhost:10.0, which is an SSH
# forwarding display and cannot be shared. Discover the local X/Xwayland
# socket and its session authority instead.
display="${GMS_VNC_DISPLAY:-}"
if [[ -z "${display}" ]]; then
    socket="$(find /tmp/.X11-unix -maxdepth 1 -type s -name 'X*' -printf '%f\n' 2>/dev/null | sort -V | head -n 1)"
    [[ -n "${socket}" ]] && display=":${socket#X}"
fi
display="${display:-:0}"

auth="${XAUTHORITY:-}"
for candidate in \
    "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gdm/Xauthority" \
    "${HOME}/.Xauthority" \
    "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/Xauthority"; do
    if [[ -z "${auth}" && -r "${candidate}" ]]; then
        auth="${candidate}"
    fi
done

args=(-display "${display}" -forever -shared -rfbport 5900 -nopw)
if [[ -n "${auth}" && -r "${auth}" ]]; then
    args+=(-auth "${auth}")
else
    args+=(-auth guess)
fi
exec /usr/bin/x11vnc "${args[@]}"
EOF
chmod 755 "${INSTALL_ROOT}/start-x11vnc.sh"

cat > "${UNIT_ROOT}/gms-worker-x11vnc.service" <<EOF
[Unit]
Description=GMS Worker x11vnc Server
After=graphical-session.target

[Service]
Type=simple
ExecStart=${INSTALL_ROOT}/start-x11vnc.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
fi

systemctl --user daemon-reload
systemctl --user enable gms-worker-agent
# `enable --now` does not restart an already-running unit. A redeployment may
# change the Worker ID, token or Controller URL, so always restart to make the
# freshly written configuration effective.
systemctl --user restart gms-worker-agent
if [[ -n "${NOVNC_WEB_ROOT}" ]]; then
    systemctl --user enable --now gms-worker-x11vnc.service gms-worker-novnc.service
fi
systemctl --user --no-pager status gms-worker-agent
