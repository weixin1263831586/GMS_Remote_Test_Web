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
# Values supplied by the deployment API arrive as quoted command arguments,
# so the remote shell does not perform tilde expansion before this script runs.
# Normalize the two supported home-relative forms before creating files or
# persisting the Worker configuration.
case "${SUITE_ROOT}" in
    "~") SUITE_ROOT="${HOME}" ;;
    "~/"*) SUITE_ROOT="${HOME}/${SUITE_ROOT:2}" ;;
esac
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${HOME}/gms-worker-agent"
CONFIG_ROOT="${HOME}/.config/gms-worker"
UNIT_ROOT="${HOME}/.config/systemd/user"
SOFTWARE_ROOT="${HOME}/Software"
HOST_TOOLS_SOURCE="${PROJECT_ROOT}/tools/GMS-Host-Tools"

mkdir -p "${INSTALL_ROOT}" "${CONFIG_ROOT}" "${UNIT_ROOT}" \
    "${HOME}/.cache/gms-worker/pycache" "${HOME}/gms-worker-data" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools"

if [[ ! -d "${HOST_TOOLS_SOURCE}/jdk-11" ]]; then
    echo "Missing bundled host tools directory: jdk-11" >&2
    exit 1
fi
if [[ ! -f "${HOST_TOOLS_SOURCE}/platform-tools-gms-linux.zip" ]]; then
    echo "Missing bundled host tools archive: platform-tools-gms-linux.zip" >&2
    exit 1
fi

# Replace complete tool directories so repeated deployments can overwrite JDK
# read-only notices and cannot retain stale files from an older tool release.
rm -rf "${SOFTWARE_ROOT}/jdk-11" "${SOFTWARE_ROOT}/platform-tools"
rsync -a "${HOST_TOOLS_SOURCE}/jdk-11/" "${SOFTWARE_ROOT}/jdk-11/"

# GitHub rejects files larger than 100 MiB. The JDK module image is stored in
# numbered chunks in the repository and restored before Java is first used.
python3 - "${SOFTWARE_ROOT}/jdk-11/lib" <<'PY'
import sys
from pathlib import Path

lib_dir = Path(sys.argv[1])
parts = sorted(lib_dir.glob("modules.part.*"))
if not parts:
    raise SystemExit("Missing JDK module chunks: modules.part.*")
with (lib_dir / "modules").open("wb") as output:
    for part in parts:
        output.write(part.read_bytes())
for part in parts:
    part.unlink()
PY
python3 "${PROJECT_ROOT}/scripts/extract_zip_preserve_mode.py" \
    "${HOST_TOOLS_SOURCE}/platform-tools-gms-linux.zip" "${SOFTWARE_ROOT}"
install -m 755 "${HOST_TOOLS_SOURCE}/env.sh" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/env.sh"
install -m 755 "${HOST_TOOLS_SOURCE}/verify.sh" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/verify.sh"
install -m 644 "${HOST_TOOLS_SOURCE}/README.md" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/README.md"
install -m 600 "${HOST_TOOLS_SOURCE}/gts-rockchip.json" \
    "${SOFTWARE_ROOT}/gts-rockchip.json"

# Replace legacy one-line Software exports and maintain one idempotent block.
python3 "${PROJECT_ROOT}/scripts/configure_gms_host_tools.py" "${HOME}/.bashrc"

# Desktop dependencies are installed together with the Worker so the host is
# immediately usable from the Controller's 主机桌面 page. Existing packages
# are left untouched. sudo may prompt once on a newly provisioned host.
if ! command -v x11vnc >/dev/null 2>&1 || \
        ! command -v websockify >/dev/null 2>&1 || \
        ! command -v Xvfb >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        x11vnc xvfb novnc websockify
fi
rsync -a --delete "${PROJECT_ROOT}/worker_agent/" "${INSTALL_ROOT}/worker_agent/"
mkdir -p "${INSTALL_ROOT}/scripts" "${INSTALL_ROOT}/tools"
install -m 755 "${PROJECT_ROOT}/scripts/run_GSI_Burn.sh" \
    "${INSTALL_ROOT}/scripts/run_GSI_Burn.sh"
install -m 755 "${PROJECT_ROOT}/scripts/run_GMS_Test_Auto.sh" \
    "${INSTALL_ROOT}/scripts/run_GMS_Test_Auto.sh"
install -m 755 "${PROJECT_ROOT}/tools/upgrade_tool" \
    "${INSTALL_ROOT}/tools/upgrade_tool"
rsync -a --delete "${PROJECT_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4/" \
    "${INSTALL_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4/"
for platform_tool in adb fastboot aapt aapt2; do
    install -m 755 "${SOFTWARE_ROOT}/platform-tools/${platform_tool}" \
        "${INSTALL_ROOT}/tools/${platform_tool}"
done
if [[ -d "${SOFTWARE_ROOT}/platform-tools/lib64" ]]; then
    rsync -a "${SOFTWARE_ROOT}/platform-tools/lib64/" \
        "${INSTALL_ROOT}/tools/lib64/"
fi
install -m 600 /dev/null "${CONFIG_ROOT}/token"
printf '%s\n' "${TOKEN}" > "${CONFIG_ROOT}/token"
CONTROLLER_CA=""
if [[ "${CONTROLLER_CERT}" != "-" ]]; then
    install -m 644 "${CONTROLLER_CERT}" "${CONFIG_ROOT}/controller.crt"
    CONTROLLER_CA="${CONFIG_ROOT}/controller.crt"
fi

# The test launcher lives at the suite root so that cluster_test_execution
# can resolve it as ${SUITE_ROOT}/run_GMS_Test_Auto.sh on the Worker.
mkdir -p "${SUITE_ROOT}"
install -m 755 "${PROJECT_ROOT}/scripts/run_GMS_Test_Auto.sh" \
    "${SUITE_ROOT}/run_GMS_Test_Auto.sh"

python3 - "${CONFIG_ROOT}/config.json" "${WORKER_ID}" "${CONTROLLER_URL}" \
    "${WORKER_ADDRESS}" "${CONTROLLER_CA}" "${CONFIG_ROOT}/token" \
    "${SUITE_ROOT}" "${HOME}/gms-worker-data" <<'PY'
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
    suite_root,
    data_root,
) = sys.argv[1:]
payload = {
    "worker_id": worker_id,
    "name": worker_id,
    "controller_url": controller_url,
    "address": worker_address,
    "controller_ca": controller_ca,
    "worker_token_file": token_file,
    "heartbeat_interval_seconds": 15,
    "suite_scan_interval_seconds": 300,
    "max_jobs": 1,
    "suite_roots": [suite_root, "/opt/GMS-Suite"],
    "data_root": data_root,
}
Path(config_path).write_text(
    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY

cat > "${UNIT_ROOT}/gms-worker-agent.service" <<EOF
[Unit]
Description=GMS Remote Test Worker Agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_ROOT}
Environment=GMS_WORKER_CONFIG=${CONFIG_ROOT}/config.json
Environment=PYTHONPYCACHEPREFIX=${HOME}/.cache/gms-worker/pycache
Environment=JAVA_HOME=${SOFTWARE_ROOT}/jdk-11
Environment=JRE_HOME=${SOFTWARE_ROOT}/jdk-11
Environment=APE_API_KEY=${SOFTWARE_ROOT}/gts-rockchip.json
Environment=GMS_WORKER_AAPT2_PATH=${SOFTWARE_ROOT}/platform-tools/aapt2
Environment=PATH=${SOFTWARE_ROOT}/platform-tools:${SOFTWARE_ROOT}/jdk-11/bin:${INSTALL_ROOT}/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
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
cat > "${UNIT_ROOT}/gms-worker-xvfb.service" <<'EOF'
[Unit]
Description=GMS Worker Headless X Display

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp -noreset
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

cat > "${UNIT_ROOT}/gms-worker-novnc.service" <<EOF
[Unit]
Description=GMS Worker noVNC Desktop
After=gms-worker-x11vnc.service
Wants=gms-worker-x11vnc.service

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

# Wayland cannot be captured by x11vnc. Use the managed Xvfb display for
# Wayland and headless hosts, while X11 hosts continue to use their real
# desktop display.
session_id="$(loginctl show-user "${USER}" -p Display --value 2>/dev/null || true)"
session_type="$(loginctl show-session "${session_id}" -p Type --value 2>/dev/null || true)"

# SSH sessions commonly expose DISPLAY=localhost:10.0, which is an SSH
# forwarding display and cannot be shared. Discover the local X/Xwayland
# socket and its session authority instead.
display="${GMS_VNC_DISPLAY:-}"
if [[ -z "${display}" && "${session_type}" == "x11" ]]; then
    socket="$(find /tmp/.X11-unix -maxdepth 1 -type s -name 'X*' -printf '%f\n' 2>/dev/null | sort -V | head -n 1)"
    [[ -n "${socket}" ]] && display=":${socket#X}"
fi
display="${display:-:99}"

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
if [[ "${display}" != ":99" && -n "${auth}" && -r "${auth}" ]]; then
    args+=(-auth "${auth}")
elif [[ "${display}" != ":99" ]]; then
    args+=(-auth guess)
fi
exec /usr/bin/x11vnc "${args[@]}"
EOF
chmod 755 "${INSTALL_ROOT}/start-x11vnc.sh"

cat > "${UNIT_ROOT}/gms-worker-x11vnc.service" <<EOF
[Unit]
Description=GMS Worker x11vnc Server
After=graphical-session.target gms-worker-xvfb.service
Wants=gms-worker-xvfb.service

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
# User services otherwise stop at logout on hosts where lingering is disabled.
# Keeping the Worker account's user manager alive is required for 7x24 tests.
sudo loginctl enable-linger "${USER}"
systemctl --user enable gms-worker-agent
# `enable --now` does not restart an already-running unit. A redeployment may
# change the Worker ID, token or Controller URL, so always restart to make the
# freshly written configuration effective.
if [[ -n "${NOVNC_WEB_ROOT}" ]]; then
    systemctl --user enable --now gms-worker-xvfb.service \
        gms-worker-x11vnc.service gms-worker-novnc.service
fi
# Register only after the optional desktop units have settled so the reported
# noVNC capability reflects the real VNC backend, not merely an installed port.
systemctl --user restart gms-worker-agent
systemctl --user --no-pager status gms-worker-agent
