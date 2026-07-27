#!/usr/bin/env bash
set -euo pipefail

RUN_USER="${1:-${GMS_RUN_USER:-${SUDO_USER:-}}}"
SERVICE_NAME="${2:-${GMS_SERVICE_NAME:-gms-web-app}}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "请使用 root 权限运行：sudo bash $0 <服务用户> [服务名]" >&2
    exit 1
fi
if [[ ! "${RUN_USER}" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || ! id "${RUN_USER}" >/dev/null 2>&1; then
    echo "无效的服务用户：${RUN_USER:-<empty>}" >&2
    exit 1
fi
if [[ ! "${SERVICE_NAME}" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
    echo "无效的服务名：${SERVICE_NAME}" >&2
    exit 1
fi
if [[ ! -d /etc/polkit-1 ]]; then
    echo "未检测到 Polkit，无法配置 NetworkManager 授权" >&2
    exit 1
fi

TEMP_FILE="$(mktemp)"
trap 'rm -f "${TEMP_FILE}"' EXIT
RULE_FILE=""
JS_RULE_FILE="/etc/polkit-1/rules.d/49-${SERVICE_NAME}-networkmanager.rules"
PKLA_RULE_FILE="/etc/polkit-1/localauthority/50-local.d/49-${SERVICE_NAME}-networkmanager.pkla"

if [[ -d /etc/polkit-1/localauthority ]]; then
    # Ubuntu/Debian Polkit 0.105 uses the Local Authority backend and ignores
    # JavaScript .rules files. ResultAny=yes is required for a systemd process
    # that is not attached to an interactive login session.
    install -d -o root -g root -m 0755 /etc/polkit-1/localauthority/50-local.d
    cat > "${TEMP_FILE}" <<EOF
[Allow ${SERVICE_NAME} NetworkManager connection control]
Identity=unix-user:${RUN_USER}
Action=org.freedesktop.NetworkManager.network-control
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF
    RULE_FILE="${PKLA_RULE_FILE}"
    rm -f "${JS_RULE_FILE}"
else
    install -d -o root -g root -m 0755 /etc/polkit-1/rules.d
    cat > "${TEMP_FILE}" <<EOF
// Allow only the GMS service account to activate/deactivate NetworkManager
// connections. VPN profile visibility remains constrained by each profile's
// connection.permissions setting.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.NetworkManager.network-control" &&
        subject.user == "${RUN_USER}") {
        return polkit.Result.YES;
    }
});
EOF
    RULE_FILE="${JS_RULE_FILE}"
    rm -f "${PKLA_RULE_FILE}"
fi

install -o root -g root -m 0644 "${TEMP_FILE}" "${RULE_FILE}"
systemctl restart polkit.service
if ! systemctl is-active --quiet polkit.service; then
    echo "Polkit 重启失败，NetworkManager 授权尚未生效" >&2
    exit 1
fi

SERVICE_UNIT="${SERVICE_NAME%.service}.service"
SERVICE_PID="$(systemctl show "${SERVICE_UNIT}" -p MainPID --value 2>/dev/null || true)"
if [[ "${SERVICE_PID}" =~ ^[1-9][0-9]*$ ]] && command -v pkcheck >/dev/null 2>&1; then
    if ! pkcheck \
        --action-id org.freedesktop.NetworkManager.network-control \
        --process "${SERVICE_PID}" >/dev/null 2>&1; then
        echo "规则已写入，但服务进程 ${SERVICE_PID} 的 NetworkManager 授权验证失败" >&2
        exit 1
    fi
fi
echo "已安装 NetworkManager 授权：${RULE_FILE}（服务用户 ${RUN_USER}）"
