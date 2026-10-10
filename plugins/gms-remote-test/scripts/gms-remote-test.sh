#!/bin/bash
set -o pipefail
# ==============================================================================
# GMS Remote Test API Helper Script (FastAPI Port 5001)
# Version is declared once in agent/gms-remote-test/package.yaml and
# propagated here (GMS_RT_VERSION) by tools/scripts/agent/release.py.
# ==============================================================================

GMS_RT_VERSION="0.22.41"
GMS_RT_OUTPUT="${GMS_RT_OUTPUT:-human}"
GMS_RT_QUIET="${GMS_RT_QUIET:-0}"
GMS_RT_NON_INTERACTIVE="${GMS_RT_NON_INTERACTIVE:-0}"
GMS_RT_ASSUME_YES="${GMS_RT_ASSUME_YES:-0}"
GMS_RT_ERROR_SEEN=0

GMS_RT_EXIT_USAGE=2
GMS_RT_EXIT_AUTH=3
GMS_RT_EXIT_PERMISSION=4
GMS_RT_EXIT_CONFLICT=5
GMS_RT_EXIT_NETWORK=6
GMS_RT_EXIT_OPERATION=7
# Batch operations where some devices succeeded and others failed.
# Mapped to GMS_RT_EXIT_OPERATION by _gms_rt_dispatch's envelope case, but
# keeps the semantic exit code for direct callers.
GMS_RT_EXIT_PARTIAL=7

# GMS Web App Configuration Directory
# Can be overridden by environment variable
GMS_WEB_APP_DIR="${GMS_WEB_APP_DIR:-${HOME}/GMS_Remote_Test/web_app}"

# Default configuration. Profile parsing and equivalence checks stay in the
# canonical Python profile_store (ADR 0003); this shell only consumes its
# resolved fields. Multiple client profiles may be collapsed for direct CLI
# use only when Controller, TLS policy, and token contents are identical.
GMS_PORT="${GMS_PORT:-5001}"
SERVER_URL="${GMS_REMOTE_TEST_SERVER:-}"
_gms_runtime_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
_gms_rt_local_catalog=0
if [[ "${BASH_SOURCE[0]}" = "$0" ]] \
    && [ "${1:-}" = "gms-rt-system-commands" ]; then
    _gms_rt_local_catalog=1
fi
if [ "$_gms_rt_local_catalog" = "1" ]; then
    _gms_profile_context=(none "" "" "" "" "" "")
elif [ -r "$_gms_runtime_dir/gms_agent/profile_store.py" ]; then
    # Resolve via `python3 -c`, NOT a heredoc: heredocs need a writable
    # temp file for the script text, so a full or read-only /tmp used to
    # zero the context array and masquerade as "无法解析 Agent profile"。
    # Fields stay NUL-terminated and stream through the <() pipe — a
    # plain $() would strip the trailing empty fields.
    mapfile -d '' -t _gms_profile_context < <(
        PYTHONPATH="$_gms_runtime_dir${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import sys
from gms_agent.profile_store import resolve_direct_cli_context

context = resolve_direct_cli_context(sys.argv[1], sys.argv[2])
for key in ("mode", "profile", "server", "ca_cert", "token_file", "insecure", "error"):
    sys.stdout.write(context[key])
    sys.stdout.write("\0")
' "${GMS_RT_PROFILE:-}" "$SERVER_URL"
    )
    if [ "${#_gms_profile_context[@]}" -lt 7 ]; then
        echo "Error: Agent profile 解析器未产生完整输出 (${#_gms_profile_context[@]}/7 字段)。" >&2
        echo "  这通常是环境问题而非配置问题: 检查磁盘空间与临时目录可写性 (df -h /tmp; df -i /tmp) 以及 python3 是否可用。" >&2
        exit 2
    fi
else
    # A standalone copied helper has no profile store. This compatibility
    # path is valid only when no named profile was requested.
    if [ -n "${GMS_RT_PROFILE:-}" ]; then
        echo "Error: 当前 gms-rt 副本缺少 Agent profile runtime。" >&2
        exit 2
    fi
    _gms_profile_context=(none "" "" "" "" "" "")
fi
_gms_profile_mode="${_gms_profile_context[0]:-error}"
if [ "$_gms_profile_mode" = "error" ]; then
    if [ -n "${_gms_profile_context[6]:-}" ]; then
        # profile_store produced a concrete diagnosis (missing or
        # ambiguous profile, controller mismatch, ...) — relay verbatim.
        echo "Error: ${_gms_profile_context[6]}" >&2
    else
        # mode=error without a message means the resolver contract broke,
        # not the user's profile selection — say so honestly.
        echo "Error: Agent profile 解析器返回了未知错误形态。" >&2
    fi
    echo "  运行 gms-agent profile list 查看可用 profile。" >&2
    exit 2
fi
if [ "$_gms_profile_mode" != "none" ]; then
    GMS_RT_PROFILE="${_gms_profile_context[1]}"
    SERVER_URL="${_gms_profile_context[2]}"
    [ -n "${GMS_CURL_CA_CERT:-}" ] || GMS_CURL_CA_CERT="${_gms_profile_context[3]}"
    [ -n "${GMS_AUTH_TOKEN_FILE:-}" ] || GMS_AUTH_TOKEN_FILE="${_gms_profile_context[4]}"
    if [ -z "${GMS_CURL_CA_CERT:-}" ] \
        && [ "${_gms_profile_context[5]}" = "true" ]; then
        GMS_CURL_INSECURE=1
        # Sticky-insecure profile 是安装期一次性环境的遗留物；每次使用
        # 都必须让操作者看见 TLS 校验被关闭，防止长期静默裸奔。
        echo "WARNING: profile '$(_gms_profile_context[1])' 以 insecure 模式安装（GMS_CURL_INSECURE=1），TLS 证书校验已关闭。" >&2
        echo "  仅限一次性环境使用；正式环境请运行 gms-agent profile 配置 ca_cert 后重装。" >&2
    fi
    export GMS_RT_PROFILE GMS_CURL_CA_CERT GMS_AUTH_TOKEN_FILE GMS_CURL_INSECURE
fi

if [ "$_gms_rt_local_catalog" != "1" ] && [ -z "$SERVER_URL" ]; then
    # Check if we're running on the server machine (use dynamic IP detection)
    # Try to get local IP using the same method as get_local_ip() in Python
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -z "$LOCAL_IP" ]; then
        # Fallback: try to get IP from routing table
        LOCAL_IP=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'src \K\S+')
    fi
    if [ -z "$LOCAL_IP" ]; then
        # Last resort: use hostname
        LOCAL_IP=$(hostname 2>/dev/null || echo "localhost")
    fi

    # Store detected IP for later use
    export DETECTED_LOCAL_IP="$LOCAL_IP"

    # Check if server host is in environment or use detected IP
    CONFIG_SERVER_HOST=""
    for config_file in "${GMS_WEB_APP_DIR}/configs/config.json" "${HOME}/GMS_Remote_Test/web_app/configs/config.json"; do
        if [ -f "$config_file" ]; then
            CONFIG_SERVER_HOST=$(grep -o '"ubuntu_host": *"[^"]*"' "$config_file" 2>/dev/null | cut -d'"' -f4 | head -n 1)
            [ -n "$CONFIG_SERVER_HOST" ] && break
        fi
    done

    SERVER_HOST="${UBUNTU_HOST:-${CONFIG_SERVER_HOST:-$DETECTED_LOCAL_IP}}"
    # 本地启发式只允许用于 Controller 本机: 端口确实在本机监听。纯构建
    # 服务器上 hostname -I 的首地址不是 Controller, 静默指过去只会得到
    # 误导性的 Connection refused — 快速失败并给出明确配置指引。
    if ! timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/${GMS_PORT}" 2>/dev/null; then
        echo "Error: 未配置 Controller 且本机端口 ${GMS_PORT} 无服务监听。" >&2
        echo "  请任选其一:" >&2
        echo "    export GMS_REMOTE_TEST_SERVER=https://CONTROLLER:${GMS_PORT}" >&2
        echo "    gms-agent profile list" >&2
        echo "    export GMS_RT_PROFILE=<PROFILE>   # 显式选择已安装的控制器绑定" >&2
        exit 2
    fi
    SERVER_URL="https://${SERVER_HOST}:${GMS_PORT}"
fi
API_BASE="${SERVER_URL}/api"

# Default SSH user - use environment variable or current system user
DEFAULT_SSH_USER="${UBUNTU_USER:-$(whoami)}"

# Colors for output
RED=$(printf '\033[0;31m')
GREEN=$(printf '\033[0;32m')
YELLOW=$(printf '\033[1;33m')
BLUE=$(printf '\033[0;34m')
NC=$(printf '\033[0m')

# Network timeout constants
PING_TIMEOUT=2
CURL_TIMEOUT="${GMS_CURL_TIMEOUT:-30}"  # 30 seconds for slow API endpoints (e.g., test results)
CURL_BURN_TIMEOUT="${GMS_CURL_BURN_TIMEOUT:-1800}"  # firmware transfer + burn can take much longer
CURL_EXIT_CANNOT_CONNECT=7
CURL_EXIT_OPERATION_TIMEOUT=28
CURL_EXIT_SSL_CERT=60

# Authentication
# The current backend authenticates API clients with the gms_session cookie.
# Keep the cookie outside the repository and allow callers to override its path.
# Multiple agents running under the same Unix user used to share one
# writable cookie jar with no concurrency protection — concurrent login/
# logout/parallel requests clobbered each other's sessions.  A profile name
# (GMS_RT_PROFILE) separates jars per agent; flock serialises writes to the
# same jar.  Profiles do NOT strengthen server-side auth: a revoked token
# stays revoked regardless of the local file.
GMS_RT_PROFILE="${GMS_RT_PROFILE:-default}"
GMS_AUTH_COOKIE_JAR="${GMS_AUTH_COOKIE_JAR:-${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/${GMS_RT_PROFILE}.cookies}"
# Agent Service Token: when GMS_AUTH_TOKEN_FILE points
# at a 0600 token file, every request carries Authorization: Bearer <token>
# instead of the session cookie. The CLI never prints the token and agents
# only ever learn the path. Token auth is mutually exclusive with the
# cookie jar: Bearer mode never reads or writes the cookie jar,
# so a request can never carry both an agent token and a leftover human
# session cookie (the server treats that combination as a privilege mix).
# Human mode stays Cookie only.
GMS_AUTH_TOKEN_FILE="${GMS_AUTH_TOKEN_FILE:-}"
# Default-path discovery: gms-rt-agent-enroll writes
# ${XDG_STATE_HOME}/gms-remote-test/${GMS_RT_PROFILE}.token when --out is not
# given, but this variable previously stayed empty, so a fresh shell/CLI (or
# an MCP server whose registration env lacks the variable) silently fell back
# to cookie mode and every call failed with "Authentication required". Pick
# up the enrolled token from the standard path unless the caller set an
# explicit location; the 0600 + owner checks in _gms_refresh_bearer_token
# still apply, so a looser or foreign file is rejected as before.
if [ -z "$GMS_AUTH_TOKEN_FILE" ]; then
    _gms_default_token="${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/${GMS_RT_PROFILE}.token"
    if [ -r "$_gms_default_token" ]; then
        _gms_default_mode="$(stat -c '%04a' "$_gms_default_token" 2>/dev/null || printf '0600')"
        if [ "$((_gms_default_mode & 077))" = "0" ]; then
            GMS_AUTH_TOKEN_FILE="$_gms_default_token"
        fi
        unset _gms_default_mode
    fi
    unset _gms_default_token
fi
_gms_bearer_token=""  # cached per process; reloaded by _refresh_tls_args
_gms_bearer_header_file=""  # 0600 header file (token stays out of argv)
# Bearer header 临时文件含有效 token：进程退出（含被信号打断）时必须
# 清理，否则每次 token 模式调用都会向 /tmp 泄漏一个含凭据的文件。
# trap 仅在被直接执行时安装（见文件尾 _is_sourced 守卫），避免劫持
# source 本库的调用方自己的 EXIT trap。
_gms_cleanup_bearer_header_file() {
    [ -n "$_gms_bearer_header_file" ] || return 0
    rm -f -- "$_gms_bearer_header_file"
    _gms_bearer_header_file=""
}
# 请求级清理：把 Bearer header 临时文件的生命周期收缩到单次 HTTP 调用
# 内。source（函数）模式无法安装 EXIT trap（会覆盖调用方自己的 trap），
# 因此每次请求结束立即清理；直接执行模式另有文件尾 EXIT trap 兜底
# （覆盖 curl 执行中被信号打断的窗口）。
_gms_end_bearer_request() {
    _gms_cleanup_bearer_header_file
    CURL_BEARER_ARGS=()
}
# Service-token mode gate: when the CLI runs under an Agent
# Service Token (Bearer), arbitrary device shell MUST carry a one-shot
# approval token — otherwise an agent with plain terminal access could
# bypass the MCP-layer approval entirely by calling this CLI directly.
_gms_is_service_token_mode() {
    [ -n "${GMS_AUTH_TOKEN_FILE:-}" ]
}
_gms_refresh_bearer_token() {
    if [ -n "${GMS_AUTH_TOKEN_FILE:-}" ]; then
        if [ ! -r "$GMS_AUTH_TOKEN_FILE" ]; then
            error "Agent token file $GMS_AUTH_TOKEN_FILE is missing or unreadable; refusing to fall back to cookie auth (fail closed)"
            _gms_bearer_token=""
            return 1
        fi
        # Reject token files whose permissions are too loose or
        # whose owner is not the current user — same hard rule as the
        # Worker Token files.
        local _mode _owner
        _mode=$(stat -c '%04a' "$GMS_AUTH_TOKEN_FILE" 2>/dev/null || printf '0600')
        _owner=$(stat -c '%u' "$GMS_AUTH_TOKEN_FILE" 2>/dev/null || printf "$(id -u)")
        if [ "$((_mode & 077))" != "0" ] || [ "$_owner" != "$(id -u)" ]; then
            error "Agent token file $GMS_AUTH_TOKEN_FILE must be 0600 and owned by the current user (got mode $_mode owner $_owner)"
            _gms_bearer_token=""
            return 1
        fi
        _gms_bearer_token=$(tr -d '[:space:]' < "$GMS_AUTH_TOKEN_FILE" 2>/dev/null)
        if [ -z "$_gms_bearer_token" ]; then
            error "Agent token file $GMS_AUTH_TOKEN_FILE is empty; refusing to fall back to cookie auth (fail closed)"
            return 1
        fi
    else
        _gms_bearer_token=""
    fi
    return 0
}
# Local deployments commonly use a self-signed HTTPS certificate. Fail
# closed by default; provide GMS_CURL_CA_CERT for a trusted CA bundle.
# GMS_CURL_INSECURE=1 is reserved for throwaway environments.
# Recomputed before every curl call: when this script is sourced (function
# mode), GMS_CURL_* may be exported after the source line, and a value
# frozen at source time would silently drop --cacert/-k.
# 轻量传输解析：只决定 TLS 参数与认证模式（Bearer 生效时清空 cookie
# 参数），不创建/删除 Bearer header 临时文件。凭据临时文件推迟到真实
# HTTP 调用前才物化（见 _refresh_tls_args），避免 source 模式下加载即
# 落盘且没有退出清理。Returns 0 when auth args are usable and 1 when a
# configured token is invalid (callers MUST abort the request instead of
# proceeding unauthenticated).
_gms_resolve_tls() {
    CURL_TLS_ARGS=()
    if [[ "$SERVER_URL" == https://* ]]; then
        if [ -n "${GMS_CURL_CA_CERT:-}" ]; then
            CURL_TLS_ARGS=(--cacert "$GMS_CURL_CA_CERT")
        elif [ "${GMS_CURL_INSECURE:-0}" = "1" ]; then
            CURL_TLS_ARGS=(-k)
        fi
    fi
}

_gms_resolve_transport() {
    _gms_resolve_tls
    # Agent token: pick up the file fresh on every call (callers may export
    # GMS_AUTH_TOKEN_FILE after sourcing this script in function mode).
    CURL_BEARER_ARGS=()
    CURL_AUTH_ARGS=(-b "$GMS_AUTH_COOKIE_JAR" -c "$GMS_AUTH_COOKIE_JAR")
    if [ -z "${GMS_AUTH_TOKEN_FILE:-}" ]; then
        return 0
    fi
    if ! _gms_refresh_bearer_token || [ -z "$_gms_bearer_token" ]; then
        # Configured-but-invalid token: send nothing. Proceeding with the
        # cookie jar would silently switch identity; proceeding empty yields
        # a server-side 401 instead.
        CURL_AUTH_ARGS=()
        CURL_BEARER_ARGS=()
        return 1
    fi
    # Bearer-only mode: never also send the cookie jar.
    CURL_AUTH_ARGS=()
    return 0
}
# 每次 HTTP 调用前的完整刷新：在轻量解析之上物化 0600 Bearer header
# 临时文件。token 绝不出现在 curl 的 argv 里（/proc/<pid>/cmdline 对同
# Unix 用户可见），而是写入临时文件后用 curl -H @file 读取。
# Credential-mode policy, fail closed:
#   GMS_AUTH_TOKEN_FILE unset          → cookie session mode (human login);
#   set and valid non-empty token      → Bearer mode (Agent Service Token),
#                                        cookie args emptied entirely;
#   set but invalid (unreadable /
#   loose perms / foreign owner /
#   empty)                             → FAIL: both auth arg sets emptied,
#                                        no silent cookie fallback. A valid
#                                        human cookie left under the same
#                                        Unix user must never authenticate
#                                        a request that was expected to
#                                        carry the Agent identity.
_refresh_tls_args() {
    if ! _gms_resolve_transport; then
        # Also drop any stale header file from a previous Bearer-mode call
        # so credentials never outlive their mode.
        if [ -n "$_gms_bearer_header_file" ]; then
            rm -f -- "$_gms_bearer_header_file"
            _gms_bearer_header_file=""
        fi
        return 1
    fi
    if [ -z "$_gms_bearer_token" ]; then
        return 0
    fi
    if [ -z "$_gms_bearer_header_file" ] || [ ! -f "$_gms_bearer_header_file" ]; then
        _gms_bearer_header_file=$(mktemp "${TMPDIR:-/tmp}/gms-bearer-header.XXXXXX") \
            || { error "无法创建 Bearer header 临时文件"; CURL_AUTH_ARGS=(); return 1; }
        chmod 600 "$_gms_bearer_header_file"
    fi
    printf 'Authorization: Bearer %s\n' "$_gms_bearer_token" \
        > "$_gms_bearer_header_file"
    CURL_BEARER_ARGS=(-H "@${_gms_bearer_header_file}")
    return 0
}
# Serialises cookie-jar writes across concurrent CLI processes.
GMS_COOKIE_LOCK_FILE="${GMS_AUTH_COOKIE_JAR}.lock"
_gms_with_cookie_lock() {
    local cookie_dir
    cookie_dir=$(dirname "$GMS_AUTH_COOKIE_JAR")
    if [ ! -d "$cookie_dir" ]; then
        mkdir -p "$cookie_dir" 2>/dev/null || true
        chmod 700 "$cookie_dir" 2>/dev/null || true
    fi
    if command -v flock >/dev/null 2>&1 && [ -f "$GMS_COOKIE_LOCK_FILE" -o -w "$cookie_dir" ]; then
        (
            # Failing to take the lock must NOT fall through to the
            # unprotected command.  `|| true` used to let a lock-starved
            # process run curl anyway, which is exactly the concurrent
            # jar clobber the lock exists to prevent.  Exit non-zero from
            # the subshell so the caller sees a transport failure.
            flock -w 10 200 || exit 99
            "$@"
        ) 200>"$GMS_COOKIE_LOCK_FILE"
    else
        "$@"
    fi
}

# Source/exec-time transport bootstrap: light mode resolution only — the
# credential temp file is materialized per HTTP call in _refresh_tls_args.
_gms_resolve_transport >/dev/null 2>&1

_refresh_transport_config() {
    SERVER_URL="${SERVER_URL%/}"
    API_BASE="${SERVER_URL}/api"
    _gms_resolve_transport >/dev/null 2>&1
}

_validate_server_url() {
    local value="${1:-}"
    local authority
    case "$value" in
        http://*|https://*) ;;
        *) return 1 ;;
    esac
    case "$value" in
        *'@'*|*'?'*|*'#'*|*[[:space:]]*) return 1 ;;
    esac
    authority="${value#*://}"
    authority="${authority%%/*}"
    [ -n "$authority" ]
}

if [ "$GMS_RT_OUTPUT" = "json" ] || [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    RED=""
    GREEN=""
    YELLOW=""
    BLUE=""
    NC=""
fi

# Print functions
error() {
    GMS_RT_ERROR_SEEN=1
    printf '%sError:%s %s\n' "$RED" "$NC" "$1" >&2
    return "$GMS_RT_EXIT_OPERATION"
}

success() {
    [ "$GMS_RT_QUIET" = "1" ] || printf '%s✓ %s%s\n' "$GREEN" "$1" "$NC"
}

warning() {
    [ "$GMS_RT_QUIET" = "1" ] || printf '%s⚠ %s%s\n' "$YELLOW" "$1" "$NC"
}

info() {
    [ "$GMS_RT_QUIET" = "1" ] || printf '%sℹ %s%s\n' "$BLUE" "$1" "$NC"
}

diagnostic() {
    printf '%s\n' "$1" >&2
}

_record_api_exit_code() {
    local exit_code="$1"
    if [ -n "${GMS_RT_STATUS_FILE:-}" ]; then
        printf '%s\n' "$exit_code" > "$GMS_RT_STATUS_FILE"
    fi
}

_http_exit_code() {
    local status="$1"
    case "$status" in
        401) printf '%s\n' "$GMS_RT_EXIT_AUTH" ;;
        403) printf '%s\n' "$GMS_RT_EXIT_PERMISSION" ;;
        409|423|429) printf '%s\n' "$GMS_RT_EXIT_CONFLICT" ;;
        000|5??) printf '%s\n' "$GMS_RT_EXIT_NETWORK" ;;
        2??) printf '0\n' ;;
        *) printf '%s\n' "$GMS_RT_EXIT_OPERATION" ;;
    esac
}

_body_from_http_response() {
    local response="$1"
    if [[ "$response" == *$'\nHTTP_STATUS:'* ]]; then
        printf '%s\n' "${response%$'\n'HTTP_STATUS:*}"
    else
        printf '%s\n' "$response"
    fi
}

_status_from_http_response() {
    local response="$1"
    if [[ "$response" == *$'\nHTTP_STATUS:'* ]]; then
        printf '%s\n' "${response##*$'\n'HTTP_STATUS:}"
    else
        printf '000\n'
    fi
}

_ensure_auth_cookie_jar() {
    local cookie_dir
    cookie_dir=$(dirname "$GMS_AUTH_COOKIE_JAR")
    if [ ! -d "$cookie_dir" ]; then
        mkdir -p "$cookie_dir" || {
            error "无法创建认证会话目录: $cookie_dir"
            return 1
        }
    fi
    chmod 700 "$cookie_dir" 2>/dev/null || true
    if [ -f "$GMS_AUTH_COOKIE_JAR" ]; then
        chmod 600 "$GMS_AUTH_COOKIE_JAR" 2>/dev/null || true
    fi
}

# Run a curl request with the active authentication mode and always destroy
# the request-scoped Bearer header before returning, including source mode and
# command substitutions where the top-level EXIT trap does not own the file.
_gms_curl_authenticated() {
    _gms_authenticated_request_auth_failed=0
    if ! _refresh_tls_args; then
        _gms_authenticated_request_auth_failed=1
        return "$GMS_RT_EXIT_AUTH"
    fi
    if [ "${#CURL_AUTH_ARGS[@]}" -gt 0 ] && ! _ensure_auth_cookie_jar; then
        _gms_end_bearer_request
        return "$GMS_RT_EXIT_OPERATION"
    fi
    _gms_with_cookie_lock curl \
        "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" "$@"
    local curl_status=$?
    _gms_end_bearer_request
    return "$curl_status"
}

# Streaming requests must not hold the cookie write lock or rewrite the jar.
_gms_curl_authenticated_readonly() {
    _gms_authenticated_request_auth_failed=0
    if ! _refresh_tls_args; then
        _gms_authenticated_request_auth_failed=1
        return "$GMS_RT_EXIT_AUTH"
    fi
    if [ "${#CURL_AUTH_ARGS[@]}" -gt 0 ] && ! _ensure_auth_cookie_jar; then
        _gms_end_bearer_request
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ "${#CURL_BEARER_ARGS[@]}" -gt 0 ]; then
        curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "$@"
    else
        curl "${CURL_TLS_ARGS[@]}" -b "$GMS_AUTH_COOKIE_JAR" "$@"
    fi
    local curl_status=$?
    _gms_end_bearer_request
    return "$curl_status"
}

_server_host_from_url() {
    # Extract host from URL: strip scheme, then path, then port — single pass
    local url="${1#*://}"  # strip scheme
    echo "${url%%[:/]*}"    # strip first : or / and everything after
}

# Show connection error message to stderr
show_connection_error() {
    local server_host
    server_host=$(_server_host_from_url "$SERVER_URL")
    error "无法连接到服务器 $SERVER_URL"
    error "请检查:"
    error "  1. 服务器是否运行 (systemctl status gms-web-app)"
    error "  2. 网络连通性 (ping $server_host)"
    error "  3. 防火墙设置 (sudo ufw status)"
    error "  4. 服务器日志 (tail -f $GMS_WEB_APP_DIR/fastapi.log)"
}

# Make an authenticated API call.
# Usage: api_call <endpoint> [method] [data] [curl_arg ...]
# The response body is always written to stdout. HTTP and transport failures
# use stable CLI exit codes and never rely on response text matching alone.
api_call() {
    local endpoint="$1"
    local method="${2:-GET}"
    local data="${3:-}"
    shift "$(( $# >= 3 ? 3 : $# ))"
    local extra_args=("$@")
    local response body http_status exit_code curl_exit_code
    # /auth/* 是认证边界本身：它们的 401/403 就是本次认证尝试的结果，
    # 不是“缺会话”信号。统一追加“请先运行 auth-login”会把服务器返回的
    # 真实原因（用户名或密码错误/限流/SSH 不可达）掩盖成误导性提示。
    local is_auth_endpoint=0
    case "$endpoint" in
        /auth/login|/auth/logout|/auth/elevate|/auth/status|/auth/setup) is_auth_endpoint=1 ;;
    esac

    if ! _refresh_tls_args; then
        # GMS_AUTH_TOKEN_FILE 已配置但无效（不可读/权限过松/非本人/为空）。
        # _refresh_tls_args 已清空认证参数并报错；这里禁止发出任何请求，
        # 以 AUTH 退出码失败（修复 Agent Token 失效后静默降级为 Cookie
        # 认证的身份混淆风险）。
        error "Agent 认证不可用: GMS_AUTH_TOKEN_FILE 配置无效，已拒绝发送请求（禁止回退 Cookie）。请重新执行 gms-rt-agent-enroll <CODE> 或修复 token 文件权限" >&2
        _record_api_exit_code "$GMS_RT_EXIT_AUTH"
        return "$GMS_RT_EXIT_AUTH"
    fi
    if [ "${#CURL_AUTH_ARGS[@]}" -gt 0 ] && ! _ensure_auth_cookie_jar; then
        _gms_end_bearer_request
        _record_api_exit_code "$GMS_RT_EXIT_OPERATION"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    # Serialise requests that may write the shared cookie jar so
    # concurrent CLI processes cannot clobber each other's session file.
    if [ "${#extra_args[@]}" -gt 0 ]; then
        response=$(_gms_with_cookie_lock curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
            -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT" \
            -X "$method" "${API_BASE}${endpoint}" "${extra_args[@]}")
    elif [ -n "$data" ] || [ "$method" = "POST" ]; then
        response=$(_gms_with_cookie_lock curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS -X "${method}" "${API_BASE}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            -w $'\nHTTP_STATUS:%{http_code}' \
            --max-time "$CURL_TIMEOUT")
    else
        response=$(_gms_with_cookie_lock curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
            -w $'\nHTTP_STATUS:%{http_code}' \
            -X "$method" "${API_BASE}${endpoint}" --max-time "$CURL_TIMEOUT")
    fi

    curl_exit_code=$?
    # Request-scoped credential teardown: the Bearer header temp file only
    # lives for this HTTP call. This closes the source(function)-mode leak
    # where the file (containing the token) outlived the request with no
    # EXIT trap to clean it.
    _gms_end_bearer_request
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [ "$curl_exit_code" -ne 0 ]; then
        if [ "$curl_exit_code" -eq "$CURL_EXIT_CANNOT_CONNECT" ] || [ "$curl_exit_code" -eq "$CURL_EXIT_OPERATION_TIMEOUT" ]; then
            show_connection_error
        elif [ "$curl_exit_code" -eq "$CURL_EXIT_SSL_CERT" ]; then
            error "HTTPS证书校验失败: $SERVER_URL" >&2
            error "请执行 gms-agent profile list，并 export GMS_RT_PROFILE=<PROFILE> 以加载该 Controller 的 CA" >&2
            error "或显式配置可信 CA: export GMS_CURL_CA_CERT=/path/to/controller-ca.pem" >&2
        else
            error "Failed to get response from server (curl exit code: $curl_exit_code)" >&2
        fi
        [ -z "$body" ] || printf '%s\n' "$body"
        _record_api_exit_code "$GMS_RT_EXIT_NETWORK"
        return "$GMS_RT_EXIT_NETWORK"
    fi

    exit_code=$(_http_exit_code "$http_status")
    _record_api_exit_code "$exit_code"
    printf '%s\n' "$body"
    if [ "$exit_code" -ne 0 ]; then
        case "$exit_code" in
            "$GMS_RT_EXIT_AUTH")
                if [ "$is_auth_endpoint" = "1" ]; then
                    # 登录/提权本身失败：真实原因已在响应 body 里输出到
                    # stdout，这里不重复追加误导性的"请先登录"。
                    :
                elif [ -n "$GMS_AUTH_TOKEN_FILE" ]; then
                    if [ -r "$GMS_AUTH_TOKEN_FILE" ]; then
                        # Bearer 模式仍 401：token 本身失效/被吊销（或 scope
                        # 不够——scope 不足走 PERMISSION 分支）。指引重新
                        # 注册而不是密码登录（agent 上下文禁止密码登录）。
                        diagnostic "需要登录。当前 token ($GMS_AUTH_TOKEN_FILE) 已失效或被吊销, 请重新注册: gms-rt-agent-enroll <CODE>"
                    else
                        diagnostic "需要登录。token 文件不可读: $GMS_AUTH_TOKEN_FILE"
                    fi
                else
                    # 未启用 Bearer 模式（cookie 会话缺失）。service-token
                    # 部署下 agent 永远不该走 auth-login（human-only）。
                    diagnostic "需要登录。未配置 GMS_AUTH_TOKEN_FILE, agent 请注册: gms-rt-agent-enroll <CODE>; 人工会话: gms-rt-auth-login [username]"
                fi
                ;;
            "$GMS_RT_EXIT_PERMISSION")
                if [ "$is_auth_endpoint" = "1" ]; then
                    :
                else
                    # 透传服务端 403 body 里的
                    # scope_required / agent_forbidden，让 agent 能区分
                    # 「缺 scope」（可自查 agent-scopes / 申请新 token）与
                    # 「human-only 端点」（跑 auth-elevate 也没有意义）。
                    local _scope_required _agent_forbidden
                    _scope_required=$(printf '%s' "$body" | jq -r '.detail.scope_required // .scope_required // empty' 2>/dev/null || true)
                    _agent_forbidden=$(printf '%s' "$body" | jq -r '.detail.agent_forbidden // .agent_forbidden // empty' 2>/dev/null || true)
                    if [ -n "$_agent_forbidden" ] && [ "$_agent_forbidden" != "false" ]; then
                        diagnostic "该端点仅限人工会话（agent token 被服务端拒绝）。agent 无法自行提权，请联系管理员评估。"
                    elif [ -n "$_scope_required" ]; then
                        diagnostic "当前 token 缺少 scope '$_scope_required'。scope 目录: GET /api/auth/agent-scopes；请在 Web UI 重新注册并勾选该 scope。"
                    else
                        diagnostic "权限不足或需要管理员提权。请运行: gms-rt-auth-elevate [username]"
                    fi
                fi
                ;;
        esac
        return "$exit_code"
    fi
    return 0
}


# Load only fixed package paths; module names never come from command arguments.
source "$_gms_runtime_dir/cli/auth.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/cluster.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/firmware.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/devices.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/redmine.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/reports.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/test-execution.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/apk.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/system.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }
source "$_gms_runtime_dir/cli/dispatch.sh" || { echo "Error: incomplete Agent CLI runtime" >&2; return 7 2>/dev/null || exit 7; }

# Main command dispatcher
# Only execute when run directly, not when sourced
_is_sourced() {
    if [ -n "$BASH_SOURCE" ]; then
        [[ "${BASH_SOURCE[0]}" != "$0" ]]
    else
        # Fallback for shells without BASH_SOURCE
        case ${0##*/} in
            sh|bash|dash) return 1 ;;
            *) return 0 ;;
        esac
    fi
}



if ! _is_sourced; then
    _gms_terminate_process_tree() {
        local parent_pid="$1" child_pid
        case "$parent_pid" in ''|*[!0-9]*) return 0 ;; esac
        for child_pid in $(ps -o pid= --ppid "$parent_pid" 2>/dev/null); do
            _gms_terminate_process_tree "$child_pid"
        done
        kill -TERM "$parent_pid" 2>/dev/null || true
    }
    _gms_handle_dispatch_signal() {
        local exit_code="$1" dispatch_pid="${_gms_dispatch_pid:-}"
        trap - HUP INT TERM
        [ -z "$dispatch_pid" ] || _gms_terminate_process_tree "$dispatch_pid"
        [ -z "$dispatch_pid" ] || wait "$dispatch_pid" 2>/dev/null || true
        exit "$exit_code"
    }

    trap '_gms_cleanup_bearer_header_file' EXIT
    (
        trap '_gms_cleanup_bearer_header_file' EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        _gms_rt_dispatch "$@"
    ) <&0 &
    _gms_dispatch_pid=$!
    trap '_gms_handle_dispatch_signal 129' HUP
    trap '_gms_handle_dispatch_signal 130' INT
    trap '_gms_handle_dispatch_signal 143' TERM
    wait "$_gms_dispatch_pid"
    _gms_dispatch_status=$?
    _gms_dispatch_pid=""
    exit "$_gms_dispatch_status"
fi
