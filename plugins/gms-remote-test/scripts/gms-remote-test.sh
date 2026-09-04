#!/bin/bash
set -o pipefail
# ==============================================================================
# GMS Remote Test API Helper Script (FastAPI Port 5001)
# Version: 2026.08.25-1
# ==============================================================================

GMS_RT_VERSION="2026.08.25-1"
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
GMS_AUTH_COOKIE_JAR="${GMS_AUTH_COOKIE_JAR:-${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test/session.cookies}"
CURL_AUTH_ARGS=(-b "$GMS_AUTH_COOKIE_JAR" -c "$GMS_AUTH_COOKIE_JAR")

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

    _refresh_tls_args
    _ensure_auth_cookie_jar || {
        _record_api_exit_code "$GMS_RT_EXIT_OPERATION"
        return "$GMS_RT_EXIT_OPERATION"
    }
    if [ "${#extra_args[@]}" -gt 0 ]; then
        response=$(curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
            -w $'\nHTTP_STATUS:%{http_code}' --max-time "$CURL_TIMEOUT" \
            -X "$method" "${API_BASE}${endpoint}" "${extra_args[@]}")
    elif [ -n "$data" ] || [ "$method" = "POST" ]; then
        response=$(curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS -X "${method}" "${API_BASE}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            -w $'\nHTTP_STATUS:%{http_code}' \
            --max-time "$CURL_TIMEOUT")
    else
        response=$(curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
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
                diagnostic "需要登录。请先运行: gms-rt-auth-login [username]"
                ;;
            "$GMS_RT_EXIT_PERMISSION")
                diagnostic "权限不足或需要管理员提权。请运行: gms-rt-auth-elevate [username]"
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
    [ "$call_status" -eq 0 ] || return "$call_status"
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

# Extract error message from API response
extract_api_error() {
    local response="$1"
    echo "$response" | jq -r '.detail // .error // .message // "Unknown error"' 2>/dev/null || echo "Unknown error"
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
    curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS -w "\nHTTP_STATUS:%{http_code}" \
        --max-time "$CURL_BURN_TIMEOUT" \
        -X POST "${API_BASE}/burn/firmware" \
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

    _refresh_tls_args
    _ensure_auth_cookie_jar || return 1
    curl "${CURL_TLS_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -# -o /dev/stdout -w "\nHTTP_STATUS:%{http_code}" \
        --max-time "$CURL_BURN_TIMEOUT" \
        -X POST "${API_BASE}/burn/firmware?devices=${device_query}" \
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
    firmware_path="$1"
    devices="$2"
    wipe_data="${3:-true}"
    if [ "$wait_online" = "1" ] && ! [[ "$wait_max" =~ ^[1-9][0-9]*$ ]]; then
        error "--wait-online requires a positive maximum wait in seconds"
        return "$GMS_RT_EXIT_USAGE"
    fi

    [ -z "$firmware_path" ] && { error "Firmware path required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--wait-online[=SECONDS]]"; return 1; }
    [ -z "$devices" ] && { error "Devices required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--wait-online[=SECONDS]]"; return 1; }
    [ ! -f "$firmware_path" ] && { error "Firmware file not found: $firmware_path"; return 1; }
    check_jq || return 1
    devices=$(_resolve_devices "$devices")

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
    local devices="$1"
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
    local devices="$1"
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
    local devices="$1"
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
    local devices="$1"
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
    local devices="$1"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-reboot DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    devices=$(_resolve_devices "$devices")
    echo "🔄 重启设备..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/reboot" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "设备重启成功"

        # 美化输出格式
        local count=$(echo "$response" | jq -r '.data.summary.total // 0')
        local success=$(echo "$response" | jq -r '.data.summary.success // 0')
        local failed=$(echo "$response" | jq -r '.data.summary.failed // 0')

        echo "📊 操作统计: 成功 $success 台, 失败 $failed 台"
        echo ""

        # 显示每个设备的详细结果
        echo "$response" | jq -r '.data.results[]? | "📱 \(.device): 重启完成 (耗时: \(.wait_time // "N/A")秒)"' 2>/dev/null || echo "$response" | jq '.'
    else
        error "设备重启失败"
        echo "$response" | jq '.'
    fi
}

# Remount multiple devices (parallel)
gms-rt-devices-remount() {
    local devices="$1"
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
gms-rt-devices-scrcpy() {
    local devices="$1"
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
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-shell DEVICE_ID [COMMAND]"; return 1; }

    shift
    local shell_command="$*"
    if [ -z "$shell_command" ] && [ "$GMS_RT_NON_INTERACTIVE" = "1" ]; then
        error "Interactive device shell is disabled by --non-interactive; provide a command"
        return "$GMS_RT_EXIT_USAGE"
    fi

    if _is_test_host && command -v adb &> /dev/null && adb devices 2>/dev/null | grep -q "$device_id"; then
        if [ -n "$shell_command" ]; then
            adb -s "$device_id" shell "$shell_command"
        else
            echo ""; echo "💻 打开设备Shell: $device_id..."
            echo "🔌 使用 Ctrl+D 退出 shell"; echo ""
            adb -s "$device_id" shell
        fi
        return 0
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
    else
        echo ""; echo "💻 打开设备Shell: $device_id... (via $user@$host)"
        echo "🔌 使用 Ctrl+D 退出 shell"; echo ""
        ssh -t -p "$port" "$user@$host" "adb -s $device_id shell"
    fi
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

    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"; return 1; }
    [ -z "$ssid" ] && { error "SSID必填. 用法: gms-rt-devices-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"; return 1; }
    [ -z "$password" ] && { error "密码必填. 用法: gms-rt-devices-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"; return 1; }

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

# Reinstall the latest Skill and CLI command links from the bound Controller.
gms-rt-system-update() {
    [ "$#" -eq 0 ] || {
        error "Usage: gms-rt-system-update"
        return "$GMS_RT_EXIT_USAGE"
    }
    local installer
    installer="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh"
    [ -f "$installer" ] || {
        error "Skill installer not found: $installer"
        return "$GMS_RT_EXIT_OPERATION"
    }
    # Dispatcher 安装由 wrapper export GMS_REMOTE_TEST_SERVER/GMS_SKILL_DOWNLOAD_URL；
    # source 模式（.bashrc 直接 source 本脚本）没有 wrapper，且仓库里的 install.sh
    # 是未渲染模板（__GMS_* 占位符）。这里把当前绑定的 Controller 注入为默认值，
    # 保证两种安装模式下更新命令都能自举；TLS 配置与当前会话保持一致。
    GMS_REMOTE_TEST_SERVER="${GMS_REMOTE_TEST_SERVER:-${SERVER_URL%/}}" \
    GMS_SKILL_DOWNLOAD_URL="${GMS_SKILL_DOWNLOAD_URL:-${SERVER_URL%/}/api/system/skills?skill_name=gms-remote-test}" \
    GMS_INSTALL_CA_CERT="${GMS_INSTALL_CA_CERT:-${GMS_CURL_CA_CERT:-}}" \
    GMS_INSTALL_INSECURE="${GMS_CURL_INSECURE:-${GMS_INSTALL_INSECURE:-0}}" \
        bash "$installer"
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
        '{
            retry_dir: $rdir,
            devices: [$dev],
            test_type: $ttype,
            test_module: $tmod,
            test_case: $tcase,
            test_suite: $tsuite
        }')

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
        gms-rt-auth-login) printf '%s' 'gms-rt-auth-login [username] [--password-stdin]' ;;
        gms-rt-auth-elevate) printf '%s' 'gms-rt-auth-elevate [admin_username] [--password-stdin]' ;;
        gms-rt-burn-firmware) printf '%s' 'gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--wait-online[=SECONDS]]' ;;
        gms-rt-burn-gsi) printf '%s' 'gms-rt-burn-gsi <gsi_path> <devices> [wipe_data] [--wait-online[=SECONDS]]' ;;
        gms-rt-burn-serial) printf '%s' 'gms-rt-burn-serial <device_id> <serial>' ;;
        gms-rt-system-command-describe) printf '%s' 'gms-rt-system-command-describe <gms-rt-command>' ;;
        gms-rt-devices-info|gms-rt-devices-reboot|gms-rt-devices-remount|gms-rt-devices-bootloader-lock|gms-rt-devices-bootloader-unlock|gms-rt-devices-bootloader-status)
            printf '%s' "$1 <devices>"
            ;;
        gms-rt-devices-wait) printf '%s' 'gms-rt-devices-wait <devices> [--state online|fastboot|any] [--interval SECONDS] [--max-wait SECONDS]' ;;
        gms-rt-devices-shell) printf '%s' 'gms-rt-devices-shell <device_id> [command]' ;;
        gms-rt-devices-push) printf '%s' 'gms-rt-devices-push <device_id> <local_file> <remote_path>' ;;
        gms-rt-jobs-list) printf '%s' 'gms-rt-jobs-list [limit]' ;;
        gms-rt-jobs-status) printf '%s' 'gms-rt-jobs-status <job_id>' ;;
        gms-rt-jobs-events) printf '%s' 'gms-rt-jobs-events <job_id> [after_sequence] [limit]' ;;
        gms-rt-jobs-wait) printf '%s' 'gms-rt-jobs-wait <job_id> [--interval SECONDS] [--max-wait SECONDS]' ;;
        gms-rt-jobs-cancel) printf '%s' 'gms-rt-jobs-cancel <job_id>' ;;
        gms-rt-system-doctor) printf '%s' 'gms-rt-system-doctor [read|device|firmware|gsi|test]' ;;
        gms-rt-system-update) printf '%s' 'gms-rt-system-update' ;;
        gms-rt-test-start) printf '%s' 'gms-rt-test-start <device> [type] [module] [case] [suite] | --retry <timestamp> [device] [type] [suite] [--wait] [--max-wait SECONDS]' ;;
        gms-rt-test-stop) printf '%s' 'gms-rt-test-stop [job_id]' ;;
        gms-rt-test-suites) printf '%s' 'gms-rt-test-suites [base_path]' ;;
        gms-rt-test-suites-result) printf '%s' 'gms-rt-test-suites-result <tools_path|suite_name> [--force-refresh]' ;;
        *) printf '%s' "$1 [arguments]" ;;
    esac
}

_gms_rt_command_summary() {
    case "$1" in
        gms-rt-system-capabilities) printf '%s' 'Print the CLI contract, global options, and exit codes' ;;
        gms-rt-system-command-describe) printf '%s' 'Describe one CLI command for machine execution' ;;
        gms-rt-system-commands) printf '%s' 'Print the machine-readable command inventory' ;;
        gms-rt-system-doctor) printf '%s' 'Check controller, session, tools, devices, and suites for an operation scope' ;;
        gms-rt-system-help) printf '%s' 'Show the human-readable CLI command list' ;;
        gms-rt-system-update) printf '%s' 'Reinstall the latest Skill and CLI command links' ;;
        gms-rt-system-version) printf '%s' 'Print the local CLI version' ;;
        gms-rt-devices-wait) printf '%s' 'Wait for selected devices to become visible in the requested state' ;;
        gms-rt-test-suites-result) printf '%s' 'List tradefed results for a suite path or short suite name' ;;
        gms-rt-test-start) printf '%s' 'Start a test with smart args, suite short names, device prefixes, and optional --wait' ;;
        gms-rt-jobs-list) printf '%s' 'List durable test jobs visible to the current session' ;;
        gms-rt-jobs-status) printf '%s' 'Get authoritative durable test job state' ;;
        gms-rt-jobs-events) printf '%s' 'Read incremental durable test job events' ;;
        gms-rt-jobs-wait) printf '%s' 'Wait for a durable test job to reach a terminal state' ;;
        gms-rt-jobs-cancel) printf '%s' 'Request cancellation of a durable test job' ;;
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
            if test("terminal-open|devices-shell|devices-scrcpy|test-logs-stream")
            then "interactive"
            elif test(
                "burn-|config-update|bootloader-(lock|unlock)|devices-(reboot|remount|push|wifi)"
                + "|reports-delete|terminal-push|test-(start|stop|clean)|usbip-(install|connect|disconnect)"
                + "|vpn-(connect|disconnect)|adb-forward-(start|stop)|desktop-vnc-(start|stop)|users-set-username"
                + "|jobs-cancel|system-update"
            )
            then "mutating"
            else "read_only"
            end;
        [inputs | split("\t") as $fields | $fields[0] as $name | {
            name: $name,
            category: ($name | category),
            summary: ($fields[2] // ""),
            usage: ($fields[1] // ($name + " [arguments]")),
            mode: ($name | mode),
            requires_auth: ($name | test("auth-(status|login)|system-(capabilities|command-describe|commands|health|help|update|version)") | not),
            requires_elevation: ($name | test(
                "burn-|config-update|devices-bootloader-(lock|unlock)|adb-forward-"
                + "|desktop-|terminal-(open|push)|usbip-(install|connect|disconnect)"
                + "|users-list|test-suites-result"
            )),
            requires_explicit_authorization: (($name | mode) == "mutating"),
            supports_json: true,
            agent_safe_unattended: (
                ($name | mode) == "read_only"
                and ($name | test("auth-(login|logout|elevate|elevation-reset)|terminal-open|devices-(shell|scrcpy)|test-logs-stream") | not)
            )
        }] |
        {
            schema_version: 2,
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
  gms-rt-auth-logout             - Revoke and remove the saved session
  gms-rt-auth-elevate [username] - Verify an admin for sensitive operations
  gms-rt-auth-elevation-reset    - Clear administrator elevation

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
  gms-rt-devices-push               - Push file to device (adb push)
  gms-rt-devices-scrcpy             - Show device screen

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
  gms-rt-system-help             - Show this command list
  gms-rt-system-skills           - Download skills directory as ZIP
  gms-rt-system-update           - Update the Skill and all CLI command links
  gms-rt-system-version          - Print CLI version

${YELLOW}Code search:${NC}
  gms-rt-opengrok-search         - Search the configured OpenGrok service

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
  gms-rt-test-suites-result      - List test results (tools path or short suite name)

${YELLOW}Durable Test Jobs:${NC}
  gms-rt-jobs-list               - List durable test jobs
  gms-rt-jobs-status             - Get one durable test job
  gms-rt-jobs-events             - Read job events incrementally
  gms-rt-jobs-wait               - Wait for a job to finish
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
        local stdout_text stderr_text
        stdout_text=$(<"$stdout_file")
        stderr_text=$(<"$stderr_file")
        jq -cn \
            --arg command "$command" \
            --arg stdout "$stdout_text" \
            --arg stderr "$stderr_text" \
            --argjson exit_code "$command_status" '
            def json_suffix:
                ([try (
                    capture("(?s)(?<json>[\\[{].*)$").json | fromjson
                ) catch empty][0] // null);
            ($stdout | json_suffix) as $parsed |
            {
                ok: ($exit_code == 0),
                command: $command,
                exit_code: $exit_code
            }
            + (if $parsed == null then {output: $stdout} else {data: $parsed} end)
            + (if $stderr == "" then {} else {diagnostics: $stderr} end)'
    fi

    rm -f -- "$status_file" "$stdout_file" "$stderr_file"
    unset GMS_RT_STATUS_FILE
    return "$command_status"
}

if ! _is_sourced; then
    _gms_rt_dispatch "$@"
fi
