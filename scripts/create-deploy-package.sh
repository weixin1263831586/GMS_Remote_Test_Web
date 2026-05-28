#!/usr/bin/env bash
# ==============================================================================
# GMS Remote Test Web App - 部署安装包生成脚本
# ==============================================================================
# 用法:
#   ./scripts/create-deploy-package.sh [输出目录] [版本]
#
# 示例:
#   ./scripts/create-deploy-package.sh ./dist 20260527
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}$*${NC}"; }
ok() { echo -e "${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}$*${NC}"; }
fail() { echo -e "${RED}错误:${NC} $*" >&2; exit 1; }

# 获取项目根目录
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${1:-${PROJECT_DIR}/dist}"
VERSION="${2:-$(date +%Y%m%d_%H%M%S)}"
PACKAGE_NAME="gms-web-app"

# 创建输出目录
mkdir -p "${DIST_DIR}"

info "========================================"
info "  GMS Web App 部署包生成工具"
info "========================================"
echo "项目目录：${PROJECT_DIR}"
echo "输出目录：${DIST_DIR}"
echo "版本号：  ${VERSION}"
echo ""

ARCHIVE="${DIST_DIR}/${PACKAGE_NAME}-${VERSION}.tar.gz"
INSTALL_SCRIPT="${DIST_DIR}/install.sh"

# 创建安装包（排除不必要的文件）
info "正在创建安装包..."
cd "${PROJECT_DIR}"

tar -czf "${ARCHIVE}" \
    --transform "s#^#${PACKAGE_NAME}/#" \
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
    --exclude='configs/config.json' \
    --exclude='configs/config_dynamic.json' \
    --exclude='configs/client_ssh_credentials.local.json' \
    --exclude='configs/redmine_auth.json' \
    --exclude='data/*.json' \
    --exclude='*.diff' \
    --exclude='.venv' \
    --exclude='fastapi.pid' \
    --exclude='fastapi.log*' \
    .

ok "安装包已生成：${ARCHIVE}"

# 创建一键安装脚本
info "正在生成安装脚本..."

cat > "${INSTALL_SCRIPT}" << 'INSTALLER_EOF'
#!/usr/bin/env bash
# ==============================================================================
# GMS Remote Test Web App - 一键安装脚本
# ==============================================================================
# 用法: ./install.sh [选项]
#
# 选项:
#   --install-dir <path>   安装目录，默认：/opt/gms-remote-test/web_app
#   --port <port>          服务端口，默认：5001
#   --user <user>          运行用户，默认：当前用户
#   --host-ip <ip>         本机 IP，默认：自动检测
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}$*${NC}"; }
ok() { echo -e "${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}$*${NC}"; }
fail() { echo -e "${RED}错误:${NC} $*" >&2; exit 1; }

# 默认配置
INSTALL_DIR="${GMS_INSTALL_DIR:-/opt/gms-remote-test/web_app}"
PORT="${GMS_PORT:-5001}"
SERVICE_NAME="gms-web-app"
RUN_USER="${GMS_RUN_USER:-${SUDO_USER:-$(id -un)}}"
HOST_IP="${GMS_HOST_IP:-}"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --user) RUN_USER="$2"; shift 2 ;;
        --host-ip) HOST_IP="$2"; shift 2 ;;
        -h|--help)
            echo "用法: ./install.sh [选项]"
            echo "选项:"
            echo "  --install-dir <path>  安装目录"
            echo "  --port <port>         服务端口"
            echo "  --user <user>         运行用户"
            echo "  --host-ip <ip>        本机 IP"
            exit 0
            ;;
        *) fail "未知参数：$1" ;;
    esac
done

# 获取用户信息
RUN_GROUP="$(id -gn "${RUN_USER}" 2>/dev/null || id -gn)"
RUN_HOME="$(getent passwd "${RUN_USER}" 2>/dev/null | cut -d: -f6 || echo "/home/${RUN_USER}")"
SSH_KEY_PATH="${RUN_HOME}/.ssh/gms_web_app_rsa"

# 自动检测本机 IP
detect_host_ip() {
    local ip
    if command -v ip >/dev/null 2>&1; then
        ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
        [[ -n "${ip}" ]] && { echo "${ip}"; return; }
    fi
    if command -v hostname >/dev/null 2>&1; then
        ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '$1 !~ /^127\./ && $1 !~ /^169\.254\./ {print; exit}')"
        [[ -n "${ip}" ]] && { echo "${ip}"; return; }
    fi
    echo "127.0.0.1"
}

HOST_IP="${HOST_IP:-$(detect_host_ip)}"

info "========================================"
info "  GMS Web App 一键安装"
info "========================================"
echo "安装目录：${INSTALL_DIR}"
echo "运行用户：${RUN_USER}"
echo "本机 IP:  ${HOST_IP}"
echo "端口：    ${PORT}"
echo ""

# 检查是否以 root 运行
if [[ $EUID -ne 0 ]]; then
    warn "请使用 sudo 运行此安装脚本"
    exec sudo -E bash "$0" "$@"
fi

# 1. 安装系统依赖
info "[1/8] 安装系统依赖..."
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    # 必需依赖
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-venv python3-pip python3-dev \
        rsync curl lsof psmisc \
        openssh-client openssh-server sudo iproute2 net-tools \
        build-essential libssl-dev libffi-dev \
        2>/dev/null || true
    # 可选依赖（USB/IP、ADB 等）
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        usbip adb fastboot android-tools-adb android-tools-fastboot \
        2>/dev/null || true
    ok "系统依赖安装完成"
else
    warn "未检测到 apt-get，请确保 python3/python3-venv/rsync/curl/ssh 已安装"
fi

# 2. 创建安装目录
info "[2/8] 创建安装目录..."
mkdir -p "${INSTALL_DIR}"
chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}"
ok "目录创建完成"

# 3. 解压安装包
info "[3/8] 解压安装包..."

# 获取脚本所在目录（兼容 sudo 执行的情况）
if [[ -n "${BASH_SOURCE[0]}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
elif [[ -n "${BASH_SOURCE[0]}" && -L "${BASH_SOURCE[0]}" ]]; then
    # 处理软链接情况
    SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
else
    SCRIPT_DIR="$(pwd)"
fi

info "  脚本目录：${SCRIPT_DIR}"
info "  当前目录：$(pwd)"

ARCHIVE_FOUND=""

# 在多个位置查找安装包
for search_dir in "${SCRIPT_DIR}" "$(pwd)" "${HOME}" "/tmp" "${SCRIPT_DIR}/.."; do
    if [[ -d "${search_dir}" ]]; then
        archive_check="$(ls "${search_dir}"/gms-web-app-*.tar.gz 2>/dev/null | head -1)"
        if [[ -n "${archive_check}" ]]; then
            ARCHIVE_FOUND="${archive_check}"
            break
        fi
    fi
done

if [[ -n "${ARCHIVE_FOUND}" ]]; then
    info "  找到安装包：${ARCHIVE_FOUND}"
    # 从安装包解压
    tar -xzf "${ARCHIVE_FOUND}" -C "${INSTALL_DIR}" --strip-components=1
    ok "文件解压完成"
else
    fail "未找到安装包 (gms-web-app-*.tar.gz)"
fi

# 4. 创建 Python 虚拟环境
info "[4/8] 创建 Python 虚拟环境..."
sudo -H -u "${RUN_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel -q
sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
ok "Python 环境配置完成"

# 5. 配置 SSH 密钥
info "[5/8] 配置 SSH 密钥..."
sudo -H -u "${RUN_USER}" mkdir -p "${RUN_HOME}/.ssh"
sudo -H -u "${RUN_USER}" chmod 700 "${RUN_HOME}/.ssh"
if [[ ! -f "${SSH_KEY_PATH}" ]]; then
    sudo -H -u "${RUN_USER}" ssh-keygen -t rsa -b 4096 -N "" -f "${SSH_KEY_PATH}" -C "gms-web-app@$(hostname)" -q
fi
sudo -H -u "${RUN_USER}" touch "${RUN_HOME}/.ssh/authorized_keys"
pub_key="$(sudo -H -u "${RUN_USER}" cat "${SSH_KEY_PATH}.pub")"
if ! sudo -H -u "${RUN_USER}" grep -qxF "${pub_key}" "${RUN_HOME}/.ssh/authorized_keys"; then
    echo "${pub_key}" | sudo -H -u "${RUN_USER}" tee -a "${RUN_HOME}/.ssh/authorized_keys" >/dev/null
fi
sudo -H -u "${RUN_USER}" chmod 600 "${RUN_HOME}/.ssh/authorized_keys"
ok "SSH 密钥配置完成"

# 6. 写入运行时配置
info "[6/8] 写入运行时配置..."
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
ok "配置文件写入完成"

# 7. 配置 sudoers
info "[7/8] 配置 sudoers..."
SUDOERS_FILE="/etc/sudoers.d/${SERVICE_NAME}-${RUN_USER}"
tmp="$(mktemp)"
cat > "${tmp}" <<EOF
${RUN_USER} ALL=(root) NOPASSWD: /usr/sbin/usbip *, /usr/bin/usbip *, /sbin/modprobe *, /usr/sbin/modprobe *, /usr/bin/udevadm *, /sbin/udevadm *, /sbin/ip *, /usr/sbin/ip *, /usr/bin/ip *, /usr/bin/nmcli *
EOF
if visudo -cf "${tmp}" >/dev/null 2>&1; then
    install -o root -g root -m 0440 "${tmp}" "${SUDOERS_FILE}"
    ok "sudoers 配置完成"
else
    warn "sudoers 配置失败，请手动配置 USB/IP 相关权限"
fi
rm -f "${tmp}"

# 8. 安装 systemd 服务
info "[8/8] 安装 systemd 服务..."
if command -v systemctl >/dev/null 2>&1; then
    service_file="/etc/systemd/system/${SERVICE_NAME}.service"
    cat > "${service_file}" <<EOF
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
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" 2>/dev/null || true
    ok "systemd 服务安装完成"
else
    warn "未检测到 systemd，跳过服务安装"
fi

# 完成
echo ""
ok "========================================"
ok "  安装完成！"
ok "========================================"
echo ""
echo "访问地址:"
echo "  本机：http://localhost:${PORT}"
echo "  远程：http://${HOST_IP}:${PORT}"
echo ""
echo "服务管理:"
echo "  启动：sudo systemctl start ${SERVICE_NAME}"
echo "  停止：sudo systemctl stop ${SERVICE_NAME}"
echo "  重启：sudo systemctl restart ${SERVICE_NAME}"
echo "  日志：sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "首次使用:"
echo "  1. 编辑配置文件：${INSTALL_DIR}/configs/config_dynamic.json"
echo "  2. 启动服务：sudo systemctl start ${SERVICE_NAME}"
echo "  3. 访问 Web 界面：http://localhost:${PORT}"
echo ""

INSTALLER_EOF

chmod +x "${INSTALL_SCRIPT}"
ok "安装脚本已生成：${INSTALL_SCRIPT}"

echo ""
ok "========================================"
ok "  部署包生成完成！"
ok "========================================"
echo ""
echo "部署包位置:"
echo "  ${ARCHIVE}"
echo "  ${INSTALL_SCRIPT}"
echo ""
echo "在其他主机上安装:"
echo "  1. 将 ${ARCHIVE} 和 ${INSTALL_SCRIPT} 复制到目标主机"
echo "  2. 运行：sudo ./install.sh"
echo ""
echo "或者使用环境变量自定义:"
echo "  sudo GMS_PORT=5002 GMS_INSTALL_DIR=/opt/gms ./install.sh"
echo ""
