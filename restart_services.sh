#!/bin/bash
# GMS Auto Test 服务管理脚本

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="${GMS_WEB_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON_BIN="${GMS_PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi
cd "$PROJECT_DIR"

ENV_FILE="${PROJECT_DIR}/configs/runtime.json"
# 加载生产环境变量（含 worker token 等配置），保证手动启动与 systemd 行为一致。
# runtime.json 是 JSON 格式，由 app.py 启动时通过 bootstrap.env_loader 加载到
# 将 JSON 环境配置导出为 Shell 变量供后台进程继承。
if [[ -f "${ENV_FILE}" ]]; then
    while IFS='=' read -r key value; do
        [[ -n "$key" ]] && export "$key=$value"
    done < <("${PYTHON_BIN}" -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for key, value in data.items():
    if isinstance(value, str) and not key.startswith("_"):
        print(f"{key}={value}")
' "$ENV_FILE")
fi

CERT_DIR="${PROJECT_DIR}/configs/certs"
CERT_KEY="${CERT_DIR}/gms-local.key"
CERT_CRT="${CERT_DIR}/gms-local.crt"
PORT="${GMS_PORT:-5001}"
CONFIGURED_SERVER_HOSTNAME="$(
    "${PYTHON_BIN}" -c \
        'import json, sys; print(str(json.load(open(sys.argv[1], encoding="utf-8")).get("ubuntu_host") or ""))' \
        "${PROJECT_DIR}/configs/config.json" 2>/dev/null || true
)"
SERVER_HOSTNAME="${GMS_SERVER_HOSTNAME:-${CONFIGURED_SERVER_HOSTNAME:-127.0.0.1}}"

SYSTEMD_SERVICE="gms-web-app.service"
WORKER_SERVICE="gms-worker-agent"
SYSTEMD_UNIT_FILE="/etc/systemd/system/${SYSTEMD_SERVICE}"
WORKER_UNIT_FILE="${HOME}/.config/systemd/user/${WORKER_SERVICE}.service"

ensure_https_cert() {
    mkdir -p "${CERT_DIR}"

    if [[ -s "${CERT_KEY}" && -s "${CERT_CRT}" ]]; then
        return 0
    fi

    local san_list="DNS:localhost,DNS:127.0.0.1,IP:127.0.0.1,IP:${SERVER_HOSTNAME}"
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

    openssl req -x509 -nodes -newkey rsa:2048 \
        -days 825 \
        -keyout "${CERT_KEY}" \
        -out "${CERT_CRT}" \
        -config "${tmp_conf}" >/dev/null 2>&1
    rm -f "${tmp_conf}"
}

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  GMS Auto Test 服务管理${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 1. 清理缓存
echo -e "${YELLOW}[1/4] 清理 Python 缓存...${NC}"
# 清理 Python 字节码缓存
cache_count=$(find . -type d -name "__pycache__" 2>/dev/null | wc -l)
pyc_count=$(find . -type f -name "*.pyc" 2>/dev/null | wc -l)

find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type f -name "*.pyo" -delete 2>/dev/null || true
find . -type f -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

echo -e "${GREEN}✓ 缓存已清理 (删除了 ${cache_count} 个 __pycache__ 目录和 ${pyc_count} 个 .pyc 文件)${NC}"
echo ""

# 2. 备份旧日志
echo -e "${YELLOW}[2/4] 备份日志...${NC}"
for log in fastapi.log; do
    [[ -f "$log" ]] && mv "$log" "${log}.backup.$(date +%Y%m%d_%H%M%S)"
done
echo -e "${GREEN}✓ 日志已备份${NC}"
echo ""

# 3. 停止旧服务
echo -e "${YELLOW}[3/4] 停止旧服务...${NC}"

# 确认环境变量已加载（含 worker token，防 503）
if [[ -f "${ENV_FILE}" ]]; then
    echo -e "${GREEN}  ✓ 已加载 ${ENV_FILE}${NC}"
else
    echo -e "${YELLOW}  ⚠ 未找到 ${ENV_FILE}，worker token 可能缺失${NC}"
fi
if [[ -f "${SYSTEMD_UNIT_FILE}" ]]; then
    sudo systemctl stop "${SYSTEMD_SERVICE}" 2>/dev/null || systemctl stop "${SYSTEMD_SERVICE}" 2>/dev/null || true
    echo -e "${GREEN}  ✓ systemd ${SYSTEMD_SERVICE} 已停止${NC}"
else
    echo -e "${BLUE}  ℹ systemd ${SYSTEMD_SERVICE} 未安装${NC}"
fi

# 确保端口释放（清理 nohup 残留进程）
for port in "${PORT}"; do
    if lsof -i :"$port" >/dev/null 2>&1; then
        fuser -k "$port/tcp" 2>/dev/null || true
        sleep 1
        echo -e "${GREEN}  ✓ 端口 ${port} 残留进程已清理${NC}"
    fi
done
echo ""

# 4. 启动新服务
echo -e "${YELLOW}[4/4] 启动新服务...${NC}"

ensure_https_cert

# 优先通过 systemd 启动（自带 configs/env.production 和自动重启）
# 直接检测 unit 文件是否存在，避免非交互式 shell 中 systemctl 连不上 D-Bus
USE_SYSTEMD=false
if [[ -f "${SYSTEMD_UNIT_FILE}" ]]; then
    USE_SYSTEMD=true
fi

if [[ "${USE_SYSTEMD}" == "true" ]]; then
    echo -e "  通过 systemd 启动 ${SYSTEMD_SERVICE}..."
    sudo systemctl restart "${SYSTEMD_SERVICE}" 2>/dev/null || systemctl restart "${SYSTEMD_SERVICE}" 2>/dev/null || true
    # systemd 启动时如果端口仍被占用，清理残留后重试一次
    if lsof -i :"${PORT}" >/dev/null 2>&1 && ! systemctl is-active --quiet "${SYSTEMD_SERVICE}" 2>/dev/null; then
        echo -e "${YELLOW}  端口 ${PORT} 仍被占用，清理残留进程后重试...${NC}"
        fuser -k "${PORT}/tcp" 2>/dev/null || true
        sleep 2
        sudo systemctl restart "${SYSTEMD_SERVICE}" 2>/dev/null || systemctl restart "${SYSTEMD_SERVICE}" 2>/dev/null || true
    fi
else
    echo -e "${YELLOW}  systemd 服务未安装，使用 nohup 方式启动...${NC}"

    # 无远程 Worker 时 token 可以为空；首次部署 Worker 后会自动写入 cluster.json。
    if ! "${PYTHON_BIN}" -c "import json,sys; d=json.load(open('${PROJECT_DIR}/configs/cluster.json')); sys.exit(0 if d.get('worker_tokens') else 1)" 2>/dev/null; then
        echo -e "${YELLOW}  ⚠ 尚未配置远程 Worker token，远程 Worker 接口暂不可用${NC}"
    fi

    echo -e "  启动 FastAPI (${PORT})..."
    nohup setsid env \
        GMS_ENV="${GMS_ENV:-production}" \
        GMS_DATA_ROOT="${GMS_DATA_ROOT:-${PROJECT_DIR}/data}" \
        PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${PROJECT_DIR}/data/pycache}" \
        GMS_SOFTWARE_ROOT="${GMS_SOFTWARE_ROOT:-${HOME}/Software}" \
        APE_API_KEY="${APE_API_KEY:-${HOME}/Software/gts-rockchip.json}" \
        "${PYTHON_BIN}" -m uvicorn app:app \
        --host 0.0.0.0 --port "${PORT}" --log-level info --access-log \
        --ssl-keyfile "${CERT_KEY}" --ssl-certfile "${CERT_CRT}" \
        >> fastapi.log 2>&1 < /dev/null &
    echo $! > fastapi.pid
fi

# 健康检查（带重试，最多等待 15 秒）
echo -e "${BLUE}  进行健康检查...${NC}"
HEALTH_OK=false
for attempt in $(seq 1 5); do
    sleep 3
    if timeout 5 curl -sk -f "https://localhost:${PORT}/" >/dev/null 2>&1; then
        HEALTH_OK=true
        break
    fi
    echo -e "${BLUE}  等待服务就绪... (${attempt}/5)${NC}"
done

if [[ "${HEALTH_OK}" == "true" ]]; then
    echo -e "${GREEN}  ✓ ${PORT} 启动成功${NC}"
else
    echo -e "${RED}  ✗ ${PORT} 健康检查失败${NC}"
    if [[ "${USE_SYSTEMD}" == "true" ]]; then
        echo -e "${YELLOW}  查看: journalctl -u ${SYSTEMD_SERVICE} -n 30${NC}"
    else
        echo -e "${YELLOW}  查看: tail -30 fastapi.log${NC}"
    fi
    exit 1
fi

# 5. 重启本地 Worker Agent
if [[ -f "${WORKER_UNIT_FILE}" ]]; then
    echo ""
    echo -e "${YELLOW}  重启 Worker Agent (${WORKER_SERVICE})...${NC}"
    systemctl --user restart "${WORKER_SERVICE}" 2>/dev/null || true
    echo -e "${GREEN}  ✓ Worker Agent 已重启${NC}"
fi
echo ""

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  ✓ 服务管理完成！${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "📋 服务状态："
if [[ "${USE_SYSTEMD}" == "true" ]]; then
    echo -e "  FastAPI: ${GREEN}https://localhost:${PORT}${NC} (systemd: ${SYSTEMD_SERVICE})"
    echo -e "  日志: journalctl -u ${SYSTEMD_SERVICE} -f"
else
    echo -e "  FastAPI: ${GREEN}https://localhost:${PORT}${NC} (nohup, PID: $(cat fastapi.pid 2>/dev/null || echo '?'))"
    echo -e "  日志: tail -f fastapi.log"
fi
echo -e "  局域网: ${GREEN}https://${SERVER_HOSTNAME}:${PORT}${NC}"
echo ""
echo -e "🔍 检查端口："
echo -e "  lsof -i :${PORT}"
echo ""
