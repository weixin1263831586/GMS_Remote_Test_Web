#!/bin/bash
set -o pipefail
# ==============================================================================
# GMS Remote Test API Helper Script (FastAPI Port 5001)
# Version: 2026.08.25-1
# ==============================================================================

GMS_RT_VERSION="0.21.1"
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

# Default configuration
# Use environment variable GMS_REMOTE_TEST_SERVER or default to localhost:${GMS_PORT:-5001}
# If running on the server machine itself, use localhost to avoid firewall issues
GMS_PORT="${GMS_PORT:-5001}"
if [ -n "${GMS_REMOTE_TEST_SERVER:-}" ]; then
    SERVER_URL="$GMS_REMOTE_TEST_SERVER"
else
    # gms-agent installs record the Controller URL (and the TLS policy for
    # self-signed deployments) in the profile TOML
    # (~/.config/gms-agent/profiles/<profile>.toml — the single authoritative
    # source). The historical "first *.env wins" glob was removed —
    # with codex-A → Controller A / codex-B → Controller B, a filesystem-glob
    # first-match is not an Agent routing policy and silently picked the
    # wrong Controller. Use `gms-agent profile` / GMS_RT_PROFILE to select.
    _gms_profile_toml=""
    if [ -n "${GMS_RT_PROFILE:-}" ] && [ -n "${HOME:-}" ]; then
        _gms_profile_toml="${XDG_CONFIG_HOME:-${HOME}/.config}/gms-agent/profiles/${GMS_RT_PROFILE}.toml"
    fi
    if [ -n "$_gms_profile_toml" ] && [ -r "$_gms_profile_toml" ]; then
        # Extract controller.url from the [controller] section (flat parser,
        # same mapping as mcp_launcher._read_toml_flat).
        SERVER_URL=$(awk -F'=' '
            /^\[/ { in_controller = ($0 ~ /^\[controller\]/); next }
            in_controller && $1 ~ /^[ \t]*url[ \t]*$/ {
                gsub(/^[ \t]+|[ \t]+$/, "", $2)
                gsub(/^"|"$/, "", $2)
                print $2; exit
            }' "$_gms_profile_toml")
    fi
    SERVER_URL="${SERVER_URL:-${GMS_REMOTE_TEST_SERVER:-}}"
fi

if [ -z "$SERVER_URL" ]; then
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
            GMS_RT_ERROR_SEEN=1
            _gms_bearer_token=""
            return 1
        fi
        _gms_bearer_token=$(tr -d '[:space:]' < "$GMS_AUTH_TOKEN_FILE" 2>/dev/null)
    else
        _gms_bearer_token=""
    fi
    return 0
}
# Recomputed before every curl call (function mode may toggle the token file
# at runtime). Bearer mode ⇒ cookie args emptied entirely; cookie mode ⇒
# standard -b/-c jar args.
CURL_AUTH_ARGS=(-b "$GMS_AUTH_COOKIE_JAR" -c "$GMS_AUTH_COOKIE_JAR")
_gms_apply_credential_mode() {
    if [ -n "$_gms_bearer_token" ]; then
        CURL_AUTH_ARGS=()
    else
        CURL_AUTH_ARGS=(-b "$GMS_AUTH_COOKIE_JAR" -c "$GMS_AUTH_COOKIE_JAR")
    fi
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

# Local deployments commonly use a self-signed HTTPS certificate. Fail
# closed by default; provide GMS_CURL_CA_CERT for a real CA bundle or set
# GMS_CURL_INSECURE=1 only for a controlled self-signed deployment.
# Recomputed before every curl call: when this script is sourced (function
# mode), GMS_CURL_* may be exported after the source line, and a value
# frozen at source time would silently drop --cacert/-k.
_refresh_tls_args() {
    CURL_TLS_ARGS=()
    if [[ "$SERVER_URL" == https://* ]]; then
        if [ -n "${GMS_CURL_CA_CERT:-}" ]; then
            CURL_TLS_ARGS=(--cacert "$GMS_CURL_CA_CERT")
        elif [ "${GMS_CURL_INSECURE:-0}" = "1" ]; then
            CURL_TLS_ARGS=(-k)
        fi
    fi
    # Agent token: pick up the file fresh on every call (callers may export
    # GMS_AUTH_TOKEN_FILE after sourcing this script in function mode).
    # The token must never appear in curl's argv (visible via
    # /proc/<pid>/cmdline to same-user processes on shared build servers).
    # Instead of -H "Authorization: Bearer <token>" we write the header to a
    # 0600 temp file and let curl read it with -H @file.
    CURL_BEARER_ARGS=()
    if _gms_refresh_bearer_token && [ -n "$_gms_bearer_token" ]; then
        if [ -z "$_gms_bearer_header_file" ] || [ ! -f "$_gms_bearer_header_file" ]; then
            _gms_bearer_header_file=$(mktemp "${TMPDIR:-/tmp}/gms-bearer-header.XXXXXX") \
                || { error "无法创建 Bearer header 临时文件"; GMS_RT_ERROR_SEEN=1; return; }
            chmod 600 "$_gms_bearer_header_file"
        fi
        printf 'Authorization: Bearer %s\n' "$_gms_bearer_token" \
            > "$_gms_bearer_header_file"
        CURL_BEARER_ARGS=(-H "@${_gms_bearer_header_file}")
        # Bearer-only mode: never also send the cookie jar.
        CURL_AUTH_ARGS=()
    else
        CURL_BEARER_ARGS=()
        CURL_AUTH_ARGS=(-b "$GMS_AUTH_COOKIE_JAR" -c "$GMS_AUTH_COOKIE_JAR")
        # Credential mode flipped back to cookie: drop any stale header file.
        if [ -n "$_gms_bearer_header_file" ]; then
            rm -f "$_gms_bearer_header_file"
            _gms_bearer_header_file=""
        fi
    fi
}
_refresh_tls_args

_refresh_transport_config() {
    SERVER_URL="${SERVER_URL%/}"
    API_BASE="${SERVER_URL}/api"
    _refresh_tls_args
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

    _refresh_tls_args
    _ensure_auth_cookie_jar || {
        _record_api_exit_code "$GMS_RT_EXIT_OPERATION"
        return "$GMS_RT_EXIT_OPERATION"
    }
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
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [ "$curl_exit_code" -ne 0 ]; then
        if [ "$curl_exit_code" -eq "$CURL_EXIT_CANNOT_CONNECT" ] || [ "$curl_exit_code" -eq "$CURL_EXIT_OPERATION_TIMEOUT" ]; then
            show_connection_error
        elif [ "$curl_exit_code" -eq "$CURL_EXIT_SSL_CERT" ]; then
            error "HTTPS证书校验失败: $SERVER_URL" >&2
            error "本地自签名证书可执行: export GMS_CURL_INSECURE=1；或配置: export GMS_CURL_CA_CERT=/path/to/ca.crt" >&2
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

# ==============================================================================
# Authentication Commands
# ==============================================================================

gms-rt-auth-status() {
    check_jq || return 1
    local response
    response=$(api_call "/auth/status") || return $?
    format_elevated_until "$response" | jq '.'
    # 本地凭据形态对照：token 文件 mtime 让
    # 「MCP 缓存的旧 token vs 新落盘文件」的不一致 10 秒内可见。
    if [ "$GMS_RT_OUTPUT" != "json" ] && [ -n "${GMS_AUTH_TOKEN_FILE:-}" ]; then
        local mtime
        mtime=$(stat -c '%y' "$GMS_AUTH_TOKEN_FILE" 2>/dev/null | cut -d'.' -f1)
        if [ -n "$mtime" ]; then
            info "local token file: $GMS_AUTH_TOKEN_FILE (modified $mtime)"
        else
            warning "local token file: $GMS_AUTH_TOKEN_FILE (not readable)"
        fi
    fi
}

gms-rt-auth-login() {
    local username=""
    local password="${GMS_REMOTE_TEST_PASSWORD:-}"
    local password_stdin=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --password-stdin) password_stdin=1 ;;
            -h|--help)
                printf 'Usage: gms-rt-auth-login [username] [--password-stdin]\n'
                return 0
                ;;
            *)
                [ -z "$username" ] || {
                    error "Unexpected argument: $1"
                    return "$GMS_RT_EXIT_USAGE"
                }
                username="$1"
                ;;
        esac
        shift
    done
    username="${username:-${GMS_REMOTE_TEST_USERNAME:-}}"
    if [ "$password_stdin" = "1" ]; then
        IFS= read -r password || true
    fi
    [ -z "$username" ] && [ "$GMS_RT_NON_INTERACTIVE" != "1" ] && {
        read -r -p "Username: " username
    }
    [ -z "$password" ] && [ "$GMS_RT_NON_INTERACTIVE" != "1" ] && {
        read -r -s -p "Password: " password
        echo
    }
    [ -z "$username" ] && { error "Username is required"; return "$GMS_RT_EXIT_USAGE"; }
    [ -z "$password" ] && {
        error "Password is required in non-interactive mode; use --password-stdin"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1
    _ensure_auth_cookie_jar || return 1

    local data response call_status
    data=$(jq -cn --arg username "$username" --arg password "$password" \
        '{username: $username, password: $password}')
    response=$(api_call "/auth/login" "POST" "$data")
    call_status=$?
    unset password data
    if [ "$call_status" -ne 0 ]; then
        # api_call 已把服务器错误 body 输出到 stdout；这里再以人话给出
        # 真实原因（密码错误/限流/SSH 不可达），不再叠加"请先登录"提示。
        error "Login failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    if echo "$response" | jq -e '.success == true and .authenticated == true' >/dev/null 2>&1; then
        chmod 600 "$GMS_AUTH_COOKIE_JAR" 2>/dev/null || true
        success "Authenticated as $(echo "$response" | jq -r '.user.username // .user.display_name // "unknown"')"
        return 0
    fi
    error "Login failed: $(extract_api_error "$response")"
    return "$GMS_RT_EXIT_AUTH"
}

gms-rt-auth-logout() {
    check_jq || return 1
    local response
    response=$(api_call "/auth/logout" "POST" "{}") || return 1
    if echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        rm -f -- "$GMS_AUTH_COOKIE_JAR"
        success "Logged out"
        return 0
    fi
    error "Logout failed: $(extract_api_error "$response")"
    return 1
}

gms-rt-auth-elevate() {
    local username=""
    local password="${GMS_REMOTE_TEST_ADMIN_PASSWORD:-}"
    local password_stdin=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --password-stdin) password_stdin=1 ;;
            -h|--help)
                printf 'Usage: gms-rt-auth-elevate [admin_username] [--password-stdin]\n'
                return 0
                ;;
            *)
                [ -z "$username" ] || {
                    error "Unexpected argument: $1"
                    return "$GMS_RT_EXIT_USAGE"
                }
                username="$1"
                ;;
        esac
        shift
    done
    username="${username:-${GMS_REMOTE_TEST_ADMIN_USERNAME:-${GMS_REMOTE_TEST_USERNAME:-}}}"
    if [ "$password_stdin" = "1" ]; then
        IFS= read -r password || true
    fi
    [ -z "$username" ] && [ "$GMS_RT_NON_INTERACTIVE" != "1" ] && {
        read -r -p "Admin username: " username
    }
    [ -z "$password" ] && [ "$GMS_RT_NON_INTERACTIVE" != "1" ] && {
        read -r -s -p "Admin password: " password
        echo
    }
    [ -z "$username" ] && { error "Admin username is required"; return "$GMS_RT_EXIT_USAGE"; }
    [ -z "$password" ] && {
        error "Admin password is required in non-interactive mode; use --password-stdin"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1

    local data response call_status
    data=$(jq -cn --arg username "$username" --arg password "$password" \
        '{username: $username, password: $password}')
    response=$(api_call "/auth/elevate" "POST" "$data")
    call_status=$?
    unset password data
    [ "$call_status" -eq 0 ] || return "$call_status"
    if echo "$response" | jq -e '.success == true and .elevated == true' >/dev/null 2>&1; then
        success "Administrator elevation active"
        format_elevated_until "$response" | jq '.'
        return 0
    fi
    error "Elevation failed: $(extract_api_error "$response")"
    return "$GMS_RT_EXIT_PERMISSION"
}

gms-rt-auth-elevation-reset() {
    check_jq || return 1
    local response
    response=$(api_call "/auth/elevation/reset" "POST" "{}") || return $?
    if echo "$response" | jq -e '.success == true and .elevated == false' >/dev/null 2>&1; then
        success "Administrator elevation cleared"
        echo "$response" | jq '.'
        return 0
    fi
    error "Failed to clear elevation: $(extract_api_error "$response")"
    return "$GMS_RT_EXIT_OPERATION"
}

# ==============================================================================
# Agent Service Token commands
# ==============================================================================

# Resolve the token output path for enrollment from a profile TOML's
# `token_file = "..."` line; echoes empty when the profile has no TOML.
_gms_enroll_profile_token_file() {
    local profile="$1"
    local toml="${XDG_CONFIG_HOME:-${HOME}/.config}/gms-agent/profiles/${profile}.toml"
    [ -r "$toml" ] || return 0
    sed -n 's/^[[:space:]]*token_file[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$toml" | head -n 1
}

_gms_enroll_profile_controller_field() {
    local profile="$1" field="$2"
    local toml="${XDG_CONFIG_HOME:-${HOME}/.config}/gms-agent/profiles/${profile}.toml"
    [ -r "$toml" ] || return 0
    awk -F'=' -v wanted="$field" '
        /^\[/ { in_controller = ($0 ~ /^\[controller\]/); next }
        in_controller {
            key = $1
            gsub(/^[ \t]+|[ \t]+$/, "", key)
            if (key == wanted) {
                value = substr($0, index($0, "=") + 1)
                gsub(/^[ \t]+|[ \t]+$/, "", value)
                gsub(/^"|"$/, "", value)
                print value
                exit
            }
        }' "$toml"
}

# Pick the enrollment token destination, profile-aware.
# Order: --out > --profile > explicitly selected
# GMS_RT_PROFILE with a TOML > the single registered profile > legacy
# default path. Multiple profiles without an explicit choice is a usage
# error (fail-closed, mirrors the launcher's profile selection rule).
_gms_enroll_resolve_out_file() {
    local out_flag="$1" profile_flag="$2"
    if [ -n "$out_flag" ]; then
        printf '%s\n' "$out_flag"
        return 0
    fi
    local profiles_root="${XDG_CONFIG_HOME:-${HOME}/.config}/gms-agent/profiles"
    local candidate=""
    if [ -n "$profile_flag" ]; then
        candidate=$(_gms_enroll_profile_token_file "$profile_flag")
        : "${candidate:=${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/${profile_flag}.token}"
        printf '%s\n' "$candidate"
        return 0
    fi
    if [ -n "${GMS_AUTH_TOKEN_FILE:-}" ]; then
        # 调用方显式导出的落点（如 MCP 注册 env）：尊重之。
        printf '%s\n' "$GMS_AUTH_TOKEN_FILE"
        return 0
    fi
    if [ -n "${GMS_RT_PROFILE:-}" ] && [ "${GMS_RT_PROFILE}" != "default" ] \
        && [ -r "${profiles_root}/${GMS_RT_PROFILE}.toml" ]; then
        # 显式导出的 GMS_RT_PROFILE 且确有对应 profile：视为显式选择。
        candidate=$(_gms_enroll_profile_token_file "$GMS_RT_PROFILE")
        : "${candidate:=${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/${GMS_RT_PROFILE}.token}"
        printf '%s\n' "$candidate"
        return 0
    fi
    local registered=()
    if [ -d "$profiles_root" ]; then
        while IFS= read -r toml_path; do
            registered+=("$(basename "$toml_path" .toml)")
        done < <(ls "$profiles_root"/*.toml 2>/dev/null | LC_ALL=C sort)
    fi
    case "${#registered[@]}" in
        1)
            candidate=$(_gms_enroll_profile_token_file "${registered[0]}")
            : "${candidate:=${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/${registered[0]}.token}"
            printf '%s\n' "$candidate"
            ;;
        0)
            printf '%s\n' "${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/${GMS_RT_PROFILE}.token"
            ;;
        *)
            error "Multiple agent profiles registered on this host:"
            local name
            for name in "${registered[@]}"; do
                error "  - $name"
            done
            error "Re-run with --profile <name> (or --out FILE) so the token lands where the caller's MCP registration expects it."
            return "$GMS_RT_EXIT_USAGE"
            ;;
    esac
}

# Exchange a one-shot enrollment code for an Agent Service Token and store it
# as a 0600 file. Run once per build server; afterwards every CLI/MCP call in
# that profile authenticates via GMS_AUTH_TOKEN_FILE with no platform
# password anywhere on the agent path.
gms-rt-agent-enroll() {
    local code="${1:-}"
    local out_flag="" profile_flag=""
    [ -n "$code" ] || {
        error "Usage: gms-rt-agent-enroll <ENROLLMENT_CODE> [--out FILE] [--profile NAME]"
        return "$GMS_RT_EXIT_USAGE"
    }
    shift
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --out)
                shift
                [ "$#" -gt 0 ] && { out_flag="$1"; } || {
                    error "--out requires a path"
                    return "$GMS_RT_EXIT_USAGE"
                }
                ;;
            --out=*) out_flag="${1#*=}" ;;
            --profile)
                shift
                [ "$#" -gt 0 ] && { profile_flag="$1"; } || {
                    error "--profile requires a profile name"
                    return "$GMS_RT_EXIT_USAGE"
                }
                ;;
            --profile=*) profile_flag="${1#*=}" ;;
            *) { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; } ;;
        esac
        shift
    done
    local SERVER_URL="$SERVER_URL" API_BASE="$API_BASE"
    local GMS_CURL_CA_CERT="${GMS_CURL_CA_CERT:-}"
    local GMS_CURL_INSECURE="${GMS_CURL_INSECURE:-0}"
    if [ -n "$profile_flag" ]; then
        [[ "$profile_flag" =~ ^[A-Za-z0-9_.-]+$ ]] || {
            error "Invalid profile name: $profile_flag"
            return "$GMS_RT_EXIT_USAGE"
        }
        local profile_toml profile_server profile_ca profile_insecure env_server
        profile_toml="${XDG_CONFIG_HOME:-${HOME}/.config}/gms-agent/profiles/${profile_flag}.toml"
        [ -r "$profile_toml" ] || {
            error "Profile '$profile_flag' does not exist or is not readable"
            return "$GMS_RT_EXIT_USAGE"
        }
        profile_server=$(_gms_enroll_profile_controller_field "$profile_flag" url)
        [ -n "$profile_server" ] || {
            error "Profile '$profile_flag' has no Controller URL"
            return "$GMS_RT_EXIT_USAGE"
        }
        env_server="${GMS_REMOTE_TEST_SERVER%/}"
        if [ -n "$env_server" ] && [ "$env_server" != "${profile_server%/}" ]; then
            error "GMS_REMOTE_TEST_SERVER and profile '$profile_flag' select different Controllers"
            return "$GMS_RT_EXIT_USAGE"
        fi
        SERVER_URL="${profile_server%/}"
        API_BASE="${SERVER_URL}/api"
        profile_ca=$(_gms_enroll_profile_controller_field "$profile_flag" ca_cert)
        profile_insecure=$(_gms_enroll_profile_controller_field "$profile_flag" insecure)
        GMS_CURL_CA_CERT="$profile_ca"
        if [ "$profile_insecure" = "true" ]; then
            GMS_CURL_INSECURE=1
        else
            GMS_CURL_INSECURE=0
        fi
    fi
    local out_file
    out_file=$(_gms_enroll_resolve_out_file "$out_flag" "$profile_flag") || return "$?"
    [ -n "$out_file" ] || {
        error "Failed to resolve the enrollment token output path"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1
    _refresh_tls_args
    local data response
    data=$(jq -cn --arg code "$code" '{code: $code}')
    # Enrollment is a cookie-free, token-free call by design. The one-shot
    # pairing code is a credential too — push it via stdin, not argv
    # (this applies to any secret that would land in /proc cmdline).
    response=$(curl "${CURL_TLS_ARGS[@]}" -sS -X POST "${API_BASE}/auth/agent-enroll" \
        -H "Content-Type: application/json" --data-binary "@-" \
        -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT" \
        <<< "$data")
    local http_status body
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [[ ! "$http_status" =~ ^2[0-9]{2}$ ]] || ! echo "$body" | jq -e '.success == true' >/dev/null 2>&1; then
        # 三态可区分：服务端 detail.reason 决定
        # 人话提示，避免"已用/已过期/无效"混成一团。
        local reason detail_time
        reason=$(echo "$body" | jq -r '.detail.reason // empty')
        case "$reason" in
            used)
                detail_time=$(iso_to_local_time "$(echo "$body" | jq -r '.detail.used_at // empty')")
                error "Enrollment failed: 配对码已被使用（消耗于 ${detail_time:-未知时间}）。请管理员重新铸造。"
                ;;
            expired)
                detail_time=$(iso_to_local_time "$(echo "$body" | jq -r '.detail.expires_at // empty')")
                error "Enrollment failed: 配对码已过期（过期于 ${detail_time:-未知时间}；默认 TTL 5 分钟）。请管理员重新铸造。"
                ;;
            invalid)
                error "Enrollment failed: 配对码不存在。请核对管理员提供的配对码。"
                ;;
            *)
                error "Enrollment failed: $(extract_api_error "$body")"
                ;;
        esac
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    local token scopes
    token=$(echo "$body" | jq -r '.token.token // empty')
    scopes=$(echo "$body" | jq -r '.token.scopes | join(",")' 2>/dev/null)
    [ -n "$token" ] || { error "Enrollment response missing token"; return "$GMS_RT_EXIT_OPERATION"; }
    local dir
    dir=$(dirname "$out_file")
    mkdir -p "$dir" && chmod 700 "$dir" 2>/dev/null || true
    umask 077
    printf '%s\n' "$token" > "$out_file"
    unset token data code response body
    local used_profile="${profile_flag:-${GMS_RT_PROFILE}}"
    success "Agent token enrolled (0600): $out_file"
    [ -n "$profile_flag" ] && warning "Token written for profile '$profile_flag'; MCP/CLI in other profiles keep their own token files."
    jq -cn --arg file "$out_file" --arg scopes "${scopes:-}" --arg profile "$used_profile" \
        '{ok: true, token_file: $file, mode: "0600", profile: $profile, scopes: ($scopes | split(",") | map(select(length > 0)))}'
}

# Show which credential mode the CLI currently uses (token file or cookie).
gms-rt-auth-credential-mode() {
    check_jq || return 1
    if [ -n "${GMS_AUTH_TOKEN_FILE:-}" ]; then
        if [ -r "$GMS_AUTH_TOKEN_FILE" ]; then
            jq -cn --arg file "$GMS_AUTH_TOKEN_FILE" \
                '{mode: "agent_token", token_file: $file}'
        else
            jq -cn --arg file "$GMS_AUTH_TOKEN_FILE" \
                '{mode: "agent_token", token_file: $file, error: "token file not readable"}'
        fi
    else
        jq -cn --arg jar "$GMS_AUTH_COOKIE_JAR" --arg profile "$GMS_RT_PROFILE" \
            '{mode: "session_cookie", cookie_jar: $jar, profile: $profile}'
    fi
}

# Pre-flight scope check: verify the current
# credential carries the scopes a documented workflow needs BEFORE the first
# 403. Default requirement = the Redmine evidence analysis chain; override
# with --requires s1,s2. Exit code is authoritative for automation.
gms-rt-auth-scopes-check() {
    local requires=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --requires)
                shift
                [ $# -gt 0 ] || { error "--requires requires a comma-separated scope list"; return "$GMS_RT_EXIT_USAGE"; }
                requires="$1"
                ;;
            --requires=*) requires="${1#*=}" ;;
            -h|--help)
                echo "Usage: gms-rt-auth-scopes-check [--requires s1,s2]"
                echo "  Default requirement: redmine.read,artifacts.read_own,apk.analyze_own,sdk.read"
                echo "  Exits with the permission exit code when any required scope is missing."
                return 0
                ;;
            *) { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; } ;;
        esac
        shift
    done
    : "${requires:=redmine.read,artifacts.read_own,apk.analyze_own,sdk.read}"
    check_jq || return 1
    local response call_status
    response=$(api_call "/auth/agent-scopes" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Scope check failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    local result
    result=$(echo "$response" | jq -c --arg requires "$requires" '
        (.granted // []) as $granted
        | ($requires | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0)) | unique) as $required
        | {required: $required, granted: ($granted | unique), missing: ($required - ($granted | unique))}') || {
        error "Failed to parse agent-scopes response"
        return "$GMS_RT_EXIT_OPERATION"
    }
    local missing_count
    missing_count=$(echo "$result" | jq '.missing | length')
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$result" | jq --argjson ok "$( [ "$missing_count" -eq 0 ] && echo true || echo false )" '. + {ok: $ok}'
    else
        echo "required scopes: $(echo "$result" | jq -r '.required | join(", ")')"
        echo "granted scopes:  $(echo "$result" | jq -r '.granted | join(", ")')"
        if [ "$(echo "$result" | jq '.granted | length')" -eq 0 ]; then
            warning "No agent-token scopes visible (human or anonymous session; scope checks apply to agent tokens)"
        fi
        if [ "$missing_count" -eq 0 ]; then
            success "All required scopes granted"
        else
            error "Missing scopes: $(echo "$result" | jq -r '.missing | join(", ")')"
            error "Ask an admin to mint a new enrollment with these scopes: gms-rt-agent-enroll-code --name <NAME> --scopes <list>"
        fi
    fi
    [ "$missing_count" -eq 0 ] || return "$GMS_RT_EXIT_PERMISSION"
    return 0
}

# Create a one-shot approval token (must run under a human session).
gms-rt-approval-create() {
    local tool="" device="" command=""
    local firmware_sha256="" wipe_data="true" burn_mode="auto"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --tool) shift; tool="${1:-}" ;;
            --tool=*) tool="${1#*=}" ;;
            --device) shift; device="${1:-}" ;;
            --device=*) device="${1#*=}" ;;
            --command) shift; command="${1:-}" ;;
            --command=*) command="${1#*=}" ;;
            # burn 审批精确绑定 固件SHA256+wipe_data+burn_mode；
            # 服务端从这些字段派生命令串，--command 对 burn 工具被忽略。
            --firmware-sha256) shift; firmware_sha256="${1:-}" ;;
            --firmware-sha256=*) firmware_sha256="${1#*=}" ;;
            --wipe-data) shift; wipe_data="${1:-true}" ;;
            --wipe-data=*) wipe_data="${1#*=}" ;;
            --burn-mode) shift; burn_mode="${1:-auto}" ;;
            --burn-mode=*) burn_mode="${1#*=}" ;;
            *) { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; } ;;
        esac
        shift
    done
    [ -n "$tool" ] && [ -n "$device" ] || {
        error "Usage: gms-rt-approval-create --tool <gms_rt_tool> --device <serial>[,<serial>...] [--command <command>|--firmware-sha256 <sha256> [--wipe-data true|false] [--burn-mode auto|uf]]"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1
    _refresh_tls_args
    local data response
    data=$(jq -cn \
        --arg tool "$tool" --arg device "$device" --arg command "$command" \
        --arg sha "$firmware_sha256" \
        --argjson wipe "$([ "$wipe_data" = "false" ] && echo false || echo true)" \
        --arg mode "$burn_mode" \
        '{tool: $tool, device: $device, command: $command,
          firmware_sha256: $sha, wipe_data: $wipe, burn_mode: $mode}')
    response=$(curl "${CURL_TLS_ARGS[@]}" -sS -X POST "${API_BASE}/auth/approval-tokens" \
        -H "Content-Type: application/json" -d "$data" \
        -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT")
    local http_status body
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [[ ! "$http_status" =~ ^2[0-9]{2}$ ]] || ! echo "$body" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Approval creation failed: $(extract_api_error "$body")"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    echo "$body" | jq '.approval // .'
}

# List Agent Service Tokens (admin session required). The raw token is
# never stored server-side, so listings only show metadata.
gms-rt-agent-tokens() {
    check_jq || return 1
    _refresh_tls_args
    local response
    response=$(curl "${CURL_TLS_ARGS[@]}" ${CURL_BEARER_ARGS:+"${CURL_BEARER_ARGS[@]}"} \
        "${CURL_AUTH_ARGS[@]}" -sS -X GET "${API_BASE}/auth/agent-tokens" \
        -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT")
    local http_status body
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [[ ! "$http_status" =~ ^2[0-9]{2}$ ]] || ! echo "$body" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Failed to list agent tokens: $(extract_api_error "$body")"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    echo "$body" | jq '.tokens // []'
}

# Mint a one-shot enrollment code (admin + elevation required). Admins run
# this on the Controller host; the build server then runs
# gms-rt-agent-enroll <CODE> once to exchange it for a Service Token.
gms-rt-agent-enroll-code() {
    local name="" scopes="" workers="*" devices="*" expires_days="90" ttl_minutes=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --name) shift; name="${1:-}" ;;
            --name=*) name="${1#*=}" ;;
            --scopes) shift; scopes="${1:-}" ;;
            --scopes=*) scopes="${1#*=}" ;;
            --workers) shift; workers="${1:-}" ;;
            --workers=*) workers="${1#*=}" ;;
            --devices) shift; devices="${1:-}" ;;
            --devices=*) devices="${1#*=}" ;;
            --expires-days) shift; expires_days="${1:-}" ;;
            --expires-days=*) expires_days="${1#*=}" ;;
            # 配对码 TTL（1–30 分钟，默认 5）：手工转录/交接慢的场景可放宽。
            --ttl-minutes) shift; ttl_minutes="${1:-}" ;;
            --ttl-minutes=*) ttl_minutes="${1#*=}" ;;
            *) { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; } ;;
        esac
        shift
    done
    [ -n "$name" ] || {
        error "Usage: gms-rt-agent-enroll-code --name <NAME> [--scopes s1,s2] [--workers w1,w2|*] [--devices d1,d2|*] [--expires-days N] [--ttl-minutes N]"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1
    _refresh_tls_args
    local data response
    data=$(jq -cn --arg name "$name" --arg scopes "$scopes" \
        --arg workers "$workers" --arg devices "$devices" --argjson days "$expires_days" \
        --argjson ttl "${ttl_minutes:-5}" \
        '{name: $name, scopes: ($scopes | split(",") | map(select(length > 0))),
          allowed_workers: $workers, allowed_devices: $devices, expires_days: $days,
          ttl_minutes: $ttl}')
    response=$(curl "${CURL_TLS_ARGS[@]}" ${CURL_BEARER_ARGS:+"${CURL_BEARER_ARGS[@]}"} \
        "${CURL_AUTH_ARGS[@]}" -sS -X POST "${API_BASE}/auth/agent-enrollment-codes" \
        -H "Content-Type: application/json" -d "$data" \
        -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT")
    local http_status body
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [[ ! "$http_status" =~ ^2[0-9]{2}$ ]] || ! echo "$body" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Enrollment code creation failed: $(extract_api_error "$body")"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    # The one-shot code is secret material: print once, never log it twice.
    echo "$body" | jq '.enrollment'
    # 人话补充：绝对过期时刻让"还剩多久"可见。
    local expires_iso
    expires_iso=$(echo "$body" | jq -r '.enrollment.expires_at // empty')
    [ -n "$expires_iso" ] && [ "$GMS_RT_OUTPUT" != "json" ] && {
        info "配对码有效至 $(iso_to_local_time "$expires_iso")（TTL $(echo "$body" | jq -r '.enrollment.ttl_minutes // 5') 分钟，一次性使用）"
    }
    return 0
}

# Revoke an Agent Service Token by id (admin + elevation required).
gms-rt-agent-token-revoke() {
    local token_id="${1:-}"
    [ -n "$token_id" ] || {
        error "Usage: gms-rt-agent-token-revoke <TOKEN_ID>"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1
    _refresh_tls_args
    local response
    response=$(curl "${CURL_TLS_ARGS[@]}" ${CURL_BEARER_ARGS:+"${CURL_BEARER_ARGS[@]}"} \
        "${CURL_AUTH_ARGS[@]}" -sS -X DELETE "${API_BASE}/auth/agent-tokens/$(_urlencode "$token_id")" \
        -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT")
    local http_status body
    body=$(_body_from_http_response "$response")
    http_status=$(_status_from_http_response "$response")
    if [[ ! "$http_status" =~ ^2[0-9]{2}$ ]] || ! echo "$body" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Revoke failed: $(extract_api_error "$body")"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    echo "$body" | jq '{ok: true, revoked: .revoked}'
}

# ==============================================================================
# Cluster worker/device discovery
# ==============================================================================

gms-rt-cluster-workers() {
    check_jq || return 1
    _refresh_tls_args
    api_call "/cluster/workers" | jq '.'
}

# Authoritative cluster-wide device inventory. Lists devices across workers
# (worker_id filter optional); agents should call this before targeting a
# device so worker_id resolution is explicit instead of guessed.
gms-rt-cluster-devices() {
    local worker_id=""
    local query=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --worker) shift; worker_id="${1:-}" ;;
            --worker=*) worker_id="${1#*=}" ;;
            --query) shift; query="${1:-}" ;;
            --query=*) query="${1#*=}" ;;
            *) { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; } ;;
        esac
        shift
    done
    check_jq || return 1
    local response
    if [ -n "$worker_id" ]; then
        response=$(api_call "/cluster/devices?worker_id=$(_urlencode "$worker_id")")
    else
        response=$(api_call "/cluster/devices")
    fi
    if [ -n "$query" ]; then
        echo "$response" | jq --arg q "$query" '[.devices[]? | select((.serial // .device_id // "") | test($q; "i"))]'
    else
        echo "$response" | jq '.'
    fi
}

# Resolve a device serial to its owning worker via the cluster inventory.
# Fails (exit 5) when the serial matches zero or several workers and no
# explicit --worker was given — never guess "the first worker".
gms-rt-cluster-resolve() {
    local device="" worker_id=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --device) shift; device="${1:-}" ;;
            --device=*) device="${1#*=}" ;;
            --worker) shift; worker_id="${1:-}" ;;
            --worker=*) worker_id="${1#*=}" ;;
            *) { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; } ;;
        esac
        shift
    done
    [ -n "$device" ] || {
        error "Usage: gms-rt-cluster-resolve --device <serial> [--worker <worker_id>]"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return 1
    if [ -n "$worker_id" ]; then
        jq -cn --arg worker "$worker_id" --arg device "$device" \
            '{worker_id: $worker, device: $device, resolved: true, explicit: true}'
        return 0
    fi
    local response matches
    response=$(gms-rt-cluster-devices) || return $?
    matches=$(echo "$response" | jq -c --arg d "$device" \
        '[.devices[]? | select((.serial // .device_id // "") == $d) | {worker_id: (.worker_id // .worker // "")}]')
    local count
    count=$(echo "$matches" | jq 'length')
    case "$count" in
        1)
            local resolved
            resolved=$(echo "$matches" | jq -r '.[0].worker_id')
            jq -cn --arg worker "$resolved" --arg device "$device" \
                '{worker_id: $worker, device: $device, resolved: true, explicit: false}'
            ;;
        0)
            error "Device '$device' not found in cluster inventory"
            return "$GMS_RT_EXIT_CONFLICT"
            ;;
        *)
            error "Device '$device' matches $count workers; pass --worker explicitly"
            echo "$matches" >&2
            return "$GMS_RT_EXIT_CONFLICT"
            ;;
    esac
}

# Extract error message from API response.
# FastAPI HTTPException detail can be an object (e.g. scope errors carry
# {message, scope_required}); surface the server-side detail instead of
# collapsing it to "Unknown error".
extract_api_error() {
    local response="$1"
    echo "$response" | jq -r '
        if (.detail | type) == "object" then
            (.detail.message // .detail.msg // "Request failed")
            + (if .detail.scope_required then " (missing scope: \(.detail.scope_required); check gms-rt-auth-scopes-check)" else "" end)
            + (if .detail.agent_forbidden then " (agent tokens cannot call this endpoint)" else "" end)
        else
            (.detail // .error // .message // "Unknown error")
        end' 2>/dev/null || echo "Unknown error"
}

# Render an ISO-8601 UTC timestamp in the local timezone for human reading;
# falls back to the original string when the conversion is not supported.
iso_to_local_time() {
    local iso="$1"
    if [ -n "$iso" ] && [ "$iso" != "null" ]; then
        date -d "$iso" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null && return 0
    fi
    printf '%s' "$iso"
}

# Rewrite .elevated_until (when present) as a local-time string for display.
format_elevated_until() {
    local response="$1" iso until_local
    iso=$(echo "$response" | jq -r '.elevated_until // empty')
    if [ -n "$iso" ]; then
        until_local=$(iso_to_local_time "$iso")
        echo "$response" | jq --arg until "$until_local" '.elevated_until = $until'
    else
        echo "$response"
    fi
}

# Check HTTP response status and extract body
# Returns: body via stdout, status via HTTP_STATUS_CODE variable
# Usage: body=$(check_http_response "$response") && echo "Success: $body"
check_http_response() {
    local response="$1"
    HTTP_STATUS_CODE=$(_status_from_http_response "$response")
    local body
    body=$(_body_from_http_response "$response")
    if [[ ! "$HTTP_STATUS_CODE" =~ ^2[0-9]{2}$ ]]; then
        local exit_code
        exit_code=$(_http_exit_code "$HTTP_STATUS_CODE")
        _record_api_exit_code "$exit_code"
        printf '%s\n' "$body"
        return "$exit_code"
    fi
    _record_api_exit_code 0
    printf '%s\n' "$body"
    return 0
}

# Check if jq is installed
check_jq() {
    if ! command -v jq &> /dev/null; then
        error "jq is required but not installed. Please install: sudo apt-get install jq"
        return 1
    fi
}

# Internal: check if current machine is the test host (has local adb access)
_is_test_host() {
    local server_host=$(_server_host_from_url "$SERVER_URL")
    local local_ips=$(hostname -I 2>/dev/null)
    echo "$local_ips" | grep -q "$server_host" || [ "$server_host" = "localhost" ] || [ "$server_host" = "127.0.0.1" ]
}

# Internal: resolve SSH host/user/port for test host
# Outputs: "host user port" (space-separated)
_resolve_ssh_host() {
    local host=$(_server_host_from_url "$SERVER_URL")
    local user="$DEFAULT_SSH_USER"
    local port="22"
    local config_files=(
        "${GMS_WEB_APP_DIR}/configs/config.json"
        "${HOME}/GMS_Remote_Test/web_app/configs/config.json"
    )
    for config_file in "${config_files[@]}"; do
        if [ -f "$config_file" ]; then
            local cfg_host=$(jq -r '.ubuntu_host // empty' "$config_file" 2>/dev/null)
            local cfg_user=$(jq -r '.ubuntu_user // empty' "$config_file" 2>/dev/null)
            local cfg_port=$(jq -r '.ssh_port // .ubuntu_port // empty' "$config_file" 2>/dev/null)
            [ -n "$cfg_user" ] && user="$cfg_user"
            [ -n "$cfg_port" ] && port="$cfg_port"
            [ -n "$cfg_host" ] && host="$cfg_host" && break
        fi
    done

    if [ "$host" = "$(_server_host_from_url "$SERVER_URL")" ] && command -v jq &> /dev/null; then
        local api_config=$(api_call "/config/read" 2>/dev/null)
        if [ -n "$api_config" ]; then
            local api_host=$(echo "$api_config" | jq -r '.ubuntu_host // empty' 2>/dev/null)
            local api_user=$(echo "$api_config" | jq -r '.ubuntu_user // empty' 2>/dev/null)
            [ -n "$api_host" ] && host="$api_host"
            [ -n "$api_user" ] && user="$api_user"
        fi
    fi

    host="${GMS_BURN_SSH_HOST:-$host}"
    user="${GMS_BURN_SSH_USER:-$user}"
    port="${GMS_BURN_SSH_PORT:-$port}"
    echo "$host $user $port"
}

_shell_quote() {
    printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

_urlencode() {
    jq -rn --arg value "$1" '$value | @uri'
}

_secret_value() {
    local value="${1:-}"
    if [ "$value" = "-" ]; then
        IFS= read -r value || true
    fi
    printf '%s' "$value"
}

_post_firmware_burn_path() {
    local remote_path="$1"
    local devices="$2"
    local wipe_data="$3"
    local device_list
    device_list=$(echo "$devices" | tr ' ' ',')

    _refresh_tls_args
    _ensure_auth_cookie_jar || return 1
    local approval_query=""
    if [ -n "${GMS_RT_BURN_APPROVAL_TOKEN:-}" ]; then
        approval_query="?approval_token=$(_urlencode "$GMS_RT_BURN_APPROVAL_TOKEN")"
    fi
    curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS -w "\nHTTP_STATUS:%{http_code}" \
        --max-time "$CURL_BURN_TIMEOUT" \
        -X POST "${API_BASE}/burn/firmware${approval_query}" \
        -F "firmware_path=${remote_path}" \
        -F "devices=${device_list}" \
        -F "wipe_data=${wipe_data}"
}

_post_firmware_burn_upload() {
    local firmware_path="$1"
    local devices="$2"
    local wipe_data="$3"
    local device_list
    local device_query
    device_list=$(echo "$devices" | tr ' ' ',')
    device_query=$(_urlencode "$device_list")
    local approval_query=""
    if [ -n "${GMS_RT_BURN_APPROVAL_TOKEN:-}" ]; then
        approval_query="&approval_token=$(_urlencode "$GMS_RT_BURN_APPROVAL_TOKEN")"
    fi

    _refresh_tls_args
    _ensure_auth_cookie_jar || return 1
    curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -# -o /dev/stdout -w "\nHTTP_STATUS:%{http_code}" \
        --max-time "$CURL_BURN_TIMEOUT" \
        -X POST "${API_BASE}/burn/firmware?devices=${device_query}${approval_query}" \
        -F "firmware_file=@${firmware_path}" \
        -F "firmware_path=$(basename "$firmware_path")" \
        -F "wipe_data=${wipe_data}"
}

_copy_firmware_to_test_host() {
    local firmware_path="$1"
    local remote_dir="$2"
    local ssh_host="$3"
    local ssh_user="$4"
    local ssh_port="$5"
    local remote_path="${remote_dir%/}/$(basename "$firmware_path")"
    local quoted_remote_dir
    local ssh_options=(-p "$ssh_port")
    local scp_options=(-P "$ssh_port")
    local rsync_ssh="ssh -p ${ssh_port}"
    local connect_timeout="${GMS_SSH_CONNECT_TIMEOUT:-10}"
    if ! [[ "$ssh_port" =~ ^[1-9][0-9]*$ ]] || [ "$ssh_port" -gt 65535 ]; then
        error "Invalid test-host SSH port: $ssh_port"
        return "$GMS_RT_EXIT_USAGE"
    fi
    if ! [[ "$connect_timeout" =~ ^[1-9][0-9]*$ ]] || [ "$connect_timeout" -gt 300 ]; then
        error "GMS_SSH_CONNECT_TIMEOUT must be between 1 and 300 seconds"
        return "$GMS_RT_EXIT_USAGE"
    fi
    if [ "$GMS_RT_NON_INTERACTIVE" = "1" ]; then
        ssh_options+=(
            -o BatchMode=yes
            -o StrictHostKeyChecking=yes
            -o "ConnectTimeout=${connect_timeout}"
        )
        scp_options+=(
            -o BatchMode=yes
            -o StrictHostKeyChecking=yes
            -o "ConnectTimeout=${connect_timeout}"
        )
        rsync_ssh+=" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=${connect_timeout}"
    fi
    quoted_remote_dir=$(_shell_quote "$remote_dir")

    info "Preparing remote firmware directory: ${ssh_user}@${ssh_host}:${remote_dir}" >&2
    ssh "${ssh_options[@]}" "${ssh_user}@${ssh_host}" "mkdir -p ${quoted_remote_dir}" >/dev/null || return 1

    if command -v rsync &> /dev/null; then
        info "Transferring firmware with rsync delta/partial support..." >&2
        rsync -a --partial --inplace --info=progress2 -s \
            -e "$rsync_ssh" \
            "$firmware_path" "${ssh_user}@${ssh_host}:${remote_dir%/}/" >&2 || return 1
    else
        warning "rsync not found; falling back to scp. Install rsync for faster repeat transfers." >&2
        scp "${scp_options[@]}" -p "$firmware_path" "${ssh_user}@${ssh_host}:${remote_dir%/}/" >&2 || return 1
    fi

    echo "$remote_path"
}

# ==============================================================================
# Device Management Commands
# ==============================================================================

# Convert device input to JSON array format and wrap in devices object
# Supports: JSON array ["dev1","dev2"], space-separated list, or single device
# Returns: {"devices":[...]} JSON object
build_devices_json_data() {
    local devices="$1"
    if [[ "$devices" == \[* ]]; then
        jq -cn --argjson devices "$devices" '{devices: $devices}'
    else
        jq -R -c '{devices: (split(" ") | map(select(length > 0)))}' <<< "$devices"
    fi
}

# Convert device input to JSON array format only
# Supports: JSON array ["dev1","dev2"], space-separated list, or single device
# Returns: [...] JSON array
convert_devices_to_json() {
    local devices="$1"
    if [[ "$devices" == \[* ]]; then
        jq -cn --argjson devices "$devices" '$devices'
    else
        # Convert space-separated list to JSON array
        echo "$devices" | jq -R -c 'split(" ") | map(select(length>0))'
    fi
}

# Resolve device input against the live device inventory. Exact IDs pass
# through untouched; otherwise a unique serial prefix is expanded (e.g.
# RK3572 -> RK3572GMS4). Ambiguous or unresolved input is returned unchanged
# so the server reports the authoritative error. Output: space-separated IDs.
_resolve_devices() {
    local devices="$1"
    if ! command -v jq >/dev/null 2>&1; then
        printf '%s\n' "$devices"
        return 0
    fi
    local requested response resolved
    requested=$(convert_devices_to_json "$devices") || { printf '%s\n' "$devices"; return 0; }
    response=$(api_call "/devices/list?force_refresh=true" 2>/dev/null) || { printf '%s\n' "$devices"; return 0; }
    resolved=$(jq -cn --argjson requested "$requested" --argjson inventory "$response" '
        ($inventory | if type == "array" then map(.device_id // empty) else [] end) as $ids |
        [($requested[] | tostring) as $raw |
            if ($ids | index($raw)) then
                {id: $raw, rewritten: false}
            else
                ($ids | map(select(startswith($raw)))) as $matches |
                if ($matches | length) == 1 then
                    {id: $matches[0], rewritten: true}
                else
                    {id: $raw, rewritten: false}
                end
            end
        ] |
        if any(.[]; .rewritten == true) then
            {
                devices: (map(.id) | join(" ")),
                rewrites: (map(select(.rewritten == true)) | map(.id) | join(" "))
            }
        else
            {devices: (map(.id) | join(" ")), rewrites: ""}
        end') || { printf '%s\n' "$devices"; return 0; }
    local rewrites
    rewrites=$(printf '%s' "$resolved" | jq -r '.rewrites')
    if [ -n "$rewrites" ]; then
        info "Resolved device prefix to: $rewrites" >&2
    fi
    printf '%s' "$resolved" | jq -r '.devices'
}

# Resolve a short suite name (e.g. android-cts-17_r1) to its tools path via
# /api/test/suites. Prints the tools path on success; on any failure prints
# nothing and returns non-zero so callers can fall back to the raw value
# (the server also resolves short names and reports a precise error).
_resolve_suite_reference() {
    local reference="$1"
    check_jq >/dev/null 2>&1 || return 1
    local response matches
    response=$(api_call "/test/suites" 2>/dev/null) || return 1
    matches=$(printf '%s' "$response" | jq -r --arg q "$reference" '
        [(.suites // [])[] |
            select(
                (.version // "") == $q
                or ((.tools_path // "") | endswith("/" + $q))
                or (((.version // "") | ascii_downcase) | contains($q | ascii_downcase))
            )]
        | unique_by(.tools_path // .full_path // .version)') || return 1
    local count
    count=$(printf '%s' "$matches" | jq 'length')
    if [ "$count" -eq 1 ]; then
        local path
        path=$(printf '%s' "$matches" | jq -r '.[0].tools_path // empty')
        if [ -n "$path" ]; then
            printf '%s\n' "$path"
            return 0
        fi
    fi
    return 1
}

# ==============================================================================
# ADB Proxy Commands
# ==============================================================================

gms-rt-adb-forward-status() {
    check_jq
    local response
    response=$(api_call "/adb-forward/status")
    echo "$response" | jq '.'
}

# Connect selected source devices to a target Worker.
gms-rt-adb-forward-start() {
    local source_worker_id="${1:-}"
    local target_worker_id="${2:-}"
    if [[ -z "$source_worker_id" || -z "$target_worker_id" || $# -lt 3 ]]; then
        error "Usage: gms-rt-adb-forward-start <source_worker_id> <target_worker_id> <serial> [serial...]"
        return 2
    fi
    shift 2
    check_jq
    echo "🔌 Connecting ADB devices through adbproxy-rs..."
    local devices data response
    devices=$(printf '%s\n' "$@" | jq -Rsc 'split("\n")[:-1]')
    data=$(jq -cn \
        --arg source_worker_id "$source_worker_id" \
        --arg target_worker_id "$target_worker_id" \
        --argjson devices "$devices" \
        '{
            source_worker_id: $source_worker_id,
            target_worker_id: $target_worker_id,
            devices: $devices
        }')
    response=$(api_call "/adb-forward/start" "POST" "$data")
    echo "$response" | jq '.'
}

# Disconnect one source-to-target assignment.
gms-rt-adb-forward-stop() {
    local source_worker_id="${1:-}"
    local target_worker_id="${2:-}"
    if [[ -z "$source_worker_id" || -z "$target_worker_id" ]]; then
        error "Usage: gms-rt-adb-forward-stop <source_worker_id> <target_worker_id>"
        return 2
    fi
    check_jq
    echo "🛑 Disconnecting ADB Proxy assignment..."
    local data
    data=$(jq -cn \
        --arg source_worker_id "$source_worker_id" \
        --arg target_worker_id "$target_worker_id" \
        '{
            source_worker_id: $source_worker_id,
            target_worker_id: $target_worker_id
        }')
    local response
    response=$(api_call "/adb-forward/stop" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Burn Commands
# ==============================================================================

# Wait for devices to come back online after a burn, then return the wait status.
_wait_devices_online_after_burn() {
    local devices="$1"
    local max_wait="$2"
    echo "⏳ Waiting for devices to come back online (max ${max_wait}s)..."
    local wait_args=("$devices" --state online --max-wait "$max_wait")
    gms-rt-devices-wait "${wait_args[@]}"
}

# Burn firmware image to devices
gms-rt-burn-firmware() {
    local firmware_path="$1"
    local devices="$2"
    local wipe_data="${3:-true}"
    local upload_mode="${GMS_BURN_UPLOAD_MODE:-auto}"
    local wait_online=0
    local wait_max="${GMS_BURN_WAIT_ONLINE_MAX:-600}"
    local approval_token=""

    local positional=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --wait-online) wait_online=1 ;;
            --wait-online=*)
                wait_online=1
                wait_max="${1#*=}"
                ;;
            --approval-token)
                shift
                [ "$#" -gt 0 ] && { approval_token="$1"; } || {
                    error "--approval-token requires a token"
                    return "$GMS_RT_EXIT_USAGE"
                }
                ;;
            --approval-token=*) approval_token="${1#*=}" ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    set -- "${positional[@]}"
    firmware_path="$1"
    devices="$2"
    wipe_data="${3:-true}"
    if [ "$wait_online" = "1" ] && ! [[ "$wait_max" =~ ^[1-9][0-9]*$ ]]; then
        error "--wait-online requires a positive maximum wait in seconds"
        return "$GMS_RT_EXIT_USAGE"
    fi

    [ -z "$firmware_path" ] && { error "Firmware path required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--approval-token TOKEN] [--wait-online[=SECONDS]]"; return 1; }
    [ -z "$devices" ] && { error "Devices required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--approval-token TOKEN] [--wait-online[=SECONDS]]"; return 1; }
    [ ! -f "$firmware_path" ] && { error "Firmware file not found: $firmware_path"; return 1; }
    check_jq || return 1
    devices=$(_resolve_devices "$devices")
    # Agent token sessions burn only with a one-shot approval; human
    # admin sessions may omit it. Forwarded to the server via env.
    GMS_RT_BURN_APPROVAL_TOKEN="$approval_token"
    export GMS_RT_BURN_APPROVAL_TOKEN

    echo "🔥 Burning firmware: $firmware_path to devices: $devices..."
    local response=""

    if [ "$upload_mode" != "http" ]; then
        local ssh_host ssh_user ssh_port remote_dir remote_path
        read -r ssh_host ssh_user ssh_port <<< "$(_resolve_ssh_host)"
        remote_dir="${GMS_BURN_REMOTE_DIR:-/home/${ssh_user}/GMS-Suite}"

        echo "🚀 Direct mode: ${ssh_user}@${ssh_host}:${remote_dir}"
        if remote_path=$(_copy_firmware_to_test_host "$firmware_path" "$remote_dir" "$ssh_host" "$ssh_user" "$ssh_port"); then
            echo "🔥 Starting burn using remote firmware path: $remote_path"
            response=$(_post_firmware_burn_path "$remote_path" "$devices" "$wipe_data")
        elif [ "$upload_mode" = "direct" ]; then
            error "Direct firmware transfer failed"
            return 1
        else
            warning "Direct transfer failed; falling back to HTTP upload"
        fi
    fi

    if [ -z "$response" ]; then
        echo "⏳ Uploading firmware through API (slower fallback)..."

        # Get terminal width for progress bars
        local term_width=${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}
        local bar_width=$((term_width * 60 / 100))

        # Set COLUMNS for curl progress bar width (60% of terminal)
        export COLUMNS=$bar_width
        response=$(_post_firmware_burn_upload "$firmware_path" "$devices" "$wipe_data")
        unset COLUMNS
    fi

    local body http_status check_status
    http_status=$(_status_from_http_response "$response")
    body=$(check_http_response "$response")
    check_status=$?
    if [ "$check_status" -ne 0 ]; then
        error "Firmware burn failed - HTTP status: $http_status"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return "$check_status"
    fi

    # Check if response contains success field
    if echo "$body" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "Firmware burn completed successfully"
        echo "$body" | jq '.'
        if [ "$wait_online" = "1" ]; then
            _wait_devices_online_after_burn "$devices" "$wait_max"
            return $?
        fi
    else
        error "Firmware burn failed - API returned error"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return 1
    fi
}

# Burn GSI image to devices
gms-rt-burn-gsi() {
    local gsi_path="${1:-}"
    local devices="${2:-}"
    local wipe_data="${3:-true}"
    local wait_online=0
    local wait_max="${GMS_BURN_WAIT_ONLINE_MAX:-600}"

    local positional=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --wait-online) wait_online=1 ;;
            --wait-online=*)
                wait_online=1
                wait_max="${1#*=}"
                ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    set -- "${positional[@]}"
    gsi_path="${1:-}"
    devices="${2:-}"
    wipe_data="${3:-true}"
    if [ "$wait_online" = "1" ] && ! [[ "$wait_max" =~ ^[1-9][0-9]*$ ]]; then
        error "--wait-online requires a positive maximum wait in seconds"
        return "$GMS_RT_EXIT_USAGE"
    fi

    [ -z "$gsi_path" ] && { error "GSI path required. Usage: gms-rt-burn-gsi <gsi_path> <devices> [wipe_data] [--wait-online[=SECONDS]]"; return "$GMS_RT_EXIT_USAGE"; }
    [ -z "$devices" ] && { error "Devices required. Usage: gms-rt-burn-gsi <gsi_path> <devices> [wipe_data] [--wait-online[=SECONDS]]"; return "$GMS_RT_EXIT_USAGE"; }
    [ ! -f "$gsi_path" ] && { error "GSI file not found: $gsi_path"; return "$GMS_RT_EXIT_USAGE"; }

    check_jq || return "$GMS_RT_EXIT_OPERATION"
    devices=$(_resolve_devices "$devices")
    echo "🔥 Burning GSI: $gsi_path to devices: $devices..."

    local absolute_path remote_path
    absolute_path=$(realpath "$gsi_path")
    remote_path="$absolute_path"
    if ! _is_test_host; then
        local ssh_host ssh_user ssh_port remote_dir
        read -r ssh_host ssh_user ssh_port <<< "$(_resolve_ssh_host)"
        remote_dir="${GMS_BURN_REMOTE_DIR:-/home/${ssh_user}/GMS-Suite}"
        info "Transferring GSI to the test host before burn..."
        remote_path=$(_copy_firmware_to_test_host \
            "$absolute_path" "$remote_dir" "$ssh_host" "$ssh_user" "$ssh_port") || {
            error "GSI transfer to the test host failed; this endpoint has no HTTP upload fallback"
            return "$GMS_RT_EXIT_NETWORK"
        }
    fi

    # The Controller uploads its trusted runner and misc.img. script_path is a
    # required legacy request field, not a client-controlled execution path.
    local json_payload
    json_payload=$(jq -n \
        --arg system_img "$remote_path" \
        --arg script_path "controller-managed" \
        --argjson devices "$(convert_devices_to_json "$devices")" \
        '{system_img: $system_img, script_path: $script_path, devices: $devices}')

    local body
    body=$(api_call "/burn/gsi" "POST" "$json_payload") || {
        local call_status=$?
        error "GSI burn request failed"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return "$call_status"
    }

    if echo "$body" | jq -e '.success' > /dev/null; then
        success "GSI burn completed successfully"
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$body" | jq '.'
        else
            echo ""
            echo "$body" | jq -r '.results[]? | "📱 \(.device): ✅ Success"' 2>/dev/null
            echo ""
            echo "📋 Detailed output:"
            echo "$body" | jq -r '.results[]? | .output' 2>/dev/null | head -20
            echo "..."
            echo "(Use --json for the full response)"
        fi
        if [ "$wait_online" = "1" ]; then
            _wait_devices_online_after_burn "$devices" "$wait_max"
            return $?
        fi
    else
        error "GSI burn failed - API returned error"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return 1
    fi
}

# Burn serial number to device
gms-rt-burn-serial() {
    local device_id="$1"
    local serial="$2"
    [ -z "$device_id" ] && { error "Device ID required. Usage: gms-rt-burn-serial <device_id> <serial>"; return 1; }
    [ -z "$serial" ] && { error "Serial required. Usage: gms-rt-burn-serial <device_id> <serial>"; return 1; }
    check_jq
    echo "🔥 Burning serial $serial to $device_id..."
    local data
    data=$(jq -cn --arg device_id "$device_id" --arg serial "$serial" \
        '{device_id: $device_id, serial: $serial}')
    local response=$(api_call "/burn/serial" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Serial burned successfully"
        echo "$response" | jq '.'
    else
        error "Failed to burn serial"
    fi
}

# ==============================================================================
# Configuration Commands
# ==============================================================================

# Read configuration
gms-rt-config-read() {
    check_jq
    echo "📖 Reading configuration..."
    api_call "/config/read" | jq '.'
}

# Update configuration
gms-rt-config-update() {
    local key="$1"
    local value="$2"
    [ -z "$key" ] && { error "Key required. Usage: gms-rt-config-update <key> <value>"; return 1; }
    check_jq
    echo "⚙️  Updating configuration: $key = $value"
    local data
    data=$(jq -cn --arg key "$key" --arg value "$value" '{($key): $value}')
    local response=$(api_call "/config/update" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Configuration updated"
    else
        local error_msg=$(extract_api_error "$response")
        error "Failed to update configuration: $error_msg"
        return 1
    fi
}


# ==============================================================================
# Desktop VNC Commands
# ==============================================================================

# Validate desktop host connection
gms-rt-desktop-validate() {
    local host="$1"
    [ -z "$host" ] && { error "Host required. Usage: gms-rt-desktop-validate <user@ip>"; return 1; }
    check_jq
    echo "🔍 Validating desktop host $host..."
    local data
    data=$(jq -cn --arg host "$host" '{host: $host}')
    local response=$(api_call "/desktop/validate" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Desktop host is valid"
        echo "$response" | jq '.'
    else
        local error_msg=$(extract_api_error "$response")
        error "Desktop host validation failed: $error_msg"
        return 1
    fi
}

# Start VNC server on remote desktop
gms-rt-desktop-vnc-start() {
    local host="${1:-}"
    local password
    local vnc_password
    password=$(_secret_value "${2:-${GMS_REMOTE_DEVICE_PASSWORD:-}}")
    vnc_password=$(_secret_value "${3:-${GMS_REMOTE_VNC_PASSWORD:-}}")
    check_jq
    echo "🚀 Starting desktop VNC..."
    local data
    data=$(jq -cn \
        --arg host "$host" \
        --arg password "$password" \
        --arg vnc_password "$vnc_password" \
        '{
            host: $host,
            password: $password,
            vnc_password: $vnc_password
        } | with_entries(select(.value != ""))')
    local response=$(api_call "/desktop/vnc/start" "POST" "$data")
    echo "$response" | jq '.'
}

# Get VNC server status
gms-rt-desktop-vnc-status() {
    check_jq
    echo "🖥️ Getting VNC status..."
    api_call "/desktop/vnc/status" | jq '.'
}

# Stop VNC server
gms-rt-desktop-vnc-stop() {
    check_jq
    echo "🛑 Stopping desktop VNC..."
    local response=$(api_call "/desktop/vnc/stop" "POST" "{}")
    echo "$response" | jq '.'
}

# ==============================================================================
# Device Commands
# ==============================================================================

# Lock bootloader
gms-rt-devices-bootloader-lock() {
    local devices="$*"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-bootloader-lock DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "🔒 锁定Bootloader..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/bootloader-lock" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Bootloader锁定成功"

        # 美化输出格式
        local count=$(echo "$response" | jq -r '.data.summary.total // 0')
        local success=$(echo "$response" | jq -r '.data.summary.success // 0')
        local failed=$(echo "$response" | jq -r '.data.summary.failed // 0')

        echo "📊 操作统计: 成功 $success 台, 失败 $failed 台"
        echo ""

        # 显示每个设备的详细结果
        echo "$response" | jq -r '.data.results[]? | "📱 \(.device // .device_id): \(.output // .message // "完成")"' 2>/dev/null || echo "$response" | jq '.'
    else
        error "Bootloader锁定失败"
        echo "$response" | jq '.'
    fi
}

# Unlock bootloader
gms-rt-devices-bootloader-unlock() {
    local devices="$*"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-bootloader-unlock DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "🔓 解锁Bootloader..."

    local data=$(build_devices_json_data "$devices")
    local response=$(api_call "/devices/bootloader-unlock" "POST" "$data")

    # 检查响应是否有效
    if [ -z "$response" ]; then
        error "API 无响应"
        return 1
    fi

    # 尝试解析 JSON，如果失败则显示原始响应
    if echo "$response" | jq -e '.' > /dev/null 2>&1; then
        if echo "$response" | jq -e '.success' > /dev/null; then
            success "Bootloader解锁成功"

            # 美化输出格式
            local count=$(echo "$response" | jq -r '.data.summary.total // 0')
            local success_count=$(echo "$response" | jq -r '.data.summary.success // 0')
            local failed=$(echo "$response" | jq -r '.data.summary.failed // 0')

            echo "📊 操作统计: 成功 $success_count 台, 失败 $failed 台"
            echo ""

            # 显示每个设备的详细结果
            echo "$response" | jq -r '.data.results[]? | "📱 \(.device // .device_id): \(.output // .message // "完成")"' 2>/dev/null || echo "$response" | jq '.'
        else
            local error_msg=$(echo "$response" | jq -r '.error // .message // .detail // "未知错误"')
            error "Bootloader解锁失败: $error_msg"
            echo "📋 响应详情:"
            echo "$response" | jq '.' 2>/dev/null || echo "$response"
        fi
    else
        error "Bootloader解锁失败: 无效的JSON响应"
        echo "📋 原始响应:"
        echo "$response"
    fi
}

# Check bootloader status
gms-rt-devices-bootloader-status() {
    local devices="$*"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-bootloader-status DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "🔐 检查Bootloader状态..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/bootloader-status" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Bootloader status retrieved"
        echo "$response" | jq '.'
    else
        error "Failed to check bootloader status"
    fi
}

# Get device details
gms-rt-devices-info() {
    local devices="$*"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-info DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "📱 获取设备信息..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/info" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "设备信息获取成功"
        echo "$response" | jq '.'
    else
        error "设备信息获取失败"
    fi
}

# List devices
gms-rt-devices-list() {
    check_jq
    echo "📱 Listing devices..."
    api_call "/devices/list" | jq '.'
}

# List Controller serial ports, or read the retained log for one port.
gms-rt-devices-console() {
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    local port_key=""
    local tail_lines=500
    local log_date=""
    local log_options=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                printf 'Usage: gms-rt-devices-console [port_key] [--tail 1..10000] [--date YYYYMMDD]\n'
                return 0
                ;;
            --tail)
                shift
                [ "$#" -gt 0 ] || {
                    error "--tail requires an integer from 1 to 10000"
                    return "$GMS_RT_EXIT_USAGE"
                }
                tail_lines="$1"
                log_options=1
                ;;
            --tail=*)
                tail_lines="${1#*=}"
                log_options=1
                ;;
            --date)
                shift
                [ "$#" -gt 0 ] || {
                    error "--date requires YYYYMMDD"
                    return "$GMS_RT_EXIT_USAGE"
                }
                log_date="$1"
                log_options=1
                ;;
            --date=*)
                log_date="${1#*=}"
                log_options=1
                ;;
            -*)
                error "Unknown option: $1"
                return "$GMS_RT_EXIT_USAGE"
                ;;
            *)
                [ -z "$port_key" ] || {
                    error "Only one serial port key may be specified"
                    return "$GMS_RT_EXIT_USAGE"
                }
                port_key="$1"
                ;;
        esac
        shift
    done

    [[ "$tail_lines" =~ ^[1-9][0-9]*$ ]] && [ "$tail_lines" -le 10000 ] || {
        error "--tail requires an integer from 1 to 10000"
        return "$GMS_RT_EXIT_USAGE"
    }
    [ -z "$log_date" ] || [[ "$log_date" =~ ^[0-9]{8}$ ]] || {
        error "--date requires YYYYMMDD"
        return "$GMS_RT_EXIT_USAGE"
    }
    if [ -z "$port_key" ] && [ "$log_options" = "1" ]; then
        error "port_key is required when --tail or --date is used"
        return "$GMS_RT_EXIT_USAGE"
    fi

    local response
    if [ -z "$port_key" ]; then
        response=$(api_call "/devices/console/ports") || return $?
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$response" | jq '.'
            return ${PIPESTATUS[1]}
        fi
        if [ "$(echo "$response" | jq -r '.data.count // 0')" = "0" ]; then
            echo "No Controller serial ports found."
            return 0
        fi
        echo "$response" | jq -r '.data.ports[] | "\(.binding.label // .devname // .port_key)\t\(.online | if . then "online" else "offline" end)\t\(.devname // "-")\t\(.port_key)\t\(.binding.baudrate // "unbound") baud\t\(.error // "")"'
        return ${PIPESTATUS[1]}
    fi

    local endpoint="/devices/console/ports/$(_urlencode "$port_key")/logs?tail=$tail_lines"
    [ -z "$log_date" ] || endpoint="$endpoint&date=$(_urlencode "$log_date")"
    response=$(api_call "$endpoint") || return $?
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.'
    else
        echo "$response" | jq -r '.data.content // empty'
    fi
}

# Wait until every requested device reaches the requested controller state.
gms-rt-devices-wait() {
    local devices="${1:-}"
    [ -n "$devices" ] || {
        error "Usage: gms-rt-devices-wait <devices> [--state online|fastboot|any] [--interval SECONDS] [--max-wait SECONDS]"
        return "$GMS_RT_EXIT_USAGE"
    }
    shift

    local expected_state="online"
    local interval=3
    local max_wait=300
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --state)
                shift
                [ "$#" -gt 0 ] || {
                    error "--state requires online, fastboot, or any"
                    return "$GMS_RT_EXIT_USAGE"
                }
                expected_state="$1"
                ;;
            --state=*) expected_state="${1#*=}" ;;
            --interval)
                shift
                [ "$#" -gt 0 ] || {
                    error "--interval requires a positive integer"
                    return "$GMS_RT_EXIT_USAGE"
                }
                interval="$1"
                ;;
            --interval=*) interval="${1#*=}" ;;
            --max-wait)
                shift
                [ "$#" -gt 0 ] || {
                    error "--max-wait requires a positive integer"
                    return "$GMS_RT_EXIT_USAGE"
                }
                max_wait="$1"
                ;;
            --max-wait=*) max_wait="${1#*=}" ;;
            *)
                error "Unexpected argument: $1"
                return "$GMS_RT_EXIT_USAGE"
                ;;
        esac
        shift
    done
    case "$expected_state" in
        online|fastboot|any) ;;
        *)
            error "--state requires online, fastboot, or any"
            return "$GMS_RT_EXIT_USAGE"
            ;;
    esac
    if ! [[ "$interval" =~ ^[1-9][0-9]*$ ]] || [ "$interval" -gt 60 ] \
            || ! [[ "$max_wait" =~ ^[1-9][0-9]*$ ]]; then
        error "--interval must be 1-60 seconds and --max-wait must be a positive integer"
        return "$GMS_RT_EXIT_USAGE"
    fi

    check_jq || return "$GMS_RT_EXIT_OPERATION"
    local requested response ready missing observed started_at elapsed
    requested=$(convert_devices_to_json "$devices") || return "$GMS_RT_EXIT_USAGE"
    [ "$(echo "$requested" | jq 'length')" -gt 0 ] || {
        error "At least one device is required"
        return "$GMS_RT_EXIT_USAGE"
    }
    # Expand unique serial prefixes (e.g. RK3572) before polling.
    devices=$(_resolve_devices "$devices")
    requested=$(convert_devices_to_json "$devices") || return "$GMS_RT_EXIT_USAGE"
    [ "$(echo "$requested" | jq 'length')" -gt 0 ] || {
        error "At least one device is required"
        return "$GMS_RT_EXIT_USAGE"
    }
    started_at=$(date +%s)
    while true; do
        response=$(api_call "/devices/list?force_refresh=true") || return $?
        if ! echo "$response" | jq -e 'type == "array"' >/dev/null 2>&1; then
            error "Device list returned an unexpected response"
            echo "$response" | jq '.' 2>/dev/null || printf '%s\n' "$response"
            return "$GMS_RT_EXIT_OPERATION"
        fi
        observed=$(jq -cn --argjson requested "$requested" --argjson devices "$response" \
            --arg state "$expected_state" '
            [$requested[] as $serial |
                ($devices | map(select(.device_id == $serial)) | first // null) as $item |
                {
                    device_id: $serial,
                    ready: ($item != null and (
                        $state == "any"
                        or ($state == "online" and $item.status == "online" and ($item.protocol // "adb") == "adb")
                        or ($state == "fastboot" and (($item.protocol // "") == "fastboot" or $item.status == "fastboot"))
                    )),
                    status: ($item.status // "missing"),
                    protocol: ($item.protocol // "")
                }
            ]')
        ready=$(echo "$observed" | jq -r 'all(.ready == true)')
        elapsed=$(( $(date +%s) - started_at ))
        if [ "$ready" = "true" ]; then
            jq -cn --arg state "$expected_state" --argjson elapsed "$elapsed" \
                --argjson devices "$observed" \
                '{success: true, ready: true, expected_state: $state, elapsed_seconds: $elapsed, devices: $devices}'
            return 0
        fi
        if [ "$elapsed" -ge "$max_wait" ]; then
            missing=$(echo "$observed" | jq -r '[.[] | select(.ready != true) | .device_id] | join(", ")')
            jq -cn --arg state "$expected_state" --argjson elapsed "$elapsed" \
                --argjson devices "$observed" \
                '{success: false, ready: false, expected_state: $state, elapsed_seconds: $elapsed, devices: $devices}'
            diagnostic "Timed out waiting for devices: $missing"
            return "$GMS_RT_EXIT_OPERATION"
        fi
        info "Waiting for devices (${elapsed}s/${max_wait}s)..."
        sleep "$interval"
    done
}

# Reboot multiple devices (parallel)
gms-rt-devices-reboot() {
    # The help text promises "DEVICE1 [DEVICE2 ...]" but the old
    # implementation only consumed $1, silently dropping extra devices.
    # Collect every argument (also accepts the space-separated single-arg
    # form for backwards compatibility).
    local devices
    devices=$(printf '%s\n' "$*" | tr ' ' '\n' | sed '/^$/d' | paste -sd' ' -)
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-reboot DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "🔄 重启设备..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/reboot" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        local success=$(echo "$response" | jq -r '.data.summary.success // 0')
        local failed=$(echo "$response" | jq -r '.data.summary.failed // 0')

        # Request-level success:true does not mean every device
        # succeeded.  Map partial/full failure to a non-zero exit so
        # agents can tell them apart.
        if [ "$failed" -gt 0 ] 2>/dev/null; then
            error "设备重启部分失败: 成功 $success 台, 失败 $failed 台"
            echo "$response" | jq -r '.data.results[]? | select(.success != true) | "❌ \(.device): \(.error // .status // "失败")"' 2>/dev/null
            [ "$success" -gt 0 ] 2>/dev/null && return "$GMS_RT_EXIT_PARTIAL"
            return "$GMS_RT_EXIT_OPERATION"
        fi
        success "设备重启成功"

        echo "📊 操作统计: 成功 $success 台, 失败 $failed 台"
        echo ""

        # 显示每个设备的详细结果
        echo "$response" | jq -r '.data.results[]? | "📱 \(.device): 重启完成 (耗时: \(.wait_time // "N/A")秒)"' 2>/dev/null || echo "$response" | jq '.'
    else
        error "设备重启失败"
        echo "$response" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

# Remount multiple devices (parallel)
gms-rt-devices-remount() {
    local devices="$*"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-remount DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "🔄 重新挂载设备..."

    # 首先检查 bootloader 状态
    echo "🔐 检查 Bootloader 状态..."
    local bootloader_check=$(api_call "/devices/bootloader-status" "POST" "$(build_devices_json_data "$devices")")

    # 检查是否有锁定的设备
    local locked_devices=$(echo "$bootloader_check" | jq -r '.data.results[]? | select(.locked == true) | .device' 2>/dev/null)

    if [ -n "$locked_devices" ]; then
        error "以下设备 Bootloader 已锁定，无法 remount:"
        echo "$locked_devices" | while read -r device; do
            echo "  • $device (状态: $(echo "$bootloader_check" | jq -r ".data.results[]? | select(.device == \"$device\") | .status"))"
        done
        echo ""
        echo "💡 解决方案:"
        echo "   1. 使用 gms-rt-devices-bootloader-unlock <device> 解锁设备"
        echo "   2. 解锁后重新执行 remount"
        return 1
    fi

    echo "✅ Bootloader 检查通过，开始 remount..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/remount" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "设备重新挂载成功"

        # 美化输出格式
        local count=$(echo "$response" | jq -r '.data.summary.total // 0')
        local success_count=$(echo "$response" | jq -r '.data.summary.success // 0')
        local failed=$(echo "$response" | jq -r '.data.summary.failed // 0')

        echo "📊 操作统计: 成功 $success_count 台, 失败 $failed 台"
        echo ""

        # 检查 verity_mode，只有当设备真正需要重启时才提示
        local needs_reboot_list=()
        local already_rw_list=()

        # Process all devices in a single jq pass to extract needed fields
        while IFS='|' read -r device verity_mode needs_reboot overlayfs_enabled success; do
            if [ "$success" = "true" ]; then
                if [ "$needs_reboot" = "true" ]; then
                    needs_reboot_list+=("$device")
                elif [ "$overlayfs_enabled" = "true" ] || [ "$verity_mode" = "disabled" ]; then
                    already_rw_list+=("$device")
                fi
            fi
        done < <(echo "$response" | jq -r '.data.results[]? | "\(.device)|\(.verity_mode // "")|\(.needs_reboot // false)|\(.overlayfs_enabled // false)|\(.success // false)"' 2>/dev/null)

        # 显示已经 RW 的设备
        if [ ${#already_rw_list[@]} -gt 0 ]; then
            success "以下设备已处于读写模式，无需重启:"
            for device in "${already_rw_list[@]}"; do
                echo "  ✅ $device (overlayfs: enabled)"
            done
            echo ""
        fi

        # 显示需要重启的设备
        if [ ${#needs_reboot_list[@]} -gt 0 ]; then
            warning "以下设备需要重启才能使 remount 生效:"
            for device in "${needs_reboot_list[@]}"; do
                echo "  • $device (第一次 remount 完成)"
            done
            echo ""

            # 询问是否自动重启；Agent 模式绝不阻塞等待输入。
            local auto_reboot="n"
            if [ "$GMS_RT_ASSUME_YES" = "1" ]; then
                auto_reboot="y"
            elif [ "$GMS_RT_NON_INTERACTIVE" = "1" ]; then
                warning "非交互模式下未自动重启；如需自动重启请增加 --yes"
            else
                echo "💡 提示: 是否自动重启这些设备? (y/n)"
                read -r -t 10 auto_reboot || auto_reboot="n"
            fi

            if [ "$auto_reboot" = "y" ] || [ "$auto_reboot" = "Y" ]; then
                echo "🔄 自动重启设备..."
                for device in "${needs_reboot_list[@]}"; do
                    echo "  重启 $device..."
                    gms-rt-devices-reboot "$device" > /dev/null 2>&1
                done
                echo "✅ 重启完成"
            else
                echo "💡 使用以下命令手动重启:"
                for device in "${needs_reboot_list[@]}"; do
                    echo "   gms-rt-devices-reboot $device"
                done
            fi
        fi

        # 显示每个设备的详细结果
        echo ""
        echo "$response" | jq -r '.data.results[]? | "📱 \(.device): \(.output // .message // "完成")"' 2>/dev/null || echo "$response" | jq '.'
    else
        error "设备重新挂载失败"
        echo "$response" | jq '.'
    fi
}

# Show device screen
# Capture one device screenshot via the controller UI-control endpoint.
# 供 MCP image tool 使用：返回 {base64, mime_type, ...}（无 data: 前缀），
# agent 端可直接转 MCP image content；人类终端请用 devices-scrcpy。
gms-rt-devices-ui-dump() {
    # uiautomator dump 的平台化版本——POST /devices/ui/layout
    # 返回控件树 JSON（bounds/text/clickable），替代 ssh→uiautomator dump→
    # scp→本地解析的四步绕行。只读、单命令。
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-ui-dump DEVICE_ID"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local response
    response=$(api_call "/devices/ui/layout" "POST" "{\"serial\":\"$device_id\"}")
    if ! echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        error "UI dump failed: $(extract_api_error "$response")"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    echo "$response" | jq '{serial, source, elements}'
}

gms-rt-devices-snapshot() {
    # 一次调用聚合设备状态快照——fingerprint、前台 activity、
    # 锁屏状态、device owner/admin 列表。之前要逐条 shell + dumpsys 拼装。
    # 复用 gms-rt-devices-shell（本地 adb / SSH 直连，同 gms_rt_shell 白名单
    # 语义之外的平台诊断路径），每条独立失败降级为 null，不拖垮整个快照。
    # Snapshot probes are the documented read-only typed set;
    # export the typed-readonly marker so the shell gate allows only these
    # fixed probe commands in service-token mode.
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-snapshot DEVICE_ID"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    # 收紧后的 typed-readonly 白名单拒绝管道（元字符
    # 复核），因此 dumpsys+grep 探针改为在函数侧取全量输出、本地 grep。
    # 每条探针命令仍是固定字符串，探针命令面不因修复而扩大。
    _snapshot_probe() {
        local output filtered
        output=$(_GMS_RT_TYPED_READONLY=1 gms-rt-devices-shell "$device_id" "$1" 2>/dev/null) || return 0
        filtered=$(printf '%s\n' "$output" | ${2:-head -3})
        printf '%s' "$filtered"
    }
    local prop_fp activity keyguard owners
    prop_fp=$(_snapshot_probe "getprop ro.build.fingerprint")
    activity=$(_snapshot_probe "dumpsys activity activities" "grep -m1 topResumedActivity")
    keyguard=$(_snapshot_probe "dumpsys window" "grep -m1 mDreamingLockscreen")
    owners=$(_snapshot_probe "dpm list-owners")
    jq -n \
        --arg device "$device_id" \
        --arg fingerprint "$prop_fp" \
        --arg activity "$activity" \
        --arg keyguard "$keyguard" \
        --arg owners "$owners" \
        '{
            device: $device,
            fingerprint: ($fingerprint | if length > 0 then . else null end),
            focused_activity: ($activity | if length > 0 then . else null end),
            lockscreen: ($keyguard | if length > 0 then . else null end),
            device_owners: ($owners | if length > 0 then . else null end)
        }'
}

gms-rt-devices-screencap() {
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-screencap DEVICE_ID"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local response
    response=$(api_call "/devices/ui/screenshot" "POST" "{\"serial\":\"$device_id\"}")
    if ! echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Screenshot failed: $(extract_api_error "$response")"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    # 剥掉 data URL 前缀，输出与 gms-rt-redmine-artifact-image 同构的载荷。
    echo "$response" | jq '{device_id: .serial, mime_type: "image/png", base64: (.image | sub("^data:image/png;base64,"; ""))}'
}

gms-rt-devices-scrcpy() {
    local devices="$*"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-scrcpy DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "📺 显示设备屏幕..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/scrcpy" "POST" "$data")
    echo "$response" | jq '.'
}

# Execute shell command (local adb or SSH fallback to test host)
gms-rt-devices-shell() {
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-shell DEVICE_ID [--approval-token TOKEN] [COMMAND]"; return 1; }

    shift
    # One-shot approval token: with this flag the CLI
    # validates-and-consumes the approval server-side (tool+device+command
    # binding, 5-min TTL, single use) before running the command.
    local approval_token=""
    local shell_args=()
    local pending_approval=0
    local _gms_approval_consumed=0
    local arg
    for arg in "$@"; do
        if [ "$pending_approval" = "1" ]; then
            approval_token="$arg"
            pending_approval=0
            continue
        fi
        case "$arg" in
            --approval-token) pending_approval=1 ;;
            --approval-token=*) approval_token="${arg#*=}" ;;
            *) shell_args+=("$arg") ;;
        esac
    done
    if [ -n "$approval_token" ] && [ "${#shell_args[@]}" -eq 0 ]; then
        error "--approval-token requires a command to approve"
        return "$GMS_RT_EXIT_USAGE"
    fi
    if [ -n "$approval_token" ]; then
        _refresh_tls_args
        local consume_data consume_response
        consume_data=$(jq -cn \
            --arg token "$approval_token" \
            --arg device "$device_id" \
            --arg command "${shell_args[*]}" \
            '{token: $token, tool: "gms_rt_shell_exec", device: $device, command: $command}')
        consume_response=$(curl "${CURL_TLS_ARGS[@]}" -sS -X POST \
            "${API_BASE}/auth/approval-tokens/consume" \
            -H "Content-Type: application/json" \
            --data-binary "@-" -w $'\nHTTP_STATUS:%{http_code}' \
            --max-time "$CURL_TIMEOUT" <<< "$consume_data")
        unset consume_data approval_token
        local consume_status
        consume_status=$(_status_from_http_response "$consume_response")
        if [[ ! "$consume_status" =~ ^2[0-9]{2}$ ]] || \
           ! echo "$consume_response" | jq -e '.success == true' >/dev/null 2>&1; then
            error "审批令牌校验失败: $(extract_api_error "$(echo "$consume_response" | sed 's/\nHTTP_STATUS:.*//')")"
            return "$GMS_RT_EXIT_PERMISSION"
        fi
        # Approval consumed server-side → unlock the local
        # adb/SSH execution path exactly once for this invocation.
        _gms_approval_consumed=1
    fi
    local shell_command="${shell_args[*]:-}"
    if [ -z "$shell_command" ] && [ "$GMS_RT_NON_INTERACTIVE" = "1" ]; then
        error "Interactive device shell is disabled by --non-interactive; provide a command"
        return "$GMS_RT_EXIT_USAGE"
    fi

    # In service-token mode the CLI is no longer a bypass around
    # the MCP approval layer. Arbitrary shell (and interactive shell) is
    # denied without a server-consumed one-shot approval token; read-only
    # diagnosis belongs to gms_rt_shell / gms-rt-devices-snapshot.
    # GMS_RT_TYPED_READONLY=1 marks first-party typed read-only surfaces
    # (MCP gms_rt_shell / gms_rt_logcat, devices-snapshot probes). The
    # marker alone NEVER grants arbitrary command execution: when set, the
    # command must still pass the same fixed read-only allowlist the MCP
    # adapter enforces — a forged marker can therefore not reach
    # `reboot`/`settings put`/... directly on the CLI.
    if _gms_is_service_token_mode && [ "$_gms_approval_consumed" -ne 1 ]; then
        if [ -z "$shell_command" ]; then
            error "Service-token 模式禁止交互式设备 shell（审批边界外）；只读诊断请使用 gms-rt-devices-snapshot / gms_rt_shell"
            return "$GMS_RT_EXIT_PERMISSION"
        fi
        if [ "${GMS_RT_TYPED_READONLY:-0}" != "1" ]; then
            error "Service-token 模式下执行设备命令必须携带一次性审批令牌: gms-rt-devices-shell $device_id --approval-token TOKEN '$shell_command'（请让用户运行 gms-rt-approval-create --tool gms_rt_shell_exec --device $device_id --command '$shell_command' 铸造令牌）"
            return "$GMS_RT_EXIT_PERMISSION"
        fi
        # Typed-readonly allowlist (mirror of the MCP adapter's structured
        # allowlist). Binaries with mutating subcommands (settings/cmd/am/
        # pm/dpm/content/device_config/wm/logcat/dmesg/dumpsys) are verified
        # per-subcommand below — the first token alone is NOT sufficient
        # (a forged marker previously let `settings put` through when only
        # the leading binary was checked).
        local _ro_first
        _ro_first=${shell_command%% *}
        # Binaries whose read-only surface is unconditional.
        case "$_ro_first" in
            getprop|ls|cat|ps|pidof|stat|uptime|vmstat|df|id|printenv|grep|head|tail|wc|pgrep) ;;
            settings)
                case "$shell_command" in
                    "settings get "*) ;;
                    *) error "Service-token 只读白名单仅允许 'settings get'"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            wm)
                case "$shell_command" in
                    "wm size"|"wm density") ;;
                    *) error "Service-token 只读白名单仅允许 'wm size'/'wm density'"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            dumpsys)
                # Must stay in lockstep with _SHELL_DUMPSYS_MUTATING in
                # runtime/mcp_server.py (17 words). Any word added there
                # MUST be added here too — this gate is the CLI mirror of
                # the MCP typed-readonly allowlist.
                case " $shell_command " in
                    *" unplug "*|*" reset "*|*" disable "*|*" enable "*|*" kill "*|*" force-stop "*|*" set "*|\
                    *" whitelist "*|*" set-debug-app "*|*" suspend "*|*" resume "*|*" reset-role "*|\
                    *" plug "*|*" charge "*|*" nocharge "*|*" persist "*|*" import "*)
                        error "dumpsys 参数可能改变设备状态，需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            logcat)
                case "$shell_command" in
                    *-c*|*" -f"*) error "logcat -c/-f 属破坏性参数，需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            dmesg)
                case "$shell_command" in
                    *-c*|*-C*) error "dmesg -c/-C 清空内核环形缓冲，需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            device_config)
                case "$shell_command" in
                    "device_config get "*|"device_config list"*) ;;
                    *) error "Service-token 只读白名单仅允许 'device_config get/list'"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            cmd|am|pm|dpm|content)
                # 这些二进制的只读子命令集合在 MCP 层枚举
                # (_SHELL_CMD_READONLY_SUBCOMMANDS)；CLI 端必须逐前缀镜像，
                # 否则 MCP 放行的命令到 CLI 被拒（工具契约破裂）。
                # 注意 MCP 是 exact-or-prefix(sub+" ")匹配，CLI 的 glob
                # "cmd package list"* 等价于 startswith——但 MCP 还接受
                # joined == sub（无参数形式，如 "pm help"），CLI 用裸 *
                # 或精确串覆盖这两种形态。
                case "$shell_command" in
                    "cmd list"*|"cmd help"*|\
                    "cmd package list"*|"cmd package path"*|"cmd package dump"*|\
                    "cmd package help"*|"cmd package query-activities"*|\
                    "cmd package query-services"*|"cmd package query-receivers"*|\
                    "cmd package query-content-providers"*|\
                    "am stack list"*|"am get-current-user"*|"am get-standby-bucket"*|\
                    "pm list users"*|"pm list packages"*|"pm list permissions"*|\
                    "pm list permission-groups"*|"pm list features"*|\
                    "pm list libraries"*|"pm list instrumentation"*|"pm list jobs"*|\
                    "pm path "*|"pm help"|\
                    "dpm list-owners"|\
                    "content query"*) ;;
                    *) error "只读子命令白名单之外的 '$_ro_first' 需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            *)
                error "只读白名单之外的命令需要一次性审批令牌: '$_ro_first'"
                return "$GMS_RT_EXIT_PERMISSION"
                ;;
        esac
        # Shell metacharacters — full mirror of _SHELL_FORBIDDEN_CHARS in
        # runtime/mcp_server.py. The command string is finally parsed by the
        # device-side `sh` (adb shell), so quote/glob/backslash/whitespace
        # metachars can smuggle a second command just like `;` does.
        case "$shell_command" in
            *[\\\"\;\|\&\>\<\`\$\(\)\{\}\[\]\'\*\?]*)
                error "Service-token 只读路径禁止 shell 元字符: $shell_command"
                return "$GMS_RT_EXIT_PERMISSION"
                ;;
        esac
        if [[ "$shell_command" == *[$'\t\r\n']* ]]; then
            error "Service-token 只读路径禁止制表符/换行符: $shell_command"
            return "$GMS_RT_EXIT_PERMISSION"
        fi
    fi

    if _is_test_host && command -v adb &> /dev/null && adb devices 2>/dev/null | grep -q "$device_id"; then
        if [ -n "$shell_command" ]; then
            adb -s "$device_id" shell "$shell_command"
            local adb_status=$?
            # Propagate the real adb exit code — swallowing it made
            # failed device commands report ok:true / exit_code:0.
            if [ "$adb_status" -ne 0 ]; then
                GMS_RT_ERROR_SEEN=1
                return "$adb_status"
            fi
            return 0
        else
            echo ""; echo "💻 打开设备Shell: $device_id..."
            echo "🔌 使用 Ctrl+D 退出 shell"; echo ""
            adb -s "$device_id" shell
            return 0
        fi
    fi

    local ssh_info=($(_resolve_ssh_host))
    local host="${ssh_info[0]}" user="${ssh_info[1]}" port="${ssh_info[2]}"
    [ -z "$host" ] && { error "无法确定测试主机地址"; return 1; }
    ! command -v ssh &> /dev/null && { error "ssh 命令未找到. 请安装 OpenSSH 客户端"; return 1; }

    if [ -n "$shell_command" ]; then
        local quoted_device quoted_command
        quoted_device=$(_shell_quote "$device_id")
        quoted_command=$(_shell_quote "$shell_command")
        ssh -p "$port" "$user@$host" "adb -s ${quoted_device} shell ${quoted_command}"
        local ssh_status=$?
        if [ "$ssh_status" -ne 0 ]; then
            GMS_RT_ERROR_SEEN=1
            return "$ssh_status"
        fi
        return 0
    else
        echo ""; echo "💻 打开设备Shell: $device_id... (via $user@$host)"
        echo "🔌 使用 Ctrl+D 退出 shell"; echo ""
        ssh -t -p "$port" "$user@$host" "adb -s $device_id shell"
    fi
}

# Capture device logcat via `adb shell logcat -v time` (local adb or SSH fallback).
# Optional -c/--clear runs `logcat -c` first, then captures with `-v time`.
# Interactive use streams live; --non-interactive (agents/CI) forces one-shot
# dump mode (-d) so the command always terminates. -f (write device file) is
# rejected, and non-interactive args must not contain shell metacharacters.
gms-rt-devices-logcat() {
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-logcat DEVICE_ID [-c] [logcat参数...]"; return "$GMS_RT_EXIT_USAGE"; }
    shift

    local clear_first=0 has_dump_flag=0
    local logcat_args=() arg
    for arg in "$@"; do
        case "$arg" in
            -c|--clear)
                clear_first=1
                ;;
            -f|--file=*)
                error "logcat 参数 -f 会写设备文件, 不允许: $arg"
                return "$GMS_RT_EXIT_USAGE"
                ;;
            *)
                case "$arg" in
                    -d|-t|-T|-g|-L|-p|-print) has_dump_flag=1 ;;
                esac
                logcat_args+=("$arg")
                ;;
        esac
    done

    if [ "$GMS_RT_NON_INTERACTIVE" = "1" ]; then
        # 设备端由远端 sh 解释拼接命令, 非交互参数禁止 shell 元字符。
        local unsafe_pattern='[;&|><$`'"'"'()\\]'
        for arg in "${logcat_args[@]}"; do
            if [[ "$arg" =~ $unsafe_pattern ]]; then
                error "非交互模式拒绝包含 shell 元字符的参数: $arg"
                return "$GMS_RT_EXIT_USAGE"
            fi
        done
        # '-T <time>' does NOT imply dump mode in logcat — it keeps
        # following output and would hang a non-interactive call until the
        # timeout.  Only -d/-t/-g/-L/-p/-print terminate on their own, so
        # append -d unless one of those (excluding -T) is present.
        local has_terminating_flag=0
        for arg in "${logcat_args[@]}"; do
            case "$arg" in
                -d|-t|-g|-L|-p|-print) has_terminating_flag=1 ;;
            esac
        done
        if [ "$has_terminating_flag" -eq 0 ]; then
            logcat_args=(-d "${logcat_args[@]}")
        fi
    elif [ "$has_dump_flag" -eq 0 ] && [ "$GMS_RT_QUIET" != "1" ]; then
        echo ""
        echo "🖥 抓取 $device_id logcat (adb shell logcat -v time), Ctrl+C 停止..."
        echo ""
    fi

    if _is_test_host && command -v adb &> /dev/null && adb devices 2>/dev/null | grep -q "$device_id"; then
        if [ "$clear_first" -eq 1 ]; then
            adb -s "$device_id" shell logcat -c || { error "logcat -c 清空缓冲失败"; return "$GMS_RT_EXIT_OPERATION"; }
        fi
        # 逐参数加引号: adb shell 将所有参数拼接后交给设备端 sh 解释,
        # 含空格的参数 (如 logcat -t '09-07 10:52:00.000') 不加引号会被拆开。
        local device_cmd="logcat -v time"
        local arg
        for arg in "${logcat_args[@]}"; do
            device_cmd+=" $(_shell_quote "$arg")"
        done
        adb -s "$device_id" shell "$device_cmd"
        return $?
    fi

    local ssh_info=($(_resolve_ssh_host))
    local host="${ssh_info[0]}" user="${ssh_info[1]}" port="${ssh_info[2]}"
    [ -z "$host" ] && { error "无法确定测试主机地址"; return 1; }
    ! command -v ssh &> /dev/null && { error "ssh 命令未找到. 请安装 OpenSSH 客户端"; return 1; }

    # 逐参数加引号: 远端 adb shell 将参数拼接后由设备端 sh 解释,
    # 含空格的参数 (如 logcat -t '09-07 10:52:00.000') 不加引号会被拆开。
    local logcat_command="logcat -v time"
    local arg
    for arg in "${logcat_args[@]}"; do
        logcat_command+=" $(_shell_quote "$arg")"
    done
    local quoted_device quoted_command remote_command
    quoted_device=$(_shell_quote "$device_id")
    quoted_command=$(_shell_quote "$logcat_command")
    remote_command="adb -s ${quoted_device} shell ${quoted_command}"
    if [ "$clear_first" -eq 1 ]; then
        remote_command="adb -s ${quoted_device} shell logcat -c && ${remote_command}"
    fi
    ssh -p "$port" "$user@$host" "$remote_command"
}

# Push file to device (adb push)
gms-rt-devices-push() {
    local device_id="$1"
    local local_path="$2"
    local remote_path="$3"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-push <DEVICE_ID> <LOCAL_FILE> <REMOTE_PATH>"; return 1; }
    [ -z "$local_path" ] && { error "本地文件路径必填. 用法: gms-rt-devices-push <DEVICE_ID> <LOCAL_FILE> <REMOTE_PATH>"; return 1; }
    [ -z "$remote_path" ] && { error "设备目标路径必填. 用法: gms-rt-devices-push <DEVICE_ID> <LOCAL_FILE> <REMOTE_PATH>"; return 1; }
    [ ! -f "$local_path" ] && { error "文件不存在: $local_path"; return 1; }

    local local_path=$(realpath "$local_path")
    local filename=$(basename "$local_path")

    if _is_test_host && command -v adb &> /dev/null && adb devices 2>/dev/null | grep -q "$device_id"; then
        echo "📤 Pushing $filename to $device_id:$remote_path..."
        adb -s "$device_id" push "$local_path" "$remote_path"
        return $?
    fi

    local ssh_info=($(_resolve_ssh_host))
    local host="${ssh_info[0]}" user="${ssh_info[1]}" port="${ssh_info[2]}"
    [ -z "$host" ] && { error "无法确定测试主机地址"; return 1; }
    ! command -v scp &> /dev/null && { error "scp 命令未找到. 请安装 OpenSSH 客户端"; return 1; }

    local tmp_remote="/tmp/gms-rt-push-$$-$filename"

    echo "📤 Step 1/2: Transferring $filename to test host..."
    scp -P "$port" "$local_path" "$user@$host:$tmp_remote" || { error "文件传输失败"; return 1; }

    echo "📤 Step 2/2: Pushing to device $device_id:$remote_path (via $user@$host)..."
    local push_result=0
    ssh -p "$port" "$user@$host" "adb -s $device_id push '$tmp_remote' '$remote_path'" || push_result=$?
    ssh -p "$port" "$user@$host" "rm -f '$tmp_remote'" 2>/dev/null
    return $push_result
}

# User locked devices
gms-rt-devices-user-locked() {
    check_jq
    echo "🔒 Getting user-locked devices..."
    api_call "/devices/user-locked" | jq '.'
}

# Connect WiFi
gms-rt-devices-wifi() {
    local devices="$1"
    local ssid="$2"
    local password
    password=$(_secret_value "${3:-${GMS_REMOTE_WIFI_PASSWORD:-}}")

    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-wifi <devices> <ssid> [password]"; return 1; }
    [ -z "$ssid" ] && { error "SSID必填. 用法: gms-rt-devices-wifi <devices> <ssid> [password]"; return 1; }
    [ -z "$password" ] && { error "密码必填（第三个参数或 GMS_REMOTE_WIFI_PASSWORD）. 用法: gms-rt-devices-wifi <devices> <ssid> [password]"; return 1; }

    check_jq
    devices=$(_resolve_devices "$devices")
    echo "📶 连接WiFi: $ssid..."

    local devices_json data
    devices_json=$(build_devices_json_data "$devices") || return "$GMS_RT_EXIT_USAGE"
    data=$(echo "$devices_json" | jq -c --arg ssid "$ssid" --arg password "$password" \
        '. + {ssid: $ssid, password: $password}')
    local response=$(api_call "/devices/wifi" "POST" "$data")

    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "WiFi连接已启动"
        echo "$response" | jq '.'
    else
        error "WiFi连接失败"
        echo "$response" | jq '.'
    fi
}

# ==============================================================================
# File Commands
# ==============================================================================

# Get upload progress
gms-rt-files-progress() {
    local upload_id="${1:-}"
    check_jq
    echo "📊 Getting upload progress..."
    local endpoint="/files/progress"
    [ -n "$upload_id" ] && endpoint="${endpoint}?upload_id=$(_urlencode "$upload_id")"
    api_call "$endpoint" | jq '.'
}

# OpenGrok search
gms-rt-opengrok-search() {
    local query="$1"
    local full="${2:-false}"
    [ -z "$query" ] && { error "Query required. Usage: gms-rt-opengrok-search <query> [full]"; return 1; }
    check_jq
    echo "🔍 Searching OpenGrok for: $query..."
    case "$full" in
        true|false) ;;
        *) error "full must be true or false"; return "$GMS_RT_EXIT_USAGE" ;;
    esac
    local data
    data=$(jq -cn --arg query "$query" --argjson full "$full" \
        '{query: $query, full: $full}')
    local response=$(api_call "/opengrok/search" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Redmine Evidence Commands (read-only evidence chain)
# ==============================================================================

gms-rt-redmine-issue-fetch() {
    local issue_ref=""
    local download="all"
    local refresh=1
    local do_wait=0
    local dry_run=0
    local max_wait=300
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-redmine-issue-fetch <issue_id_or_url> [--download none|analyzable|all] [--refresh|--no-refresh] [--wait] [--max-wait SECONDS] [--dry-run]"
                echo "  Create/refresh a full evidence snapshot (raw JSON, journals, attachments)."
                echo "  --refresh is the default; --no-refresh may reuse a recent ready snapshot (response carries cache_hit)."
                echo "  --dry-run validates the issue reference, read access, base_url, and credentials without creating a snapshot."
                return 0
                ;;
            --download)
                shift
                [ $# -gt 0 ] || { error "--download requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    none|analyzable|all) download="$1" ;;
                    *) error "--download accepts none, analyzable, or all"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            --no-refresh) refresh=0 ;;
            --refresh) refresh=1 ;;
            --wait) do_wait=1 ;;
            --dry-run) dry_run=1 ;;
            --max-wait)
                shift
                [ $# -gt 0 ] || { error "--max-wait requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                max_wait="$1"
                ;;
            *)
                [ -z "$issue_ref" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                issue_ref="$1"
                ;;
        esac
        shift
    done
    [ -z "$issue_ref" ] && { error "Issue ID or URL required. Usage: gms-rt-redmine-issue-fetch <issue_id_or_url> [options]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local payload response snapshot_id
    payload=$(jq -cn --argjson refresh "$refresh" --arg download "$download" \
        --argjson dry_run "$dry_run" \
        '{refresh: $refresh, download: $download, dry_run: $dry_run}')
    response=$(api_call "/redmine-agent/issues/$(_urlencode "$issue_ref")/evidence" "POST" "$payload")
    local call_status=$?
    if [ "$call_status" -ne 0 ]; then
        # api_call 已把服务器错误 body 打到 stdout；这里给出人话根因
        #（scope/凭据/issue 不存在），不再让错误详情被 jq 过滤吞掉。
        error "Evidence snapshot creation failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    if [ "$dry_run" = "1" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$response" | jq '.'
        else
            success "Preconditions met: issue reference, Redmine base_url, and credentials are valid."
        fi
        return 0
    fi
    snapshot_id=$(echo "$response" | jq -r '.data.snapshot_id // empty')
    if [ -z "$snapshot_id" ]; then
        error "Failed to create evidence snapshot for '$issue_ref'"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ "$do_wait" != "1" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            jq -cn --arg snapshot_id "$snapshot_id" \
                '{success: true, snapshot_id: $snapshot_id, status: "queued", poll: "gms-rt-redmine-issue-show '$snapshot_id'"}'
        else
            success "Evidence snapshot queued: $snapshot_id"
            echo "Poll with: gms-rt-redmine-issue-show $snapshot_id"
        fi
        return 0
    fi
    local waited=0 interval=3
    while true; do
        local status_resp status
        status_resp=$(api_call "/redmine-agent/evidence/$snapshot_id" "GET")
        status=$(echo "$status_resp" | jq -r '.data.status // empty')
        case "$status" in
            ready|partial|failed) break ;;
        esac
        [ -z "$status" ] && { error "Failed to read snapshot status"; return "$GMS_RT_EXIT_OPERATION"; }
        [ "$waited" -ge "$max_wait" ] && break
        sleep "$interval"
        waited=$((waited + interval))
    done
    gms-rt-redmine-issue-show "$snapshot_id"
}

gms-rt-redmine-issue-show() {
    local snapshot_id="$1"
    [ -z "$snapshot_id" ] && { error "Snapshot ID or issue ID required. Usage: gms-rt-redmine-issue-show <snapshot_id | issue_id> (--issue forces issue-id interpretation)"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local force_issue=0
    if [ "$2" = "--issue" ] || [ "$1" = "--issue" ]; then
        # 显式声明第一个参数是 issue_id。
        if [ "$1" = "--issue" ]; then
            snapshot_id="$2"
        fi
        force_issue=1
    fi
    # 第一个参数历史上必须是 snapshot_id，传 issue_id
    # 会得到费解的 "Snapshot not readable"。约定：ev_ 前缀或含连字符视为
    # snapshot_id；纯数字视为 issue_id 并解析最新快照；--issue/--snapshot
    # 可显式消歧。
    case "$snapshot_id" in
        --snapshot)
            snapshot_id="$2"
            ;;
        --issue)
            snapshot_id="$2"
            force_issue=1
            ;;
    esac
    if [ "$force_issue" = "1" ] || [[ "$snapshot_id" =~ ^[0-9]+$ ]]; then
        local latest_resp resolved status_line
        latest_resp=$(api_call "/redmine-agent/issues/$(_urlencode "$snapshot_id")/evidence/latest" "GET")
        local latest_status=$?
        if [ "$latest_status" -ne 0 ]; then
            error "$(extract_api_error "$latest_resp")"
            return "$latest_status"
        fi
        status_line=$(echo "$latest_resp" | jq -r '.data.status // ""')
        if [ "$status_line" != "ready" ]; then
            warning "latest snapshot for issue $snapshot_id is '$status_line' (not ready); showing it anyway"
        fi
        snapshot_id=$(echo "$latest_resp" | jq -r '.data.snapshot_id // empty')
        [ -z "$snapshot_id" ] && { error "Failed to resolve latest snapshot"; return "$GMS_RT_EXIT_OPERATION"; }
    fi
    local status_resp description_resp
    status_resp=$(api_call "/redmine-agent/evidence/$snapshot_id" "GET")
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        description_resp=$(api_call "/redmine-agent/evidence/$snapshot_id/issue" "GET")
        # Combine: status payload + description head
        echo "$status_resp" | jq --argjson desc "$(echo "$description_resp" | jq -c '.data // {}')" \
            '.data + {description: ($desc.description // {text: ""})}'
        return $?
    fi
    if echo "$status_resp" | jq -e '.success' > /dev/null; then
        echo "$status_resp" | jq -r '.data | "snapshot: \(.snapshot_id)\nissue: \(.issue_id)\nstatus: \(.status) complete=\(.complete)\njournals: \(.journal_count)  attachments: \(.attachment_count)/\(.downloaded_count) downloaded\nfetched_at: \(.fetched_at)  source_updated: \(.source_updated_on)\nsha256: \(.content_sha256)"'
        # failed/partial 快照必须把服务端 errors[] 透传出来（MCP
        # 可见而 CLI 不可见会导致排障绕路）。
        if echo "$status_resp" | jq -e '.data.status == "failed" or .data.status == "partial" or ((.data.errors // []) | length > 0)' >/dev/null; then
            echo "errors:"
            echo "$status_resp" | jq -r '.data.errors[]? | "  [\(.stage // "issue")] \(.message // .)"'
        fi
        description_resp=$(api_call "/redmine-agent/evidence/$snapshot_id/issue" "GET")
        echo "$description_resp" | jq -r '.data | "\nsubject: \(.subject)\nstatus: \(.status)  tracker: \(.tracker)\n\n--- description (first 2000 chars, use gms-rt-artifact-read for more) ---\n\(.description.text[:2000])"'
    else
        error "Snapshot not readable: $(extract_api_error "$status_resp")"
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

gms-rt-redmine-journals() {
    local snapshot_id="$1"
    local limit=50
    local cursor=""
    local argument
    shift 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            --cursor)
                shift
                [ $# -gt 0 ] || { error "--cursor requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                cursor="$1"
                ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$snapshot_id" ] && { error "Snapshot ID required. Usage: gms-rt-redmine-journals <snapshot_id> [--limit N] [--cursor C]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/redmine-agent/evidence/$snapshot_id/journals?limit=$limit"
    [ -n "$cursor" ] && url="$url&cursor=$(_urlencode "$cursor")"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "total: \(.total) returned: \(.returned) next_cursor: \(.next_cursor // "-")", (.journals[] | "[journal:\(.id)] \(.user_name) @ \(.created_on)\n\(.notes)\n---")'
}

gms-rt-redmine-attachments() {
    local snapshot_id="$1"
    [ -z "$snapshot_id" ] && { error "Snapshot ID required. Usage: gms-rt-redmine-attachments <snapshot_id>"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/redmine-agent/evidence/$snapshot_id/artifacts" "GET" | jq '.'
        return $?
    fi
    api_call "/redmine-agent/evidence/$snapshot_id/artifacts" "GET" | jq -r '.data | "total: \(.total)", (.artifacts[] | "[\(.artifact_id)] attachment:\(.attachment_id) \(.original_filename) kind=\(.kind) status=\(.status) size=\(.size_bytes) sha256=\(.sha256[0:16])\(.error // "" | if . != "" then "  error: \(.)" else "" end)")'
}

gms-rt-redmine-attachment-download() {
    local artifact_id="$1"
    local output="${2:-}"
    [ -z "$artifact_id" ] && { error "Artifact ID required. Usage: gms-rt-redmine-attachment-download <artifact_id> [output_path]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    [ -n "$output" ] || output="gms-evidence-$artifact_id.bin"
    local curl_status status_file tmp_output
    status_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-dl-status.XXXXXX") || return "$GMS_RT_EXIT_OPERATION"
    tmp_output=$(mktemp "${TMPDIR:-/tmp}/gms-rt-dl-body.XXXXXX") || { rm -f -- "$status_file"; return "$GMS_RT_EXIT_OPERATION"; }
    _refresh_tls_args
    _ensure_auth_cookie_jar || { rm -f -- "$status_file" "$tmp_output"; return "$GMS_RT_EXIT_OPERATION"; }
    # 先落临时文件：非 200 时响应体是 JSON 错误（如 scope_required），
    # 不能写进目标输出再整文件删除（否则错误详情会丢失）。
    curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
        -o "$tmp_output" -w '%{http_code}' --max-time "$CURL_TIMEOUT" \
        "${API_BASE}/redmine-agent/artifacts/$artifact_id/download" > "$status_file"
    curl_status=$(cat "$status_file" 2>/dev/null)
    rm -f -- "$status_file"
    case "$curl_status" in
        200)
            mv -f -- "$tmp_output" "$output" || {
                rm -f -- "$tmp_output"
                error "Failed to write $output"
                return "$GMS_RT_EXIT_OPERATION"
            }
            ;;
        000|'')
            rm -f -- "$tmp_output"
            error "Download failed (network error)"
            return "$GMS_RT_EXIT_NETWORK"
            ;;
        *)
            local error_body exit_code
            error_body=$(cat "$tmp_output" 2>/dev/null || true)
            rm -f -- "$tmp_output"
            exit_code=$(_http_exit_code "$curl_status")
            error "Download failed (HTTP $curl_status): $(extract_api_error "$error_body")"
            _record_api_exit_code "$exit_code"
            return "$exit_code"
            ;;
    esac
    local size sha
    size=$(stat -c '%s' "$output" 2>/dev/null || echo 0)
    sha=$(sha256sum "$output" 2>/dev/null | awk '{print $1}' || echo "")
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg artifact_id "$artifact_id" --arg path "$output" \
            --arg size "$size" --arg sha256 "$sha" \
            '{success: true, artifact_id: $artifact_id, saved_path: $path, size_bytes: ($size | tonumber), sha256: $sha256}'
    else
        success "Saved $output ($size bytes)"
        [ -n "$sha" ] && echo "sha256: $sha"
    fi
}

# Pre-flight credential check: reports whether the
# owner account behind the current credential has Redmine credentials
# configured. Equivalent to GET /redmine-agent/config/credentials; never
# returns secret material.
# 当天待处理 triage（waiting_my_reply / no_reply_3_days 去重）。
# 只读：数据来自个人看板 workload 统计，本命令不做任何业务判断。
gms-rt-redmine-triage() {
    local stale_days=""
    local list_limit=""
    local refresh=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-redmine-triage [--stale-days N] [--list-limit N] [--refresh]"
                echo "  List today's pending Redmine issues (waiting_my_reply + no_reply_3_days, deduped)."
                echo "  Read-only; source of truth is the personal dashboard workload statistics."
                return 0
                ;;
            --stale-days)
                shift
                [ $# -gt 0 ] || { error "--stale-days requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                stale_days="$1"
                ;;
            --list-limit)
                shift
                [ $# -gt 0 ] || { error "--list-limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                list_limit="$1"
                ;;
            --refresh) refresh=1 ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    check_jq || return 1

    local query=""
    [ -n "$stale_days" ] && query="${query}&stale_days=${stale_days}"
    [ -n "$list_limit" ] && query="${query}&list_limit=${list_limit}"
    [ "$refresh" = "1" ] && query="${query}&refresh=true"
    [ -n "$query" ] && query="?${query#&}"

    local response call_status
    response=$(api_call "/redmine-agent/daily-brief/triage${query}" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Redmine triage failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    local configured
    configured=$(echo "$response" | jq -r '.data.configured // true')
    if [ "$configured" = "false" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$response" | jq '.'
        else
            error "Redmine credentials not configured for this owner account."
            warning "Ask the enrolling account owner to configure them in the Web UI settings page."
        fi
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.data'
    else
        echo "$response" | jq -r '"generated_at: \(.data.generated_at)  snapshot: \(.data.snapshot_hash[0:12])",
            "waiting_my_reply: \(.data.counts.waiting_my_reply)  no_reply_3_days: \(.data.counts.no_reply_3_days)  total: \(.data.counts.total)"'
        echo "$response" | jq -r '.data.issues[] |
            "\(.priority)  #\(.issue_id)  [\((.buckets // []) | join(","))]  unreplied=\(.unreplied_days // 0)  \(.subject)"'
    fi
    return 0
}

gms-rt-redmine-history-search() {
    local query=""
    local limit=""
    local exclude_issue_id=""
    local resolved_only=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-redmine-history-search <query> [--limit N] [--exclude-issue-id N] [--resolved-only]"
                echo "  Search ALL historical Redmine issues (local archive + Redmine site search) for"
                echo "  same/similar problems and reusable fixes. Read-only, scoped to the owner account."
                return 0
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            --exclude-issue-id)
                shift
                [ $# -gt 0 ] || { error "--exclude-issue-id requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                exclude_issue_id="$1"
                ;;
            --resolved-only) resolved_only=1 ;;
            -*)
                # Allow the query to start with a dash (rare) only via explicit
                # value forms; otherwise reject unknown options.
                error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
            *)
                [ -z "$query" ] && query="$1" || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                ;;
        esac
        shift
    done
    [ -n "$query" ] || { error "Query required. Usage: gms-rt-redmine-history-search <query> [options]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq || return 1

    local qs="?q=$(_urlencode "$query")"
    [ -n "$limit" ] && qs="${qs}&limit=${limit}"
    [ -n "$exclude_issue_id" ] && qs="${qs}&exclude_issue_id=${exclude_issue_id}"
    [ "$resolved_only" = "1" ] && qs="${qs}&resolved_only=true"

    local response call_status
    response=$(api_call "/redmine-agent/history/search${qs}" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Redmine history search failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.data'
    else
        echo "$response" | jq -r '.data.items[] |
            "\(.is_resolved == true | if . then "[已解决]" else "[未解决]" end)  #\(.issue_id)  [\(.source // "local_db")]  \(.subject)"' \
            2>/dev/null || echo "$response" | jq '.data'
        echo "$response" | jq -r '.data.items[] | select(.solution != "" and .solution != null) |
            "  #\(.issue_id) fix: \(.solution)"' 2>/dev/null || true
    fi
    return 0
}

gms-rt-redmine-credentials-status() {
    check_jq || return 1
    local response call_status
    response=$(api_call "/redmine-agent/config/credentials" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Credentials status check failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    local configured username api_key_configured
    configured=$(echo "$response" | jq -r '.data.configured // false')
    username=$(echo "$response" | jq -r '.data.username // ""')
    api_key_configured=$(echo "$response" | jq -r '.data.api_key_configured // false')
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg configured "$configured" --arg username "$username" \
            --arg api_key_configured "$api_key_configured" \
            '{configured: ($configured == "true"), username: $username, api_key_configured: ($api_key_configured == "true")}'
    else
        echo "configured: $configured"
        [ -n "$username" ] && echo "username:  $username"
        echo "api_key:   $api_key_configured"
    fi
    if [ "$configured" != "true" ]; then
        warning "Owner account has no Redmine credentials; evidence fetch will fail."
        warning "Ask the enrolling account owner to configure them in the Web UI settings page"
        warning "(agent shares the owner storage; POST /config/credentials is human-only)."
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    return 0
}

gms-rt-artifact-read() {
    local artifact_id="$1"
    local offset=0
    local limit=65536
    shift 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --offset)
                shift
                [ $# -gt 0 ] || { error "--offset requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                offset="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$artifact_id" ] && { error "Artifact ID required. Usage: gms-rt-artifact-read <artifact_id> [--offset N] [--limit N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/redmine-agent/artifacts/$artifact_id/text?offset=$offset&limit=$limit" "GET" | jq '.'
        return $?
    fi
    api_call "/redmine-agent/artifacts/$artifact_id/text?offset=$offset&limit=$limit" "GET" | jq -r '.data | "chars \(.offset)+\(.returned_chars)/\(.total_chars)\(if .truncated then " (truncated; use --offset)" else "" end)\n\n\(.text)"'
}

gms-rt-redmine-artifact-image() {
    local artifact_id="$1"
    [ -z "$artifact_id" ] && { error "Artifact ID required. Usage: gms-rt-redmine-artifact-image <artifact_id>"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    # 供 MCP image tool 使用：返回 base64 + 元数据；人类终端请用 download 命令。
    api_call "/redmine-agent/artifacts/$artifact_id/image" "GET" | jq '.'
}

gms-rt-artifact-search() {
    local snapshot_id=""
    local query=""
    local limit=50
    local positional=()
    # 契约：<snapshot_id> --query QUERY；同时兼容两个位置参数。
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --query)
                shift
                [ $# -gt 0 ] || { error "--query requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                query="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    if [ -z "$snapshot_id" ] && [ "${#positional[@]}" -ge 1 ]; then
        snapshot_id="${positional[0]}"
    fi
    if [ -z "$query" ] && [ "${#positional[@]}" -ge 2 ]; then
        query="${positional[1]}"
    fi
    [ -z "$snapshot_id" ] || [ -z "$query" ] && { error "Usage: gms-rt-artifact-search <snapshot_id> <query|--query QUERY> [--limit N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/redmine-agent/evidence/$snapshot_id/search?q=$(_urlencode "$query")&limit=$limit"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "query: \(.query) matches: \(.total)\(if .limited then " (limited)" else "" end)", (.matches[] | "[\(.kind)\(.journal_id // .artifact_id // "")] \(.snippet)")'
}

gms-rt-apk-analyze-attachment() {
    local snapshot_id="$1"
    local artifact_id="$2"
    shift 2 2>/dev/null || true
    [ -z "$snapshot_id" ] || [ -z "$artifact_id" ] && { error "Usage: gms-rt-apk-analyze-attachment <snapshot_id> <artifact_id>"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    # artifact_id 已是全局唯一 opaque ID；snapshot_id 仅用于人工核对。
    local response
    response=$(api_call "/redmine-agent/artifacts/$artifact_id/apk-analysis" "POST")
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.'
        return $?
    fi
    if echo "$response" | jq -e '.success' > /dev/null; then
        echo "$response" | jq -r '.data | "task: \(.task_id)\nstatus: \(.status)\nsource: issue \(.source_ref.issue_id) sha256=\(.source_ref.sha256[0:16])"'
        echo "Poll with: gms-rt-apk-status $(echo "$response" | jq -r '.data.task_id')"
    else
        error "Failed to import artifact into APK analysis"
        echo "$response" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

gms-rt-apk-source-read() {
    local task_id="$1"
    local path="$2"
    local offset=0
    local limit=400
    shift 2 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --offset)
                shift
                [ $# -gt 0 ] || { error "--offset requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                offset="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$task_id" ] || [ -z "$path" ] && { error "Usage: gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/apk/source-read/$task_id?path=$(_urlencode "$path")&offset=$offset&limit=$limit"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "lines \(.offset)+\(.returned_lines)/\(.total_lines)\(if .truncated then " (truncated)" else "" end)", (.lines[] | "\(.line)\t\(.text)")'
}

gms-rt-sdk-sources() {
    check_jq
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/sdk/sources" "GET" | jq '.'
        return $?
    fi
    api_call "/sdk/sources" "GET" | jq -r '.data.sources[] | "\(.source_id) (provider=\(.provider), default_revision=\(.default_revision // "-"))"'
}

gms-rt-sdk-search() {
    local source="" revision="" query=""
    local limit=50 path_filter=""
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-sdk-search --source ID --revision REV --query TEXT [--path FILTER] [--limit N]"
                return 0
                ;;
            --source) shift; [ $# -gt 0 ] || { error "--source requires a value"; return "$GMS_RT_EXIT_USAGE"; }; source="$1" ;;
            --revision) shift; [ $# -gt 0 ] || { error "--revision requires a value"; return "$GMS_RT_EXIT_USAGE"; }; revision="$1" ;;
            --query) shift; [ $# -gt 0 ] || { error "--query requires a value"; return "$GMS_RT_EXIT_USAGE"; }; query="$1" ;;
            --path) shift; [ $# -gt 0 ] || { error "--path requires a value"; return "$GMS_RT_EXIT_USAGE"; }; path_filter="$1" ;;
            --limit) shift; [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }; limit="$1" ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$source" ] || [ -z "$revision" ] || [ -z "$query" ] && { error "Usage: gms-rt-sdk-search --source ID --revision REV --query TEXT"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/sdk/search?source=$(_urlencode "$source")&revision=$(_urlencode "$revision")&query=$(_urlencode "$query")&limit=$limit"
    [ -n "$path_filter" ] && url="$url&path_filter=$(_urlencode "$path_filter")"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "source: \(.source_id) commit: \(.commit)\nmatches: \(.total)\(if .limited then " (limited)" else "" end)", (.matches[] | "\(.path):\(.line): \(.snippet)\n  result_id: \(.result_id)")'
}

gms-rt-sdk-read() {
    # read 只接受自包含 opaque result_id；source/path/commit
    # 由 result_id 载荷携带，客户端不再拼接自由路径。
    local result_id=""
    local offset=0 limit=400
    # 位置参数形式：gms-rt-sdk-read SDK_RESULT_ID（同上契约）。
    if [ $# -ge 1 ]; then
        case "$1" in
            -*) : ;;
            *) result_id="$1"; shift ;;
        esac
    fi
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --result-id) shift; [ $# -gt 0 ] || { error "--result-id requires a value"; return "$GMS_RT_EXIT_USAGE"; }; result_id="$1" ;;
            --offset) shift; [ $# -gt 0 ] || { error "--offset requires a value"; return "$GMS_RT_EXIT_USAGE"; }; offset="$1" ;;
            --limit) shift; [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }; limit="$1" ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$result_id" ] && {
        error "Usage: gms-rt-sdk-read SDK_RESULT_ID [--offset N] [--limit N]"
        error "       (result_id comes from gms-rt-sdk-search matches; source/path/commit are bound inside)"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq
    local url="/sdk/read?result_id=$(_urlencode "$result_id")&offset=$offset&limit=$limit"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "source: \(.source_id) commit: \(.commit) blob_sha256: \(.blob_sha256)\nlines \(.offset)+\(.returned_lines)/\(.total_lines)\(if .truncated then " (truncated)" else "" end)", (.lines[] | "\(.line)\t\(.text)")'
}

# ==============================================================================
# Report Commands
# ==============================================================================

_print_report_candidates() {
    local reports_json="$1"
    echo "$reports_json" | jq -r '
        .reports[:10][]? |
        [
            (.timestamp // "N/A"),
            (.test_type // "N/A"),
            (.test_module // .module // "N/A"),
            (.device // ((.devices // []) | join(",")) // "N/A"),
            (.result_dir // "N/A")
        ] | @tsv' |
        while IFS=$'\t' read -r timestamp type module device result_dir; do
            printf "  %-28s %-8s %-28s %-18s %s\n" "$timestamp" "$type" "$module" "$device" "$result_dir" >&2
        done
}

_resolve_report_timestamp() {
    local report_query="$1"
    local normalized_query="${report_query%.zip}"
    local reports_json
    reports_json=$(api_call "/reports/list") || return 1

    local match_count
    local resolved

    # Single jq invocation: try exact match, then fuzzy match, then single-report fallback
    # Returns: <match_count>|<timestamp>  (count>1 means ambiguous, 0 means not found)
    local result
    result=$(echo "$reports_json" | jq -r \
        --arg q "$report_query" \
        --arg nq "$normalized_query" \
        --arg fq "$(echo "$normalized_query" | tr '[:upper:]' '[:lower:]')" '
        # Stage 1: exact timestamp match
        (.reports // []) as $rs |
        ($rs | map(select(
            (.timestamp // "") == $q or (.timestamp // "") == $nq or ((.timestamp // "") + ".zip") == $q
        ))) as $exact |
        if ($exact | length) == 1 then
            "1|\($exact[0].timestamp)"
        elif ($exact | length) > 1 then
            "\($exact | length)|"
        else
            # Stage 2: fuzzy text match
            ($rs | map(select(
                ([
                    (.timestamp // ""), (.test_module // ""), (.module // ""),
                    (.report_name // ""), (.test_type // ""),
                    (.result_dir // ""), (.suite_path // "")
                ] | join(" ") | ascii_downcase) as $text |
                $text | contains($fq)
            ))) as $fuzzy |
            if ($fuzzy | length) == 1 then
                "1|\($fuzzy[0].timestamp)"
            elif ($fuzzy | length) > 1 then
                "\($fuzzy | length)|"
            elif ($rs | length) == 1 then
                "1|\($rs[0].timestamp)"
            else
                "0|"
            end
        end
    ')

    match_count="${result%%|*}"
    resolved="${result#*|}"

    if [ "$match_count" -eq 1 ] && [ -n "$resolved" ]; then
        echo "$resolved"
        return 0
    fi

    if [ "$match_count" -gt 1 ]; then
        error "报告关键字 '$report_query' 匹配到多条报告，请改用具体 TIMESTAMP。" >&2
    else
        error "报告不存在: $report_query" >&2
    fi
    echo "可用报告:" >&2
    _print_report_candidates "$reports_json"
    return 1
}

# Analyze report
gms-rt-reports-analyze() {
    local report_query="$1"
    [ -z "$report_query" ] && { error "Report required. Usage: gms-rt-reports-analyze <local_report.zip|test_result.xml|report_timestamp|keyword>"; return 1; }
    check_jq

    local response
    if [ -f "$report_query" ]; then
        echo "🔍 Analyzing uploaded report file: $report_query..."
        _ensure_auth_cookie_jar || return 1
        response=$(api_call "/reports/analyze" "POST" "" \
            -F "mode=upload" \
            -F "file=@${report_query}") || return $?
    else
        local report_timestamp
        report_timestamp=$(_resolve_report_timestamp "$report_query") || return 1
        echo "🔍 Analyzing saved report: $report_timestamp..."
        _ensure_auth_cookie_jar || return 1
        response=$(api_call "/reports/analyze" "POST" "" \
            -F "mode=saved" \
            -F "report_timestamp=${report_timestamp}") || return $?
    fi

    # Check if request was successful
    local success=$(echo "$response" | jq -r '.success // false')
    if [ "$success" != "true" ]; then
        error "Failed to analyze report: $(echo "$response" | jq -r '.error // "Unknown error"')"
        return 1
    fi

    # Display formatted output similar to web UI
    echo ""
    echo "┌─────────────────────────────────────────────────────────────────┐"
    echo "│                    📊 REPORT ANALYSIS                            │"
    echo "└─────────────────────────────────────────────────────────────────┘"
    echo ""

    # Summary section
    local total=$(echo "$response" | jq -r '.data.summary.total // 0')
    local pass=$(echo "$response" | jq -r '.data.summary.pass // 0')
    local fail=$(echo "$response" | jq -r '.data.summary.fail // 0')
    local pass_rate=$(echo "$response" | jq -r '.data.summary.pass_rate // "0.00%"')

    echo "📈 Summary:"
    echo "   Total Tests:  $total"
    echo "   ✓ Passed:     $pass"
    echo "   ✗ Failed:     $fail"
    echo "   Pass Rate:    $pass_rate"
    echo ""

    # Details section
    local test_type=$(echo "$response" | jq -r '.data.details.test_type // "N/A"')
    local device=$(echo "$response" | jq -r '.data.details.device // "N/A"')
    local android_version=$(echo "$response" | jq -r '.data.details.android_version // "N/A"')
    local start_time=$(echo "$response" | jq -r '.data.details.start_time // "N/A"')

    echo "📋 Details:"
    echo "   Test Type:      $test_type"
    echo "   Device:         $device"
    echo "   Android Version: $android_version"
    echo "   Start Time:     $start_time"
    echo ""

    # Failures section - Web UI format
    local failure_count=$(echo "$response" | jq -r '.data.failures | length')
    if [ "$failure_count" -gt 0 ]; then
        echo "❌ Failures ($failure_count):"
        echo ""

        # Iterate through each failure with Web UI format
        for i in $(seq 0 $((failure_count - 1))); do
            local failure=$(echo "$response" | jq ".data.failures[$i]")
            local name=$(echo "$failure" | jq -r '.name // "Unknown"')
            local module=$(echo "$failure" | jq -r '.module // "Unknown"')
            local reason=$(echo "$failure" | jq -r '.reason // "No reason provided"')

            # Web UI format: 测试模块
            echo "   ┌──────────────────────────────────────────────────────────────┐"
            echo "   │ 测试模块: $module"
            echo "   └──────────────────────────────────────────────────────────────┘"

            # Web UI format: 测试用例
            echo "   测试用例: $name"
            echo ""

            # Web UI format: 失败详情
            echo "   失败详情:"
            # Preserve original formatting with proper indentation
            echo "$reason" | sed 's/^/   /'
            echo ""
        done
    elif [ "$total" -eq 0 ]; then
        echo "⚠ No test case records found in this report."
        echo ""
    else
        echo "✅ No failures! All tests passed."
        echo ""
    fi

    echo "└─────────────────────────────────────────────────────────────────┘"
}

# Delete report
gms-rt-reports-delete() {
    local report_timestamp="$1"
    [ -z "$report_timestamp" ] && { error "Report timestamp required. Usage: gms-rt-reports-delete <report_timestamp>"; return 1; }
    check_jq
    echo "🗑️  Deleting report: $report_timestamp..."
    local encoded_timestamp response
    encoded_timestamp=$(_urlencode "$report_timestamp")
    response=$(api_call "/reports/delete?timestamp=${encoded_timestamp}" "DELETE") || return $?
    echo "$response" | jq '.'
}

# Get/download report
gms-rt-reports-download() {
    local report_timestamp="$1"
    local output_dir="${2:-${report_timestamp}}"
    [ -z "$report_timestamp" ] && { error "Report timestamp required. Usage: gms-rt-reports-download <report_timestamp> [output_dir]"; return 1; }
    check_jq

    echo "📥 Downloading report folder: $report_timestamp to $output_dir..."

    # 创建输出目录
    mkdir -p "$output_dir"

    # 获取文件列表
    local encoded_timestamp response
    encoded_timestamp=$(_urlencode "$report_timestamp")
    response=$(api_call "/reports/download?report_timestamp=$encoded_timestamp")

    if ! echo "$response" | jq -e '.success' > /dev/null; then
        local error_msg=$(echo "$response" | jq -r '.error // "Unknown error"')
        error "Failed to get report files: $error_msg"
        return 1
    fi

    # 下载每个文件
    local file_count=$(echo "$response" | jq '.files | length')
    echo "Found $file_count files, downloading..."

    local success_count=0
    local fail_count=0

    while IFS= read -r file_info; do
        local file_path=$(echo "$file_info" | jq -r '.path')
        local relative_path=$(echo "$file_info" | jq -r '.relative_path')
        if [ -z "$relative_path" ] \
                || [[ "$relative_path" = /* ]] \
                || [[ "$relative_path" = ".." ]] \
                || [[ "$relative_path" = ../* ]] \
                || [[ "$relative_path" = */../* ]] \
                || [[ "$relative_path" = */.. ]]; then
            error "Rejected unsafe report path: $relative_path"
            ((fail_count++))
            continue
        fi
        local output_path="${output_dir}/${relative_path}"

        # 创建目标目录
        local target_dir=$(dirname "$output_path")
        mkdir -p "$target_dir"

        # 下载文件内容
        local encoded_path file_response
        encoded_path=$(_urlencode "$file_path")
        file_response=$(api_call "/reports/download?path=${encoded_path}")
        if echo "$file_response" | jq -e '.success' > /dev/null; then
            echo "$file_response" | jq -r '.content' > "$output_path"
            echo "✓ Downloaded: $relative_path"
            ((success_count++))
        else
            echo "✗ Failed: $relative_path"
            ((fail_count++))
        fi
    done < <(echo "$response" | jq -c '.files[] | {path, relative_path}')

    echo ""
    if [ "$fail_count" -gt 0 ]; then
        error "Report download incomplete: ${success_count} succeeded, ${fail_count} failed"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    success "Report folder downloaded to: $output_dir"
}

# List all reports
gms-rt-reports-list() {
    check_jq
    # --json mode must emit machine-readable data; the fixed-width human
    # table below is for terminals only (agents pay for every padded column).
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/reports/list" | jq '.'
        return $?
    fi
    echo "📋 Listing all reports..."
    local response=$(api_call "/reports/list")
    local count=$(echo "$response" | jq '.reports | length')

    if [ "$count" -eq 0 ]; then
        warning "No reports found"
        return
    fi

    echo "Found $count report(s):"
    echo ""
    printf "%-30s %-20s %-8s %-8s %-8s %-8s %-10s\n" "CLIENT" "TYPE" "PASS" "FAIL" "TOTAL" "RATE%" "TIMESTAMP"
    printf "%-30s %-20s %-8s %-8s %-8s %-8s %-10s\n" "------" "----" "----" "----" "-----" "-----" "---------"

    echo "$response" | jq -r '.reports[] |
        "\(.client_id // "N/A") \(.test_type // "N/A") \(.pass // 0) \(.fail // 0) \(.total // 0) \(.pass_rate // "N/A") \(.timestamp // "N/A")"' |
        while read -r client type pass fail total rate timestamp; do
            printf "%-30s %-20s %-8s %-8s %-8s %-10s %s\n" "$client" "$type" "$pass" "$fail" "$total" "$rate" "$timestamp"
        done
}

# ==============================================================================
# SSH Commands
# ==============================================================================

# Test SSH ping between test host and client
gms-rt-ssh-ping() {
    local test_host_ip="$1"
    local client_ip="$2"
    [ -z "$test_host_ip" ] && { error "Test host IP required. Usage: gms-rt-ssh-ping <test_host_ip> <client_ip>"; return 1; }
    [ -z "$client_ip" ] && { error "Client IP required. Usage: gms-rt-ssh-ping <test_host_ip> <client_ip>"; return 1; }
    check_jq
    echo "🌐 Testing SSH connectivity..."
    local data
    data=$(jq -cn --arg test_host_ip "$test_host_ip" --arg client_ip "$client_ip" \
        '{test_host_ip: $test_host_ip, client_ip: $client_ip}')
    local response=$(api_call "/ssh/ping" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        local reachable=$(echo "$response" | jq -r '.reachable')
        local latency=$(echo "$response" | jq -r '.latency')
        if [ "$reachable" = "true" ]; then
            success "Network reachable (latency: $latency)"
        else
            warning "Network not reachable"
        fi
        # Show route commands if available; tolerate both object
        # ({linux:[],windows:[]}) and plain array ([...]) shapes and
        # missing sides, so a successful ping never fails on formatting.
        local route_commands=$(echo "$response" | jq -r '
            .route_commands // empty |
            if type == "array" then {linux: ., windows: []}
            elif type == "object" then {linux: (.linux // []), windows: (.windows // [])}
            else {linux: [], windows: []} end' 2>/dev/null)
        if [ -n "$route_commands" ] && [ "$route_commands" != "null" ]; then
            echo ""
            echo "📋 Suggested route commands:"
            echo ""
            echo "${YELLOW}Linux:${NC}"
            echo "$route_commands" | jq -r '.linux[]' 2>/dev/null || true
            echo ""
            echo "${YELLOW}Windows:${NC}"
            echo "$route_commands" | jq -r '.windows[]' 2>/dev/null || true
        fi
    else
        error "Network test failed"
    fi
}

# Check SSH route
gms-rt-ssh-route() {
    check_jq
    echo "🛣️  Checking SSH route..."
    api_call "/ssh/route" | jq '.'
}


# Check SSHD status (returns install guide if not installed)
gms-rt-ssh-sshd() {
    local device_host="$1"
    check_jq

    if [ -n "$device_host" ]; then
        # 验证格式：必须包含 @ 符号
        if [[ "$device_host" != *@* ]]; then
            error "❌ 设备主机格式错误：'$device_host'"
            echo "   正确格式应为：user@ip（例如：${DEFAULT_SSH_USER}@192.168.1.100）" >&2
            return 1
        fi
        echo "🔍 Checking SSHD status for $device_host..."
        # Use GET with query parameter (like USB/IP status)
        local response=$(api_call "/ssh/sshd?device_host=$(_urlencode "$device_host")")
    else
        echo "🔍 Checking SSHD status for current client..."
        local response=$(api_call "/ssh/sshd")
    fi

    # 解析响应
    local installed=$(echo "$response" | jq -r '.installed')
    local running=$(echo "$response" | jq -r '.running')

    # 显示简洁的状态摘要
    if [ "$installed" = "true" ]; then
        if [ "$running" = "true" ]; then
            echo "✅ SSHD 已安装并运行中"
        else
            echo "⚠️  SSHD 已安装但未运行"
        fi
    else
        echo "❌ SSHD 未安装"
        echo ""
        echo "📋 Windows 电脑安装指南:"
        echo "$response" | jq -r '.install_guide'
    fi
}

# ==============================================================================
# System Commands
# ==============================================================================

# System docs
gms-rt-system-docs() {
    check_jq
    echo "📚 Getting API documentation..."
    api_call "/system/docs" | jq '.'
}

# Health check
gms-rt-system-health() {
    check_jq
    echo "🏥 Checking server health..."
    api_call "/system/health" | jq '.'
}

# Validate a remote CLI host before device, firmware, or test automation.
gms-rt-system-doctor() {
    local scope="${1:-read}"
    case "$scope" in
        read|device|firmware|gsi|test) ;;
        *)
            error "Usage: gms-rt-system-doctor [read|device|firmware|gsi|test]"
            return "$GMS_RT_EXIT_USAGE"
            ;;
    esac
    check_jq || return "$GMS_RT_EXIT_OPERATION"

    local binaries blockers health auth devices suites
    local authenticated auth_required elevated device_count suite_count
    local firmware_upload_mode="${GMS_BURN_UPLOAD_MODE:-auto}"
    local direct_firmware_transfer=false ssh_found=false transfer_found=false
    local ready=true result_status=0 binary found
    binaries=$(jq -cn '{}')
    blockers=$(jq -cn '[]')

    for binary in curl jq; do
        if command -v "$binary" >/dev/null 2>&1; then found=true; else found=false; fi
        binaries=$(echo "$binaries" | jq -c --arg name "$binary" --argjson found "$found" '. + {($name): $found}')
        if [ "$found" != "true" ]; then
            blockers=$(echo "$blockers" | jq -c --arg value "missing_binary:$binary" '. + [$value]')
            ready=false
            result_status="$GMS_RT_EXIT_OPERATION"
        fi
    done
    if [ "$scope" = "firmware" ] || [ "$scope" = "gsi" ]; then
        if command -v ssh >/dev/null 2>&1; then ssh_found=true; fi
        binaries=$(echo "$binaries" | jq -c --argjson found "$ssh_found" '. + {ssh: $found}')
        if command -v rsync >/dev/null 2>&1; then
            binaries=$(echo "$binaries" | jq -c '. + {rsync: true, scp: null}')
            transfer_found=true
        elif command -v scp >/dev/null 2>&1; then
            binaries=$(echo "$binaries" | jq -c '. + {rsync: false, scp: true}')
            transfer_found=true
        else
            binaries=$(echo "$binaries" | jq -c '. + {rsync: false, scp: false}')
        fi
        if [ "$ssh_found" = "true" ] && [ "$transfer_found" = "true" ]; then
            direct_firmware_transfer=true
        elif [ "$firmware_upload_mode" = "direct" ] || [ "$scope" = "gsi" ]; then
            blockers=$(echo "$blockers" | jq -c '. + ["direct_firmware_transfer_unavailable"]')
            ready=false
            result_status="$GMS_RT_EXIT_OPERATION"
        fi
    fi

    health=$(api_call "/system/health") || return $?
    if ! echo "$health" | jq -e 'type == "object"' >/dev/null 2>&1; then
        health=$(jq -cn --arg raw "$health" '{raw: $raw}')
        blockers=$(echo "$blockers" | jq -c '. + ["invalid_health_response"]')
        ready=false
        result_status="$GMS_RT_EXIT_NETWORK"
    fi

    auth=$(api_call "/auth/status") || return $?
    authenticated=$(echo "$auth" | jq -r '.authenticated == true')
    auth_required=$(echo "$auth" | jq -r '.auth_required == true')
    elevated=$(echo "$auth" | jq -r '.elevated == true')
    if [ "$auth_required" = "true" ] && [ "$authenticated" != "true" ]; then
        blockers=$(echo "$blockers" | jq -c '. + ["authentication_required"]')
        ready=false
        result_status="$GMS_RT_EXIT_AUTH"
    elif { [ "$scope" = "firmware" ] || [ "$scope" = "gsi" ]; } \
            && [ "$elevated" != "true" ]; then
        blockers=$(echo "$blockers" | jq -c '. + ["administrator_elevation_required"]')
        ready=false
        result_status="$GMS_RT_EXIT_PERMISSION"
    fi

    devices='null'
    suites='null'
    device_count=0
    suite_count=0
    if { [ "$auth_required" != "true" ] || [ "$authenticated" = "true" ]; } \
            && [ "$scope" != "read" ]; then
        devices=$(api_call "/devices/list?force_refresh=true") || return $?
        if echo "$devices" | jq -e 'type == "array"' >/dev/null 2>&1; then
            device_count=$(echo "$devices" | jq 'length')
        else
            blockers=$(echo "$blockers" | jq -c '. + ["invalid_devices_response"]')
            ready=false
            [ "$result_status" -ne 0 ] || result_status="$GMS_RT_EXIT_OPERATION"
        fi
        if [ "$device_count" -eq 0 ]; then
            blockers=$(echo "$blockers" | jq -c '. + ["no_visible_devices"]')
            ready=false
            [ "$result_status" -ne 0 ] || result_status="$GMS_RT_EXIT_CONFLICT"
        fi
    fi
    if { [ "$auth_required" != "true" ] || [ "$authenticated" = "true" ]; } \
            && [ "$scope" = "test" ]; then
        suites=$(api_call "/test/suites") || return $?
        suite_count=$(echo "$suites" | jq -r '.count // (.suites | length) // 0' 2>/dev/null || printf '0')
        if ! [[ "$suite_count" =~ ^[0-9]+$ ]] || [ "$suite_count" -eq 0 ]; then
            suite_count=0
            blockers=$(echo "$blockers" | jq -c '. + ["no_available_test_suites"]')
            ready=false
            [ "$result_status" -ne 0 ] || result_status="$GMS_RT_EXIT_CONFLICT"
        fi
    fi

    jq -cn \
        --argjson success "$ready" \
        --arg scope "$scope" \
        --arg version "$GMS_RT_VERSION" \
        --arg server "$SERVER_URL" \
        --argjson binaries "$binaries" \
        --argjson health "$health" \
        --argjson auth "$auth" \
        --argjson device_count "$device_count" \
        --argjson suite_count "$suite_count" \
        --arg firmware_upload_mode "$firmware_upload_mode" \
        --argjson direct_firmware_transfer "$direct_firmware_transfer" \
        --argjson blockers "$blockers" \
        '{
            success: $success,
            ready: $success,
            scope: $scope,
            cli_version: $version,
            server: $server,
            checks: {
                binaries: $binaries,
                controller: $health,
                authentication: $auth,
                visible_devices: $device_count,
                available_test_suites: $suite_count,
                firmware_upload_mode: $firmware_upload_mode,
                direct_firmware_transfer: $direct_firmware_transfer
            },
            blockers: $blockers
        }'
    if [ "$result_status" -ne 0 ]; then
        diagnostic "Doctor found blockers: $(echo "$blockers" | jq -r 'join(", ")')"
    fi
    return "$result_status"
}

# Download skills ZIP
gms-rt-system-skills() {
    local skill_name="${1:-gms-remote-test}"
    local encoded_skill target temporary http_status curl_status exit_code
    encoded_skill=$(_urlencode "$skill_name")
    target="${skill_name}-skills.zip"
    temporary="${target}.tmp.$$"
    echo "📁 Downloading skills directory as ZIP..."
    echo "URL: ${API_BASE}/system/skills?skill_name=${encoded_skill}"
    echo "Saving to: ${target}"
    _refresh_tls_args
    _ensure_auth_cookie_jar || return 1
    http_status=$(curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
        -o "$temporary" -w '%{http_code}' --max-time "$CURL_TIMEOUT" \
        "${API_BASE}/system/skills?skill_name=${encoded_skill}")
    curl_status=$?
    exit_code=$(_http_exit_code "$http_status")
    if [ "$curl_status" -eq 0 ] && [ "$exit_code" -eq 0 ]; then
        mv -f -- "$temporary" "$target"
        _record_api_exit_code 0
        success "Skills ZIP downloaded successfully"
        ls -lh "$target"
    else
        rm -f -- "$temporary"
        [ "$curl_status" -eq 0 ] || exit_code="$GMS_RT_EXIT_NETWORK"
        _record_api_exit_code "$exit_code"
        error "Failed to download skills ZIP"
        return "$exit_code"
    fi
}

# Reinstall the latest package from the bound Controller.
gms-rt-system-update() {
    [ "$#" -eq 0 ] || {
        error "Usage: gms-rt-system-update"
        return "$GMS_RT_EXIT_USAGE"
    }
    # 旧实现查找相邻的 install.sh，但该脚本已随包结构
    # 迁移删除——现代更新生命周期是 `gms-agent update`（registry → 校验 →
    # versions/<v>/ → 整包重激活）。优先取本脚本旁边的 gms-agent（安装的
    # runtime 与源码检出都成立），退回已安装的 current 链接。
    local gms_agent
    gms_agent="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gms-agent"
    if [ ! -f "$gms_agent" ]; then
        gms_agent="${GMS_AGENT_RUNTIME_ROOT:-${HOME}/.local/share/gms-remote-test}/current/scripts/gms-agent"
    fi
    if [ ! -f "$gms_agent" ]; then
        error "gms-agent not found; run the bootstrap install first"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    # TLS 配置与当前会话保持一致：gms-agent 下载层读取
    # GMS_INSTALL_CA_CERT / GMS_INSTALL_INSECURE。
    GMS_INSTALL_CA_CERT="${GMS_INSTALL_CA_CERT:-${GMS_CURL_CA_CERT:-}}" \
    GMS_INSTALL_INSECURE="${GMS_CURL_INSECURE:-${GMS_INSTALL_INSECURE:-0}}" \
        python3 "$gms_agent" update
}


# Open terminal on test host (SSH connection)
gms-rt-terminal-open() {
    local host="${1:-}"
    local user="${2:-}"
    local port="${3:-}"

    # 显示帮助信息
    if [[ "$host" == "-h" ]] || [[ "$host" == "--help" ]]; then
        echo "🖥️  Open SSH terminal on test host"
        echo ""
        echo "Usage: gms-rt-terminal-open [host] [user] [port]"
        echo ""
        echo "Parameters:"
        echo "  host  - Test host IP address (default: from API config)"
        echo "  user  - SSH username (default: from API config)"
        echo "  port  - SSH port (default: from API config)"
        echo ""
        echo "Examples:"
        echo "  gms-rt-terminal-open                    # Use API config"
        echo "  gms-rt-terminal-open 192.168.1.100      # Specify host"
        echo "  gms-rt-terminal-open 192.168.1.100 $DEFAULT_SSH_USER  # Full parameters"
        echo ""
        return 0
    fi
    if [ "$GMS_RT_NON_INTERACTIVE" = "1" ]; then
        error "Interactive SSH terminal is disabled by --non-interactive"
        return "$GMS_RT_EXIT_USAGE"
    fi

    # 如果没有提供参数，优先使用本地配置，回退到API获取SSH连接信息
    if [ -z "$host" ] && [ -z "$user" ] && [ -z "$port" ]; then
        echo "🖥️  Opening terminal on test host (using config)..."

        # 优先尝试本地配置文件（更快，避免网络调用）
        local config_host=""
        local config_files=(
            "${GMS_WEB_APP_DIR}/configs/config.json"
            "${HOME}/GMS_Remote_Test/web_app/configs/config.json"
        )

        for config_file in "${config_files[@]}"; do
            if [ -f "$config_file" ]; then
                config_host=$(grep -o '"ubuntu_host": *"[^"]*"' "$config_file" 2>/dev/null | cut -d'"' -f4)
                if [ -n "$config_host" ]; then
                    host="$config_host"
                    user="$DEFAULT_SSH_USER"
                    port="22"
                    echo "📂 Using local config: $config_file"
                    break
                fi
            fi
        done

        # 如果本地配置未找到，回退到API调用
        if [ -z "$host" ]; then
            echo "📡 Fetching SSH connection info from API..."

            local api_response=$(api_call "/terminal/open" 2>/dev/null)

            if [ $? -ne 0 ] || [ -z "$api_response" ]; then
                error "Failed to connect to API server at ${SERVER_URL}"
                echo ""
                echo "💡 Troubleshooting:"
                echo "   1. Check if the API server is running: systemctl status gms-web-app"
                echo "   2. Verify server URL: echo \$GMS_REMOTE_TEST_SERVER"
                echo "   3. Test connection with the configured CA/TLS settings: gms-rt-terminal-open"
                return 1
            fi

            # 检查API响应是否成功并一次性提取所有字段（优化jq性能）
            local parsed_data=$(echo "$api_response" | jq -r 'if .success then "\(.host)|\(.user)|\(.port // 22)" else empty end' 2>/dev/null)

            if [ -z "$parsed_data" ]; then
                local error_msg=$(echo "$api_response" | jq -r '.error // "Unknown error"' 2>/dev/null)
                error "API returned error: $error_msg"
                return 1
            fi

            # 从解析的数据中提取字段（避免多次jq调用）
            IFS='|' read -r host user port <<< "$parsed_data"

            if [ -z "$host" ] || [ -z "$user" ]; then
                error "Failed to extract SSH connection info from API response"
                return 1
            fi

            echo "✓ API config loaded successfully"
        fi

        echo "🐧 Host: $user@$host"
        echo "🔌 Port: $port"
        echo ""
    else
        # 使用用户提供的参数（优先级高于API配置）
        user="${user:-$DEFAULT_SSH_USER}"
        port="${port:-22}"
        echo "🖥️  Opening terminal on test host: $user@$host:$port"
    fi

    echo "🔐 Establishing SSH connection..."
    echo ""

    # 直接使用ssh命令打开终端
    if command -v ssh &> /dev/null; then
        ssh -p "$port" "$user@$host"
    else
        error "ssh command not found. Please install OpenSSH client"
        return 1
    fi
}

# Terminal push command - Push file to test host
gms-rt-terminal-push() {
    local file_path="$1"
    local target_path="${2:-${GMS_WEB_APP_DIR}/tmp}"

    # 显示帮助信息
    if [[ "$file_path" == "-h" ]] || [[ "$file_path" == "--help" ]]; then
        echo "📤 Push file to test host directory"
        echo ""
        echo "Usage: gms-rt-terminal-push <file_path> [target_path]"
        echo ""
        echo "Parameters:"
        echo "  file_path    - Path to local file to upload (required)"
        echo "  target_path  - Target directory on test host (default: ${GMS_WEB_APP_DIR}/tmp)"
        echo ""
        echo "Examples:"
        echo "  gms-rt-terminal-push ./config.json                    # Use default target"
        echo "  gms-rt-terminal-push ./script.sh /tmp/scripts         # Custom target"
        echo "  gms-rt-terminal-push ./firmware.zip ${GMS_WEB_APP_DIR} # Absolute path"
        echo ""
        return 0
    fi

    [ -z "$file_path" ] && { error "File path required. Usage: gms-rt-terminal-push <file_path> [target_path]"; return 1; }
    [ ! -f "$file_path" ] && { error "File not found: $file_path"; return 1; }

    check_jq
    local filename=$(basename "$file_path")
    echo "📤 Pushing file to terminal: $filename"
    echo "📁 Target path: $target_path"

    local body
    body=$(api_call "/terminal/push" "POST" "" \
        -F "file=@${file_path}" \
        -F "path=${target_path}" \
        -F "auto_rename=true") || {
        local call_status=$?
        error "Failed to push file"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return "$call_status"
    }

    if echo "$body" | jq -e '.success' > /dev/null; then
        success "File pushed successfully"
        echo "$body" | jq '.'
    else
        local msg=$(extract_api_error "$body")
        error "Failed to push file: $msg"
        return 1
    fi
}


# ==============================================================================
# Test Management Commands
# ==============================================================================

# Durable Cluster Job status commands. Test launches return cluster_job_id;
# these endpoints are the authoritative way for agents to follow completion.
gms-rt-jobs-list() {
    local limit="${1:-100}"
    if ! [[ "$limit" =~ ^[1-9][0-9]*$ ]] || [ "$limit" -gt 500 ]; then
        error "Usage: gms-rt-jobs-list [limit: 1-500]"
        return "$GMS_RT_EXIT_USAGE"
    fi
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    api_call "/cluster/jobs?limit=${limit}" | jq '.'
}

gms-rt-jobs-status() {
    local job_id="${1:-}"
    [ -n "$job_id" ] || {
        error "Usage: gms-rt-jobs-status <job_id>"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    api_call "/cluster/jobs/$(_urlencode "$job_id")" | jq '.'
}

gms-rt-jobs-events() {
    local job_id="${1:-}"
    local after="${2:--1}"
    local limit="${3:-500}"
    [ -n "$job_id" ] || {
        error "Usage: gms-rt-jobs-events <job_id> [after_sequence] [limit]"
        return "$GMS_RT_EXIT_USAGE"
    }
    if ! [[ "$after" =~ ^-?[0-9]+$ ]] || ! [[ "$limit" =~ ^[1-9][0-9]*$ ]] || [ "$limit" -gt 2000 ]; then
        error "after_sequence must be an integer and limit must be between 1 and 2000"
        return "$GMS_RT_EXIT_USAGE"
    fi
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    api_call "/cluster/jobs/$(_urlencode "$job_id")/events?after=${after}&limit=${limit}" | jq '.'
}

gms-rt-jobs-cancel() {
    local job_id="${1:-}"
    [ -n "$job_id" ] || {
        error "Usage: gms-rt-jobs-cancel <job_id>"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    api_call "/cluster/jobs/$(_urlencode "$job_id")/cancel" "POST" "{}" | jq '.'
}

gms-rt-jobs-follow() {
    # 一次调用代替 jobs_status + jobs_events 的 ping-pong。
    # Returns current status + incremental events since a cursor; when the
    # job already finished it appends a compact failed-case summary (parsed
    # server-side from test_result.xml) so agents never page raw logs.
    local job_id="${1:-}"
    local after=-1
    local limit=100
    [ -n "$job_id" ] || {
        error "Usage: gms-rt-jobs-follow <job_id> [--after SEQUENCE] [--limit N]"
        return "$GMS_RT_EXIT_USAGE"
    }
    shift
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --after) shift; [ "$#" -gt 0 ] && after="$1" || return "$GMS_RT_EXIT_USAGE" ;;
            --after=*) after="${1#*=}" ;;
            --limit) shift; [ "$#" -gt 0 ] && limit="$1" || return "$GMS_RT_EXIT_USAGE" ;;
            --limit=*) limit="${1#*=}" ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    if ! [[ "$after" =~ ^-?[0-9]+$ ]] || ! [[ "$limit" =~ ^[1-9][0-9]*$ ]]; then
        error "--after must be an integer and --limit a positive integer"
        return "$GMS_RT_EXIT_USAGE"
    fi
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    local status_json events_json next_cursor
    status_json=$(api_call "/cluster/jobs/$(_urlencode "$job_id")")
    local call_status=$?
    if [ "$call_status" -ne 0 ]; then
        printf '%s\n' "$status_json"
        return "$call_status"
    fi
    events_json=$(api_call "/cluster/jobs/$(_urlencode "$job_id")/events?after=${after}&limit=${limit}")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        events_json='{"events":[]}'
    fi
    next_cursor=$(echo "$events_json" | jq -r '.next_cursor // (.events | if length > 0 then (map(.sequence // .seq) | max) else "'"$after"'" end) // "'"$after"'"' 2>/dev/null)
    local status
    status=$(echo "$status_json" | jq -r '.job.status // empty')
    local summary_json='null'
    case "$status" in
        completed|failed|cancelled)
            # Terminal state: attach the failed-case summary (server parses
            # test_result.xml). Errors degrade to null, never fail the call.
            summary_json=$(api_call "/reports/failure-summary?cluster_job_id=$(_urlencode "$job_id")" 2>/dev/null \
                | jq '{failed: (.failed // null), cases: (.cases // null)}' 2>/dev/null) \
                || summary_json='null'
            ;;
    esac
    jq -n \
        --argjson status "$status_json" \
        --argjson events "$events_json" \
        --argjson after "$after" \
        --argjson next_cursor "${next_cursor:-$after}" \
        --argjson failure_summary "$summary_json" \
        '{
            job: ($status.job // $status),
            events_since_cursor: ($events.events // []),
            cursor: {before: $after, after: $next_cursor},
            failure_summary: $failure_summary
        }'
}

gms-rt-jobs-wait() {
    local job_id="${1:-}"
    [ -n "$job_id" ] || {
        error "Usage: gms-rt-jobs-wait <job_id> [--interval SECONDS] [--max-wait SECONDS]"
        return "$GMS_RT_EXIT_USAGE"
    }
    shift
    local interval=5
    local max_wait=21600
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --interval)
                shift
                [ "$#" -gt 0 ] || {
                    error "--interval requires a positive integer"
                    return "$GMS_RT_EXIT_USAGE"
                }
                interval="$1"
                ;;
            --interval=*) interval="${1#*=}" ;;
            --max-wait)
                shift
                [ "$#" -gt 0 ] || {
                    error "--max-wait requires a positive integer"
                    return "$GMS_RT_EXIT_USAGE"
                }
                max_wait="$1"
                ;;
            --max-wait=*) max_wait="${1#*=}" ;;
            *)
                error "Unexpected argument: $1"
                return "$GMS_RT_EXIT_USAGE"
                ;;
        esac
        shift
    done
    if ! [[ "$interval" =~ ^[1-9][0-9]*$ ]] || [ "$interval" -gt 60 ] \
            || ! [[ "$max_wait" =~ ^[1-9][0-9]*$ ]]; then
        error "--interval must be 1-60 seconds and --max-wait must be a positive integer"
        return "$GMS_RT_EXIT_USAGE"
    fi

    check_jq || return "$GMS_RT_EXIT_OPERATION"
    local response status started_at elapsed call_status
    started_at=$(date +%s)
    while true; do
        # api_call prints the error body on stdout and returns non-zero for
        # HTTP failures (404 not found, 401, ...); propagate that body so the
        # JSON envelope carries the reason instead of an empty output.
        response=$(api_call "/cluster/jobs/$(_urlencode "$job_id")")
        call_status=$?
        if [ "$call_status" -ne 0 ]; then
            printf '%s\n' "$response"
            error "Failed to fetch job $job_id: $(printf '%s' "$response" | jq -r '.error // .detail // empty' 2>/dev/null || printf '%s' "$response" | head -c 200)" || true
            # Preserve api_call's semantic exit code (3 auth / 4 permission /
            # 5 conflict / 6 network / 7 operation). Calling error() changes
            # $? to GMS_RT_EXIT_OPERATION, so never return $? here.
            return "$call_status"
        fi
        status=$(echo "$response" | jq -r '.job.status // empty')
        [ -n "$status" ] || {
            # Surface the server's error (e.g. 404 job not found) instead of
            # an empty envelope, so agents see *why* the wait failed.
            error "Job $job_id not found or response missing job.status: $(echo "$response" | jq -r '.error // .detail // empty' 2>/dev/null || printf '%s' "$response" | head -c 200)"
            echo "$response" | jq '.' 2>/dev/null || printf '%s\n' "$response"
            return "$GMS_RT_EXIT_OPERATION"
        }
        case "$status" in
            completed)
                echo "$response" | jq '.'
                return 0
                ;;
            failed|cancelled)
                echo "$response" | jq '.'
                diagnostic "Job $job_id finished with status: $status"
                return "$GMS_RT_EXIT_OPERATION"
                ;;
        esac
        elapsed=$(( $(date +%s) - started_at ))
        if [ "$elapsed" -ge "$max_wait" ]; then
            echo "$response" | jq '.'
            diagnostic "Timed out waiting for job $job_id (last status: $status)"
            return "$GMS_RT_EXIT_OPERATION"
        fi
        info "Job $job_id: $status (${elapsed}s/${max_wait}s)"
        sleep "$interval"
    done
}

# Clean test environment
gms-rt-test-clean() {
    check_jq
    echo "🧹 Cleaning test environment..."
    local response=$(api_call "/test/clean" "POST" "{}")
    echo "$response" | jq '.'
}

# Stream test logs
gms-rt-test-logs-stream() {
    echo "📡 Streaming test logs (Ctrl+C to stop)..."
    _refresh_tls_args
    _ensure_auth_cookie_jar || return 1
    curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -N "${API_BASE}/test/logs/stream"
}

# Start a test - delegates to /api/test/parse-args for intelligent parameter parsing
gms-rt-test-start() {
    check_jq

    # Collect all arguments into an array; separate out local options.
    local args=()
    local wait_for_job=0
    local wait_max=""
    local worker_id=""
    local first_param="${1:-}"

    # Show help if no arguments
    if [ -z "$first_param" ]; then
        _gms_rt_test_start_help
        return 1
    fi

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --wait)
                wait_for_job=1
                ;;
            --wait=*)
                wait_for_job=1
                wait_max="${1#*=}"
                ;;
            --max-wait)
                shift
                [ "$#" -gt 0 ] && { wait_for_job=1; wait_max="$1"; } || {
                    error "--max-wait requires a positive integer"
                    return "$GMS_RT_EXIT_USAGE"
                }
                ;;
            --max-wait=*)
                wait_for_job=1
                wait_max="${1#*=}"
                ;;
            --worker)
                shift
                [ "$#" -gt 0 ] && { worker_id="$1"; } || {
                    error "--worker requires a worker id"
                    return "$GMS_RT_EXIT_USAGE"
                }
                ;;
            --worker=*)
                worker_id="${1#*=}"
                ;;
            *) args+=("$1") ;;
        esac
        shift
    done
    if [ -n "$wait_max" ] && ! [[ "$wait_max" =~ ^[1-9][0-9]*$ ]]; then
        error "--max-wait requires a positive integer"
        return "$GMS_RT_EXIT_USAGE"
    fi
    first_param="${args[0]:-}"
    if [ -z "$first_param" ]; then
        _gms_rt_test_start_help
        return 1
    fi

    # Resolve short suite names (e.g. android-cts-17_r1) to tools paths so the
    # positional parser receives a canonical path argument.
    local positional=()
    local argument
    for argument in "${args[@]}"; do
        if [[ "$argument" == android-* ]] && [[ "$argument" != */* ]]; then
            local resolved
            resolved=$(_resolve_suite_reference "$argument") || resolved=""
            if [ -n "$resolved" ]; then
                info "Suite '$argument' resolved to: $resolved"
                positional+=("$resolved")
                continue
            fi
            # Leave the raw value in place; the server resolves short names
            # too and reports a precise error when it cannot match.
        fi
        positional+=("$argument")
    done
    args=("${positional[@]}")

    # Call API to parse arguments
    local params_json=$(printf '%s\n' "${args[@]}" | jq -R . | jq -s .)
    local parse_response=$(api_call "/test/parse-args" "POST" "{\"params\":$params_json}")

    # Check if parsing succeeded
    if ! echo "$parse_response" | jq -e '.success' > /dev/null 2>/dev/null; then
        local error_msg=$(extract_api_error "$parse_response")
        error "Failed to parse arguments: $error_msg"
        echo ""
        _gms_rt_test_start_help
        return 1
    fi

    # Extract all parsed values in single jq call (efficiency optimization)
    local device=$(echo "$parse_response" | jq -r '.device // ""')
    local test_type=$(echo "$parse_response" | jq -r '.test_type // ""')
    local test_module=$(echo "$parse_response" | jq -r '.test_module // ""')
    local test_case=$(echo "$parse_response" | jq -r '.test_case // ""')
    local test_suite=$(echo "$parse_response" | jq -r '.test_suite // ""')
    local retry_dir=$(echo "$parse_response" | jq -r '.retry_dir // ""')
    local warnings=$(echo "$parse_response" | jq -r '.warnings[]?' 2>/dev/null)
    if [ -n "$device" ]; then
        device=$(_resolve_devices "$device")
    fi

    # Display parsed parameters
    if [ -n "$retry_dir" ]; then
        echo "🔄 Starting test retry..."
        echo "  Report: $retry_dir"
        [ -n "$device" ] && echo "  Device: $device"
        [ -n "$test_type" ] && echo "  Test_Type: $test_type"
        [ -n "$test_suite" ] && echo "  Test_Suite: $test_suite"
    else
        echo "🚀 Starting test..."
        echo "  Device: $device"
        [ -n "$test_type" ] && echo "  Test_Type: $test_type"
        [ -n "$test_module" ] && echo "  Test_Module: $test_module"
        [ -n "$test_case" ] && echo "  Test_Case: $test_case"
        [ -n "$test_suite" ] && echo "  Test_Suite: $test_suite"
    fi

    # Display warnings
    if [ -n "$warnings" ]; then
        echo ""
        echo "$warnings" | while read -r warning; do
            warning "⚠️  $warning"
        done
    fi

    # Build request data for /api/test/start
    # Use simple jq syntax to avoid parsing issues
    local data=$(jq -n \
        --arg rdir "$retry_dir" \
        --arg dev "$device" \
        --arg ttype "$test_type" \
        --arg tmod "$test_module" \
        --arg tcase "$test_case" \
        --arg tsuite "$test_suite" \
        --arg wid "$worker_id" \
        '{
            retry_dir: $rdir,
            devices: [$dev],
            test_type: $ttype,
            test_module: $tmod,
            test_case: $tcase,
            test_suite: $tsuite
        }
        + (if $wid == "" then {} else {worker_id: $wid} end)')

    # Call /api/test/start
    local response=$(api_call "/test/start" "POST" "$data")

    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Test started successfully"
        local job_id
        job_id=$(echo "$response" | jq -r '.data.cluster_job_id // .cluster_job_id // .data.job_id // .job_id // empty')
        if [ "$wait_for_job" = "1" ] && [ -n "$job_id" ]; then
            local wait_args=("$job_id")
            [ -n "$wait_max" ] && wait_args+=(--max-wait "$wait_max")
            gms-rt-jobs-wait "${wait_args[@]}"
            return $?
        fi
        echo "$response" | jq '.'
    else
        local msg=$(extract_api_error "$response")
        error "Failed to start test: $msg"
        return 1
    fi
}

# Help function for gms-rt-test-start
_gms_rt_test_start_help() {
    cat << EOF
Usage:
  Mode 1 (Direct test): gms-rt-test-start <DEVICE> [TYPE] [MODULE/SUITE] [CASE/SUITE] [SUITE] [--wait] [--max-wait SECONDS]
  Mode 2 (Retry report): gms-rt-test-start --retry <REPORT_TIMESTAMP> [DEVICE] [TYPE] [SUITE] [--wait]

智能参数识别：
  - 包含 '/' 的参数自动识别为路径（test_suite）
  - 以 android- 开头的短套件名（如 android-cts-17_r1）自动解析为套件 tools 路径
  - 其他参数按位置识别为 test_module, test_case

示例:
  gms-rt-test-start RK3572GMS4 CTS android-cts-17_r1
  gms-rt-test-start RK3572GMS4 CTS /path/to/android-cts/tools
  gms-rt-test-start RK3572GMS4 CTS TestModuleName
  gms-rt-test-start RK3572GMS4 CTS TestModuleName TestCaseName
  gms-rt-test-start RK3572GMS4 CTS TestModuleName TestCaseName /path/to/suite
  gms-rt-test-start RK3572GMS4 CTS TestModuleName --wait --max-wait 3600

模块名说明:
  - MODULE 必须是 tradefed 模块名（即套件 testcases/ 下的文件名去掉扩展名，如 CtsHardwareTestCases），
    不是 apk 里的 instrumentation/java 包名（如 android.hardware.cts）——包名不是模块名，tradefed 会报
    "No matched tradefed modules"。
  - 不确定模块名时，先查询: curl "$GMS_RT_BASE/test/suites/modules?query=<关键词>"，
    或用 Web 端固件/套件页的模块搜索。

Supported Test Types:
  CTS      - Compatibility Test Suite
  GTS      - Google Mobile Services Test Suite
  GTS-ROOT - GTS with root permissions
  STS      - Security Test Suite
  VTS      - Vendor Test Suite
  APTS     - Android Peripheral Test Suite
  GSI      - Generic System Image tests (uses CTS suite)

Examples:
  gms-rt-test-start RF8TC2W4JNH CTS CtsPermissionTestCases
  gms-rt-test-start RF8TC2W4JNH GTS-ROOT
  gms-rt-test-start --retry 2026.04.11_17.27.04.421_2920 RF8TC2W4JNH GTS
  gms-rt-test-start --retry 2026.04.11_17.27.04.421_2920 RF8TC2W4JNH /path/to/suite
EOF
}


gms-rt-test-status() {
    check_jq
    echo "📊 Checking test status..."
    api_call "/test/status" | jq '.'
}

# Stop running test
gms-rt-test-stop() {
    local job_id="${1:-}"
    check_jq
    echo "🛑 Stopping test..."
    local endpoint="/test/stop"
    [ -n "$job_id" ] && endpoint="${endpoint}?job_id=$(_urlencode "$job_id")"
    local response=$(api_call "$endpoint" "POST")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Test stopped successfully"
        echo "$response" | jq '.'
    else
        warning "Failed to stop test or no test was running"
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

# List available test suites
gms-rt-test-suites() {
    local base_path="${1:-}"
    check_jq
    # --json mode emits the raw suite inventory; the fixed-width table below
    # is terminal-only output.
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        local url="/test/suites"
        [ -n "$base_path" ] && url="/test/suites?base_path=$(_urlencode "$base_path")"
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    if [ -n "$base_path" ]; then
        echo "📋 Listing test suites under $base_path..."
    else
        echo "📋 Listing test suites..."
    fi
    local url="/test/suites"
    [ -n "$base_path" ] && url="/test/suites?base_path=$(_urlencode "$base_path")"
    local response=$(api_call "$url" "GET")
    if echo "$response" | jq -e '.success' > /dev/null; then
        local count=$(echo "$response" | jq '.count')
        success "Found $count test suite(s)"
        # Format output in 3 fixed-width columns
        echo ""
        printf "%-12s %-25s %-70s\n" "TYPE" "VERSION" "PATH"
        printf "%s\n" "$(printf '=%.0s' {1..107})"
        echo "$response" | jq -r '.suites[] | "\(.test_type)\t\(.version)\t\(.tools_path)"' | while IFS=$'\t' read -r type version path; do
            printf "%-12s %-25s %-70s\n" "$type" "$version" "$path"
        done
        echo ""
    else
        error "Failed to list test suites"
        echo "$response" | jq '.'
    fi
}

# List test suite results (tradefed list results) - Using HTTP API
gms-rt-test-suites-result() {
    local suite_path=""
    local force_refresh=0
    local argument

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -f|--force-refresh) force_refresh=1 ;;
            -h|--help)
                echo "Usage: gms-rt-test-suites-result <suite_path|suite_name> [--force-refresh]"
                echo "  suite_path  Full tools path, e.g. ~/GMS-Suite/android-cts-17_r1/android-cts/tools"
                echo "  suite_name  Short suite name, e.g. android-cts-17_r1 (resolved automatically)"
                return 0
                ;;
            *)
                [ -z "$suite_path" ] || {
                    error "Unexpected argument: $1"
                    return "$GMS_RT_EXIT_USAGE"
                }
                suite_path="$1"
                ;;
        esac
        shift
    done

    [ -z "$suite_path" ] && { error "Suite path required. Usage: gms-rt-test-suites-result <suite_path|suite_name> [--force-refresh]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    # Expand tilde to home directory
    suite_path="${suite_path/#\~/$HOME}"

    # Resolve short suite names (no path separator) against the suite inventory.
    if [[ "$suite_path" != */* ]]; then
        local resolved
        resolved=$(_resolve_suite_reference "$suite_path") || resolved=""
        if [ -n "$resolved" ]; then
            info "Suite '$suite_path' resolved to: $resolved"
            suite_path="$resolved"
        fi
        # Unresolved values are sent as-is; the server resolves short names
        # too and returns a precise error listing next steps.
    fi

    echo "📋 Listing test results for suite: $suite_path..."

    # Find tradefed binary (optional - API can auto-detect)
    local tradefed_bin=$(find "$suite_path" -maxdepth 1 -type f -executable -name '*-tradefed' 2>/dev/null | head -1)

    # Build request data
    local data
    data=$(jq -cn --arg suite_path "$suite_path" '{suite_path: $suite_path}')
    if [ -n "$tradefed_bin" ]; then
        data=$(echo "$data" | jq --arg bin "$tradefed_bin" '. + {tradefed_bin: $bin}')
    fi

    # Call HTTP API endpoint with optional force_refresh parameter
    local url="/test/suites/result"
    if [ "$force_refresh" = "1" ]; then
        url="$url?force_refresh=true"
        echo "🔄 Force refresh requested (bypassing cache)..."
    fi

    local start_time=$(date +%s.%3N)
    local response=$(api_call "$url" "POST" "$data")
    local api_call_status=$?
    local end_time=$(date +%s.%3N)
    local elapsed=$(echo "$end_time - $start_time" | bc)

    # Check if api_call succeeded
    if [ $api_call_status -ne 0 ]; then
        return 1
    fi

    # Also check if response is empty (api_call may have failed but returned 0)
    if [ -z "$response" ]; then
        error "No response from server"
        return 1
    fi

    if echo "$response" | jq -e '.success' > /dev/null; then
        local count=$(echo "$response" | jq '.count')
        local cached=$(echo "$response" | jq -r '.cached // false')

        if [ "$cached" = "true" ]; then
            local cache_age=$(echo "$response" | jq -r '.cache_age // 0')
            success "Found $count test result(s) (from cache, ${cache_age}s old)"
        else
            success "Found $count test result(s)"
        fi

        echo "⏱️  Query time: ${elapsed}s"
        echo ""
        # Output raw format (same as tradefed list results) - fast processing.
        # The pipeline's exit status must not turn a successful query into a
        # failure: an empty match just means no result rows for this suite.
        echo "$response" | jq -r '.raw_output' | grep -E 'Session|^[ ]*[0-9]' | grep -v -E '^04-|^D/|DeviceManager' || true
    else
        local msg=$(extract_api_error "$response")
        error "Failed to list test results: $msg"
        if [[ "$msg" == *suites_path* ]]; then
            diagnostic "提示: 传入套件 tools 目录完整路径, 或短套件名 (如 android-cts-17_r1); 运行 gms-rt-test-suites 查看可用套件。"
        fi
        echo "$response" | jq '.'
        return 1
    fi
}

# 列出套件可用模块（解析 testcases/ 目录，精确/模糊过滤）。
# 用法: gms-rt-test-modules <suite_path|suite_name> [--filter PATTERN]
gms-rt-test-modules() {
    local suite_path=""
    local filter_pattern=""
    local argument

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-test-modules <suite_path|suite_name> [--filter PATTERN]"
                echo "  suite_path  Full tools path, e.g. ~/GMS-Suite/android-cts-17_r1/android-cts/tools"
                echo "  suite_name  Short suite name, e.g. android-cts-17_r1 (resolved automatically)"
                echo "  --filter    Case-insensitive substring filter, e.g. CtsHardware"
                return 0
                ;;
            --filter)
                shift
                [ $# -gt 0 ] || { error "--filter requires a pattern"; return "$GMS_RT_EXIT_USAGE"; }
                filter_pattern="$1"
                ;;
            *)
                [ -z "$suite_path" ] || {
                    error "Unexpected argument: $1"
                    return "$GMS_RT_EXIT_USAGE"
                }
                suite_path="$1"
                ;;
        esac
        shift
    done

    [ -z "$suite_path" ] && { error "Suite path required. Usage: gms-rt-test-modules <suite_path|suite_name> [--filter PATTERN]"; return "$GMS_RT_EXIT_USAGE"; }

    suite_path="${suite_path/#\~/$HOME}"

    # Resolve short suite names against the suite inventory.
    if [[ "$suite_path" != */* ]]; then
        local resolved
        resolved=$(_resolve_suite_reference "$suite_path") || resolved=""
        if [ -n "$resolved" ]; then
            [ "$GMS_RT_QUIET" != "1" ] && info "Suite '$suite_path' resolved to: $resolved"
            suite_path="$resolved"
        fi
    fi

    # testcases/ lives next to the tools directory (suite tools path layout:
    # <suite>/android-cts/tools → <suite>/android-cts/testcases).
    local testcases_dir="${suite_path%/}/../testcases"
    if [ ! -d "$testcases_dir" ]; then
        error "testcases directory not found: $testcases_dir"
        diagnostic "提示: 传入套件的 tools 目录（如 ~/GMS-Suite/android-cts-17_r2/android-cts/tools）。"
        return "$GMS_RT_EXIT_OPERATION"
    fi

    # Modules are testcases/ entries: directories and *.config files.
    local modules
    modules=$(
        {
            find "$testcases_dir" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null
            find "$testcases_dir" -maxdepth 1 -type f -name '*.config' -printf '%f\n' 2>/dev/null | sed 's/\.config$//'
        } | sort -u
    )
    if [ -n "$filter_pattern" ]; then
        modules=$(echo "$modules" | grep -iF "$filter_pattern" || true)
    fi

    local count
    count=$(echo "$modules" | grep -c . || true)
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$modules" | jq -Rn '[inputs | select(length > 0)] | {success: true, count: length, modules: .}'
        return 0
    fi

    if [ -z "$modules" ]; then
        success "No modules matched (filter: $filter_pattern)"
        return 0
    fi
    success "Found $count module(s)$( [ -n "$filter_pattern" ] && echo " matching '$filter_pattern'" )"
    echo "$modules"
    return 0
}

# ==============================================================================
# APK Analysis Commands (suite module -> decompiled source)
# ==============================================================================

gms-rt-apk-resolve() {
    local module_query=""
    local suite_types=""
    local prefer="apk"
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-resolve <module_query> [--types cts,vts,gts,sts] [--prefer apk|jar]"
                echo "  module_query  Test module keyword, e.g. CtsCamera"
                echo "  --types       Comma-separated suite types (default cts,vts,gts,sts)"
                echo "  --prefer      Preferred artifact type: apk (default) or jar"
                return 0
                ;;
            --types)
                shift
                [ $# -gt 0 ] || { error "--types requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                suite_types="$1"
                ;;
            --prefer)
                shift
                [ $# -gt 0 ] || { error "--prefer requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    apk|jar) prefer="$1" ;;
                    *) error "--prefer accepts apk or jar"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            *)
                [ -z "$module_query" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                module_query="$1"
                ;;
        esac
        shift
    done
    [ -z "$module_query" ] && { error "Module query required. Usage: gms-rt-apk-resolve <module_query> [--types cts,vts,gts,sts] [--prefer apk|jar]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local url="/test/suites/modules/apk?query=$(_urlencode "$module_query")&prefer=$prefer"
    [ -n "$suite_types" ] && url="$url&suite_types=$(_urlencode "$suite_types")"

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    local response
    response=$(api_call "$url" "GET")
    if echo "$response" | jq -e '.success' > /dev/null; then
        echo "$response" | jq -r '.data | "module: \(.module)\ntype: \(.suite_type) \(.suite_version)\nartifact: \(.file_name)\nsuite_path: \(.analyze_suite_path)\nanalyze_path: \(.analyze_path)"'
    else
        error "No APK/JAR artifact resolved for '$module_query'"
        echo "$response" | jq '.'
    fi
}

gms-rt-apk-analyze() {
    local module_query=""
    local suite_types=""
    local prefer="apk"
    local do_wait=0
    local max_wait=300
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-analyze <module_query> [--types cts,vts,gts,sts] [--prefer apk|jar] [--wait] [--max-wait SECONDS]"
                echo "  Resolve a test module to its APK/JAR in the latest suites, copy it into an"
                echo "  analysis task, and start jadx decompilation. --wait polls until completed."
                return 0
                ;;
            --types)
                shift
                [ $# -gt 0 ] || { error "--types requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                suite_types="$1"
                ;;
            --prefer)
                shift
                [ $# -gt 0 ] || { error "--prefer requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    apk|jar) prefer="$1" ;;
                    *) error "--prefer accepts apk or jar"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            --wait) do_wait=1 ;;
            --max-wait)
                shift
                [ $# -gt 0 ] || { error "--max-wait requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                max_wait="$1"
                ;;
            *)
                [ -z "$module_query" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                module_query="$1"
                ;;
        esac
        shift
    done
    [ -z "$module_query" ] && { error "Module query required. Usage: gms-rt-apk-analyze <module_query> [--types cts,vts,gts,sts] [--prefer apk|jar] [--wait] [--max-wait SECONDS]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    # Step 1: resolve module keyword -> suite artifact
    local url="/test/suites/modules/apk?query=$(_urlencode "$module_query")&prefer=$prefer"
    [ -n "$suite_types" ] && url="$url&suite_types=$(_urlencode "$suite_types")"
    local resolve_resp
    resolve_resp=$(api_call "$url" "GET")
    if ! echo "$resolve_resp" | jq -e '.success' > /dev/null; then
        error "Module resolution failed"
        echo "$resolve_resp" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
    local suite_path analyze_path file_name module_name
    suite_path=$(echo "$resolve_resp" | jq -r '.data.analyze_suite_path')
    analyze_path=$(echo "$resolve_resp" | jq -r '.data.analyze_path')
    file_name=$(echo "$resolve_resp" | jq -r '.data.file_name')
    module_name=$(echo "$resolve_resp" | jq -r '.data.module')
    [ "$GMS_RT_QUIET" != "1" ] && info "Resolved '$module_query' -> $module_name ($file_name)"

    # Step 2: copy the artifact from the suite into an analysis task
    local copy_data copy_resp task_id start_resp
    copy_data=$(jq -cn --arg suite_path "$suite_path" --arg path "$analyze_path" '{suite_path: $suite_path, path: $path}')
    copy_resp=$(api_call "/test/suites/apk/analyze" "POST" "$copy_data")
    if ! echo "$copy_resp" | jq -e '.success' > /dev/null; then
        error "Failed to copy suite artifact into an analysis task"
        echo "$copy_resp" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
    task_id=$(echo "$copy_resp" | jq -r '.data.task_id')

    # Step 3: start jadx decompilation
    start_resp=$(api_call "/apk/analyze/$task_id" "POST")
    if ! echo "$start_resp" | jq -e '.success' > /dev/null; then
        error "Failed to start decompilation for task $task_id"
        echo "$start_resp" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi

    if [ "$do_wait" != "1" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            jq -cn --arg task_id "$task_id" --arg module "$module_name" --arg file "$file_name" \
                '{success: true, task_id: $task_id, module: $module, file: $file, status: "analyzing"}'
        else
            success "Decompilation started for $module_name (task $task_id)"
            echo "Poll with: gms-rt-apk-status $task_id"
        fi
        return 0
    fi

    # Step 4: poll until completed/error or --max-wait is exceeded
    local waited=0 interval=5 status="" status_resp
    while true; do
        status_resp=$(api_call "/apk/status/$task_id" "GET")
        status=$(echo "$status_resp" | jq -r '.data.status // empty')
        if [ -z "$status" ]; then
            error "Failed to read analysis status for task $task_id"
            echo "$status_resp" | jq '.'
            return "$GMS_RT_EXIT_OPERATION"
        fi
        case "$status" in
            completed|error) break ;;
        esac
        [ "$waited" -ge "$max_wait" ] && break
        sleep "$interval"
        waited=$((waited + interval))
    done

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg task_id "$task_id" --arg module "$module_name" --arg file "$file_name" --arg status "$status" \
            '{success: ($status == "completed"), task_id: $task_id, module: $module, file: $file, status: $status}'
        [ "$status" = "completed" ] && return 0
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ "$status" = "completed" ]; then
        success "Decompilation completed for $module_name (task $task_id)"
        echo "Browse: gms-rt-apk-source $task_id"
        return 0
    fi
    error "Decompilation not completed (status: ${status:-unknown}, waited ${waited}s)"
    echo "Task: $task_id (continue polling: gms-rt-apk-status $task_id)"
    return "$GMS_RT_EXIT_OPERATION"
}

# One task id -> status; no task id -> the full task list.
gms-rt-apk-status() {
    local task_id="${1:-}"
    shift 2>/dev/null || true
    [ "$#" -gt 0 ] && { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local response
    if [ -z "$task_id" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            api_call "/apk/tasks" "GET" | jq '.'
            return $?
        fi
        response=$(api_call "/apk/tasks" "GET")
        if echo "$response" | jq -e '.success' > /dev/null; then
            local count
            count=$(echo "$response" | jq '.data.total')
            if [ "$count" = "0" ]; then
                success "No APK analysis tasks"
                return 0
            fi
            success "Found $count APK analysis task(s)"
            printf "%-40s %-14s %-9s %s\n" "TASK" "STATUS" "PROGRESS" "FILE"
            echo "$response" | jq -r '.data.tasks[] | "\(.task_id)\t\(.status)\t\(.progress)\t\(.filename)"' |
                while IFS=$'\t' read -r tid status progress filename; do
                    printf "%-40s %-14s %-9s %s\n" "$tid" "$status" "$progress" "$filename"
                done
        else
            error "Failed to list APK analysis tasks"
            echo "$response" | jq '.'
            return "$GMS_RT_EXIT_OPERATION"
        fi
        return 0
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/apk/status/$task_id" "GET" | jq '.'
        return $?
    fi
    response=$(api_call "/apk/status/$task_id" "GET")
    if echo "$response" | jq -e '.success' > /dev/null; then
        echo "$response" | jq -r '.data | "task: \(.task_id)\nstatus: \(.status)\nprogress: \(.progress)\nfile: \(.filename)" + (if .error then "\nerror: \(.error)" else "" end)'
    else
        error "Failed to get APK analysis status"
        echo "$response" | jq '.'
    fi
}

# Manifest view; --permissions selects the declared-permissions subset.
gms-rt-apk-manifest() {
    local task_id=""
    local permissions=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-manifest <task_id> [--permissions]"
                echo "  --permissions  List only the permissions declared by the artifact"
                return 0
                ;;
            --permissions) permissions=1 ;;
            *)
                [ -z "$task_id" ] && task_id="$1" || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                ;;
        esac
        shift
    done
    [ -z "$task_id" ] && { error "Task ID required. Usage: gms-rt-apk-manifest <task_id> [--permissions]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    if [ "$permissions" = "1" ]; then
        api_call "/apk/permissions/$task_id" "GET" | jq '.'
        return $?
    fi
    api_call "/apk/manifest/$task_id" "GET" | jq '.'
}

gms-rt-apk-source() {
    local task_id=""
    local path=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-source <task_id> [path]"
                echo "  path  Relative path inside the decompiled sources (default: tree root)"
                echo "File contents: gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N]"
                return 0
                ;;
            *)
                [ -z "$task_id" ] && task_id="$1" || {
                    [ -z "$path" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                    path="$1"
                }
                ;;
        esac
        shift
    done
    [ -z "$task_id" ] && { error "Task ID required. Usage: gms-rt-apk-source <task_id> [path]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local url="/apk/source/$task_id"
    [ -n "$path" ] && url="$url?path=$(_urlencode "$path")"

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    local response
    response=$(api_call "$url" "GET")
    if ! echo "$response" | jq -e '.success' > /dev/null; then
        error "Failed to browse decompiled source"
        echo "$response" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
    echo "$response" | jq -r '.data.items[] | "\(.type)\t\(.path)"' |
        while IFS=$'\t' read -r type item_path; do
            printf "%-4s %s\n" "$type" "$item_path"
        done
}

# Unified source lookup: filename (name), file content (content), or Java
# symbol definition (symbol).
gms-rt-apk-search() {
    local task_id=""
    local query=""
    local mode="name"
    local limit=""
    local path_filter=""
    local symbol_line=0
    local positional=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-search <task_id> <query> [--mode name|content|symbol] [--limit N] [--path FILTER] [--line N]"
                echo "  --mode name     Search decompiled file names by substring (default)"
                echo "  --mode content  Search decompiled file content (path:line:column + snippet)"
                echo "  --mode symbol   Locate a best-effort Java symbol definition"
                return 0
                ;;
            --mode)
                shift
                [ $# -gt 0 ] || { error "--mode requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    name|content|symbol) mode="$1" ;;
                    *) error "--mode accepts name, content, or symbol"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            --query)
                shift
                [ $# -gt 0 ] || { error "--query requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                query="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            --path)
                shift
                [ $# -gt 0 ] || { error "--path requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                path_filter="$1"
                ;;
            --line)
                shift
                [ $# -gt 0 ] || { error "--line requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                symbol_line="$1"
                ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    if [ -z "$task_id" ] && [ "${#positional[@]}" -ge 1 ]; then
        task_id="${positional[0]}"
    fi
    if [ -z "$query" ] && [ "${#positional[@]}" -ge 2 ]; then
        query="${positional[1]}"
    fi
    [ -z "$task_id" ] || [ -z "$query" ] && { error "Usage: gms-rt-apk-search <task_id> <query> [--mode name|content|symbol] [--limit N] [--path FILTER] [--line N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local url response
    case "$mode" in
        name)
            [ -z "$path_filter" ] || { error "--path applies to content/symbol modes only"; return "$GMS_RT_EXIT_USAGE"; }
            [ "$symbol_line" = "0" ] || { error "--line applies to symbol mode only"; return "$GMS_RT_EXIT_USAGE"; }
            limit="${limit:-20}"
            url="/apk/search/$task_id?q=$(_urlencode "$query")&limit=$limit"
            if [ "$GMS_RT_OUTPUT" = "json" ]; then
                api_call "$url" "GET" | jq '.'
                return $?
            fi
            response=$(api_call "$url" "GET")
            if echo "$response" | jq -e '.success' > /dev/null; then
                echo "$response" | jq -r '.data.items[] | .path'
            else
                error "Failed to search decompiled source files"
                echo "$response" | jq '.'
                return "$GMS_RT_EXIT_OPERATION"
            fi
            ;;
        content)
            [ "$symbol_line" = "0" ] || { error "--line applies to symbol mode only"; return "$GMS_RT_EXIT_USAGE"; }
            limit="${limit:-50}"
            url="/apk/source-search/$task_id?q=$(_urlencode "$query")&limit=$limit"
            [ -n "$path_filter" ] && url="$url&path_filter=$(_urlencode "$path_filter")"
            if [ "$GMS_RT_OUTPUT" = "json" ]; then
                api_call "$url" "GET" | jq '.'
                return $?
            fi
            api_call "$url" "GET" | jq -r '.data | "query: \(.query) matches: \(.total)\(if .limited then " (limited)" else "" end) scanned_files: \(.scanned_files)", (.matches[] | "\(.path):\(.line):\(.column): \(.snippet)")'
            ;;
        symbol)
            [ -z "$limit" ] || { error "--limit applies to name/content modes only"; return "$GMS_RT_EXIT_USAGE"; }
            url="/apk/definition/$task_id?symbol=$(_urlencode "$query")&line=$symbol_line"
            [ -n "$path_filter" ] && url="$url&path=$(_urlencode "$path_filter")"
            api_call "$url" "GET" | jq '.'
            ;;
    esac
}

gms-rt-apk-download() {
    local task_id="$1"
    local output="$2"
    [ -z "$task_id" ] && { error "Task ID required. Usage: gms-rt-apk-download <task_id> [output.zip]"; return "$GMS_RT_EXIT_USAGE"; }
    [ -z "$output" ] && output="${task_id}_decompiled.zip"
    echo "⬇ Downloading decompiled source for $task_id -> $output"
    # Binary payload: stream straight to the target file via curl extra args.
    if ! api_call "/apk/download/$task_id" "GET" "" -o "$output" -sS; then
        rm -f -- "$output"
        error "Download failed (HTTP error); check the task id and that analysis completed"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ ! -s "$output" ]; then
        rm -f -- "$output"
        error "Download failed: $output is empty"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    success "Saved: $output ($(du -h "$output" | cut -f1))"
}

# ==============================================================================
# USB/IP Commands
# ==============================================================================

# Install USB/IP on specified host
gms-rt-usbip-install() {
    local device_host="$1"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-usbip-install <user@ip>"; return 1; }
    check_jq
    echo "🔧 Installing USB/IP on host: $device_host..."
    local data
    data=$(jq -cn --arg device_host "$device_host" '{device_host: $device_host}')
    local response=$(api_call "/usbip/install" "POST" "$data")
    echo "$response" | jq '.'
}

# Start USB/IP connection
gms-rt-usbip-connect() {
    local device_host="$1"
    local device_password
    device_password=$(_secret_value "${2:-${GMS_REMOTE_DEVICE_PASSWORD:-}}")
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-usbip-connect <user@ip> [password]"; return 1; }
    check_jq
    echo "🔌 Starting USB/IP connection to $device_host..."
    local data
    data=$(jq -cn --arg device_host "$device_host" --arg device_password "$device_password" \
        '{device_host: $device_host, device_password: $device_password}
         | with_entries(select(.value != ""))')
    local response=$(api_call "/usbip/connect" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "USB/IP connection started"
        echo "$response" | jq '.'
    else
        local msg=$(extract_api_error "$response")
        error "Failed to start USB/IP: $msg"
    fi
}

# Stop USB/IP connection
gms-rt-usbip-disconnect() {
    local device_host="$1"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-usbip-disconnect <user@ip>"; return 1; }
    check_jq
    echo "🔌 Stopping USB/IP connection for $device_host..."
    local data
    data=$(jq -cn --arg device_host "$device_host" '{device_host: $device_host}')
    local response=$(api_call "/usbip/disconnect" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "USB/IP stopped"
        echo "$response" | jq '.'
    else
        warning "Failed to stop USB/IP or not connected"
        echo "$response" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

# Check USB/IP status
gms-rt-usbip-status() {
    local device_host="$1"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-usbip-status <user@ip>"; return 1; }
    check_jq
    echo "🔌 Checking USB/IP status for $device_host..."
    # Use GET with query parameter
    api_call "/usbip/status?device_host=$(_urlencode "$device_host")" | jq '.'
}

# ==============================================================================
# User Management Commands
# ==============================================================================

# Get current user info
gms-rt-users-current() {
    check_jq
    echo "👤 Getting current user info..."
    api_call "/users/current" | jq '.'
}

# Detect user
gms-rt-users-detect() {
    local ip="$1"
    local username="${2:-}"
    local password
    password=$(_secret_value "${3:-${GMS_REMOTE_DEVICE_PASSWORD:-}}")
    check_jq
    echo "🔍 Detecting user for $ip..."
    local data
    data=$(jq -cn --arg ip "$ip" --arg username "$username" --arg password "$password" \
        '{ip: $ip, username: $username, password: $password}
         | with_entries(select(.value != ""))')
    local response=$(api_call "/users/detect" "POST" "$data")
    echo "$response" | jq '.'
}

# List users
gms-rt-users-list() {
    check_jq
    echo "👥 Listing all users..."
    api_call "/users/list" | jq '.'
}

# Set username
gms-rt-users-set-username() {
    local username="${1:-$(whoami)}"
    [ -z "$username" ] && { error "Username required. Usage: gms-rt-users-set-username [username]"; return 1; }
    check_jq
    echo "👤 Setting username to $username..."
    local data
    data=$(jq -cn --arg username "$username" '{username: $username}')
    local response=$(api_call "/users/set-username" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# VPN Management Commands
# ==============================================================================

# Connect to VPN
gms-rt-vpn-connect() {
    check_jq
    echo "🔐 Connecting to VPN..."
    local response=$(api_call "/vpn/connect" "POST")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "VPN connected"
        echo "$response" | jq '.'
    else
        local error_msg=$(extract_api_error "$response")
        error "Failed to connect VPN: $error_msg"
        return 1
    fi
}

# Disconnect VPN
gms-rt-vpn-disconnect() {
    check_jq
    echo "🔌 Disconnecting VPN..."
    local response=$(api_call "/vpn/disconnect" "POST")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "VPN disconnected"
        echo "$response" | jq '.'
    else
        local error_msg=$(extract_api_error "$response")
        error "Failed to disconnect VPN: $error_msg"
        return 1
    fi
}

# Check VPN status
gms-rt-vpn-status() {
    check_jq
    echo "📊 Checking VPN status..."
    local response=$(api_call "/vpn/status")
    if echo "$response" | jq -e '.success' > /dev/null; then
        local connected=$(echo "$response" | jq -r '.connected')
        if [ "$connected" = "true" ]; then
            success "VPN is connected"
            echo "$response" | jq '.'
        else
            warning "VPN is not connected"
            echo "$response" | jq '.'
        fi
    else
        error "Failed to get VPN status"
    fi
}

# ==============================================================================
# Help Function
# ==============================================================================

_gms_rt_command_names() {
    declare -F | awk '$3 ~ /^gms-rt-/ {print $3}' | sort -u
}

_gms_rt_command_usage() {
    case "$1" in
        gms-rt-adb-forward-status|gms-rt-auth-credential-mode|gms-rt-auth-elevation-reset|gms-rt-auth-logout|gms-rt-auth-status|gms-rt-cluster-workers|gms-rt-config-read|gms-rt-desktop-vnc-status|gms-rt-desktop-vnc-stop|gms-rt-devices-list|gms-rt-devices-user-locked|gms-rt-reports-list|gms-rt-ssh-route|gms-rt-system-capabilities|gms-rt-system-commands|gms-rt-system-docs|gms-rt-system-health|gms-rt-system-help|gms-rt-system-selfcheck|gms-rt-system-version|gms-rt-test-clean|gms-rt-test-logs-stream|gms-rt-test-status|gms-rt-users-current|gms-rt-users-list|gms-rt-vpn-connect|gms-rt-vpn-disconnect|gms-rt-vpn-status)
            printf '%s' "$1"
            ;;
        gms-rt-adb-forward-start) printf '%s' 'gms-rt-adb-forward-start <source_worker_id> <target_worker_id> <serial> [serial...]' ;;
        gms-rt-adb-forward-stop) printf '%s' 'gms-rt-adb-forward-stop <source_worker_id> <target_worker_id>' ;;
        gms-rt-auth-login) printf '%s' 'gms-rt-auth-login [username] [--password-stdin]' ;;
        gms-rt-auth-elevate) printf '%s' 'gms-rt-auth-elevate [admin_username] [--password-stdin]' ;;
        gms-rt-auth-scopes-check) printf '%s' 'gms-rt-auth-scopes-check [--requires s1,s2]' ;;
        gms-rt-agent-enroll) printf '%s' 'gms-rt-agent-enroll <ENROLLMENT_CODE> [--out FILE] [--profile NAME]' ;;
        gms-rt-agent-tokens) printf '%s' 'gms-rt-agent-tokens' ;;
        gms-rt-agent-enroll-code) printf '%s' 'gms-rt-agent-enroll-code --name <NAME> [--scopes s1,s2] [--workers w1,w2|*] [--devices d1,d2|*] [--expires-days N] [--ttl-minutes N]' ;;
        gms-rt-agent-token-revoke) printf '%s' 'gms-rt-agent-token-revoke <TOKEN_ID>' ;;
        gms-rt-approval-create) printf '%s' 'gms-rt-approval-create --tool <gms_rt_tool> --device <serial>[,<serial>...] [--command <command>|--firmware-sha256 <sha256> [--wipe-data true|false] [--burn-mode auto|uf]]' ;;
        gms-rt-burn-firmware) printf '%s' 'gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--approval-token TOKEN] [--wait-online[=SECONDS]]' ;;
        gms-rt-burn-gsi) printf '%s' 'gms-rt-burn-gsi <gsi_path> <devices> [wipe_data] [--wait-online[=SECONDS]]' ;;
        gms-rt-burn-serial) printf '%s' 'gms-rt-burn-serial <device_id> <serial>' ;;
        gms-rt-cluster-devices) printf '%s' 'gms-rt-cluster-devices [--worker WORKER_ID] [--query REGEX]' ;;
        gms-rt-cluster-resolve) printf '%s' 'gms-rt-cluster-resolve --device <serial> [--worker <worker_id>]' ;;
        gms-rt-config-update) printf '%s' 'gms-rt-config-update <key> <value>' ;;
        gms-rt-desktop-validate) printf '%s' 'gms-rt-desktop-validate <user@ip>' ;;
        gms-rt-desktop-vnc-start) printf '%s' 'gms-rt-desktop-vnc-start [host] [password] [vnc_password]' ;;
        gms-rt-system-command-describe) printf '%s' 'gms-rt-system-command-describe <gms-rt-command>' ;;
        gms-rt-devices-info|gms-rt-devices-reboot|gms-rt-devices-remount|gms-rt-devices-bootloader-lock|gms-rt-devices-bootloader-unlock|gms-rt-devices-bootloader-status)
            printf '%s' "$1 <devices>"
            ;;
        gms-rt-devices-wait) printf '%s' 'gms-rt-devices-wait <devices> [--state online|fastboot|any] [--interval SECONDS] [--max-wait SECONDS]' ;;
        gms-rt-devices-console) printf '%s' 'gms-rt-devices-console [port_key] [--tail N] [--date YYYYMMDD]' ;;
        gms-rt-devices-shell) printf '%s' 'gms-rt-devices-shell <device_id> [--approval-token TOKEN] [command]' ;;
        gms-rt-devices-scrcpy) printf '%s' 'gms-rt-devices-scrcpy DEVICE1 [DEVICE2 ...]' ;;
        gms-rt-devices-screencap) printf '%s' 'gms-rt-devices-screencap <device_id>' ;;
        gms-rt-devices-ui-dump) printf '%s' 'gms-rt-devices-ui-dump <device_id>' ;;
        gms-rt-devices-snapshot) printf '%s' 'gms-rt-devices-snapshot <device_id>' ;;
        gms-rt-devices-wifi) printf '%s' 'gms-rt-devices-wifi <devices> <ssid> [password]' ;;
        gms-rt-files-progress) printf '%s' 'gms-rt-files-progress [upload_id]' ;;
        gms-rt-opengrok-search) printf '%s' 'gms-rt-opengrok-search <query> [true|false]' ;;
        gms-rt-jobs-follow) printf '%s' 'gms-rt-jobs-follow <job_id> [--after SEQUENCE] [--limit N]' ;;
        gms-rt-devices-logcat) printf '%s' 'gms-rt-devices-logcat <device_id> [-c] [logcat args]' ;;
        gms-rt-devices-push) printf '%s' 'gms-rt-devices-push <device_id> <local_file> <remote_path>' ;;
        gms-rt-jobs-list) printf '%s' 'gms-rt-jobs-list [limit:1..500]' ;;
        gms-rt-jobs-status) printf '%s' 'gms-rt-jobs-status <job_id>' ;;
        gms-rt-jobs-events) printf '%s' 'gms-rt-jobs-events <job_id> [after_sequence] [limit]' ;;
        gms-rt-jobs-wait) printf '%s' 'gms-rt-jobs-wait <job_id> [--interval SECONDS] [--max-wait SECONDS]' ;;
        gms-rt-jobs-cancel) printf '%s' 'gms-rt-jobs-cancel <job_id>' ;;
        gms-rt-system-doctor) printf '%s' 'gms-rt-system-doctor [read|device|firmware|gsi|test]' ;;
        gms-rt-system-update) printf '%s' 'gms-rt-system-update' ;;
        gms-rt-reports-analyze) printf '%s' 'gms-rt-reports-analyze <local_report.zip|test_result.xml|report_timestamp|keyword>' ;;
        gms-rt-reports-delete) printf '%s' 'gms-rt-reports-delete <report_timestamp>' ;;
        gms-rt-reports-download) printf '%s' 'gms-rt-reports-download <report_timestamp> [output_dir]' ;;
        gms-rt-ssh-ping) printf '%s' 'gms-rt-ssh-ping <test_host_ip> <client_ip>' ;;
        gms-rt-ssh-sshd) printf '%s' 'gms-rt-ssh-sshd [user@ip]' ;;
        gms-rt-system-skills) printf '%s' 'gms-rt-system-skills [skill_name]' ;;
        gms-rt-terminal-open) printf '%s' 'gms-rt-terminal-open [host] [user] [port]' ;;
        gms-rt-terminal-push) printf '%s' 'gms-rt-terminal-push <file_path> [target_path]' ;;
        gms-rt-test-start) printf '%s' 'gms-rt-test-start <device> [type] [module] [case] [suite] [--worker ID] [--wait[=SECONDS]] [--max-wait SECONDS] | --retry <timestamp> [device] [type] [suite] [options]' ;;
        gms-rt-test-stop) printf '%s' 'gms-rt-test-stop [job_id]' ;;
        gms-rt-test-suites) printf '%s' 'gms-rt-test-suites [base_path]' ;;
        gms-rt-test-suites-result) printf '%s' 'gms-rt-test-suites-result <tools_path|suite_name> [--force-refresh]' ;;
        gms-rt-test-modules) printf '%s' 'gms-rt-test-modules <tools_path|suite_name> [--filter PATTERN]' ;;
        gms-rt-apk-resolve) printf '%s' 'gms-rt-apk-resolve <module_query> [--types cts,vts,gts,sts] [--prefer apk|jar]' ;;
        gms-rt-apk-analyze) printf '%s' 'gms-rt-apk-analyze <module_query> [--types cts,vts,gts,sts] [--prefer apk|jar] [--wait] [--max-wait SECONDS]' ;;
        gms-rt-apk-status) printf '%s' 'gms-rt-apk-status [task_id]' ;;
        gms-rt-apk-manifest) printf '%s' 'gms-rt-apk-manifest <task_id> [--permissions]' ;;
        gms-rt-apk-source) printf '%s' 'gms-rt-apk-source <task_id> [path]' ;;
        gms-rt-apk-search) printf '%s' 'gms-rt-apk-search <task_id> <query> [--mode name|content|symbol] [--limit N] [--path FILTER] [--line N]' ;;
        gms-rt-apk-download) printf '%s' 'gms-rt-apk-download <task_id> [output.zip]' ;;
        gms-rt-usbip-install) printf '%s' 'gms-rt-usbip-install <user@ip>' ;;
        gms-rt-usbip-connect) printf '%s' 'gms-rt-usbip-connect <user@ip> [password]' ;;
        gms-rt-usbip-disconnect) printf '%s' 'gms-rt-usbip-disconnect <user@ip>' ;;
        gms-rt-usbip-status) printf '%s' 'gms-rt-usbip-status <user@ip>' ;;
        gms-rt-users-detect) printf '%s' 'gms-rt-users-detect <ip> [username] [password]' ;;
        gms-rt-users-set-username) printf '%s' 'gms-rt-users-set-username [username]' ;;
        gms-rt-redmine-issue-fetch) printf '%s' 'gms-rt-redmine-issue-fetch <issue_id_or_url> [--download none|analyzable|all] [--refresh|--no-refresh] [--wait] [--max-wait SECONDS] [--dry-run]' ;;
        gms-rt-redmine-issue-show) printf '%s' 'gms-rt-redmine-issue-show <snapshot_id | issue_id> [--issue|--snapshot]' ;;
        gms-rt-redmine-journals) printf '%s' 'gms-rt-redmine-journals <snapshot_id> [--limit N] [--cursor C]' ;;
        gms-rt-redmine-attachments) printf '%s' 'gms-rt-redmine-attachments <snapshot_id>' ;;
        gms-rt-redmine-attachment-download) printf '%s' 'gms-rt-redmine-attachment-download <artifact_id> [output_path]' ;;
        gms-rt-redmine-credentials-status) printf '%s' 'gms-rt-redmine-credentials-status' ;;
        gms-rt-redmine-triage) printf '%s' 'gms-rt-redmine-triage [--stale-days N] [--list-limit N] [--refresh]' ;;
        gms-rt-redmine-history-search) printf '%s' 'gms-rt-redmine-history-search <query> [--limit N] [--exclude-issue-id N] [--resolved-only]' ;;
        gms-rt-artifact-read) printf '%s' 'gms-rt-artifact-read <artifact_id> [--offset N] [--limit N]' ;;
        gms-rt-redmine-artifact-image) printf '%s' 'gms-rt-redmine-artifact-image <artifact_id>' ;;
        gms-rt-artifact-search) printf '%s' 'gms-rt-artifact-search <snapshot_id> <query> [--limit N]' ;;
        gms-rt-apk-analyze-attachment) printf '%s' 'gms-rt-apk-analyze-attachment <snapshot_id> <artifact_id>' ;;
        gms-rt-apk-source-read) printf '%s' 'gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N]' ;;
        gms-rt-sdk-sources) printf '%s' 'gms-rt-sdk-sources' ;;
        gms-rt-sdk-search) printf '%s' 'gms-rt-sdk-search --source ID --revision REV --query TEXT [--path FILTER] [--limit N]' ;;
        gms-rt-sdk-read) printf '%s' 'gms-rt-sdk-read SDK_RESULT_ID [--offset N] [--limit N]' ;;
        *) printf '%s' "$1 [arguments]" ;;
    esac
}

_gms_rt_command_summary() {
    case "$1" in
        gms-rt-adb-forward-status) printf '%s' 'List ADB proxy Workers and active source-to-target assignments' ;;
        gms-rt-adb-forward-start) printf '%s' 'Forward selected device serials from one Worker to another through adbproxy-rs' ;;
        gms-rt-adb-forward-stop) printf '%s' 'Stop one ADB proxy source-to-target Worker assignment' ;;
        gms-rt-auth-status) printf '%s' 'Show whether authentication is required and describe the current principal/session' ;;
        gms-rt-auth-login) printf '%s' 'Create and save a human API session (password prompt or --password-stdin)' ;;
        gms-rt-auth-logout) printf '%s' 'Revoke the current human API session and remove its local cookie jar' ;;
        gms-rt-auth-elevate) printf '%s' 'Activate administrator elevation for the current human session' ;;
        gms-rt-auth-elevation-reset) printf '%s' 'Clear administrator elevation from the current human session' ;;
        gms-rt-auth-credential-mode) printf '%s' 'Show whether this CLI invocation uses an Agent Token file or a session cookie' ;;
        gms-rt-auth-scopes-check) printf '%s' 'Pre-flight check that the current credential carries required agent scopes (default: Redmine evidence chain)' ;;
        gms-rt-agent-enroll) printf '%s' 'Exchange a one-shot enrollment code for an Agent Service Token stored as a 0600 file' ;;
        gms-rt-agent-tokens) printf '%s' 'List Agent Service Tokens (admin; metadata only, raw tokens are never stored)' ;;
        gms-rt-agent-enroll-code) printf '%s' 'Mint a one-shot enrollment code for a build server agent (admin + elevation)' ;;
        gms-rt-agent-token-revoke) printf '%s' 'Revoke an Agent Service Token by id (admin + elevation)' ;;
        gms-rt-approval-create) printf '%s' 'Create a one-shot approval token for a destructive agent action (human session only)' ;;
        gms-rt-cluster-workers) printf '%s' 'List registered Cluster Workers and their current availability' ;;
        gms-rt-cluster-devices) printf '%s' 'List the authoritative cross-Worker device inventory, optionally filtered by Worker or serial' ;;
        gms-rt-cluster-resolve) printf '%s' 'Resolve an exact device serial to its owning Worker without guessing ambiguous matches' ;;
        gms-rt-config-read) printf '%s' 'Read the Controller configuration visible to the current principal' ;;
        gms-rt-config-update) printf '%s' 'Update one Controller configuration key' ;;
        gms-rt-desktop-validate) printf '%s' 'Validate SSH access to a desktop host' ;;
        gms-rt-desktop-vnc-start) printf '%s' 'Start the configured VNC service on a desktop host' ;;
        gms-rt-desktop-vnc-status) printf '%s' 'Read the configured desktop VNC service status' ;;
        gms-rt-desktop-vnc-stop) printf '%s' 'Stop the configured desktop VNC service' ;;
        gms-rt-burn-firmware) printf '%s' 'Transfer and burn a firmware image, with optional approved Agent execution and online wait' ;;
        gms-rt-burn-gsi) printf '%s' 'Transfer and burn a GSI image, with optional online wait' ;;
        gms-rt-burn-serial) printf '%s' 'Program a serial number on one device' ;;
        gms-rt-system-capabilities) printf '%s' 'Print the CLI contract, global options, and exit codes' ;;
        gms-rt-system-command-describe) printf '%s' 'Describe one CLI command for machine execution' ;;
        gms-rt-system-commands) printf '%s' 'Print the machine-readable command inventory' ;;
        gms-rt-system-selfcheck) printf '%s' 'One-shot read-only agent environment report (auth, health, devices, suites, hints)' ;;
        gms-rt-system-doctor) printf '%s' 'Check controller, session, tools, devices, and suites for an operation scope' ;;
        gms-rt-system-help) printf '%s' 'Show the human-readable CLI command list' ;;
        gms-rt-system-update) printf '%s' 'Reinstall the latest Skill and CLI command links' ;;
        gms-rt-system-version) printf '%s' 'Print the local CLI version' ;;
        gms-rt-system-docs) printf '%s' 'Read the Controller API documentation catalog' ;;
        gms-rt-system-health) printf '%s' 'Check Controller service liveness and version' ;;
        gms-rt-system-skills) printf '%s' 'Download a Controller-hosted Skill archive to the current directory' ;;
        gms-rt-devices-list) printf '%s' 'List devices visible through the Controller device inventory' ;;
        gms-rt-devices-info) printf '%s' 'Read detailed properties for one or more devices' ;;
        gms-rt-devices-wait) printf '%s' 'Wait for selected devices to become visible in the requested state' ;;
        gms-rt-devices-logcat) printf '%s' 'Capture device logcat via adb shell logcat -v time (-c clears the buffer first; dump mode in non-interactive sessions)' ;;
        gms-rt-devices-console) printf '%s' 'List Controller serial ports or read one port retained console log' ;;
        gms-rt-devices-bootloader-lock) printf '%s' 'Lock the bootloader on one or more devices' ;;
        gms-rt-devices-bootloader-unlock) printf '%s' 'Unlock the bootloader on one or more devices' ;;
        gms-rt-devices-bootloader-status) printf '%s' 'Read bootloader lock status for one or more devices' ;;
        gms-rt-devices-reboot) printf '%s' 'Reboot one or more devices and report partial failures' ;;
        gms-rt-devices-remount) printf '%s' 'Remount one or more devices read-write and optionally reboot when required' ;;
        gms-rt-devices-shell) printf '%s' 'Open a human ADB shell or run one approved device command' ;;
        gms-rt-devices-push) printf '%s' 'Push one local file to a device through ADB' ;;
        gms-rt-devices-wifi) printf '%s' 'Connect one or more devices to a Wi-Fi network' ;;
        gms-rt-devices-scrcpy) printf '%s' 'Start human interactive screen mirroring for one or more devices' ;;
        gms-rt-devices-user-locked) printf '%s' 'List devices currently locked by a platform user' ;;
        gms-rt-devices-screencap) printf '%s' 'Capture one device screenshot as a base64 PNG payload' ;;
        gms-rt-devices-ui-dump) printf '%s' 'Read one device UI hierarchy as structured elements' ;;
        gms-rt-devices-snapshot) printf '%s' 'Collect a fixed read-only device diagnostic snapshot (build, activity, lock and owners)' ;;
        gms-rt-files-progress) printf '%s' 'Read upload progress, optionally for one upload id' ;;
        gms-rt-opengrok-search) printf '%s' 'Search the configured OpenGrok index' ;;
        gms-rt-reports-list) printf '%s' 'List test reports visible to the current principal' ;;
        gms-rt-reports-analyze) printf '%s' 'Analyze a local report file or a uniquely resolved saved report' ;;
        gms-rt-reports-download) printf '%s' 'Download a saved report tree into a local output directory' ;;
        gms-rt-reports-delete) printf '%s' 'Delete one saved report by timestamp' ;;
        gms-rt-ssh-ping) printf '%s' 'Test network reachability between a test host and client address' ;;
        gms-rt-ssh-route) printf '%s' 'Read the configured SSH route information' ;;
        gms-rt-ssh-sshd) printf '%s' 'Inspect SSHD status locally or on a user@host target and show setup guidance' ;;
        gms-rt-terminal-open) printf '%s' 'Open a human interactive SSH terminal on the test host' ;;
        gms-rt-terminal-push) printf '%s' 'Upload a local file into a test-host directory' ;;
        gms-rt-test-suites-result) printf '%s' 'List tradefed results for a suite path or short suite name' ;;
        gms-rt-test-modules) printf '%s' 'List available tradefed modules for a suite (testcases/ directory)' ;;
        gms-rt-apk-resolve) printf '%s' 'Resolve a test module keyword to its APK/JAR artifact in the latest suites' ;;
        gms-rt-apk-analyze) printf '%s' 'Resolve a module artifact, copy it from the suite, and start jadx decompilation' ;;
        gms-rt-apk-status) printf '%s' 'Get one APK analysis task status, or list all tasks when no task id is given' ;;
        gms-rt-apk-manifest) printf '%s' 'Show the parsed AndroidManifest.xml, or its declared permissions with --permissions' ;;
        gms-rt-apk-source) printf '%s' 'Browse the decompiled source tree (read file windows with gms-rt-apk-source-read)' ;;
        gms-rt-apk-search) printf '%s' 'Search decompiled sources by filename (name), file content (content), or Java symbol definition (symbol)' ;;
        gms-rt-apk-download) printf '%s' 'Download the decompiled source ZIP of an analysis task' ;;
        gms-rt-redmine-issue-fetch) printf '%s' 'Create/refresh a full Redmine evidence snapshot (raw JSON, journals, attachments)' ;;
        gms-rt-redmine-issue-show) printf '%s' 'Show snapshot completeness plus issue fields and description head; accepts snapshot_id or issue_id (resolves the latest snapshot)' ;;
        gms-rt-redmine-journals) printf '%s' 'Read full (untruncated) issue journals with cursor pagination' ;;
        gms-rt-redmine-attachments) printf '%s' 'List evidence artifacts with kind, size, sha256, and per-attachment status' ;;
        gms-rt-redmine-attachment-download) printf '%s' 'Stream one evidence artifact original to a client path (reports saved path/bytes/sha256)' ;;
        gms-rt-redmine-credentials-status) printf '%s' 'Pre-flight check that the owner account has Redmine credentials configured (no secret material returned)' ;;
        gms-rt-redmine-triage) printf '%s' "List today's pending Redmine issues (waiting_my_reply + no_reply_3_days, deduped; read-only)" ;;
        gms-rt-redmine-history-search) printf '%s' "Search all historical Redmine issues (local archive + site search) for similar problems and reusable fixes (read-only)" ;;
        gms-rt-artifact-read) printf '%s' 'Read a text/log artifact derived text by char window (--offset/--limit)' ;;
        gms-rt-redmine-artifact-image) printf '%s' 'Return an image artifact as JSON with base64 payload and metadata (for MCP image tooling)' ;;
        gms-rt-artifact-search) printf '%s' 'Search description, journals, and artifact text for a fixed query with evidence refs' ;;
        gms-rt-apk-analyze-attachment) printf '%s' 'Import a Redmine .apk artifact into the JADX analysis pipeline (owner-scoped)' ;;
        gms-rt-apk-source-read) printf '%s' 'Read a window of one decompiled source file by task-relative path' ;;
        gms-rt-sdk-sources) printf '%s' 'List admin-configured SDK source providers and default revisions' ;;
        gms-rt-sdk-search) printf '%s' 'Search an SDK source at a pinned revision; matches carry commit-bound result ids' ;;
        gms-rt-sdk-read) printf '%s' 'Read a commit-pinned SDK source window by signed result id (returns commit and blob sha256)' ;;
        gms-rt-test-start) printf '%s' 'Start a test with smart args, suite short names, device prefixes, and optional --wait' ;;
        gms-rt-test-stop) printf '%s' 'Compatibility stop entry: cancel an explicit job, or the only active owned test job' ;;
        gms-rt-test-status) printf '%s' 'Read the legacy aggregate test execution status' ;;
        gms-rt-test-clean) printf '%s' 'Clean the test execution environment' ;;
        gms-rt-test-logs-stream) printf '%s' 'Stream live test logs until interrupted' ;;
        gms-rt-test-suites) printf '%s' 'List available test suite installations and tools paths' ;;
        gms-rt-jobs-list) printf '%s' 'List durable test jobs visible to the current session' ;;
        gms-rt-jobs-status) printf '%s' 'Get authoritative durable test job state' ;;
        gms-rt-jobs-events) printf '%s' 'Read incremental durable test job events' ;;
        gms-rt-jobs-wait) printf '%s' 'Wait for a durable test job to reach a terminal state' ;;
        gms-rt-jobs-cancel) printf '%s' 'Request cancellation of a durable test job' ;;
        gms-rt-jobs-follow) printf '%s' 'Return job status, incremental events and a terminal failure summary in one call' ;;
        gms-rt-usbip-install) printf '%s' 'Install USB/IP prerequisites on a specified device host' ;;
        gms-rt-usbip-connect) printf '%s' 'Start a USB/IP connection to a specified device host' ;;
        gms-rt-usbip-disconnect) printf '%s' 'Stop a USB/IP connection to a specified device host' ;;
        gms-rt-usbip-status) printf '%s' 'Read USB/IP status for a specified device host' ;;
        gms-rt-users-current) printf '%s' 'Read the current platform user identity' ;;
        gms-rt-users-detect) printf '%s' 'Detect a platform username from a remote host identity' ;;
        gms-rt-users-list) printf '%s' 'List platform users' ;;
        gms-rt-users-set-username) printf '%s' 'Set the current platform username' ;;
        gms-rt-vpn-connect) printf '%s' 'Connect the configured VPN' ;;
        gms-rt-vpn-disconnect) printf '%s' 'Disconnect the configured VPN' ;;
        gms-rt-vpn-status) printf '%s' 'Read configured VPN connection status' ;;
        gms-rt-burn-*) printf '%s' 'Perform an elevated firmware operation' ;;
        gms-rt-devices-*) printf '%s' 'Inspect or operate Android devices' ;;
        gms-rt-test-*) printf '%s' 'Inspect or operate GMS test execution' ;;
        gms-rt-auth-*) printf '%s' 'Inspect or change the authenticated CLI session' ;;
        gms-rt-system-*) printf '%s' 'Inspect controller system capabilities' ;;
        *) printf '%s' 'GMS Remote Test CLI operation' ;;
    esac
}

_gms_rt_command_catalog() {
    local command
    while IFS= read -r command; do
        printf '%s\t%s\t%s\n' \
            "$command" \
            "$(_gms_rt_command_usage "$command")" \
            "$(_gms_rt_command_summary "$command")"
    done < <(_gms_rt_command_names)
}

gms-rt-system-commands() {
    check_jq || return 1
    _gms_rt_command_catalog | jq -Rn --arg version "$GMS_RT_VERSION" '
        def category:
            split("-")[2] // "other";
        def mode:
            if test("terminal-open|devices-shell|devices-scrcpy|devices-logcat|test-logs-stream|auth-(login|elevate)")
            then "interactive"
            elif test(
                "burn-|config-update|bootloader-(lock|unlock)|devices-(reboot|remount|push|wifi)"
                + "|reports-delete|apk-analyze|terminal-push|test-(start|stop|clean)|usbip-(install|connect|disconnect)"
                + "|vpn-(connect|disconnect)|adb-forward-(start|stop)|desktop-vnc-(start|stop)|users-set-username"
                + "|jobs-cancel|system-update"
                + "|agent-(enroll|enroll-code|token-revoke)|approval-create|auth-(logout|elevation-reset)"
            )
            then "mutating"
            else "read_only"
            end;
        # Explicit capability hints so safety classification
        # never depends on the mode regex alone. Downstream consumers ignore
        # unknown fields; server-side scope checks are never skipped because
        # of these hints.
        def risk:
            if test("apk-analyze-attachment")
            then {external_side_effects: false, resource_intensive: true, required_scope: "apk.analyze_own"}
            elif test("artifact-(read|search)|redmine-attachment-download|redmine-attachments")
            then {external_side_effects: false, resource_intensive: false, required_scope: "artifacts.read_own"}
            elif test("redmine-|artifact-")
            then {external_side_effects: false, resource_intensive: false, required_scope: "redmine.read"}
            elif test("sdk-")
            then {external_side_effects: false, resource_intensive: false, required_scope: "sdk.read"}
            elif test("^(gms-rt-cluster-devices|gms-rt-devices-(list|console))$")
            then {external_side_effects: false, resource_intensive: false, required_scope: "devices.read"}
            elif test("^gms-rt-jobs-(list|status|events|follow|wait)$")
            then {external_side_effects: false, resource_intensive: false, required_scope: "jobs.read"}
            elif test("^(gms-rt-jobs-cancel|gms-rt-test-stop)$")
            then {external_side_effects: true, resource_intensive: false, required_scope: "tests.cancel"}
            elif test("^gms-rt-test-start$")
            then {external_side_effects: true, resource_intensive: true, required_scope: "tests.execute"}
            elif test("^gms-rt-reports-(list|analyze|download)$")
            then {
                external_side_effects: test("(analyze|download)$"),
                resource_intensive: test("analyze$"),
                required_scope: "reports.read"
            }
            elif test("^gms-rt-(adb-forward-|usbip-)")
            then {external_side_effects: true, resource_intensive: false, required_scope: "devices.lease"}
            elif test("^(gms-rt-agent-enroll|gms-rt-system-skills|gms-rt-apk-download|gms-rt-terminal-push)$")
            then {external_side_effects: true, resource_intensive: false, required_scope: ""}
            elif mode != "read_only"
            then {external_side_effects: true, resource_intensive: false, required_scope: ""}
            else {external_side_effects: false, resource_intensive: false, required_scope: ""}
            end;
        [inputs | split("\t") as $fields | $fields[0] as $name | {
            name: $name,
            category: ($name | category),
            summary: ($fields[2] // ""),
            usage: ($fields[1] // ($name + " [arguments]")),
            mode: ($name | mode),
            requires_auth: ($name | test("^(gms-rt-agent-enroll|gms-rt-auth-(credential-mode|login|status|scopes-check)|gms-rt-system-(capabilities|command-describe|commands|health|help|selfcheck|update|version)|gms-rt-test-modules)$") | not),
            requires_elevation: ($name | test(
                "burn-|config-update|devices-bootloader-(lock|unlock)|adb-forward-"
                + "|desktop-|terminal-(open|push)|usbip-(install|connect|disconnect)"
                + "|users-list|test-suites-result"
                + "|agent-(enroll-code|token-revoke)"
            )),
            requires_explicit_authorization: (($name | mode) == "mutating"),
            supports_json: true,
            agent_safe_unattended: (
                ($name | mode) == "read_only"
                and ($name | test("auth-(login|logout|elevate|elevation-reset)|approval-create|terminal-open|devices-(shell|scrcpy|logcat)|test-logs-stream") | not)
            )
        } + ($name | risk)] |
        {
            schema_version: 3,
            cli_version: $version,
            commands: .
        }'
}

gms-rt-system-command-describe() {
    local requested="${1:-}"
    [ -n "$requested" ] || {
        error "Usage: gms-rt-system-command-describe <gms-rt-command>"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    local commands description
    commands=$(gms-rt-system-commands) || return "$GMS_RT_EXIT_OPERATION"
    description=$(echo "$commands" | jq -c --arg name "$requested" '.commands[] | select(.name == $name)')
    [ -n "$description" ] || {
        error "Unknown command: $requested"
        return "$GMS_RT_EXIT_USAGE"
    }
    echo "$description" | jq '.'
}

gms-rt-system-version() {
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg version "$GMS_RT_VERSION" '{name: "gms-remote-test", version: $version}'
    else
        printf 'gms-remote-test %s\n' "$GMS_RT_VERSION"
    fi
}

gms-rt-system-capabilities() {
    check_jq || return 1
    local command_count
    command_count=$(gms-rt-system-commands | jq '.commands | length') || return 1
    jq -cn \
        --arg version "$GMS_RT_VERSION" \
        --arg server "$SERVER_URL" \
        --argjson command_count "$command_count" \
        '{
            schema_version: 3,
            name: "gms-remote-test",
            version: $version,
            server: $server,
            transport: "authenticated HTTPS API",
            output_modes: ["human", "json"],
            global_options: [
                "--json",
                "--quiet",
                "--no-color",
                "--non-interactive",
                "--yes",
                "--timeout SECONDS",
                "--server URL",
                "--ca-cert PATH",
                "--insecure"
            ],
            authentication: {
                cookie_session: true,
                password_stdin: true,
                elevation_command: "gms-rt-auth-elevate"
            },
            exit_codes: {
                success: 0,
                usage: 2,
                authentication_required: 3,
                permission_or_elevation_required: 4,
                conflict_or_busy: 5,
                network_or_timeout: 6,
                operation_failed: 7
            },
            command_count: $command_count,
            command_inventory: {
                list_command: "gms-rt-system-commands",
                describe_command: "gms-rt-system-command-describe"
            }
        }'
}

# ==============================================================================
# Agent-facing environment self-check
# ==============================================================================

# One-shot read-only environment report for agents: credential mode, identity,
# server health, device inventory and locally visible test suites. Every
# section degrades gracefully; hints carry actionable next steps so an agent
# can recover (e.g. re-enroll) without a human walkthrough.
gms-rt-system-selfcheck() {
    check_jq || return 1
    local auth_json health_json devices_json hints_json suites_json
    local auth_ok=false health_ok=false devices_ok=false
    local hints=()

    if [ "$GMS_RT_OUTPUT" != "json" ]; then
        echo "🔍 Environment self-check..."
    fi

    # api_call 失败时输出可能为空；--argjson 对空串直接崩溃，所以每个
    # 数据源都必须保证最终是合法 JSON（空串视为失败并兜底）。
    # 注意：echo '' | jq -e . 退出码为 0，空串不会被 jq 拦下，必须显式判空。
    _gms_rt_selfcheck_is_json() {
        [ -n "$1" ] && echo "$1" | jq -e type >/dev/null 2>&1
    }

    # --- authentication / identity --------------------------------------------
    auth_ok=true
    auth_json=$(api_call "/auth/status" 2>/dev/null)
    _gms_rt_selfcheck_is_json "$auth_json" || { auth_ok=false; auth_json='{}'; }
    local credential_mode
    credential_mode=$(gms-rt-auth-credential-mode 2>/dev/null) || credential_mode='{"mode":"unknown"}'
    _gms_rt_selfcheck_is_json "$credential_mode" || credential_mode='{"mode":"unknown"}'
    if [ "$auth_ok" != true ]; then
        case "${GMS_AGENT_PROCESS:-0}:$(echo "$credential_mode" | jq -r '.mode // empty')" in
            1:*|*:agent_token)
                hints+=("agent token check failed: re-enroll with 'gms-rt-agent-enroll <CODE>' (mint the code in the web UI, 5-minute TTL)")
                ;;
            *)
                hints+=("not authenticated: run 'gms-rt-auth-login <username> --password-stdin' or set GMS_AUTH_TOKEN_FILE")
                ;;
        esac
    fi

    # --- server health ----------------------------------------------------------
    health_ok=true
    health_json=$(api_call "/system/health" 2>/dev/null)
    _gms_rt_selfcheck_is_json "$health_json" || { health_ok=false; health_json='{}'; }
    if [ "$health_ok" != true ]; then
        hints+=("server ${SERVER_URL} unreachable or unhealthy: verify the web app is running and GMS_REMOTE_TEST_SERVER is correct; for a self-signed TLS deployment export GMS_CURL_INSECURE=1 or GMS_CURL_CA_CERT=/path/to/ca.crt")
    fi

    # --- device inventory (optional scope) --------------------------------------
    devices_ok=true
    devices_json=$(api_call "/devices/list" 2>/dev/null)
    _gms_rt_selfcheck_is_json "$devices_json" || { devices_ok=false; devices_json='[]'; }
    if [ "$devices_ok" != true ]; then
        hints+=("device inventory unavailable: the current credential may lack the devices.read scope")
    fi

    # --- locally visible test suites --------------------------------------------
    local suite_dirs=() candidate
    for candidate in \
        "${GMS_SUITE_DIR:-$HOME/GMS-Suite}" \
        "$HOME/CTS-Suite" \
        "$HOME"/android-cts-verifier* \
        "$HOME"/android-gts-*; do
        [ -d "$candidate" ] && suite_dirs+=("$candidate")
    done

    # --- TLS trust ----------------------------------------------------------------
    # insecure 模式禁用证书校验，信任链可被 MITM 替换——与 SKILL.md 对
    # bootstrap 阶段的禁令同理，运行时 API 调用也不该用 -k。
    local tls_insecure=false
    if [[ "$SERVER_URL" == https://* ]] \
        && [ -z "${GMS_CURL_CA_CERT:-}" ] \
        && [ "${GMS_CURL_INSECURE:-0}" = "1" ]; then
        tls_insecure=true
        hints+=("TLS verification is disabled (GMS_CURL_INSECURE=1 without GMS_CURL_CA_CERT): the controller CA can be replaced by a MITM. Install the controller CA (see docs/agent/installation.md, e.g. GMS_CURL_CA_CERT=/etc/gms/controller-ca.pem) and unset GMS_CURL_INSECURE.")
    fi
    suites_json=$(printf '%s\n' "${suite_dirs[@]:-}" | jq -R 'select(length > 0)' | jq -s '.')
    hints_json=$(printf '%s\n' "${hints[@]:-}" | jq -R 'select(length > 0)' | jq -s '.')

    # /devices/list 可能返回数组或 {devices: [...]} 包裹对象，两种都接受。
    jq -n \
        --arg server "$SERVER_URL" \
        --arg version "$GMS_RT_VERSION" \
        --arg profile "${GMS_RT_PROFILE:-${GMS_AGENT_PROFILE:-}}" \
        --arg agent_client "${GMS_AGENT_CLIENT:-}" \
        --argjson agent_process "$([ "${GMS_AGENT_PROCESS:-0}" = "1" ] && echo true || echo false)" \
        --argjson ca_configured "$([ -n "${GMS_CURL_CA_CERT:-}" ] && echo true || echo false)" \
        --argjson tls_insecure "$tls_insecure" \
        --argjson credential "$credential_mode" \
        --argjson auth "$auth_json" \
        --argjson health "$health_json" \
        --argjson devices "$devices_json" \
        --argjson suites "$suites_json" \
        --argjson hints "$hints_json" \
        --argjson auth_ok "$auth_ok" \
        --argjson health_ok "$health_ok" \
        --argjson devices_ok "$devices_ok" \
        '{
            schema_version: 1,
            cli_version: $version,
            server: $server,
            profile: (if $profile == "" then null else $profile end),
            agent_client: (if $agent_client == "" then null else $agent_client end),
            agent_process: $agent_process,
            ca_configured: $ca_configured,
            tls_insecure: $tls_insecure,
            credential: $credential,
            auth: {ok: $auth_ok, status: (if $auth_ok then $auth else null end)},
            server_health: {ok: $health_ok, status: (if $health_ok then $health else null end)},
            devices: (if $devices_ok
                then {ok: true, items: (if ($devices | type) == "array" then $devices else ($devices.devices // []) end)}
                else {ok: false} end),
            local_suites: $suites,
            hints: $hints
        }'
}

gms-rt-system-help() {
    cat << EOF
${BLUE}GMS Remote Test API Helper (FastAPI Port 5001)${NC}
========================================

${YELLOW}ADB Proxy:${NC}
  gms-rt-adb-forward-status      - List ADB Proxy hosts and assignments
  gms-rt-adb-forward-start       - Connect selected devices between Workers
  gms-rt-adb-forward-stop        - Disconnect a source-to-target assignment

${YELLOW}Authentication:${NC}
  gms-rt-auth-login [username]   - Log in and save an API session
  gms-rt-auth-status             - Show the current authentication status
  gms-rt-auth-credential-mode    - Show whether the CLI uses an Agent Token or session cookie
  gms-rt-auth-scopes-check       - Pre-flight required agent scopes (default: Redmine evidence chain)
  gms-rt-auth-logout             - Revoke and remove the saved session
  gms-rt-auth-elevate [username] - Verify an admin for sensitive operations
  gms-rt-auth-elevation-reset    - Clear administrator elevation

${YELLOW}Agent Credentials and Approval:${NC}
  gms-rt-agent-enroll            - Exchange a one-shot code for a local 0600 Agent Token
  gms-rt-agent-tokens            - List Agent Token metadata (admin)
  gms-rt-agent-enroll-code       - Mint a one-shot Agent enrollment code (admin + elevation)
  gms-rt-agent-token-revoke      - Revoke an Agent Token (admin + elevation)
  gms-rt-approval-create         - Mint a human-approved one-shot destructive-action token

${YELLOW}Cluster Inventory:${NC}
  gms-rt-cluster-workers         - List registered Workers
  gms-rt-cluster-devices         - List the authoritative cross-Worker device inventory
  gms-rt-cluster-resolve         - Resolve a device serial to its owning Worker

${YELLOW}Firmware Burning:${NC}
  gms-rt-burn-firmware           - Burn firmware image (optional --wait-online[=SECONDS])
  gms-rt-burn-gsi                - Burn GSI image (optional --wait-online[=SECONDS])
  gms-rt-burn-serial             - Burn serial number

${YELLOW}Configuration:${NC}
  gms-rt-config-read             - Read full configuration
  gms-rt-config-update           - Update configuration

${YELLOW}Desktop VNC:${NC}
  gms-rt-desktop-validate        - Validate desktop host
  gms-rt-desktop-vnc-start       - Start VNC
  gms-rt-desktop-vnc-status      - Check VNC status
  gms-rt-desktop-vnc-stop        - Stop VNC

${YELLOW}Device Management:${NC}
  gms-rt-devices-list               - List all connected devices
  gms-rt-devices-console            - List Controller serial ports or read retained serial logs
  gms-rt-devices-info               - Get detailed device information
  gms-rt-devices-wait               - Wait for devices to become ready
  gms-rt-devices-bootloader-lock    - Lock bootloader
  gms-rt-devices-bootloader-unlock  - Unlock bootloader
  gms-rt-devices-bootloader-status  - Check bootloader status
  gms-rt-devices-user-locked        - List user-locked devices
  gms-rt-devices-reboot             - Reboot devices
  gms-rt-devices-remount            - Remount RW (with auto-reboot prompt)
  gms-rt-devices-wifi               - Connect to WiFi
  gms-rt-devices-shell              - Open interactive ADB shell
  gms-rt-devices-logcat             - Capture device logcat (adb shell logcat -v time; -c clears buffer first)
  gms-rt-devices-push               - Push file to device (adb push)
  gms-rt-devices-screencap          - Capture device screenshot as base64 PNG
  gms-rt-devices-ui-dump            - Dump UI layout tree as JSON elements
  gms-rt-devices-snapshot           - One-shot device state snapshot (fp/activity/lock/owners)
  gms-rt-devices-scrcpy             - Start interactive screen mirroring

${YELLOW}File Management:${NC}
  gms-rt-files-progress          - Get upload progress

${YELLOW}Reports:${NC}
  gms-rt-reports-list            - List all test reports
  gms-rt-reports-download        - Download report folder
  gms-rt-reports-analyze         - Analyze report
  gms-rt-reports-delete          - Delete report

${YELLOW}SSH Management:${NC}
  gms-rt-ssh-ping                - Test SSH connectivity
  gms-rt-ssh-route               - Check SSH route
  gms-rt-ssh-sshd          - Check SSHD status & install guide (optional: user@ip, e.g. ${DEFAULT_SSH_USER}@192.168.1.100)

${YELLOW}System:${NC}
  gms-rt-system-capabilities     - Print machine-readable CLI capabilities
  gms-rt-system-command-describe - Describe one command for an AI agent
  gms-rt-system-commands         - Print machine-readable command inventory
  gms-rt-system-docs             - Get API documentation
  gms-rt-system-doctor           - Validate a remote CLI host and operation scope
  gms-rt-system-health           - Check server health
  gms-rt-system-selfcheck        - One-shot agent environment report (auth, health, devices, suites)
  gms-rt-system-help             - Show this command list
  gms-rt-system-skills           - Download skills directory as ZIP
  gms-rt-system-update           - Update the Skill and all CLI command links
  gms-rt-system-version          - Print CLI version

${YELLOW}Code search:${NC}
  gms-rt-opengrok-search         - Search the configured OpenGrok service

${YELLOW}APK Analysis:${NC}
  gms-rt-apk-resolve             - Resolve a suite module to its APK/JAR artifact
  gms-rt-apk-analyze             - Start JADX analysis for a resolved suite module
  gms-rt-apk-status              - Read one task status, or list all tasks
  gms-rt-apk-manifest            - Read a decompiled AndroidManifest (--permissions for the permission list)
  gms-rt-apk-source              - Browse the decompiled source tree
  gms-rt-apk-search              - Search decompiled names, content, or symbol definitions
  gms-rt-apk-download            - Download the decompiled source ZIP

${YELLOW}Redmine Evidence (read-only analysis chain):${NC}
  gms-rt-redmine-issue-fetch     - Create/refresh a full evidence snapshot (journals untruncated, attachments hashed)
  gms-rt-redmine-triage          - List today's pending issues (waiting_my_reply + no_reply_3_days)
  gms-rt-redmine-history-search  - Search historical issues for similar problems and reusable fixes
  gms-rt-redmine-issue-show      - Show snapshot completeness and issue fields
  gms-rt-redmine-journals        - Read full journals with cursor pagination
  gms-rt-redmine-attachments     - List artifacts (kind, size, sha256, status)
  gms-rt-redmine-attachment-download - Save one artifact original locally
  gms-rt-redmine-credentials-status - Pre-flight check for owner Redmine credentials
  gms-rt-redmine-artifact-image  - Return an image artifact as base64 plus metadata
  gms-rt-artifact-read           - Read artifact derived text by char window
  gms-rt-artifact-search         - Search description/journals/artifact text
  gms-rt-apk-analyze-attachment  - Import a Redmine .apk artifact into JADX
  gms-rt-apk-source-read         - Read a window of one decompiled file
  gms-rt-sdk-sources             - List configured SDK source providers
  gms-rt-sdk-search              - Search SDK source pinned to a revision
  gms-rt-sdk-read                - Read commit-pinned SDK source by result id

${YELLOW}Terminal:${NC}
  gms-rt-terminal-open           - Open SSH terminal on test host
  gms-rt-terminal-push           - Push file to test host directory

${YELLOW}Test Management:${NC}
  gms-rt-test-clean              - Clean test environment
  gms-rt-test-logs-stream        - Stream logs in real-time
  gms-rt-test-start              - Start test or retry report (suite short names, --wait)
  gms-rt-test-status             - Check test status
  gms-rt-test-stop               - Stop currently running test
  gms-rt-test-suites             - List available test suites
  gms-rt-test-modules            - List tradefed modules for a suite
  gms-rt-test-suites-result      - List test results (tools path or short suite name)

${YELLOW}Durable Test Jobs:${NC}
  gms-rt-jobs-list               - List durable test jobs
  gms-rt-jobs-status             - Get one durable test job
  gms-rt-jobs-events             - Read job events incrementally
  gms-rt-jobs-wait               - Wait for a job to finish
  gms-rt-jobs-follow             - Status + incremental events + failure summary in one call
  gms-rt-jobs-cancel             - Cancel a durable test job

${YELLOW}USB/IP Connection:${NC}
  gms-rt-usbip-install           - Install USB/IP (requires host parameter)
  gms-rt-usbip-connect           - Start USB/IP connection (requires host parameter)
  gms-rt-usbip-disconnect        - Stop USB/IP connection (requires host parameter)
  gms-rt-usbip-status            - Check USB/IP status (requires host parameter)

${YELLOW}User Management:${NC}
  gms-rt-users-current           - Get current user info
  gms-rt-users-detect            - Auto-detect username
  gms-rt-users-list              - List all users
  gms-rt-users-set-username      - Set username manually

${YELLOW}VPN Management:${NC}
  gms-rt-vpn-connect             - Connect to VPN
  gms-rt-vpn-disconnect          - Disconnect VPN
  gms-rt-vpn-status              - Check VPN status

${YELLOW}Examples:${NC}
  gms-rt-devices-list
  gms-rt-devices-list --json
  printf '%s\n' "\$PASSWORD" | gms-rt-auth-login admin --password-stdin --non-interactive
  gms-rt-system-capabilities --json
  gms-rt-system-doctor test --json --non-interactive
  gms-rt-devices-wait DEVICE --state online --max-wait 300
  gms-rt-devices-bootloader-lock '["DEVICE-1", "DEVICE-2"]'
  gms-rt-desktop-vnc-start
  gms-rt-test-start DEVICE CTS TestModule
  gms-rt-test-start DEVICE CTS android-cts-17_r1 --wait
  gms-rt-test-suites-result android-cts-17_r1
  gms-rt-burn-firmware firmware.zip DEVICE --wait-online
  gms-rt-test-logs-stream
  gms-rt-reports-list

${YELLOW}Test Start (Retry Mode):${NC}
  gms-rt-test-start --retry <TIMESTAMP> <DEVICE> <TYPE> <SUITE_PATH>
  gms-rt-test-start --retry 2026.04.11_17.27.04.421_2920 c3d9b8674f4b94f6 GTS /path/to/suite

${YELLOW}Terminal:${NC}
  gms-rt-terminal-open
  gms-rt-terminal-open 192.168.1.100 $DEFAULT_SSH_USER
  gms-rt-terminal-push ./config.json

Server: ${GREEN}$SERVER_URL${NC}
Docs:   ${GREEN}${SERVER_URL}/docs${NC}
Help:   ${GREEN}${SERVER_URL}/api/system/help${NC}

Global options:
  --json                 Emit exactly one JSON envelope on stdout
  --quiet                Suppress helper progress messages where supported
  --no-color             Disable ANSI colors
  --non-interactive      Never prompt for input
  --yes                  Accept supported confirmations
  --timeout SECONDS      Override the API timeout for this invocation
  --server URL           Use another Controller for this invocation
  --ca-cert PATH         Verify HTTPS with a trusted CA certificate
  --insecure             Allow a controlled self-signed HTTPS Controller

Exit codes: 0 success, 2 usage, 3 authentication, 4 permission/elevation,
            5 conflict/busy, 6 network/timeout, 7 operation failure
EOF
}

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

_gms_rt_dispatch_usage_error() {
    local command="$1"
    local message="$2"
    if [ "$GMS_RT_OUTPUT" = "json" ] && command -v jq >/dev/null 2>&1; then
        jq -cn --arg command "$command" --arg message "$message" \
            --argjson exit_code "$GMS_RT_EXIT_USAGE" \
            '{ok: false, command: $command, exit_code: $exit_code, error: $message}'
    else
        error "$message"
    fi
    return "$GMS_RT_EXIT_USAGE"
}

_gms_rt_dispatch() {
    local command="${1:-gms-rt-system-help}"
    [ "$#" -eq 0 ] || shift
    local args=()
    local argument

    # Detect JSON mode before validating other global options so usage errors
    # still honor the one-document stdout contract regardless of option order.
    for argument in "$@"; do
        if [ "$argument" = "--json" ]; then
            GMS_RT_OUTPUT=json
            GMS_RT_QUIET=1
            NO_COLOR=1
            break
        fi
    done

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --json)
                GMS_RT_OUTPUT=json
                GMS_RT_QUIET=1
                NO_COLOR=1
                ;;
            --quiet) GMS_RT_QUIET=1 ;;
            --no-color) NO_COLOR=1 ;;
            --non-interactive) GMS_RT_NON_INTERACTIVE=1 ;;
            --yes) GMS_RT_ASSUME_YES=1 ;;
            --timeout)
                shift
                if [ "$#" -eq 0 ] || ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
                    _gms_rt_dispatch_usage_error "$command" "--timeout requires a positive integer"
                    return $?
                fi
                CURL_TIMEOUT="$1"
                ;;
            --timeout=*)
                CURL_TIMEOUT="${1#*=}"
                if ! [[ "$CURL_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
                    _gms_rt_dispatch_usage_error "$command" "--timeout requires a positive integer"
                    return $?
                fi
                ;;
            --server)
                shift
                if [ "$#" -eq 0 ] || ! _validate_server_url "$1"; then
                    _gms_rt_dispatch_usage_error "$command" "--server requires an http(s) Controller URL without credentials, query, or fragment"
                    return $?
                fi
                SERVER_URL="$1"
                GMS_REMOTE_TEST_SERVER="$1"
                ;;
            --server=*)
                SERVER_URL="${1#*=}"
                if ! _validate_server_url "$SERVER_URL"; then
                    _gms_rt_dispatch_usage_error "$command" "--server requires an http(s) Controller URL without credentials, query, or fragment"
                    return $?
                fi
                GMS_REMOTE_TEST_SERVER="$SERVER_URL"
                ;;
            --ca-cert)
                shift
                if [ "$#" -eq 0 ] || [ ! -r "$1" ]; then
                    _gms_rt_dispatch_usage_error "$command" "--ca-cert requires a readable certificate file"
                    return $?
                fi
                GMS_CURL_CA_CERT="$1"
                GMS_CURL_INSECURE=0
                ;;
            --ca-cert=*)
                GMS_CURL_CA_CERT="${1#*=}"
                if [ ! -r "$GMS_CURL_CA_CERT" ]; then
                    _gms_rt_dispatch_usage_error "$command" "--ca-cert requires a readable certificate file"
                    return $?
                fi
                GMS_CURL_INSECURE=0
                ;;
            --insecure)
                GMS_CURL_CA_CERT=""
                GMS_CURL_INSECURE=1
                ;;
            *) args+=("$1") ;;
        esac
        shift
    done

    export GMS_REMOTE_TEST_SERVER GMS_CURL_CA_CERT GMS_CURL_INSECURE
    _refresh_transport_config

    if [[ "$command" != gms-rt-* ]] || ! declare -F "$command" >/dev/null; then
        _gms_rt_dispatch_usage_error "$command" "Unknown command: $command"
        return $?
    fi

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
    elif [ -n "${NO_COLOR:-}" ]; then
        RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
    fi

    local status_file stdout_file stderr_file command_status api_status
    status_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-status.XXXXXX") || return "$GMS_RT_EXIT_OPERATION"
    stdout_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-stdout.XXXXXX") || {
        rm -f -- "$status_file"
        return "$GMS_RT_EXIT_OPERATION"
    }
    stderr_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-stderr.XXXXXX") || {
        rm -f -- "$status_file" "$stdout_file"
        return "$GMS_RT_EXIT_OPERATION"
    }
    GMS_RT_STATUS_FILE="$status_file"
    export GMS_RT_STATUS_FILE GMS_RT_OUTPUT GMS_RT_QUIET GMS_RT_NON_INTERACTIVE GMS_RT_ASSUME_YES

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        "$command" "${args[@]}" >"$stdout_file" 2>"$stderr_file"
        command_status=$?
    else
        "$command" "${args[@]}"
        command_status=$?
    fi

    api_status=$(tail -n 1 "$status_file" 2>/dev/null || true)
    if [[ "$api_status" =~ ^[1-9][0-9]*$ ]]; then
        command_status="$api_status"
    fi
    if [ "$command_status" -eq 0 ] && [ "$GMS_RT_ERROR_SEEN" = "1" ]; then
        command_status="$GMS_RT_EXIT_OPERATION"
    fi
    case "$command_status" in
        0|2|3|4|5|6|7) ;;
        *) command_status="$GMS_RT_EXIT_OPERATION" ;;
    esac

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        # NOTE: 大输出(如 logcat -d / jobs-events)不能经 shell 变量 + jq --arg 传递,
        # 否则超过单参数上限报 "Argument list too long"。改用 --rawfile 直接读临时文件。
        # 同时对未解析为 JSON 的纯文本输出做尾部截断, 保护调用方(如 AI agent)的上下文。
        # jq 自身失败（损坏安装/内存不足）绝不能沿用业务命令的成功码——
        # 之前 stub jq 返回 99 时 CLI 仍退出 0 且 stdout 为空，Agent 把
        # “序列化失败”当成功。序列化失败按操作失败上报。
        local envelope
        envelope=$(jq -cn \
            --arg command "$command" \
            --rawfile stdout "$stdout_file" \
            --rawfile stderr "$stderr_file" \
            --argjson exit_code "$command_status" '
            def json_suffix:
                ([try (
                    capture("(?s)(?<json>[\\[{].*)$").json | fromjson
                ) catch empty][0] // null);
            ($stdout | json_suffix) as $parsed |
            def truncate_text:
                if (length > 200000)
                then "...[output truncated, kept last 200000 of \(length) chars]...\n" + .[-200000:]
                else . end;
            {
                ok: ($exit_code == 0),
                command: $command,
                exit_code: $exit_code
            }
            + (if $parsed == null then {output: ($stdout | truncate_text)} else {data: $parsed} end)
            + (if $stderr == "" then {} else {diagnostics: ($stderr | truncate_text)} end)')
        local jq_status=$?
        if [ "$jq_status" -ne 0 ]; then
            error "输出序列化失败 (jq exit $jq_status)，无法生成 JSON envelope" >&2
            rm -f -- "$status_file" "$stdout_file" "$stderr_file"
            unset GMS_RT_STATUS_FILE
            return "$GMS_RT_EXIT_OPERATION"
        fi
        printf '%s\n' "$envelope"
    fi

    rm -f -- "$status_file" "$stdout_file" "$stderr_file"
    unset GMS_RT_STATUS_FILE
    return "$command_status"
}

if ! _is_sourced; then
    _gms_rt_dispatch "$@"
fi
