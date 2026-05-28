#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="install"

INSTALL_DIR="${GMS_INSTALL_DIR:-/opt/gms-remote-test/web_app}"
SERVICE_NAME="${GMS_SERVICE_NAME:-gms-web-app}"
PORT="${GMS_PORT:-5001}"
RUN_USER="${GMS_RUN_USER:-${SUDO_USER:-$(id -un)}}"
HOST_IP="${GMS_HOST_IP:-}"

DIST_DIR="${GMS_DIST_DIR:-${PROJECT_DIR}/dist}"
PACKAGE_NAME="${GMS_PACKAGE_NAME:-gms-web-app}"
PACKAGE_VERSION="${GMS_PACKAGE_VERSION:-$(date +%Y%m%d_%H%M%S)}"

RUN_GROUP=""
RUN_HOME=""
SSH_KEY_PATH=""
SUDOERS_FILE=""

info() { echo -e "${BLUE}$*${NC}"; }
ok() { echo -e "${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}$*${NC}"; }
fail() { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

usage() {
    cat <<EOF
用法:
  ./install.sh [install] [选项]      一键安装到当前电脑
  ./install.sh package [选项]        生成可复制到其他电脑的安装包

安装选项:
  --install-dir <path>       安装目录，默认: ${INSTALL_DIR}
  --service-name <name>      systemd 服务名，默认: ${SERVICE_NAME}
  --port <port>              FastAPI 监听端口，默认: ${PORT}
  --user <user>              运行服务的本机用户，默认: ${RUN_USER}
  --host-ip <ip>             手动指定本机 IP，默认自动检测

打包选项:
  --dist-dir <path>          安装包输出目录，默认: ${DIST_DIR}
  --package-name <name>      安装包目录/文件名前缀，默认: ${PACKAGE_NAME}
  --version <version>        安装包版本，默认: 当前时间戳
  --package                  等同于 package 子命令

环境变量:
  GMS_INSTALL_DIR / GMS_SERVICE_NAME / GMS_PORT / GMS_RUN_USER / GMS_HOST_IP
  GMS_DIST_DIR / GMS_PACKAGE_NAME / GMS_PACKAGE_VERSION
EOF
}

refresh_user_paths() {
    RUN_GROUP="$(id -gn "${RUN_USER}")"
    RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
    [[ -n "${RUN_HOME}" ]] || fail "无法解析用户 ${RUN_USER} 的 HOME 目录"
    SSH_KEY_PATH="${GMS_SSH_KEY_PATH:-${RUN_HOME}/.ssh/gms_web_app_rsa}"
    SUDOERS_FILE="/etc/sudoers.d/${SERVICE_NAME}-${RUN_USER}"
}

parse_args() {
    if [[ $# -gt 0 ]]; then
        case "$1" in
            install|package)
                ACTION="$1"
                shift
                ;;
            --package)
                ACTION="package"
                shift
                ;;
        esac
    fi

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --install-dir)
                INSTALL_DIR="${2:-}"; shift 2 ;;
            --service-name)
                SERVICE_NAME="${2:-}"; shift 2 ;;
            --port)
                PORT="${2:-}"; shift 2 ;;
            --user)
                RUN_USER="${2:-}"; shift 2 ;;
            --host-ip)
                HOST_IP="${2:-}"; shift 2 ;;
            --dist-dir)
                DIST_DIR="${2:-}"; shift 2 ;;
            --package-name)
                PACKAGE_NAME="${2:-}"; shift 2 ;;
            --version)
                PACKAGE_VERSION="${2:-}"; shift 2 ;;
            --package)
                ACTION="package"; shift ;;
            -h|--help)
                usage; exit 0 ;;
            *)
                fail "未知参数: $1" ;;
        esac
    done

    [[ "${PORT}" =~ ^[0-9]+$ ]] || fail "--port 必须是数字: ${PORT}"
    refresh_user_paths
}

detect_host_ip() {
    local ip
    if command -v ip >/dev/null 2>&1; then
        ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
        [[ -n "${ip}" ]] && { printf '%s\n' "${ip}"; return; }
    fi
    if command -v hostname >/dev/null 2>&1; then
        ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '$1 !~ /^127\./ && $1 !~ /^169\.254\./ {print; exit}')"
        [[ -n "${ip}" ]] && { printf '%s\n' "${ip}"; return; }
    fi
    printf '127.0.0.1\n'
}

package_web_app() {
    local archive root_name
    root_name="${PACKAGE_NAME}"
    archive="${DIST_DIR}/${PACKAGE_NAME}-${PACKAGE_VERSION}.tar.gz"
    mkdir -p "${DIST_DIR}"

    cd "${PROJECT_DIR}"
    tar -czf "${archive}" \
        --transform "s#^#${root_name}/#" \
        --exclude='.git' \
        --exclude='.agents' \
        --exclude='.codex' \
        --exclude='dist' \
        --exclude='__pycache__' \
        --exclude='*/__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        --exclude='.pytest_cache' \
        --exclude='logs' \
        --exclude='*.log' \
        --exclude='*.log.backup.*' \
        --exclude='local.diff' \
        --exclude='configs/config_dynamic.json' \
        --exclude='configs/client_ssh_credentials.local.json' \
        --exclude='configs/redmine_auth.json' \
        --exclude='data/*.json' \
        .

    cat <<EOF
安装包已生成:
  ${archive}

目标电脑部署:
  tar -xzf $(basename "${archive}")
  cd ${root_name}
  ./install.sh
EOF
}

install_system_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3 python3-venv python3-pip rsync curl lsof psmisc \
            openssh-client openssh-server sudo iproute2
        for optional_pkg in usbip adb fastboot android-tools-adb android-tools-fastboot; do
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${optional_pkg}" >/dev/null 2>&1 || true
        done
    else
        warn "未检测到 apt-get，跳过系统依赖安装；请确认 python3/venv/rsync/curl/ssh 已安装"
    fi
}

copy_project() {
    sudo mkdir -p "${INSTALL_DIR}"
    if [[ "$(readlink -f "${PROJECT_DIR}")" != "$(readlink -f "${INSTALL_DIR}")" ]]; then
        sudo rsync -a --delete \
            --exclude '.git/' \
            --exclude '.agents/' \
            --exclude '.codex/' \
            --exclude '__pycache__/' \
            --exclude '*.pyc' \
            --exclude '*.pyo' \
            --exclude '.pytest_cache/' \
            --exclude 'logs/' \
            --exclude '*.log' \
            --exclude '*.log.backup.*' \
            --exclude 'local.diff' \
            --exclude 'dist/' \
            --exclude 'configs/client_ssh_credentials.local.json' \
            --exclude 'configs/redmine_auth.json' \
            "${PROJECT_DIR}/" "${INSTALL_DIR}/"
    fi
    sudo chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}"
}

setup_python_env() {
    sudo -H -u "${RUN_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" -m pip install -r "${INSTALL_DIR}/requirements.txt"
}

setup_local_ssh_key() {
    sudo -H -u "${RUN_USER}" mkdir -p "${RUN_HOME}/.ssh"
    sudo -H -u "${RUN_USER}" chmod 700 "${RUN_HOME}/.ssh"
    if [[ ! -f "${SSH_KEY_PATH}" ]]; then
        sudo -H -u "${RUN_USER}" ssh-keygen -t rsa -b 4096 -N "" -f "${SSH_KEY_PATH}" -C "gms-web-app@$(hostname)"
    fi

    local pub_key
    pub_key="$(sudo -H -u "${RUN_USER}" cat "${SSH_KEY_PATH}.pub")"
    sudo -H -u "${RUN_USER}" touch "${RUN_HOME}/.ssh/authorized_keys"
    if ! sudo -H -u "${RUN_USER}" grep -qxF "${pub_key}" "${RUN_HOME}/.ssh/authorized_keys"; then
        printf '%s\n' "${pub_key}" | sudo -H -u "${RUN_USER}" tee -a "${RUN_HOME}/.ssh/authorized_keys" >/dev/null
    fi
    sudo -H -u "${RUN_USER}" chmod 600 "${RUN_HOME}/.ssh/authorized_keys"

    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl enable --now ssh >/dev/null 2>&1 || sudo systemctl enable --now sshd >/dev/null 2>&1 || true
    fi

    sudo -H -u "${RUN_USER}" touch "${RUN_HOME}/.ssh/known_hosts"
    for known_host in "${HOST_IP}" localhost 127.0.0.1; do
        sudo -H -u "${RUN_USER}" ssh-keyscan -H -T 3 "${known_host}" >> "${RUN_HOME}/.ssh/known_hosts" 2>/dev/null || true
    done
    sudo -H -u "${RUN_USER}" sh -c "sort -u '${RUN_HOME}/.ssh/known_hosts' -o '${RUN_HOME}/.ssh/known_hosts'"
    sudo -H -u "${RUN_USER}" chmod 600 "${RUN_HOME}/.ssh/known_hosts"
}

write_runtime_config() {
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" - "${INSTALL_DIR}/configs/config_dynamic.json" "${RUN_USER}" "${HOST_IP}" "${RUN_HOME}" "${SSH_KEY_PATH}" "${PORT}" <<'PY'
import json
import os
import sys

path, user, host_ip, home, key_path, port = sys.argv[1:7]
os.makedirs(os.path.dirname(path), exist_ok=True)

try:
    with open(path, 'r', encoding='utf-8') as f:
        config = json.load(f)
except Exception:
    config = {}

gms_suite = os.path.join(home, 'GMS-Suite')
config.update({
    'ubuntu_user': user,
    'ubuntu_host': host_ip,
    'local_server': f'{user}@{host_ip}',
    'use_key_auth': True,
    'private_key_path': key_path,
    'ubuntu_pswd': '',
    'suites_path': gms_suite,
    'script_path': os.path.join(gms_suite, 'run_GMS_Test_Auto.sh'),
    'gsi_scripts': os.path.join(gms_suite, 'run_GSI_Burn.sh'),
    'scrcpy_path': os.path.join(home, 'Software', 'scrcpy-linux-x86_64-v3.3.4', 'scrcpy'),
    'install_host_ip': host_ip,
    'install_port': int(port),
})

client_hosts = config.setdefault('client_hosts', {})
if isinstance(client_hosts, dict):
    client_hosts.setdefault(host_ip, user)

with open(path, 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=4)
    f.write('\n')
PY
}

setup_suite_dir() {
    local suite_dir="${RUN_HOME}/GMS-Suite"
    sudo -H -u "${RUN_USER}" mkdir -p "${suite_dir}" "${suite_dir}/tmp"
    for file in run_GMS_Test_Auto.sh run_GSI_Burn.sh run_Device_Lock.sh; do
        if [[ -f "${INSTALL_DIR}/scripts/${file}" ]]; then
            sudo install -o "${RUN_USER}" -g "${RUN_GROUP}" -m 0755 "${INSTALL_DIR}/scripts/${file}" "${suite_dir}/${file}"
        fi
    done
    for file in upgrade_tool misc.img; do
        if [[ -f "${INSTALL_DIR}/tools/${file}" ]]; then
            sudo install -o "${RUN_USER}" -g "${RUN_GROUP}" -m 0755 "${INSTALL_DIR}/tools/${file}" "${suite_dir}/${file}"
        fi
    done
}

configure_sudoers() {
    local tmp
    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
# Allow ${RUN_USER} to run the runtime commands used by GMS Web App without storing a sudo password.
Cmnd_Alias GMS_WEB_APP_CMDS = /usr/sbin/usbip *, /usr/bin/usbip *, /sbin/modprobe *, /usr/sbin/modprobe *, /usr/bin/udevadm *, /sbin/udevadm *, /sbin/ip *, /usr/sbin/ip *, /usr/bin/ip *, /usr/bin/nmcli *
${RUN_USER} ALL=(root) NOPASSWD: GMS_WEB_APP_CMDS
EOF
    sudo visudo -cf "${tmp}" >/dev/null
    sudo install -o root -g root -m 0440 "${tmp}" "${SUDOERS_FILE}"
    rm -f "${tmp}"
}

install_systemd_service() {
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "未检测到 systemd，跳过服务安装"
        return
    fi

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"
    local tmp
    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
[Unit]
Description=GMS Remote Test Web App
After=network-online.target ssh.service sshd.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=UBUNTU_USER=${RUN_USER}
Environment=UBUNTU_HOST=${HOST_IP}
Environment=GMS_LOCAL_SERVER=${RUN_USER}@${HOST_IP}
Environment=GMS_PRIVATE_KEY_PATH=${SSH_KEY_PATH}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m uvicorn app_fastapi_full:app --host 0.0.0.0 --port ${PORT} --log-level info --access-log
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    sudo install -o root -g root -m 0644 "${tmp}" "${service_file}"
    rm -f "${tmp}"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "${SERVICE_NAME}"
}

install_web_app() {
    HOST_IP="${HOST_IP:-$(detect_host_ip)}"

    info "========================================"
    info "  GMS Web App 一键安装"
    info "========================================"
    echo "安装目录: ${INSTALL_DIR}"
    echo "运行用户: ${RUN_USER}"
    echo "本机 IP:  ${HOST_IP}"
    echo "端口:     ${PORT}"
    echo ""

    sudo -v
    install_system_packages
    copy_project
    setup_python_env
    setup_local_ssh_key
    write_runtime_config
    setup_suite_dir
    configure_sudoers
    install_systemd_service

    ok "安装完成"
    echo "访问地址: http://${HOST_IP}:${PORT}"
    echo "本机访问: http://localhost:${PORT}"
    echo "查看日志: sudo journalctl -u ${SERVICE_NAME} -f"
}

main() {
    parse_args "$@"
    case "${ACTION}" in
        install) install_web_app ;;
        package) package_web_app ;;
        *) fail "未知动作: ${ACTION}" ;;
    esac
}

main "$@"
