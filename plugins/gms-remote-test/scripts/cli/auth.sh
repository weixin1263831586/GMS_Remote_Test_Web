# Fixed auth command domain; sourced by gms-remote-test.sh.
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
    # 保存/恢复 umask：本脚本可被 source（函数模式），泄漏 077 会改变调用者
    # shell 之后创建的所有文件权限。
    local _old_umask
    _old_umask=$(umask)
    umask 077
    printf '%s\n' "$token" > "$out_file"
    umask "$_old_umask"
    # 权限不依赖调用方 shell 的 umask 语境，显式钉死 0600（与
    # profile_store.py 的 os.open(0o600) 纪律一致）。
    chmod 600 "$out_file" 2>/dev/null || true
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
            -h|--help)
                printf 'Usage: gms-rt-approval-create --tool <tool> --device <serial>[,<serial>...]\n'
                printf '       [--command <command> | --firmware-sha256 <sha256>\n'
                printf '        [--wipe-data true|false] [--burn-mode auto|uf]]\n'
                printf '\n'
                printf 'Mint a one-shot approval token (5-minute TTL) bound to the exact\n'
                printf 'tool + device + command. Requires a HUMAN session: the server rejects\n'
                printf 'agent service tokens here (agent_forbidden).\n'
                printf '\n'
                printf 'On a host with agent profiles, a plain gms-rt-* call resolves to an\n'
                printf 'agent token — enter the human session first, in the same shell:\n'
                printf '\n'
                printf '  GMS_RT_HUMAN_SESSION=1 gms-rt-auth-login <username>\n'
                printf '\n'
                printf 'then re-run this command (env-prefix form, NOT "VAR=x && cmd":\n'
                printf 'a bare && does not export the variable into the command).\n'
                return 0
                ;;
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
        error "Usage: gms-rt-approval-create --tool <tool> --device <serial>[,<serial>...] [--command <command>|--firmware-sha256 <sha256> [--wipe-data true|false] [--burn-mode auto|uf]]"
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
    # 审批令牌由人工会话铸造（服务端拒匿名与 agent token），
    # 这里必须携带当前凭据（cookie 或 Bearer），否则永远 401。
    # 走 api_call：网络失败(000)映射网络错误码而非权限错误，cookie
    # 写入也拿到 flock 保护（历史裸 curl 两者都缺）。
    response=$(api_call "/auth/approval-tokens" POST "$data")
    local api_status=$?
    if [ "$api_status" -ne 0 ] || ! echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        local _agent_forbidden
        _agent_forbidden=$(printf '%s' "$response" | jq -r '.detail.agent_forbidden // .agent_forbidden // empty' 2>/dev/null || true)
        error "Approval creation failed: $(extract_api_error "$response")"
        if [ -n "$_agent_forbidden" ] && [ "$_agent_forbidden" != "false" ]; then
            diagnostic "审批令牌仅限人工会话铸造。多 profile 主机请先: GMS_RT_HUMAN_SESSION=1 gms-rt-auth-login <用户名>, 再在同一 shell 重试。"
        fi
        [ "$api_status" -eq 0 ] || return "$api_status"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    echo "$response" | jq '.approval // .'
    [ "$GMS_RT_OUTPUT" = "json" ] || info "approval token: 5 分钟 TTL、单次有效, 仅绑定该 tool+device+command。"
}

# List Agent Service Tokens (admin session required). The raw token is
# never stored server-side, so listings only show metadata.
gms-rt-agent-tokens() {
    check_jq || return 1
    local response api_status
    response=$(api_call "/auth/agent-tokens" GET)
    api_status=$?
    if [ "$api_status" -ne 0 ] || ! echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Failed to list agent tokens: $(extract_api_error "$response")"
        [ "$api_status" -eq 0 ] || return "$api_status"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    echo "$response" | jq '.tokens // []'
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
    response=$(api_call "/auth/agent-enrollment-codes" POST "$data")
    local api_status=$?
    if [ "$api_status" -ne 0 ] || ! echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Enrollment code creation failed: $(extract_api_error "$response")"
        [ "$api_status" -eq 0 ] || return "$api_status"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    # The one-shot code is secret material: print once, never log it twice.
    echo "$response" | jq '.enrollment'
    # 人话补充：绝对过期时刻让"还剩多久"可见。
    local expires_iso
    expires_iso=$(echo "$response" | jq -r '.enrollment.expires_at // empty')
    [ -n "$expires_iso" ] && [ "$GMS_RT_OUTPUT" != "json" ] && {
        info "配对码有效至 $(iso_to_local_time "$expires_iso")（TTL $(echo "$response" | jq -r '.enrollment.ttl_minutes // 5') 分钟，一次性使用）"
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
    local response api_status
    response=$(api_call "/auth/agent-tokens/$(_urlencode "$token_id")" DELETE)
    api_status=$?
    if [ "$api_status" -ne 0 ] || ! echo "$response" | jq -e '.success == true' >/dev/null 2>&1; then
        error "Revoke failed: $(extract_api_error "$response")"
        [ "$api_status" -eq 0 ] || return "$api_status"
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    echo "$response" | jq '{ok: true, revoked: .revoked}'
}
