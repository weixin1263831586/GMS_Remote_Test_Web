# Fixed command domain; sourced by gms-remote-test.sh.
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
