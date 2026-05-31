#!/usr/bin/env bash
# ==============================================================================
# GMS Remote Test - Tailscale network setup helper
# ==============================================================================
# Common usage:
#   Install & connect:  sudo bash scripts/setup_tailscale.sh
#   Show status:        bash scripts/setup_tailscale.sh --status
#   Get IP only:        bash scripts/setup_tailscale.sh --ip
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

GMS_PORT="${GMS_PORT:-5001}"
ACTION="setup"

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

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --status)
                ACTION="status"
                shift
                ;;
            --ip)
                ACTION="ip"
                shift
                ;;
            --help|-h)
                cat <<EOF
用法:
  sudo bash scripts/setup_tailscale.sh          安装 Tailscale 并连接
  bash scripts/setup_tailscale.sh --status       查看连接状态
  bash scripts/setup_tailscale.sh --ip           仅输出 Tailscale IP

选项:
  --status       查看 Tailscale 连接状态
  --ip           仅输出 Tailscale IPv4 地址
  -h, --help     显示帮助

环境变量:
  GMS_PORT       GMS 服务端口，默认: 5001
EOF
                exit 0
                ;;
            *)
                fail "未知参数: $1"
                ;;
        esac
    done
}

check_tailscale_installed() {
    command -v tailscale >/dev/null 2>&1
}

check_tailscaled_running() {
    systemctl is-active --quiet tailscaled 2>/dev/null
}

get_tailscale_ip() {
    tailscale ip -4 2>/dev/null || true
}

do_status() {
    if ! check_tailscale_installed; then
        warn "Tailscale 未安装"
        echo ""
        echo "安装命令:"
        echo "  curl -fsSL https://tailscale.com/install.sh | sudo sh"
        return
    fi

    if ! check_tailscaled_running; then
        warn "tailscaled 服务未运行"
        echo ""
        echo "启动命令:"
        echo "  sudo systemctl enable --now tailscaled"
        return
    fi

    local ip
    ip="$(get_tailscale_ip)" || true
    if [[ -z "${ip}" ]]; then
        warn "Tailscale 已安装但未连接（需要先登录）"
        echo ""
        echo "连接命令:"
        echo "  sudo tailscale up"
        return
    fi

    ok "Tailscale 已连接"
    echo "  IP:     ${ip}"
    echo "  访问地址: http://${ip}:${GMS_PORT}"
    echo ""
    echo "在 tailnet 内的其他设备访问以上地址即可使用 GMS。"
}

do_ip() {
    local ip
    ip="$(get_tailscale_ip)" || true
    if [[ -n "${ip}" ]]; then
        echo "${ip}"
    else
        exit 1
    fi
}

do_setup() {
    info "========================================"
    info "  GMS Remote Test - Tailscale 组网"
    info "========================================"
    echo ""

    echo "[1/3] 检查 Tailscale"
    if check_tailscale_installed; then
        ok "  Tailscale 已安装: $(tailscale version 2>/dev/null | head -1)"
    else
        info "  未安装 Tailscale，开始安装..."
        curl -fsSL https://tailscale.com/install.sh | sudo sh
        ok "  Tailscale 安装完成"
    fi
    echo ""

    echo "[2/3] 启动 tailscaled 服务"
    if check_tailscaled_running; then
        ok "  tailscaled 已运行"
    else
        sudo systemctl enable --now tailscaled
        ok "  tailscaled 已启动"
    fi
    echo ""

    echo "[3/3] 连接 Tailscale 网络"
    local ip
    ip="$(get_tailscale_ip)" || true
    if [[ -n "${ip}" ]]; then
        ok "  已连接，Tailscale IP: ${ip}"
    else
        info "  需要登录 Tailscale 账号，请在浏览器中完成授权..."
        sudo tailscale up
        ip="$(get_tailscale_ip)" || true
        if [[ -n "${ip}" ]]; then
            ok "  连接成功，Tailscale IP: ${ip}"
        else
            fail "连接失败，请检查网络或 Tailscale 账号"
        fi
    fi

    echo ""
    info "========================================"
    ok "  Tailscale 组网完成"
    info "========================================"
    echo ""
    echo "GMS 访问地址:"
    ok "  http://${ip}:${GMS_PORT}"
    echo ""
    echo "在 tailnet 内的其他设备访问以上地址即可使用 GMS。"
}

main() {
    parse_args "$@"

    case "${ACTION}" in
        status)
            do_status
            ;;
        ip)
            do_ip
            ;;
        setup)
            do_setup
            ;;
        *)
            fail "未知动作: ${ACTION}"
            ;;
    esac
}

main "$@"
