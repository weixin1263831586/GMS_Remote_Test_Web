#!/usr/bin/env bash
# ==============================================================================
# GMS Remote Test - ngrok tunnel helper
# ==============================================================================
# Common usage:
#   First run:       bash scripts/setup_ngrok.sh --token <your-authtoken>
#   Start tunnel:    bash scripts/setup_ngrok.sh
#   Stop tunnel:     bash scripts/setup_ngrok.sh --stop
#   Show status:     bash scripts/setup_ngrok.sh --status
#   Custom domain:   bash scripts/setup_ngrok.sh --url https://your-name.ngrok.app
# ==============================================================================

set -Eeuo pipefail
IFS=$'\n\t'

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ACTION="start"
LOCAL_HOST="${GMS_NGROK_LOCAL_HOST:-127.0.0.1}"
LOCAL_PORT="${GMS_NGROK_LOCAL_PORT:-5001}"
HEALTH_PATH="${GMS_NGROK_HEALTH_PATH:-/api/system/health}"

NGROK_BIN="${GMS_NGROK_BIN:-${HOME}/.local/bin/ngrok}"
NGROK_CONFIG="${GMS_NGROK_CONFIG:-${HOME}/.config/ngrok/ngrok.yml}"
NGROK_API_URL="${GMS_NGROK_API_URL:-http://127.0.0.1:4040}"
NGROK_URL="${GMS_NGROK_URL:-}"
NGROK_TOKEN="${NGROK_AUTHTOKEN:-}"

PID_FILE="${GMS_NGROK_PID_FILE:-}"
LOG_FILE="${GMS_NGROK_LOG_FILE:-}"
INSTALL_NGROK=true
CHECK_HEALTH=true
EXTRA_NGROK_ARGS=()
TMP_DIRS=()

cleanup() {
    local tmp_dir
    for tmp_dir in "${TMP_DIRS[@]}"; do
        [[ -n "${tmp_dir}" && -d "${tmp_dir}" ]] && rm -rf "${tmp_dir}"
    done
}
trap cleanup EXIT

usage() {
    cat <<EOF
用法:
  bash scripts/setup_ngrok.sh [选项] [-- <ngrok http 额外参数>]

常用:
  bash scripts/setup_ngrok.sh --token <authtoken>     首次保存 ngrok token 并启动
  bash scripts/setup_ngrok.sh                         启动 127.0.0.1:5001 隧道
  bash scripts/setup_ngrok.sh --status                查看隧道状态
  bash scripts/setup_ngrok.sh --stop                  停止本脚本启动的隧道

选项:
  --token, --authtoken <token>     配置 ngrok authtoken
  --host <host>                    本地服务地址，默认: ${LOCAL_HOST}
  --port <port>                    本地服务端口，默认: ${LOCAL_PORT}
  --health-path <path>             健康检查路径，默认: ${HEALTH_PATH}
  --url, --domain <url>            固定 ngrok URL，例如 https://demo.ngrok.app
  --api-url <url>                  ngrok 本地 API，默认: ${NGROK_API_URL}
  --log-file <path>                ngrok 日志文件，默认: /tmp/gms_ngrok_<port>.log
  --pid-file <path>                PID 文件，默认: /tmp/gms_ngrok_<port>.pid
  --no-install                     未安装 ngrok 时直接失败
  --skip-health-check              不检查本地 FastAPI 服务
  -h, --help                       显示帮助

环境变量:
  GMS_NGROK_LOCAL_HOST / GMS_NGROK_LOCAL_PORT / GMS_NGROK_URL
  GMS_NGROK_BIN / GMS_NGROK_CONFIG / GMS_NGROK_LOG_FILE / GMS_NGROK_PID_FILE
EOF
}

fail() {
    echo -e "${RED}ERROR:${NC} $*" >&2
    exit 1
}

warn() {
    echo -e "${YELLOW}WARN:${NC} $*" >&2
}

info() {
    echo -e "${BLUE}$*${NC}"
}

ok() {
    echo -e "${GREEN}$*${NC}"
}

require_value() {
    local opt="$1"
    local value="${2:-}"
    if [[ -z "${value}" || "${value}" == --* ]]; then
        fail "${opt} 需要参数"
    fi
    printf '%s\n' "${value}"
}

validate_runtime_config() {
    [[ "${LOCAL_PORT}" =~ ^[0-9]+$ ]] || fail "--port 必须是数字: ${LOCAL_PORT}"
    (( LOCAL_PORT > 0 && LOCAL_PORT < 65536 )) || fail "--port 超出范围: ${LOCAL_PORT}"

    [[ "${HEALTH_PATH}" == /* ]] || HEALTH_PATH="/${HEALTH_PATH}"
    NGROK_API_URL="${NGROK_API_URL%/}"

    if [[ -n "${NGROK_URL}" && ! "${NGROK_URL}" =~ ^[a-zA-Z][a-zA-Z0-9+.-]*:// ]]; then
        NGROK_URL="https://${NGROK_URL}"
    fi

    PID_FILE="${PID_FILE:-/tmp/gms_ngrok_${LOCAL_PORT}.pid}"
    LOG_FILE="${LOG_FILE:-/tmp/gms_ngrok_${LOCAL_PORT}.log}"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --token|--authtoken)
                NGROK_TOKEN="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --host)
                LOCAL_HOST="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --port)
                LOCAL_PORT="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --health-path)
                HEALTH_PATH="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --url|--domain)
                NGROK_URL="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --api-url)
                NGROK_API_URL="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --log-file)
                LOG_FILE="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --pid-file)
                PID_FILE="$(require_value "$1" "${2:-}")"
                shift 2
                ;;
            --no-install)
                INSTALL_NGROK=false
                shift
                ;;
            --skip-health-check)
                CHECK_HEALTH=false
                shift
                ;;
            --stop)
                ACTION="stop"
                shift
                ;;
            --status)
                ACTION="status"
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            --)
                shift
                EXTRA_NGROK_ARGS+=("$@")
                break
                ;;
            *)
                fail "未知参数: $1"
                ;;
        esac
    done

    validate_runtime_config
}

install_ngrok() {
    local machine
    local arch
    local tmp_dir
    local url

    machine="$(uname -m)"
    case "${machine}" in
        x86_64|amd64)
            arch="amd64"
            ;;
        aarch64|arm64)
            arch="arm64"
            ;;
        armv7l|armv6l|arm)
            arch="arm"
            ;;
        *)
            fail "不支持自动安装 ngrok 的架构: ${machine}。请手动安装后通过 GMS_NGROK_BIN 指定路径"
            ;;
    esac

    tmp_dir="$(mktemp -d)"
    TMP_DIRS+=("${tmp_dir}")
    url="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-${arch}.tgz"

    echo "  下载: ${url}"
    curl -fsSL "${url}" -o "${tmp_dir}/ngrok.tgz"
    tar -xzf "${tmp_dir}/ngrok.tgz" -C "${tmp_dir}"

    mkdir -p "${HOME}/.local/bin"
    install -m 0755 "${tmp_dir}/ngrok" "${HOME}/.local/bin/ngrok"
    NGROK_BIN="${HOME}/.local/bin/ngrok"
}

resolve_ngrok_bin() {
    if [[ -x "${NGROK_BIN}" ]]; then
        return
    fi

    if command -v ngrok >/dev/null 2>&1; then
        NGROK_BIN="$(command -v ngrok)"
        return
    fi

    [[ "${INSTALL_NGROK}" == "true" ]] || fail "未找到 ngrok，可去掉 --no-install 或设置 GMS_NGROK_BIN"

    echo "  未找到 ngrok，开始安装到 ${HOME}/.local/bin/ngrok"
    install_ngrok
}

ensure_ngrok_ready() {
    resolve_ngrok_bin

    if ! "${NGROK_BIN}" version >/dev/null 2>&1; then
        fail "ngrok 无法执行: ${NGROK_BIN}"
    fi

    if [[ -n "${NGROK_URL}" ]] && ! "${NGROK_BIN}" http --help 2>/dev/null | grep -q -- '--url'; then
        fail "当前 ngrok 版本不支持 --url，请升级 ngrok: ${NGROK_BIN} update"
    fi

    echo "  ngrok: $("${NGROK_BIN}" version)"
}

has_authtoken() {
    [[ -f "${NGROK_CONFIG}" ]] && grep -Eq '^[[:space:]]*authtoken:' "${NGROK_CONFIG}"
}

configure_authtoken() {
    if [[ -n "${NGROK_TOKEN}" ]]; then
        mkdir -p "$(dirname "${NGROK_CONFIG}")"
        "${NGROK_BIN}" config add-authtoken "${NGROK_TOKEN}" --config "${NGROK_CONFIG}" >/dev/null
        ok "  authtoken 已写入 ${NGROK_CONFIG}"
        return
    fi

    if has_authtoken; then
        ok "  authtoken 已存在: ${NGROK_CONFIG}"
        return
    fi

    cat >&2 <<EOF
${RED}ERROR:${NC} 未找到 ngrok authtoken。
请先运行:
  bash scripts/setup_ngrok.sh --token <你的 authtoken>

获取 token:
  https://dashboard.ngrok.com/get-started/your-authtoken
EOF
    exit 1
}

pid_from_file() {
    local pid
    [[ -f "${PID_FILE}" ]] || return 1
    read -r pid < "${PID_FILE}" || return 1
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "${pid}"
}

is_ngrok_pid() {
    local pid="$1"
    local args
    args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    [[ "${args}" =~ (^|[/[:space:]])ngrok([[:space:]]|$) ]]
}

is_running() {
    local pid
    pid="$(pid_from_file)" || return 1
    kill -0 "${pid}" 2>/dev/null && is_ngrok_pid "${pid}"
}

find_matching_ngrok_pids() {
    local current_pid
    local line
    local pid
    local args

    current_pid="$$"
    while IFS= read -r line; do
        pid="${line%% *}"
        args="${line#* }"
        [[ "${pid}" == "${current_pid}" ]] && continue
        [[ "${args}" == *" http "* ]] || continue
        if [[ "${args}" =~ (^|[/:[:space:]])${LOCAL_PORT}($|[[:space:]]) ]]; then
            printf '%s\n' "${pid}"
        fi
    done < <(pgrep -af '(^|[/[:space:]])ngrok([[:space:]]|$)' 2>/dev/null || true)
}

terminate_pid() {
    local pid="$1"
    local i

    kill "${pid}" 2>/dev/null || return 0
    for _ in 1 2 3 4 5; do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 1
    done

    kill -TERM "${pid}" 2>/dev/null || true
    for i in 1 2 3; do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 1
    done

    kill -KILL "${pid}" 2>/dev/null || true
}

stop_ngrok() {
    local quiet="${1:-false}"
    local pid
    local stopped=false
    local seen_pids=""

    if pid="$(pid_from_file)" && kill -0 "${pid}" 2>/dev/null; then
        if is_ngrok_pid "${pid}"; then
            terminate_pid "${pid}"
            stopped=true
            seen_pids=" ${pid} "
        else
            warn "PID 文件指向的进程不是 ngrok，已忽略: ${pid}"
        fi
    fi

    while IFS= read -r pid; do
        [[ -z "${pid}" ]] && continue
        [[ "${seen_pids}" == *" ${pid} "* ]] && continue
        terminate_pid "${pid}"
        stopped=true
    done < <(find_matching_ngrok_pids)

    rm -f "${PID_FILE}"

    if [[ "${quiet}" != "true" ]]; then
        if [[ "${stopped}" == "true" ]]; then
            ok "ngrok 已停止"
        else
            info "ngrok 未在运行"
        fi
    fi
}

get_public_url() {
    local json
    json="$(curl -fsS --connect-timeout 2 --max-time 5 "${NGROK_API_URL}/api/tunnels" 2>/dev/null || true)"
    [[ -n "${json}" ]] || return 1

    python3 -c '
import json
import sys

port = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

tunnels = data.get("tunnels") or []
preferred = []
fallback = []

for tunnel in tunnels:
    public_url = tunnel.get("public_url") or ""
    if not public_url:
        continue
    config = tunnel.get("config") or {}
    addr = str(config.get("addr") or "")
    item = (public_url, addr)
    if addr.endswith(":" + port) or (":" + port) in addr:
        preferred.append(item)
    fallback.append(item)

candidates = preferred or fallback
if not candidates:
    sys.exit(1)

for public_url, _ in candidates:
    if public_url.startswith("https://"):
        print(public_url)
        sys.exit(0)

print(candidates[0][0])
' "${LOCAL_PORT}" <<<"${json}"
}

check_local_service() {
    local health_url
    local root_url

    [[ "${CHECK_HEALTH}" == "true" ]] || {
        warn "已跳过本地服务健康检查"
        return
    }

    health_url="http://${LOCAL_HOST}:${LOCAL_PORT}${HEALTH_PATH}"
    root_url="http://${LOCAL_HOST}:${LOCAL_PORT}/"

    if curl -fsS --connect-timeout 2 --max-time 5 "${health_url}" >/dev/null 2>&1; then
        ok "  本地服务运行正常: ${health_url}"
        return
    fi

    if curl -fsS --connect-timeout 2 --max-time 5 "${root_url}" >/dev/null 2>&1; then
        warn "健康检查路径不可用，但端口已有 HTTP 响应: ${root_url}"
        return
    fi

    cat >&2 <<EOF
${RED}ERROR:${NC} 本地服务未响应: ${health_url}

请先启动 GMS 服务，例如:
  cd ${PROJECT_DIR}
  bash restart_services.sh

如服务不是 FastAPI 默认健康检查路径，可使用:
  bash scripts/setup_ngrok.sh --health-path /
EOF
    exit 1
}

print_last_log_lines() {
    [[ -f "${LOG_FILE}" ]] || return
    echo ""
    echo "最近 ngrok 日志:"
    tail -n 40 "${LOG_FILE}" >&2 || true
}

start_ngrok() {
    local target
    local url
    local args=()
    local pid

    stop_ngrok true

    mkdir -p "$(dirname "${LOG_FILE}")"
    : > "${LOG_FILE}"

    target="http://${LOCAL_HOST}:${LOCAL_PORT}"
    args=("${NGROK_BIN}" "http" "${target}" "--log=stdout")

    [[ -f "${NGROK_CONFIG}" ]] && args+=("--config" "${NGROK_CONFIG}")
    [[ -n "${NGROK_URL}" ]] && args+=("--url" "${NGROK_URL}")
    args+=("${EXTRA_NGROK_ARGS[@]}")

    echo "  转发目标: ${target}"
    echo "  ngrok API: ${NGROK_API_URL}"
    echo "  日志文件: ${LOG_FILE}"

    nohup "${args[@]}" > "${LOG_FILE}" 2>&1 &
    pid="$!"
    echo "${pid}" > "${PID_FILE}"

    echo -n "  正在建立隧道"
    url=""
    for _ in $(seq 1 30); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi

        url="$(get_public_url || true)"
        [[ -n "${url}" ]] && break

        echo -n "."
        sleep 1
    done
    echo ""

    if [[ -z "${url}" ]]; then
        rm -f "${PID_FILE}"
        echo -e "  ${RED}隧道建立失败${NC}"
        print_last_log_lines
        exit 1
    fi

    ok "  隧道建立成功"
    echo ""
    info "========================================"
    ok "  ngrok 内网穿透已启动"
    info "========================================"
    echo ""
    echo "手机访问地址:"
    ok "  ${url}"
    echo ""
    echo "管理命令:"
    echo "  查看状态:  bash scripts/setup_ngrok.sh --status"
    echo "  停止穿透:  bash scripts/setup_ngrok.sh --stop"
    echo "  管理面板:  ${NGROK_API_URL}"
    echo "  日志文件:  ${LOG_FILE}"
    echo ""
    echo "说明:"
    echo "  - 免费版随机 URL 每次重启可能变化；需要固定地址时使用 --url"
    echo "  - 手机端访问 ngrok 地址时，首次可能出现 ngrok 确认页"
    echo "  - 如果生产环境启用了 TRUSTED_HOSTS，请把 ngrok 域名加入配置后重启服务"
}

show_status() {
    local pid
    local url

    if is_running; then
        pid="$(pid_from_file)"
        url="$(get_public_url || true)"
        ok "ngrok 正在运行"
        echo "  PID:       ${pid}"
        echo "  本地目标:  http://${LOCAL_HOST}:${LOCAL_PORT}"
        if [[ -n "${url}" ]]; then
            ok "  公网地址:  ${url}"
        else
            warn "ngrok 进程存在，但无法从 ${NGROK_API_URL}/api/tunnels 读取公网地址"
        fi
        echo "  管理面板:  ${NGROK_API_URL}"
        echo "  日志文件:  ${LOG_FILE}"
        return
    fi

    url="$(get_public_url || true)"
    if [[ -n "${url}" ]]; then
        warn "检测到 ngrok 本地 API 有隧道，但 PID 文件不匹配"
        echo "  公网地址:  ${url}"
        echo "  如需重建本项目隧道，请运行: bash scripts/setup_ngrok.sh"
        return
    fi

    info "ngrok 未在运行"
}

main() {
    parse_args "$@"

    case "${ACTION}" in
        stop)
            stop_ngrok
            ;;
        status)
            show_status
            ;;
        start)
            info "========================================"
            info "  GMS Remote Test - ngrok 内网穿透"
            info "========================================"
            echo ""

            echo "[1/4] 检查 ngrok"
            ensure_ngrok_ready
            echo ""

            echo "[2/4] 配置 authtoken"
            configure_authtoken
            echo ""

            echo "[3/4] 检查本地服务"
            check_local_service
            echo ""

            echo "[4/4] 启动 ngrok"
            start_ngrok
            ;;
        *)
            fail "未知动作: ${ACTION}"
            ;;
    esac
}

main "$@"
