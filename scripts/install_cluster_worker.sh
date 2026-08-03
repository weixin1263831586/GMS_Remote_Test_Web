#!/bin/bash
set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "Usage: $0 WORKER_ID CONTROLLER_URL TOKEN CONTROLLER_CERT [SUITE_ROOT] [WORKER_ADDRESS] [GTS_CREDENTIAL_FILE]" >&2
    exit 2
fi

WORKER_ID="$1"
CONTROLLER_URL="$2"
TOKEN="$3"
CONTROLLER_CERT="$4"
SUITE_ROOT="${5:-${HOME}/GMS-Suite}"
WORKER_ADDRESS="${6:-}"
GTS_CREDENTIAL_FILE="${7:-${GMS_GTS_CREDENTIAL_FILE:-}}"
NOVNC_LISTEN_HOST="${WORKER_ADDRESS:-127.0.0.1}"
if [[ "${NOVNC_LISTEN_HOST}" == "0.0.0.0" || "${NOVNC_LISTEN_HOST}" == "::" ]]; then
    echo "Worker noVNC must bind to a private address or loopback, not ${NOVNC_LISTEN_HOST}" >&2
    exit 2
fi
python3 - "${NOVNC_LISTEN_HOST}" <<'PY'
import ipaddress
import socket
import sys

host = sys.argv[1]
try:
    addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
except socket.gaierror as exc:
    raise SystemExit(f"Cannot resolve Worker noVNC listen host {host}: {exc}")
cgnat = ipaddress.ip_network("100.64.0.0/10")
for value in addresses:
    address = ipaddress.ip_address(value)
    if not (address.is_private or address.is_loopback or address in cgnat):
        raise SystemExit(
            f"Worker noVNC listen address must be private/loopback: {address}"
        )
PY
CONTROLLER_ALLOW_ADDRESSES="$(python3 - "${CONTROLLER_URL}" <<'PY'
import socket
import sys
from urllib.parse import urlsplit

host = urlsplit(sys.argv[1]).hostname or ""
if not host:
    raise SystemExit("Controller URL must contain a hostname")
try:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None)})
except socket.gaierror as exc:
    raise SystemExit(f"Cannot resolve Controller host {host}: {exc}")
if not addresses:
    raise SystemExit(f"Controller host {host} resolved to no addresses")
print(" ".join(addresses))
PY
)"
# 展开部署参数中的主目录路径。
case "${SUITE_ROOT}" in
    "~") SUITE_ROOT="${HOME}" ;;
    "~/"*) SUITE_ROOT="${HOME}/${SUITE_ROOT:2}" ;;
esac
case "${GTS_CREDENTIAL_FILE}" in
    "~") GTS_CREDENTIAL_FILE="${HOME}" ;;
    "~/"*) GTS_CREDENTIAL_FILE="${HOME}/${GTS_CREDENTIAL_FILE:2}" ;;
esac
if [[ -z "${GTS_CREDENTIAL_FILE}" || ! -f "${GTS_CREDENTIAL_FILE}" ]]; then
    echo "GTS credential must be supplied through GTS_CREDENTIAL_FILE" >&2
    exit 2
fi
python3 - "${GTS_CREDENTIAL_FILE}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("type") != "service_account" or not payload.get("client_email"):
    raise SystemExit("GTS credential is not a service-account document")
private_key = str(payload.get("private_key") or "")
if "-----BEGIN PRIVATE KEY-----" not in private_key or "-----END PRIVATE KEY-----" not in private_key:
    raise SystemExit("GTS credential does not contain a valid private key")
PY
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

# 完整替换工具目录，避免残留文件。
rm -rf "${SOFTWARE_ROOT}/jdk-11" "${SOFTWARE_ROOT}/platform-tools"
rsync -a "${HOST_TOOLS_SOURCE}/jdk-11/" "${SOFTWARE_ROOT}/jdk-11/"

# 合并仓库中的 JDK 模块分片。
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
# 同步 scrcpy 到 Controller 配置使用的路径。
rsync -a --delete \
    "${PROJECT_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4/" \
    "${SOFTWARE_ROOT}/scrcpy-linux-x86_64-v3.3.4/"
install -m 600 "${GTS_CREDENTIAL_FILE}" \
    "${SOFTWARE_ROOT}/gts-rockchip.json"

# 幂等更新 Shell 环境配置块。
python3 "${PROJECT_ROOT}/scripts/configure_gms_host_tools.py" "${HOME}/.bashrc"
"${PROJECT_ROOT}/scripts/install_adbproxy_rs.sh"

# 安装 Worker 桌面功能依赖。
if ! command -v x11vnc >/dev/null 2>&1 || \
        ! command -v websockify >/dev/null 2>&1 || \
        ! command -v Xvfb >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        x11vnc xvfb novnc websockify
fi
if ! command -v usbip >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y linux-tools-generic
fi
sudo install -d -m 755 /usr/local/libexec
sudo install -m 755 "${PROJECT_ROOT}/scripts/gms_worker_usbip.sh" \
    /usr/local/libexec/gms-worker-usbip
printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/gms-worker-usbip *\n' "$(id -un)" \
    | sudo tee /etc/sudoers.d/gms-worker-usbip >/dev/null
sudo chmod 440 /etc/sudoers.d/gms-worker-usbip
sudo visudo -cf /etc/sudoers.d/gms-worker-usbip >/dev/null
rsync -a --delete "${PROJECT_ROOT}/worker_agent/" "${INSTALL_ROOT}/worker_agent/"
rsync -a --delete "${PROJECT_ROOT}/foundation/" "${INSTALL_ROOT}/foundation/"
mkdir -p "${INSTALL_ROOT}/scripts" "${INSTALL_ROOT}/tools"
install -m 755 "${PROJECT_ROOT}/scripts/run_GSI_Burn.sh" \
    "${INSTALL_ROOT}/scripts/run_GSI_Burn.sh"
install -m 755 "${PROJECT_ROOT}/scripts/run_GMS_Test_Auto.sh" \
    "${INSTALL_ROOT}/scripts/run_GMS_Test_Auto.sh"
install -m 755 "${PROJECT_ROOT}/tools/upgrade_tool" \
    "${INSTALL_ROOT}/tools/upgrade_tool"
install -m 644 "${PROJECT_ROOT}/tools/misc.img" \
    "${INSTALL_ROOT}/tools/misc.img"
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
install -m 600 /dev/null "${CONFIG_ROOT}/novnc-targets"
printf '%s: localhost:5900\n' "${TOKEN}" > "${CONFIG_ROOT}/novnc-targets"
CONTROLLER_CA=""
if [[ "${CONTROLLER_CERT}" != "-" ]]; then
    install -m 644 "${CONTROLLER_CERT}" "${CONFIG_ROOT}/controller.crt"
    CONTROLLER_CA="${CONFIG_ROOT}/controller.crt"
fi

# 测试启动脚本固定安装到套件根目录。
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
Environment=GMS_ENV=production
Environment=PYTHONPYCACHEPREFIX=${HOME}/.cache/gms-worker/pycache
Environment=JAVA_HOME=${SOFTWARE_ROOT}/jdk-11
Environment=JRE_HOME=${SOFTWARE_ROOT}/jdk-11
Environment=APE_API_KEY=${SOFTWARE_ROOT}/gts-rockchip.json
Environment=GMS_WORKER_AAPT2_PATH=${SOFTWARE_ROOT}/platform-tools/aapt2
Environment=GMS_ADB_PROXY_BIN_DIR=${HOME}/.local/bin
Environment=PATH=${HOME}/.local/bin:${SOFTWARE_ROOT}/platform-tools:${SOFTWARE_ROOT}/jdk-11/bin:${INSTALL_ROOT}/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/python3 -m worker_agent.app
Restart=on-failure
RestartSec=5
KillMode=process
UMask=0077
LimitNOFILE=65536
TasksMax=4096
# 用户级服务不启用需要 CAP_SYS_ADMIN 的命名空间限制。

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
UMask=0077
# 用户级服务不启用命名空间限制。

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
ExecStart=/usr/bin/websockify --web=${NOVNC_WEB_ROOT} --token-plugin=TokenFile --token-source=${CONFIG_ROOT}/novnc-targets ${NOVNC_LISTEN_HOST}:6080
Restart=on-failure
RestartSec=3
UMask=0077
# 用户级服务不启用命名空间限制。
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
IPAddressAllow=${CONTROLLER_ALLOW_ADDRESSES}

[Install]
WantedBy=default.target
EOF

cat > "${INSTALL_ROOT}/start-x11vnc.sh" <<'EOF'
#!/bin/bash
set -euo pipefail

# Wayland 和无头主机使用 Xvfb，X11 主机使用真实显示。
session_id="$(loginctl show-user "${USER}" -p Display --value 2>/dev/null || true)"
session_type="$(loginctl show-session "${session_id}" -p Type --value 2>/dev/null || true)"

# 忽略 SSH 转发 DISPLAY，查找本地 X/Xwayland 显示。
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

args=(-display "${display}" -forever -shared -rfbport 5900 -localhost -nopw)
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
UMask=0077
# 用户级服务不启用命名空间限制。

[Install]
WantedBy=default.target
EOF
fi

systemctl --user daemon-reload
# 保持 Worker 用户服务在注销后继续运行。
sudo loginctl enable-linger "${USER}"
systemctl --user enable gms-worker-agent
# 重启服务以应用部署配置。
if [[ -n "${NOVNC_WEB_ROOT}" ]]; then
    systemctl --user enable --now gms-worker-xvfb.service \
        gms-worker-x11vnc.service gms-worker-novnc.service
fi
# 桌面服务稳定后再注册 Worker 能力。
systemctl --user restart gms-worker-agent
systemctl --user --no-pager status gms-worker-agent
