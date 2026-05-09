#!/bin/bash
# ==============================================================================
# GMS Remote Test API Helper Script (FastAPI Port 5001)
# Version: 2026.04.05-100000
# ==============================================================================

# Default configuration
# Use environment variable GMS_REMOTE_TEST_SERVER or default to localhost:5001
# If running on the server machine itself, use localhost to avoid firewall issues
if [ -n "${GMS_REMOTE_TEST_SERVER:-}" ]; then
    SERVER_URL="$GMS_REMOTE_TEST_SERVER"
else
    # Check if we're running on the server machine (172.16.14.233)
    if hostname -I 2>/dev/null | grep -q "172.16.14.233"; then
        SERVER_URL="http://localhost:5001"
    else
        SERVER_URL="http://172.16.14.233:5001"
    fi
fi
API_BASE="${SERVER_URL}/api"

# GMS Web App Configuration Directory
# Can be overridden by environment variable
GMS_WEB_APP_DIR="${GMS_WEB_APP_DIR:-/home/hcq/GMS_Remote_Test/web_app}"

# Colors for output
RED=$(printf '\033[0;31m')
GREEN=$(printf '\033[0;32m')
YELLOW=$(printf '\033[1;33m')
BLUE=$(printf '\033[0;34m')
NC=$(printf '\033[0m')

# Network timeout constants
PING_TIMEOUT=2
CURL_TIMEOUT=30  # 30 seconds for slow API endpoints (e.g., test results)
CURL_EXIT_CANNOT_CONNECT=7
CURL_EXIT_OPERATION_TIMEOUT=28

# Print functions
error() {
    echo -e "${RED}Error:${NC} $1" >&2
}

success() {
    echo -e "${GREEN}✓ $1${NC}"
}

warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Show connection error message to stderr
show_connection_error() {
    error "无法连接到服务器 $SERVER_URL"
    error "请检查:"
    error "  1. 服务器是否运行 (systemctl status gms-web-app)"
    error "  2. 网络连通性 (ping $(echo "$SERVER_URL" | sed 's|http://||' | sed 's|/.*||'))"
    error "  3. 防火墙设置 (sudo ufw status)"
    error "  4. 服务器日志 (tail -f $GMS_WEB_APP_DIR/fastapi.log)"
}

# Make API call and return JSON with optimized error handling
# Usage: api_call <endpoint> [method] [data] [curl_extra_args]
api_call() {
    local endpoint="$1"
    local method="${2:-GET}"
    local data="${3:-}"
    local curl_extra="${4:-}"
    local response

    if [ -n "$curl_extra" ]; then
        # File upload or custom curl args mode
        response=$(eval "curl -w \"\\nHTTP_STATUS:%{http_code}\" --max-time $CURL_TIMEOUT -X \"\${method}\" \"\${API_BASE}\${endpoint}\" $curl_extra" 2>/dev/null)
    elif [ -n "$data" ] || [ "$method" = "POST" ]; then
        response=$(curl -s -X "${method}" "${API_BASE}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            --max-time $CURL_TIMEOUT 2>/dev/null)
    else
        response=$(curl -s "${API_BASE}${endpoint}" --max-time $CURL_TIMEOUT 2>/dev/null)
    fi

    local curl_exit_code=$?
    if [ $curl_exit_code -eq 0 ] && [ -n "$response" ]; then
        echo "$response"
        return 0
    else
        if [ $curl_exit_code -eq $CURL_EXIT_CANNOT_CONNECT ] || [ $curl_exit_code -eq $CURL_EXIT_OPERATION_TIMEOUT ]; then
            show_connection_error
        else
            error "Failed to get response from server (curl exit code: $curl_exit_code)" >&2
        fi
        return 1
    fi
}

# Extract error message from API response
extract_api_error() {
    local response="$1"
    echo "$response" | jq -r '.detail // .error // .message // "Unknown error"' 2>/dev/null || echo "Unknown error"
}

# Check HTTP response status and extract body
# Returns: body via stdout, status via HTTP_STATUS_CODE variable
# Usage: body=$(check_http_response "$response") && echo "Success: $body"
check_http_response() {
    local response="$1"
    HTTP_STATUS_CODE=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    local body=$(echo "$response" | grep -v "HTTP_STATUS:")
    if [[ ! "$HTTP_STATUS_CODE" =~ ^2[0-9]{2}$ ]]; then
        return 1
    fi
    echo "$body"
    return 0
}

# Check if jq is installed
check_jq() {
    if ! command -v jq &> /dev/null; then
        error "jq is required but not installed. Please install: sudo apt-get install jq"
    fi
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
        # Already JSON array - wrap directly
        echo "{\"devices\":$devices}"
    else
        # Convert space-separated list to JSON array and wrap
        local device_array=$(echo "$devices" | jq -R -c 'split(" ") | map(select(length>0))')
        echo "{\"devices\":$device_array}"
    fi
}

# Convert device input to JSON array format only
# Supports: JSON array ["dev1","dev2"], space-separated list, or single device
# Returns: [...] JSON array
convert_devices_to_json() {
    local devices="$1"
    if [[ "$devices" == \[* ]]; then
        # Already JSON array
        echo "$devices"
    else
        # Convert space-separated list to JSON array
        echo "$devices" | jq -R -c 'split(" ") | map(select(length>0))'
    fi
}

# ==============================================================================
# ADB Port Forward Commands
# ==============================================================================

# Start ADB Port forwarding
gms-rt-adb-forward-start() {
    local device_host="$1"
    local device_password="$2"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-adb-forward-start <device_host> <device_password>"; return 1; }
    [ -z "$device_password" ] && { error "Device password required. Usage: gms-rt-adb-forward-start <device_host> <device_password>"; return 1; }
    check_jq
    echo "🔌 Starting ADB Port forward..."
    local data="{\"device_host\":\"$device_host\",\"device_password\":\"$device_password\"}"
    local response=$(api_call "/adb-forward/start" "POST" "$data")
    echo "$response" | jq '.'
}

# Stop ADB port forwarding
gms-rt-adb-forward-stop() {
    local device_host="$1"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-adb-forward-stop <device_host>"; return 1; }
    check_jq
    echo "🛑 Stopping ADB Port forward..."
    local data="{\"device_host\":\"$device_host\"}"
    local response=$(api_call "/adb-forward/stop" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Burn Commands
# ==============================================================================

# Burn firmware image to devices
gms-rt-burn-firmware() {
    local firmware_path="$1"
    local devices="$2"
    local wipe_data="${3:-true}"

    [ -z "$firmware_path" ] && { error "Firmware path required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data]"; return 1; }
    [ -z "$devices" ] && { error "Devices required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data]"; return 1; }
    [ ! -f "$firmware_path" ] && { error "Firmware file not found: $firmware_path"; return 1; }

    echo "🔥 Burning firmware: $firmware_path to devices: $devices..."
    echo "⏳ Uploading firmware (this may take a few minutes)..."

    # Get terminal width for progress bars
    local term_width=${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}
    local bar_width=$((term_width * 60 / 100))

    # Set COLUMNS for curl progress bar width (60% of terminal)
    export COLUMNS=$bar_width

    # Use api_call with custom curl args for file upload with progress bar
    local curl_args="-# -o /dev/stdout -F \"firmware_file=@$firmware_path\" -F \"devices=$devices\" -F \"wipe_data=$wipe_data\""
    local response=$(api_call "/burn/firmware" "POST" "" "$curl_args")

    unset COLUMNS

    local body
    if ! body=$(check_http_response "$response"); then
        error "Firmware burn failed - HTTP status: $HTTP_STATUS_CODE"
        echo "$response" | grep -v "HTTP_STATUS:" | jq '.' 2>/dev/null || echo "$response" | grep -v "HTTP_STATUS:"
        return 1
    fi

    # Check if response contains success field
    if echo "$body" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "Firmware burn completed successfully"
        echo "$body" | jq '.'
    else
        error "Firmware burn failed - API returned error"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return 1
    fi
}

# Burn GSI image to devices
gms-rt-burn-gsi() {
    local gsi_path="$1"
    local devices="$2"
    local wipe_data="${3:-true}"

    [ -z "$gsi_path" ] && { error "GSI path required. Usage: gms-rt-burn-gsi <gsi_path> <devices> [wipe_data]"; return 1; }
    [ -z "$devices" ] && { error "Devices required. Usage: gms-rt-burn-gsi <gsi_path> <devices> [wipe_data]"; return 1; }
    [ ! -f "$gsi_path" ] && { error "GSI file not found: $gsi_path"; return 1; }

    check_jq
    echo "🔥 Burning GSI: $gsi_path to devices: $devices..."

    # Get absolute path of GSI image
    local absolute_path=$(realpath "$gsi_path")

    # Get absolute path of burn script (on local machine)
    local local_script="${GMS_WEB_APP_DIR}/scripts/run_GSI_Burn.sh"

    # Check if script exists
    if [ ! -f "$local_script" ]; then
        error "GSI burn script not found: $local_script"
        return 1
    fi

    # Build JSON payload with script_path
    local json_payload=$(jq -n \
        --arg system_img "$absolute_path" \
        --arg script_path "$local_script" \
        --argjson devices "$(convert_devices_to_json "$devices")" \
        '{system_img: $system_img, script_path: $script_path, devices: $devices}')

    # Use api_call with custom curl args for JSON POST with status code
    local response=$(api_call "/burn/gsi" "POST" "" "-H \"Content-Type: application/json\" -d \"$json_payload\"")

    local body
    if ! body=$(check_http_response "$response"); then
        error "GSI burn failed - HTTP status: $HTTP_STATUS_CODE"
        echo "$response" | grep -v "HTTP_STATUS:" | jq '.' 2>/dev/null || echo "$response" | grep -v "HTTP_STATUS:"
        return 1
    fi

    if echo "$body" | jq -e '.success' > /dev/null; then
        success "GSI burn completed successfully"
        echo ""
        echo "$body" | jq -r '.results[]? | "📱 \(.device): ✅ Success"' 2>/dev/null
        echo ""
        echo "📋 Detailed output:"
        echo "$body" | jq -r '.results[]? | .output' 2>/dev/null | head -20
        echo "..."
        echo "(Full output available in response JSON)"
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
    local data="{\"device_id\":\"$device_id\",\"serial\":\"$serial\"}"
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
    local data="{\"$key\":\"$value\"}"
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
    local data="{\"host\":\"$host\"}"
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
    local password="${2:-}"
    local vnc_password="${3:-}"
    check_jq
    echo "🚀 Starting desktop VNC..."
    local data="{"
    [ -n "$host" ] && data="$data\"host\":\"$host\","
    [ -n "$password" ] && data="$data\"password\":\"$password\","
    [ -n "$vnc_password" ] && data="$data\"vnc_password\":\"$vnc_password\","
    data="${data%,}}"
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

# Reboot multiple devices (parallel)
gms-rt-devices-reboot() {
    local devices="$1"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-reboot DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
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

            # 询问是否自动重启
            echo "💡 提示: 是否自动重启这些设备? (y/n)"
            read -r -t 10 auto_reboot || auto_reboot="n"

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
gms-rt-devices-screen() {
    local devices="$1"
    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-screen DEVICE1 [DEVICE2 ...]"; return 1; }
    check_jq
    echo "📺 显示设备屏幕..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/screen" "POST" "$data")
    echo "$response" | jq '.'
}

# Execute shell command (direct local adb shell)
gms-rt-devices-shell() {
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-shell DEVICE_ID [COMMAND]"; return 1; }

    # Shift the first argument (device_id) and get the rest as command
    shift
    local shell_command="$*"

    if ! command -v adb &> /dev/null; then
        error "adb 命令未找到. 请确保 Android SDK 已安装并配置 PATH"
        return 1
    fi

    # Check if device is connected
    if ! adb devices | grep -q "$device_id"; then
        error "设备 $device_id 未找到或未连接"
        echo "📱 当前连接的设备:"
        adb devices
        return 1
    fi

    # Check if command argument is provided
    if [ -n "$shell_command" ]; then
        # Direct command execution mode
        adb -s "$device_id" shell "$shell_command"
    else
        # Interactive shell mode
        echo ""
        echo "💻 打开设备Shell: $device_id..."
        echo "🔌 使用 Ctrl+D 退出 shell"
        echo ""
        adb -s "$device_id" shell
    fi
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
    local password="$3"

    [ -z "$devices" ] && { error "设备ID必填. 用法: gms-rt-devices-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"; return 1; }
    [ -z "$ssid" ] && { error "SSID必填. 用法: gms-rt-devices-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"; return 1; }
    [ -z "$password" ] && { error "密码必填. 用法: gms-rt-devices-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"; return 1; }

    check_jq
    echo "📶 连接WiFi: $ssid..."

    local devices_json=$(build_devices_json_data "$devices")
    local data=$(echo "$devices_json" | jq -c ". + {ssid: \"$ssid\", password: \"$password\"}")
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
    local url="${SERVER_URL}/files/progress"
    [ -n "$upload_id" ] && url="$url?upload_id=$upload_id"
    api_call "/files/progress" | jq '.'
}

# OpenGrok search
gms-rt-opengrok-search() {
    local query="$1"
    local full="${2:-false}"
    [ -z "$query" ] && { error "Query required. Usage: gms-rt-opengrok-search <query> [full]"; return 1; }
    check_jq
    echo "🔍 Searching OpenGrok for: $query..."
    local data="{\"query\":\"$query\",\"full\":$full}"
    local response=$(api_call "/opengrok/search" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Report Commands
# ==============================================================================

# Analyze report
gms-rt-reports-analyze() {
    local report_timestamp="$1"
    [ -z "$report_timestamp" ] && { error "Report timestamp required. Usage: gms-rt-reports-analyze <report_timestamp>"; return 1; }
    check_jq
    echo "🔍 Analyzing report: $report_timestamp..."

    # Use unified API with POST and mode parameter
    local response=$(curl -s -X POST "${API_BASE}/reports/analyze" \
        -F "mode=saved" \
        -F "report_timestamp=${report_timestamp}")

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
    # Use curl directly for DELETE with query parameter
    local response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X DELETE "${API_BASE}/reports/delete?timestamp=${report_timestamp}")
    local body=$(echo "$response" | grep -v "HTTP_STATUS:")
    echo "$body" | jq '.'
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
    local response=$(api_call "/reports/download?report_timestamp=$report_timestamp")

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

    echo "$response" | jq -r '.files[] | @json "{\"path\": \"" + .path + "\", \"relative_path\": \"" + .relative_path + "\"}"' | while IFS= read -r file_info; do
        local file_path=$(echo "$file_info" | jq -r '.path')
        local relative_path=$(echo "$file_info" | jq -r '.relative_path')
        local output_path="${output_dir}/${relative_path}"

        # 创建目标目录
        local target_dir=$(dirname "$output_path")
        mkdir -p "$target_dir"

        # 下载文件内容
        local file_response=$(api_call "/reports/download?path=${file_path}")
        if echo "$file_response" | jq -e '.success' > /dev/null; then
            echo "$file_response" | jq -r '.content' > "$output_path"
            echo "✓ Downloaded: $relative_path"
            ((success_count++))
        else
            echo "✗ Failed: $relative_path"
            ((fail_count++))
        fi
    done

    echo ""
    success "Report folder downloaded to: $output_dir"
}

# List all reports
gms-rt-reports-list() {
    check_jq
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
    local data="{\"test_host_ip\":\"$test_host_ip\", \"client_ip\":\"$client_ip\"}"
    local response=$(api_call "/ssh/ping" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        local reachable=$(echo "$response" | jq -r '.reachable')
        local latency=$(echo "$response" | jq -r '.latency')
        if [ "$reachable" = "true" ]; then
            success "Network reachable (latency: $latency)"
        else
            warning "Network not reachable"
        fi
        # Show route commands if available
        local route_commands=$(echo "$response" | jq '.route_commands')
        if [ "$route_commands" != "null" ]; then
            echo ""
            echo "📋 Suggested route commands:"
            echo ""
            echo "${YELLOW}Linux:${NC}"
            echo "$response" | jq -r '.route_commands.linux[]'
            echo ""
            echo "${YELLOW}Windows:${NC}"
            echo "$response" | jq -r '.route_commands.windows[]'
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
            echo "   正确格式应为：user@ip（例如：hcq@172.16.14.68）" >&2
            return 1
        fi
        echo "🔍 Checking SSHD status for $device_host..."
        # Use GET with query parameter (like USB/IP status)
        local response=$(api_call "/ssh/sshd?device_host=$device_host")
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

# Download skills ZIP
gms-rt-system-skills() {
    local skill_name="${1:-gms-remote-test}"
    echo "📁 Downloading skills directory as ZIP..."
    echo "URL: ${API_BASE}/system/skills?skill_name=${skill_name}"
    echo "Saving to: ${skill_name}-skills.zip"
    curl -o "${skill_name}-skills.zip" "${API_BASE}/system/skills?skill_name=${skill_name}"
    if [ $? -eq 0 ]; then
        success "Skills ZIP downloaded successfully"
        ls -lh "${skill_name}-skills.zip"
    else
        error "Failed to download skills ZIP"
    fi
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
        echo "  gms-rt-terminal-open 172.16.14.233      # Specify host"
        echo "  gms-rt-terminal-open 172.16.14.233 hcq  # Full parameters"
        echo ""
        return 0
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
                    user="hcq"
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
                echo "   3. Test connection: curl -s ${API_BASE}/terminal/open"
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
        user="${user:-hcq}"
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

    # 使用 api_call with custom curl args for file upload
    local curl_args="-F \"file=@$file_path\" -F \"path=$target_path\" -F \"auto_rename=true\""
    local response=$(api_call "/terminal/push" "POST" "" "$curl_args")

    local body
    if ! body=$(check_http_response "$response"); then
        error "Failed to push file - HTTP status: $HTTP_STATUS_CODE"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
        return 1
    fi

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
    curl -N "${API_BASE}/test/logs/stream"
}

# Start a test - delegates to /api/test/parse-args for intelligent parameter parsing
gms-rt-test-start() {
    check_jq

    # Collect all arguments into an array
    local args=("$@")
    local first_param="${args[0]:-}"

    # Show help if no arguments
    if [ -z "$first_param" ]; then
        _gms_rt_test_start_help
        return 1
    fi

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
  Mode 1 (Direct test): gms-rt-test-start <DEVICE> [TYPE] [MODULE/SUITE] [CASE/SUITE] [SUITE]
  Mode 2 (Retry report): gms-rt-test-start --retry <REPORT_TIMESTAMP> [DEVICE] [TYPE] [SUITE]

智能参数识别：
  - 包含 '/' 的参数自动识别为路径（test_suite）
  - 其他参数按位置识别为 test_module, test_case

示例:
  gms-rt-test-start RK3572GMS4 CTS /path/to/android-cts/tools
  gms-rt-test-start RK3572GMS4 CTS TestModuleName
  gms-rt-test-start RK3572GMS4 CTS TestModuleName TestCaseName
  gms-rt-test-start RK3572GMS4 CTS TestModuleName TestCaseName /path/to/suite

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
    check_jq
    echo "🛑 Stopping test..."
    local response=$(api_call "/test/stop" "POST")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Test stopped successfully"
    else
        warning "Failed to stop test or no test was running"
    fi
}

# List available test suites
gms-rt-test-suites() {
    local base_path="${1:-}"
    check_jq
    if [ -n "$base_path" ]; then
        echo "📋 Listing test suites under $base_path..."
    else
        echo "📋 Listing test suites..."
    fi
    local url="/test/suites"
    [ -n "$base_path" ] && url="/test/suites?base_path=$base_path"
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
    local suite_path="$1"
    local force_refresh="$2"
    [ -z "$suite_path" ] && { error "Suite path required. Usage: gms-rt-test-suites-result ~/GMS-Suite/android-gts-13.1-R2/android-gts/tools [--force-refresh]"; return 1; }
    check_jq

    # Expand tilde to home directory
    suite_path="${suite_path/#\~/$HOME}"

    echo "📋 Listing test results for suite: $suite_path..."

    # Find tradefed binary (optional - API can auto-detect)
    local tradefed_bin=$(find "$suite_path" -maxdepth 1 -type f -executable -name '*-tradefed' 2>/dev/null | head -1)

    # Build request data
    local data="{\"suite_path\":\"$suite_path\"}"
    if [ -n "$tradefed_bin" ]; then
        data=$(echo "$data" | jq --arg bin "$tradefed_bin" '. + {tradefed_bin: $bin}')
    fi

    # Call HTTP API endpoint with optional force_refresh parameter
    local url="/test/suites/result"
    if [ "$force_refresh" = "--force-refresh" ] || [ "$force_refresh" = "-f" ]; then
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
        # Output raw format (same as tradefed list results) - fast processing
        echo "$response" | jq -r '.raw_output' | grep -E 'Session|^[ ]*[0-9]' | grep -v '^04-' | grep -v '^D/' | grep -v 'DeviceManager'
    else
        local msg=$(extract_api_error "$response")
        error "Failed to list test results: $msg"
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
    local data="{\"device_host\":\"$device_host\"}"
    local response=$(api_call "/usbip/install" "POST" "$data")
    echo "$response" | jq '.'
}

# Start USB/IP connection
gms-rt-usbip-connect() {
    local device_host="$1"
    local device_password="$2"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-usbip-connect <user@ip> [password]"; return 1; }
    check_jq
    echo "🔌 Starting USB/IP connection to $device_host..."
    local data="{\"device_host\":\"$device_host\"}"
    [ -n "$device_password" ] && data=$(echo "$data" | jq ". + {\"device_password\":\"$device_password\"}")
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
    local data="{\"device_host\":\"$device_host\"}"
    local response=$(api_call "/usbip/disconnect" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "USB/IP stopped"
        echo "$response" | jq '.'
    else
        warning "Failed to stop USB/IP or not connected"
        echo "$response" | jq '.'
    fi
}

# Check USB/IP status
gms-rt-usbip-status() {
    local device_host="$1"
    [ -z "$device_host" ] && { error "Device host required. Usage: gms-rt-usbip-status <user@ip>"; return 1; }
    check_jq
    echo "🔌 Checking USB/IP status for $device_host..."
    # Use GET with query parameter
    api_call "/usbip/status?device_host=$device_host" | jq '.'
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
    local password="${3:-}"
    check_jq
    echo "🔍 Detecting user for $ip..."
    local data="{\"ip\":\"$ip\""
    [ -n "$username" ] && data="$data,\"username\":\"$username\""
    [ -n "$password" ] && data="$data,\"password\":\"$password\""
    data="$data}"
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
    local data="{\"username\":\"$username\"}"
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

gms-rt-system-help() {
    cat << EOF
${BLUE}GMS Remote Test API Helper (FastAPI Port 5001)${NC}
========================================

${YELLOW}ADB Port forward:${NC}
  gms-rt-adb-forward-start       - Start ADB port forwarding
  gms-rt-adb-forward-stop        - Stop ADB port forwarding

${YELLOW}Firmware Burning:${NC}
  gms-rt-burn-firmware           - Burn firmware image
  gms-rt-burn-gsi                - Burn GSI image
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
  gms-rt-devices-bootloader-lock    - Lock bootloader
  gms-rt-devices-bootloader-unlock  - Unlock bootloader
  gms-rt-devices-bootloader-status  - Check bootloader status
  gms-rt-devices-user-locked        - List user-locked devices
  gms-rt-devices-reboot             - Reboot devices
  gms-rt-devices-remount            - Remount RW (with auto-reboot prompt)
  gms-rt-devices-wifi       - Connect to WiFi
  gms-rt-devices-shell              - Open interactive ADB shell
  gms-rt-devices-screen             - Show device screen

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
  gms-rt-ssh-sshd          - Check SSHD status & install guide (optional: user@ip, e.g. hcq@172.16.14.68)

${YELLOW}System:${NC}
  gms-rt-system-docs             - Get API documentation
  gms-rt-system-health           - Check server health
  gms-rt-system-skills           - Download skills directory as ZIP

${YELLOW}Terminal:${NC}
  gms-rt-terminal-open           - Open SSH terminal on test host
  gms-rt-terminal-push           - Push file to test host directory

${YELLOW}Test Management:${NC}
  gms-rt-test-clean              - Clean test environment
  gms-rt-test-logs-stream        - Stream logs in real-time
  gms-rt-test-start              - Start test or retry report
  gms-rt-test-status             - Check test status
  gms-rt-test-stop               - Stop currently running test
  gms-rt-test-suites             - List available test suites
  gms-rt-test-suites-result      - List test results (tradefed list results)

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
  gms-rt-devices-bootloader-lock '["DEVICE-1", "DEVICE-2"]'
  gms-rt-desktop-vnc-start
  gms-rt-test-start DEVICE CTS TestModule
  gms-rt-test-logs-stream
  gms-rt-reports-list

${YELLOW}Test Start (Retry Mode):${NC}
  gms-rt-test-start --retry <TIMESTAMP> <DEVICE> <TYPE> <SUITE_PATH>
  gms-rt-test-start --retry 2026.04.11_17.27.04.421_2920 c3d9b8674f4b94f6 GTS /path/to/suite

${YELLOW}Terminal:${NC}
  gms-rt-terminal-open
  gms-rt-terminal-open 172.16.14.233 hcq
  gms-rt-terminal-push ./config.json

Server: ${GREEN}$SERVER_URL${NC}
Docs:   ${GREEN}${SERVER_URL}/docs${NC}
Help:   ${GREEN}${SERVER_URL}/api/system/help${NC}
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

if ! _is_sourced; then
    if [ $# -eq 0 ]; then
        gms-rt-system-help
    else
        "$@"
    fi
fi
