#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [[ -n "${SCRIPT_SOURCE}" && -f "${SCRIPT_SOURCE}" ]]; then
    PROJECT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
else
    PROJECT_DIR="$(pwd)"
fi
ACTION="install"

INSTALL_DIR="${GMS_INSTALL_DIR:-/opt/gms-remote-test/web_app}"
SERVICE_NAME="${GMS_SERVICE_NAME:-gms-web-app}"
PORT="${GMS_PORT:-5001}"
RUN_USER="${GMS_RUN_USER:-${SUDO_USER:-$(id -un)}}"
HOST_IP="${GMS_HOST_IP:-}"
CERT_DIR="${GMS_CERT_DIR:-${INSTALL_DIR}/configs/certs}"
CERT_KEY="${GMS_CERT_KEY:-${CERT_DIR}/gms-local.key}"
CERT_CRT="${GMS_CERT_CRT:-${CERT_DIR}/gms-local.crt}"

DIST_DIR="${GMS_DIST_DIR:-${PROJECT_DIR}/dist}"
PACKAGE_NAME="${GMS_PACKAGE_NAME:-gms-web-app}"
PACKAGE_VERSION="${GMS_PACKAGE_VERSION:-$(date +%Y%m%d_%H%M%S)}"
PACKAGE_SIGNING_KEY="${GMS_RELEASE_SIGNING_KEY:-}"

RUN_GROUP=""
RUN_HOME=""
SSH_KEY_PATH=""
SUDOERS_FILE=""
BACKUP_DIR=""
BACKUP_KEY_FILE=""

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
    BACKUP_DIR="${GMS_BACKUP_DIR:-/var/backups/${SERVICE_NAME}}"
    BACKUP_KEY_FILE="${GMS_BACKUP_KEY_FILE:-/etc/${SERVICE_NAME}/backup.key}"
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

verify_install_source() {
    if [[ -n "${PROJECT_DIR}" && -f "${PROJECT_DIR}/app.py" && -f "${PROJECT_DIR}/requirements.txt" ]]; then
        return
    fi
    fail "未找到完整的签名发布包目录；运行服务不再提供 curl | bash 在线打包"
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

ensure_sudo() {
    if sudo -n true 2>/dev/null; then
        return
    fi
    if [[ -r /dev/tty ]]; then
        sudo -v </dev/tty
    else
        sudo -v
    fi
}

package_web_app() {
    local archive archive_name root_name
    root_name="${PACKAGE_NAME}"
    [[ "${root_name}" =~ ^[A-Za-z0-9._-]+$ ]] || fail "安装包名称只能包含字母、数字、点、下划线和连字符"
    command -v rsync >/dev/null 2>&1 || fail "打包需要 rsync"
    command -v python3 >/dev/null 2>&1 || fail "打包需要 python3"
    mkdir -p "${DIST_DIR}"
    archive="$(cd "${DIST_DIR}" && pwd)/${PACKAGE_NAME}-${PACKAGE_VERSION}.tar.gz"
    archive_name="$(basename "${archive}")"

    (
        local stage package_root
        stage="$(mktemp -d)"
        trap 'rm -rf "${stage}"' EXIT
        package_root="${stage}/${root_name}"
        mkdir -p "${package_root}"
        rsync -a \
            --exclude '.git/' \
            --exclude '.agents/' \
            --exclude '.codex/' \
            --exclude '.certs/' \
            --exclude '.env.production' \
            --exclude 'configs/env.production' \
            --exclude 'configs/certs/' \
            --exclude 'configs/runtime.json' \
            --exclude 'configs/worker_tokens.json' \
            --exclude 'configs/user_tools_data.json' \
            --exclude 'configs/redmine_user_map.json' \
            --exclude '.venv/' \
            --exclude '.pytest_cache/' \
            --exclude '.ruff_cache/' \
            --exclude 'AGENTS.md' \
            --exclude '__pycache__/' \
            --exclude '*/__pycache__/' \
            --exclude '*.pyc' \
            --exclude '*.pyo' \
            --exclude 'data/' \
            --exclude '/dist/' \
            --exclude '/tools/gms-worker-native/target/' \
            --exclude '/tools/adbproxy-rs/target/' \
            --exclude 'logs/' \
            --exclude '*.log' \
            --exclude '*.log.backup.*' \
            --exclude '*.pid' \
            --exclude 'local.diff' \
            --exclude 'scripts_local/' \
            --exclude 'tests/' \
            --exclude '*/tests/' \
            --exclude 'docs/superpowers/' \
            --exclude 'docs/android-cli-ui-control-integration.md' \
            --exclude 'docs/build-server-integration-assessment.md' \
            --exclude 'docs/multi-host-cluster-implementation-plan.md' \
            --exclude 'docs/refactor-parity-audit.md' \
            --exclude 'configs/config_runtime.json' \
            --exclude 'configs/client_ssh_credentials.local.json' \
            --exclude 'configs/redmine_auth.json' \
            --exclude 'tools/GMS-Host-Tools/gts-rockchip.json' \
            "${PROJECT_DIR}/" "${package_root}/"
        mkdir -p "${package_root}/data"
        python3 "${PROJECT_DIR}/scripts/sanitize_release_config.py" \
            "${package_root}/configs/config.json" \
            "${package_root}/configs/automation_profiles.json" \
            "${package_root}/configs/build_servers.json" \
            "${package_root}/configs/cluster.json" \
            "${package_root}/skills/rk_codesearch/config/config.json"
        python3 "${PROJECT_DIR}/scripts/verify_release_tree.py" "${package_root}"
        tar -czf "${archive}" -C "${stage}" "${root_name}"
    )

    [[ -n "${PACKAGE_SIGNING_KEY}" ]] \
        || fail "发布打包必须设置 GMS_RELEASE_SIGNING_KEY（GPG signing key ID）"
    command -v sha256sum >/dev/null 2>&1 || fail "发布打包需要 sha256sum"
    command -v gpg >/dev/null 2>&1 || fail "发布打包需要 gpg"
    (
        cd "$(dirname "${archive}")"
        sha256sum "${archive_name}" > "${archive_name}.sha256"
        gpg --batch --yes --armor --detach-sign \
            --local-user "${PACKAGE_SIGNING_KEY}" \
            --output "${archive_name}.sig" "${archive_name}"
    )

    cat <<EOF
安装包已生成:
  ${archive}
  ${archive}.sha256
  ${archive}.sig

目标电脑部署:
  sha256sum -c ${archive_name}.sha256
  gpg --verify ${archive_name}.sig ${archive_name}
  tar -xzf ${archive_name}
  cd ${root_name}
  ./install.sh
EOF
}

install_system_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update || fail "apt-get update 失败。请先检查目标机器 DNS/网络/apt 源，例如: resolvectl status、ping cn.archive.ubuntu.com"
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3 python3-venv python3-pip rsync curl lsof psmisc openssl \
            openssh-client openssh-server sudo iproute2 x11vnc novnc websockify \
            libudev1 \
            || fail "系统依赖安装失败。当前日志显示多为 DNS 解析失败，请先修复目标机器外网 DNS 或 apt 源后重试"
        for optional_pkg in usbip adb fastboot android-tools-adb android-tools-fastboot default-jre; do
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${optional_pkg}" >/dev/null 2>&1 || true
        done
        # 生产安装器不执行远程脚本。Tailscale 必须来自组织批准、
        # 已验证签名的软件仓库或预装镜像。
        if command -v tailscale >/dev/null 2>&1; then
            sudo systemctl enable --now tailscaled >/dev/null 2>&1 || \
                warn "Tailscale 已安装，但 tailscaled 服务启动失败"
        else
            warn "未检测到 Tailscale；如需组网，请从组织批准的软件仓库安装已签名软件包"
        fi
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
            --exclude '.certs/' \
            --exclude '.env.production' \
            --exclude 'configs/env.production' \
            --exclude 'configs/certs/' \
            --exclude 'configs/runtime.json' \
            --exclude 'configs/worker_tokens.json' \
            --exclude 'configs/user_tools_data.json' \
            --exclude 'configs/redmine_user_map.json' \
            --exclude '.venv/' \
            --exclude '__pycache__/' \
            --exclude '*.pyc' \
            --exclude '*.pyo' \
            --exclude '.pytest_cache/' \
            --exclude '.ruff_cache/' \
            --exclude 'AGENTS.md' \
            --exclude 'data/' \
            --exclude 'logs/' \
            --exclude '*.log' \
            --exclude '*.log.backup.*' \
            --exclude 'local.diff' \
            --exclude '/dist/' \
            --exclude '/tools/gms-worker-native/target/' \
            --exclude '/tools/adbproxy-rs/target/' \
            --exclude 'scripts_local/' \
            --exclude 'tests/' \
            --exclude '*/tests/' \
            --exclude 'docs/superpowers/' \
            --exclude 'docs/android-cli-ui-control-integration.md' \
            --exclude 'docs/build-server-integration-assessment.md' \
            --exclude 'docs/multi-host-cluster-implementation-plan.md' \
            --exclude 'docs/refactor-parity-audit.md' \
            --exclude 'skills/rk_codesearch/config/config.json' \
            --exclude 'configs/config_runtime.json' \
            --exclude 'configs/client_ssh_credentials.local.json' \
            --exclude 'configs/redmine_auth.json' \
            --exclude 'tools/GMS-Host-Tools/gts-rockchip.json' \
            "${PROJECT_DIR}/" "${INSTALL_DIR}/"
    fi
    sudo chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}"
}

setup_python_env() {
    sudo -H -u "${RUN_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" -m pip install -r "${INSTALL_DIR}/requirements.txt"
    sudo -H -u "${RUN_USER}" bash -c "cd '${INSTALL_DIR}' && '${INSTALL_DIR}/.venv/bin/python' - <<'PY'
import importlib

for module in ('jinja2', 'uvicorn', 'fastapi', 'app'):
    importlib.import_module(module)
PY"
}

setup_runtime_secrets() {
    local secret_root secret_key worker_token env_file worker_config worker_tokens_file
    secret_root="${INSTALL_DIR}/data/secrets"
    secret_key="${secret_root}/master.key"
    worker_token="${secret_root}/local-worker.token"
    env_file="${INSTALL_DIR}/configs/runtime.json"
    worker_config="${INSTALL_DIR}/data/local-worker/config.json"
    worker_tokens_file="${INSTALL_DIR}/configs/worker_tokens.json"

    sudo -H -u "${RUN_USER}" mkdir -p "${secret_root}" "$(dirname "${worker_config}")"
    sudo -H -u "${RUN_USER}" chmod 700 "${secret_root}"
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" - \
        "${secret_key}" "${worker_token}" "${env_file}" "${worker_config}" \
        "${INSTALL_DIR}/configs/cluster.json" "${worker_tokens_file}" \
        "${CERT_CRT}" "${RUN_HOME}" \
        "${RUN_USER}" "${HOST_IP}" "${PORT}" <<'PY'
import json
import os
import secrets
import shlex
import sys
from pathlib import Path

from cryptography.fernet import Fernet

(
    key_path_raw,
    token_path_raw,
    env_path_raw,
    worker_config_raw,
    cluster_config_raw,
    worker_tokens_raw,
    certificate_raw,
    run_home,
    run_user,
    host_ip,
    port,
) = sys.argv[1:]
key_path = Path(key_path_raw)
token_path = Path(token_path_raw)
env_path = Path(env_path_raw)
worker_config = Path(worker_config_raw)
cluster_config_path = Path(cluster_config_raw)
worker_tokens_path = Path(worker_tokens_raw)

def create_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, (value.rstrip("\n") + "\n").encode())
        finally:
            os.close(descriptor)
    os.chmod(path, 0o600)

create_private(key_path, Fernet.generate_key().decode("ascii"))
create_private(token_path, secrets.token_urlsafe(48))
audit_key_path = key_path.parent / "audit_hmac.key"
webhook_token_path = key_path.parent / "automation-webhook.token"
metrics_token_path = key_path.parent / "metrics.token"
create_private(audit_key_path, secrets.token_hex(32))
create_private(webhook_token_path, secrets.token_urlsafe(48))
create_private(metrics_token_path, secrets.token_urlsafe(48))
token = token_path.read_text(encoding="utf-8").strip()

try:
    cluster_config = json.loads(cluster_config_path.read_text(encoding="utf-8"))
except Exception:
    cluster_config = {}
worker_id = str(cluster_config.get("local_worker_id") or "ats-worker-controller")
default_max_jobs = max(1, min(32, int(cluster_config.get("default_max_jobs", 6))))

values = {}
if env_path.exists():
    try:
        loaded = json.loads(env_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            values = {str(k): str(v) for k, v in loaded.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass

token_raw = {}
if worker_tokens_path.exists():
    try:
        token_raw = json.loads(worker_tokens_path.read_text(encoding="utf-8"))
        if not isinstance(token_raw, dict):
            token_raw = {}
    except (OSError, json.JSONDecodeError):
        token_raw = {}
existing_tokens = token_raw.get("worker_tokens")
tokens = {}
if isinstance(existing_tokens, dict):
    tokens.update({str(k): str(v) for k, v in existing_tokens.items() if v})
tokens[worker_id] = token
worker_tokens_path.write_text(
    json.dumps(
        {"worker_tokens": dict(sorted(tokens.items()))},
        indent=2,
        ensure_ascii=False,
    ) + "\n",
    encoding="utf-8",
)
os.chmod(worker_tokens_path, 0o600)
values.update({
    "GMS_SECRET_KEY_FILE": str(key_path),
    "GMS_AUDIT_HMAC_KEY_FILE": str(audit_key_path),
    "GMS_WORKER_TOKENS_FILE": str(worker_tokens_path),
    "GMS_AUTOMATION_WEBHOOK_TOKEN": webhook_token_path.read_text(encoding="utf-8").strip(),
    "GMS_AUTOMATION_OWNER_ID": "service-automation",
    "GMS_METRICS_TOKEN": metrics_token_path.read_text(encoding="utf-8").strip(),
    "GMS_AUTH_REQUIRED": "true",
    "GMS_SECURE_COOKIES": "true",
    "GMS_ALLOWED_ORIGINS": f"https://{host_ip}:{port},https://localhost:{port}",
    "TRUSTED_HOSTS": f"{host_ip},localhost,127.0.0.1",
    "GMS_SSH_KNOWN_HOSTS": str(Path(run_home) / ".ssh/known_hosts"),
})
env_path.write_text(
    json.dumps(values, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
os.chmod(env_path, 0o600)

worker_payload = {
    "worker_id": worker_id,
    "name": f"{worker_id} (local)",
    "controller_url": f"https://127.0.0.1:{port}",
    "address": "127.0.0.1",
    "ssh_user": run_user,
    "controller_ca": certificate_raw,
    "worker_token_file": str(token_path),
    "heartbeat_interval_seconds": 15,
    "suite_scan_interval_seconds": 300,
    "max_jobs": default_max_jobs,
    "suite_roots": [str(Path(run_home) / "GMS-Suite"), "/opt/GMS-Suite"],
    "data_root": str(worker_config.parent / "runtime"),
}
worker_config.parent.mkdir(parents=True, exist_ok=True)
worker_config.write_text(
    json.dumps(worker_payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(worker_config, 0o600)
PY
}

setup_https_cert() {
    sudo -H -u "${RUN_USER}" mkdir -p "${CERT_DIR}"

    if [[ -s "${CERT_KEY}" && -s "${CERT_CRT}" ]]; then
        sudo chown "${RUN_USER}:${RUN_GROUP}" "${CERT_KEY}" "${CERT_CRT}"
        sudo chmod 600 "${CERT_KEY}"
        sudo chmod 644 "${CERT_CRT}"
        return
    fi

    command -v openssl >/dev/null 2>&1 || fail "未检测到 openssl，无法生成 HTTPS 证书"

    local san_list="DNS:localhost,DNS:127.0.0.1,IP:127.0.0.1,IP:${HOST_IP}"
    if command -v hostname >/dev/null 2>&1; then
        local host_ips
        host_ips="$(hostname -I 2>/dev/null || true)"
        for ip in ${host_ips}; do
            [[ -n "${ip}" ]] && san_list="${san_list},IP:${ip}"
        done
    fi

    local tmp_conf
    tmp_conf="$(mktemp)"
    cat > "${tmp_conf}" <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = GMS Remote Test Local

[v3_req]
subjectAltName = ${san_list}
keyUsage = keyEncipherment, dataEncipherment, digitalSignature
extendedKeyUsage = serverAuth
EOF

    sudo -H -u "${RUN_USER}" openssl req -x509 -nodes -newkey rsa:2048 \
        -days 825 \
        -keyout "${CERT_KEY}" \
        -out "${CERT_CRT}" \
        -config "${tmp_conf}" >/dev/null 2>&1
    rm -f "${tmp_conf}"
    sudo chmod 600 "${CERT_KEY}"
    sudo chmod 644 "${CERT_CRT}"
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
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" - "${INSTALL_DIR}/configs/config_runtime.json" "${INSTALL_DIR}/configs/config.json" "${INSTALL_DIR}/configs/cluster.json" "${RUN_USER}" "${HOST_IP}" "${RUN_HOME}" "${SSH_KEY_PATH}" "${PORT}" <<'PY'
import json
import os
import sys

runtime_path, static_path, cluster_path, user, host_ip, home, key_path, port = sys.argv[1:9]
os.makedirs(os.path.dirname(runtime_path), exist_ok=True)

try:
    with open(runtime_path, 'r', encoding='utf-8') as f:
        runtime_config = json.load(f)
except Exception:
    runtime_config = {}

gms_suite = os.path.join(home, 'GMS-Suite')
deployment_config = {
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
}

runtime_config.update(deployment_config)

client_hosts = runtime_config.setdefault('client_hosts', {})
if isinstance(client_hosts, dict):
    client_hosts.setdefault(host_ip, user)

with open(runtime_path, 'w', encoding='utf-8') as f:
    json.dump(runtime_config, f, ensure_ascii=False, indent=4)
    f.write('\n')

# The packaged config.json may contain the source machine identity. Keep the
# installed static defaults aligned with the deployment host so templates and
# fallback paths never expose the package builder's user/host.
try:
    with open(static_path, 'r', encoding='utf-8') as f:
        static_config = json.load(f)
except Exception:
    static_config = {}

static_config.update(deployment_config)

with open(static_path, 'w', encoding='utf-8') as f:
    json.dump(static_config, f, ensure_ascii=False, indent=4)
    f.write('\n')

try:
    with open(cluster_path, 'r', encoding='utf-8') as f:
        cluster_config = json.load(f)
except Exception:
    cluster_config = {}
cluster_config['controller_url'] = f'https://{host_ip}:{port}'
with open(cluster_path, 'w', encoding='utf-8') as f:
    json.dump(cluster_config, f, ensure_ascii=False, indent=4)
    f.write('\n')
PY
}

verify_runtime_config() {
    sudo -H -u "${RUN_USER}" "${INSTALL_DIR}/.venv/bin/python" - "${INSTALL_DIR}/configs/config_runtime.json" "${RUN_USER}" "${HOST_IP}" <<'PY'
import json
import sys

path, expected_user, expected_host = sys.argv[1:4]
with open(path, 'r', encoding='utf-8') as f:
    config = json.load(f)

actual_user = config.get('ubuntu_user')
actual_host = config.get('ubuntu_host')
if actual_user != expected_user or actual_host != expected_host:
    raise SystemExit(
        f"runtime config mismatch: expected {expected_user}@{expected_host}, "
        f"got {actual_user}@{actual_host}"
    )
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
# USB/IP commands are executed only over the authenticated host SSH boundary.
# Network routes and Tailscale lifecycle remain deployment-time administration.
# VPN activation uses the separate NetworkManager Polkit rule below, not sudo.
Cmnd_Alias GMS_WEB_APP_CMDS = /usr/sbin/usbip *, /usr/bin/usbip *, /sbin/modprobe *, /usr/sbin/modprobe *, /usr/bin/udevadm *, /sbin/udevadm *, /usr/bin/systemctl start ${SERVICE_NAME}-local-software.service, /bin/systemctl start ${SERVICE_NAME}-local-software.service
${RUN_USER} ALL=(root) NOPASSWD: GMS_WEB_APP_CMDS
EOF
    sudo visudo -cf "${tmp}" >/dev/null
    sudo install -o root -g root -m 0440 "${tmp}" "${SUDOERS_FILE}"
    rm -f "${tmp}"
}

configure_networkmanager_policy() {
    if ! command -v nmcli >/dev/null 2>&1; then
        warn "未检测到 NetworkManager，跳过 VPN 后台控制授权"
        return
    fi
    sudo bash "${INSTALL_DIR}/scripts/install_networkmanager_policy.sh" \
        "${RUN_USER}" "${SERVICE_NAME}"
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
Environment=GMS_ENV=production
Environment=GMS_SERVICE_NAME=${SERVICE_NAME}
Environment=GMS_DATA_ROOT=${INSTALL_DIR}/data
Environment=PYTHONPYCACHEPREFIX=${INSTALL_DIR}/data/pycache
Environment=GMS_SOFTWARE_ROOT=${RUN_HOME}/Software
Environment=APE_API_KEY=${RUN_HOME}/Software/gts-rockchip.json
Environment=UBUNTU_USER=${RUN_USER}
Environment=UBUNTU_HOST=${HOST_IP}
Environment=GMS_LOCAL_SERVER=${RUN_USER}@${HOST_IP}
Environment=GMS_PRIVATE_KEY_PATH=${SSH_KEY_PATH}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --backlog 2048 --limit-concurrency 512 --limit-max-requests 100000 --timeout-keep-alive 10 --log-level info --access-log --ssl-keyfile ${CERT_KEY} --ssl-certfile ${CERT_CRT}
Restart=always
RestartSec=3
UMask=0077
LimitNOFILE=65536
TasksMax=4096
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ReadWritePaths=${INSTALL_DIR}/data ${INSTALL_DIR}/configs

[Install]
WantedBy=multi-user.target
EOF
    sudo install -o root -g root -m 0644 "${tmp}" "${service_file}"
    rm -f "${tmp}"
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}" >/dev/null
    sudo systemctl restart "${SERVICE_NAME}"
}

install_local_worker_service() {
    command -v systemctl >/dev/null 2>&1 || return
    local worker_service="${SERVICE_NAME}-local-worker.service"
    local service_file="/etc/systemd/system/${worker_service}"
    local tmp
    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
[Unit]
Description=GMS Remote Test Local Worker Agent
After=network-online.target ${SERVICE_NAME}.service
Wants=network-online.target
Requires=${SERVICE_NAME}.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_DIR}
Environment=GMS_ENV=production
Environment=GMS_WORKER_CONFIG=${INSTALL_DIR}/data/local-worker/config.json
Environment=GMS_ADB_PROXY_BIN_DIR=${RUN_HOME}/.local/bin
Environment=GMS_ADB_PROXY_STATE_ROOT=${RUN_HOME}/.local/state/gms-adbproxy
Environment=PYTHONPYCACHEPREFIX=${INSTALL_DIR}/data/pycache-worker
ExecStart=${INSTALL_DIR}/.venv/bin/python -m worker_agent.app
Restart=on-failure
RestartSec=5
KillMode=process
UMask=0077
LimitNOFILE=65536
TasksMax=4096
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ReadWritePaths=${INSTALL_DIR}/data ${RUN_HOME}/GMS-Suite ${RUN_HOME}/.local/state/gms-adbproxy

[Install]
WantedBy=multi-user.target
EOF
    sudo install -o root -g root -m 0644 "${tmp}" "${service_file}"
    rm -f "${tmp}"
    sudo systemctl daemon-reload
    sudo systemctl enable "${worker_service}" >/dev/null
    sudo systemctl restart "${worker_service}"
}

install_local_software_service() {
    command -v systemctl >/dev/null 2>&1 || return
    local software_service="${SERVICE_NAME}-local-software.service"
    local service_file="/etc/systemd/system/${software_service}"
    local tmp
    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
[Unit]
Description=Reconfigure GMS Local Worker Software
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_DIR}
Environment=HOME=${RUN_HOME}
ExecStart=${INSTALL_DIR}/scripts/configure_local_worker_software.sh ${INSTALL_DIR} ${RUN_HOME}
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
EOF
    sudo install -o root -g root -m 0644 "${tmp}" "${service_file}"
    rm -f "${tmp}"
    sudo chmod 0755 "${INSTALL_DIR}/scripts/configure_local_worker_software.sh"
    sudo systemctl daemon-reload
}

install_backup_service() {
    command -v systemctl >/dev/null 2>&1 || return
    local backup_service="${SERVICE_NAME}-backup.service"
    local backup_timer="${SERVICE_NAME}-backup.timer"
    local worker_service="${SERVICE_NAME}-local-worker.service"
    local service_file="/etc/systemd/system/${backup_service}"
    local timer_file="/etc/systemd/system/${backup_timer}"
    local key_dir tmp

    key_dir="$(dirname "${BACKUP_KEY_FILE}")"
    sudo install -d -o root -g root -m 0700 "${key_dir}" "${BACKUP_DIR}"
    if [[ ! -s "${BACKUP_KEY_FILE}" ]]; then
        tmp="$(mktemp)"
        python3 - "${tmp}" <<'PY'
import base64
import os
import sys

path = sys.argv[1]
descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
try:
    os.write(descriptor, base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
finally:
    os.close(descriptor)
PY
        sudo install -o root -g root -m 0600 "${tmp}" "${BACKUP_KEY_FILE}"
        rm -f "${tmp}"
    fi
    sudo chmod 0600 "${BACKUP_KEY_FILE}"
    sudo chmod 0755 "${INSTALL_DIR}/scripts/gms_backup.py" \
        "${INSTALL_DIR}/scripts/run_production_backup.sh"

    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
[Unit]
Description=Encrypted application-consistent backup for GMS Remote Test
After=local-fs.target

[Service]
Type=oneshot
User=root
Group=root
Environment=GMS_PROJECT_ROOT=${INSTALL_DIR}
Environment=GMS_RUN_HOME=${RUN_HOME}
Environment=GMS_BACKUP_DIR=${BACKUP_DIR}
Environment=GMS_BACKUP_KEY_FILE=${BACKUP_KEY_FILE}
Environment=GMS_BACKUP_KEEP=14
Environment=GMS_CONTROLLER_SERVICE=${SERVICE_NAME}.service
Environment=GMS_LOCAL_WORKER_SERVICE=${worker_service}
ExecStart=${INSTALL_DIR}/scripts/run_production_backup.sh
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ReadWritePaths=${BACKUP_DIR}
ReadOnlyPaths=${INSTALL_DIR} ${RUN_HOME} ${BACKUP_KEY_FILE}

[Install]
WantedBy=multi-user.target
EOF
    sudo install -o root -g root -m 0644 "${tmp}" "${service_file}"
    rm -f "${tmp}"

    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
[Unit]
Description=Daily encrypted backup timer for GMS Remote Test

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=45m
Persistent=true
Unit=${backup_service}

[Install]
WantedBy=timers.target
EOF
    sudo install -o root -g root -m 0644 "${tmp}" "${timer_file}"
    rm -f "${tmp}"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "${backup_timer}" >/dev/null
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

    ensure_sudo
    install_system_packages
    copy_project
    setup_python_env
    setup_https_cert
    setup_runtime_secrets
    setup_local_ssh_key
    write_runtime_config
    verify_runtime_config
    setup_suite_dir
    configure_sudoers
    configure_networkmanager_policy
    install_systemd_service
    install_local_worker_service
    install_local_software_service
    install_backup_service

    ok "安装完成"
    echo "访问地址: https://${HOST_IP}:${PORT}"
    echo "本机访问: https://localhost:${PORT}"
    echo "查看日志: sudo journalctl -u ${SERVICE_NAME} -f"
    echo "立即备份: sudo systemctl start ${SERVICE_NAME}-backup.service"
    echo "备份目录: ${BACKUP_DIR}（密钥为 root-only，请另行托管副本）"
}

main() {
    parse_args "$@"
    verify_install_source
    case "${ACTION}" in
        install) install_web_app ;;
        package) package_web_app ;;
        *) fail "未知动作: ${ACTION}" ;;
    esac
}

main "$@"
