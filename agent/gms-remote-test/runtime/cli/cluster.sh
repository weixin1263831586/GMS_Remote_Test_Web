# Fixed cluster command domain; sourced by gms-remote-test.sh.
# ==============================================================================
# Cluster worker/device discovery
# ==============================================================================

gms-rt-cluster-workers() {
    check_jq || return 1
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

    local approval_query=""
    if [ -n "${GMS_RT_BURN_APPROVAL_TOKEN:-}" ]; then
        approval_query="?approval_token=$(_urlencode "$GMS_RT_BURN_APPROVAL_TOKEN")"
    fi
    _gms_curl_authenticated -sS -w "\nHTTP_STATUS:%{http_code}" \
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

    _gms_curl_authenticated -# -o /dev/stdout -w "\nHTTP_STATUS:%{http_code}" \
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
