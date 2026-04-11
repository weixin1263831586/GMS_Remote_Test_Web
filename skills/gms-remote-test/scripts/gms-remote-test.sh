#!/bin/bash
# ==============================================================================
# GMS Remote Test API Helper Script (FastAPI Port 5001)
# Version: 2026.04.05-100000
# ==============================================================================

# Default configuration
# Use environment variable GMS_REMOTE_TEST_SERVER or default to localhost:5001
SERVER_URL="${GMS_REMOTE_TEST_SERVER:-http://172.16.14.233:5001}"
API_BASE="${SERVER_URL}/api"

# Colors for output
RED=$(printf '\033[0;31m')
GREEN=$(printf '\033[0;32m')
YELLOW=$(printf '\033[1;33m')
BLUE=$(printf '\033[0;34m')
NC=$(printf '\033[0m')

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

# Make API call and return JSON
api_call() {
    local endpoint="$1"
    local method="${2:-GET}"
    local data="${3:-}"

    if [ -n "$data" ] || [ "$method" = "POST" ]; then
        curl -s -X "${method}" "${API_BASE}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "${data}"
    else
        curl -s "${API_BASE}${endpoint}"
    fi
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

# Get device details
# Get device info for multiple devices (parallel)
gms-rt-devices-info() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-info DEVICE1 [DEVICE2 ...]"
    check_jq
    echo "📱 获取设备信息..."

    local device_array=$(convert_devices_to_json "$devices")
    local data="{\"devices\":$device_array}"

    local response=$(api_call "/devices/info" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "设备信息获取成功"
        echo "$response" | jq '.'
    else
        error "设备信息获取失败"
    fi
}

# Reboot multiple devices (parallel)
gms-rt-devices-reboot() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-reboot DEVICE1 [DEVICE2 ...]"
    check_jq
    echo "🔄 重启设备..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/reboot" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "设备重启成功"
        echo "$response" | jq '.'
    else
        error "设备重启失败"
    fi
}

# Remount multiple devices (parallel)
gms-rt-devices-remount() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-remount DEVICE1 [DEVICE2 ...]"
    check_jq
    echo "🔄 重新挂载设备..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/remount" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "设备重新挂载成功"
        echo "$response" | jq '.'
    else
        error "设备重新挂载失败"
    fi
}

# Lock bootloader
gms-rt-devices-bootloader-lock() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-bootloader-lock DEVICE1 [DEVICE2 ...]"
    check_jq
    echo "🔒 锁定Bootloader..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/bootloader-lock" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Bootloader锁定成功"
        echo "$response" | jq '.'
    else
        error "Bootloader锁定失败"
    fi
}

# Unlock bootloader
gms-rt-devices-bootloader-unlock() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-bootloader-unlock DEVICE1 [DEVICE2 ...]"
    check_jq
    echo "🔓 解锁Bootloader..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/bootloader-unlock" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Bootloader解锁成功"
        echo "$response" | jq '.'
    else
        error "Bootloader解锁失败"
    fi
}

# Check bootloader status
gms-rt-devices-bootloader-status() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-bootloader-status DEVICE1 [DEVICE2 ...]"
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

# ==============================================================================
# Desktop VNC Commands
# ==============================================================================

# Validate desktop host
gms-rt-desktop-validate() {
    local host="$1"
    [ -z "$host" ] && error "Host required. Usage: gms-rt-desktop-validate <host>"
    check_jq
    echo "🔍 Validating desktop host $host..."
    local data="{\"host\":\"$host\"}"
    local response=$(api_call "/desktop/validate" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Desktop host is valid"
        echo "$response" | jq '.'
    else
        error "Desktop host validation failed"
    fi
}

# ==============================================================================
# USB/IP Commands
# ==============================================================================

# Start USB/IP connection
gms-rt-usbip-start() {
    local device_host="$1"
    local device_password="$2"

    if [ -z "$device_host" ]; then
        error "Device host required. Usage: gms-rt-usbip-start <user@ip> [password]"
    fi

    check_jq
    echo "🔌 Starting USB/IP connection to $device_host..."

    local data="{\"device_host\":\"$device_host\"}"
    [ -n "$device_password" ] && data=$(echo "$data" | jq ". + {\"device_password\":\"$device_password\"}")

    local response=$(api_call "/usbip/start" "POST" "$data")

    if echo "$response" | jq -e '.success' > /dev/null; then
        success "USB/IP connection started"
        echo "$response" | jq '.'
    else
        local msg=$(echo "$response" | jq -r '.message // .detail // "Unknown error"')
        error "Failed to start USB/IP: $msg"
    fi
}

# Stop USB/IP connection
gms-rt-usbip-stop() {
    check_jq
    echo "🔌 Stopping USB/IP connection..."
    local response=$(api_call "/usbip/stop" "POST")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "USB/IP stopped"
    else
        warning "Failed to stop USB/IP or not connected"
    fi
}

# Check USB/IP status
gms-rt-usbip-status() {
    check_jq
    echo "🔌 Checking USB/IP status..."
    api_call "/usbip/status" | jq '.'
}

# ==============================================================================
# Test Management Commands
# ==============================================================================

# Start a test
gms-rt-test-start() {
    local device_serial="$1"
    local test_type="${2:-CTS}"
    local test_module="${3:-CtsPermissionTestCases}"
    local test_case="${4:-}"
    local test_suite="${5:-/home/hcq/GMS-Suite/android-cts-16_r4/android-cts/tools}"

    if [ -z "$device_serial" ]; then
        error "Device serial required. Usage: gms-rt-test-start <DEVICE> [TYPE] [MODULE] [CASE] [SUITE]"
        return 1
    fi

    check_jq
    echo "🚀 Starting test..."
    echo "  Device: $device_serial"
    echo "  Type: $test_type"
    echo "  Module: $test_module"

    local data="{\"devices\":[\"$device_serial\"],\"test_type\":\"$test_type\",\"test_module\":\"$test_module\",\"test_suite\":\"$test_suite\"}"
    if [ -n "$test_case" ]; then
        data=$(echo "$data" | jq ". + {\"test_case\":\"$test_case\"}")
    fi

    local response=$(api_call "/test/start" "POST" "$data")

    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Test started successfully"
        echo "$response" | jq '.'
    else
        local msg=$(echo "$response" | jq -r '.message // .detail // "Unknown error"')
        error "Failed to start test: $msg"
        return 1
    fi
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

# Check test status
gms-rt-test-status() {
    check_jq
    echo "📊 Checking test status..."
    api_call "/test/status" | jq '.'
}

# Monitor test progress
gms-rt-test-monitor() {
    check_jq
    echo "⏳ Monitoring test progress (Ctrl+C to stop)..."

    while true; do
        local response=$(api_call "/test/status")
        local running=$(echo "$response" | jq -r '.running')

        if [ "$running" = "false" ]; then
            echo ""
            success "Test completed!"
            echo "$response" | jq '.'
            break
        fi

        local current_test=$(echo "$response" | jq -r '.current_test // "Unknown"')
        echo -ne "\r⏳ Running: $current_test ($(date '+%H:%M:%S')) "
        sleep 5
    done
}

# Download current log
gms-rt-test-logs-download() {
    local output_file="${1:-test.log}"
    echo "📥 Downloading current log to $output_file..."
    curl -s "${API_BASE}/test/logs/download" -o "$output_file"
    if [ $? -eq 0 ]; then
        success "Log downloaded to $output_file"
    else
        error "Failed to download log"
    fi
}

# ==============================================================================
# Report Commands
# ==============================================================================

# Get latest test report
gms-rt-reports-latest() {
    check_jq
    echo "📄 Fetching latest report..."
    local response=$(api_call "/reports/list")
    local latest=$(echo "$response" | jq '.reports[0]')

    if [ "$latest" != "null" ]; then
        echo "$latest" | jq '.'
    else
        warning "No reports found"
    fi
}

# List all reports
gms-rt-reports-list() {
    check_jq
    echo "📋 Listing all reports..."
    local response=$(api_call "/reports/list")
    local count=$(echo "$response" | jq '.reports | length')
    echo "Found $count report(s):"
    echo "$response" | jq -r '.reports[] | "\(.timestamp // "N/A") | \(.client_id // "N/A") | \(.test_type // "N/A") | \(.result // "N/A")"'
}

# Get report files
gms-rt-reports-files() {
    local timestamp="$1"
    [ -z "$timestamp" ] && error "Timestamp required. Usage: gms-rt-reports-files <TIMESTAMP>"
    check_jq
    echo "📄 Fetching report files for $timestamp..."
    api_call "/reports/files/$timestamp" | jq '.'
}

# ==============================================================================
# Configuration Commands
# ==============================================================================

# Update config
gms-rt-config-update() {
    local key="$1"
    local value="$2"
    [ -z "$key" ] && error "Key required. Usage: gms-rt-config-update <key> <value>"
    check_jq
    echo "⚙️  Updating configuration: $key = $value"
    local data="{\"$key\":\"$value\"}"
    local response=$(api_call "/config/update" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Configuration updated"
    else
        error "Failed to update configuration"
    fi
}

# Validate config
gms-rt-config-validate() {
    check_jq
    echo "⚙️  Validating configuration..."
    api_call "/config/validate" | jq '.'
}

# ==============================================================================
# Network & Client Commands
# ==============================================================================

# Get client IP
gms-rt-client-info() {
    check_jq
    echo "🖥️  Getting client information..."
    api_call "/client-info" | jq '.'
}

# Record client info
gms-rt-client-record() {
    local ip="$1"
    local username="$2"
    [ -z "$ip" ] && error "IP required. Usage: gms-rt-client-record <ip> [username]"
    check_jq
    echo "📝 Recording client info..."
    local data="{\"ip\":\"$ip\""
    [ -n "$username" ] && data=$(echo "$data" | jq ". + {\"username\":\"$username\"}")
    data=$(echo "$data" | jq '.')
    local response=$(api_call "/client-info" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Client info recorded"
        echo "$response" | jq '.'
    else
        error "Failed to record client info"
    fi
}

# Auto-detect client username
gms-rt-client-detect() {
    local ip="$1"
    local username="$2"
    local password="$3"
    [ -z "$ip" ] && error "IP required. Usage: gms-rt-client-detect <ip> [username] [password]"
    check_jq
    echo "🔍 Detecting client username..."
    local data="{\"ip\":\"$ip\""
    [ -n "$username" ] && data=$(echo "$data" | jq ". + {\"username\":\"$username\"}")
    [ -n "$password" ] && data=$(echo "$data" | jq ". + {\"password\":\"$password\"}")
    data=$(echo "$data" | jq '.')
    local response=$(api_call "/client-info/detect" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Client detected"
        echo "$response" | jq '.'
    else
        error "Failed to detect client"
    fi
}

# Check SSH route
gms-rt-ssh-route() {
    check_jq
    echo "🛣️  Checking SSH route..."
    api_call "/ssh/route" | jq '.'
}

# Test SSH ping between test host and client
gms-rt-ssh-ping() {
    local test_host_ip="$1"
    local client_ip="$2"
    [ -z "$test_host_ip" ] && error "Test host IP required. Usage: gms-rt-network-ping <test_host_ip> <client_ip>"
    [ -z "$client_ip" ] && error "Client IP required. Usage: gms-rt-network-ping <test_host_ip> <client_ip>"
    check_jq
    echo "🌐 Testing SSH connectivity..."
    local data="{\"test_host_ip\":\"$test_host_ip\", \"client_ip\":\"$client_ip\"}"
    local response=$(api_call "/ssh/route/ping" "POST" "$data")
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
        error "Failed to connect VPN"
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
        error "Failed to disconnect VPN"
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
# Firmware Burning Commands
# ==============================================================================

# Burn firmware
gms-rt-burn-firmware() {
    local firmware_path="$1"
    local device_id="$2"
    [ -z "$firmware_path" ] && error "Firmware path required. Usage: gms-rt-burn-firmware <firmware.img> <device_id>"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-burn-firmware <firmware.img> <device_id>"
    [ ! -f "$firmware_path" ] && error "Firmware file not found: $firmware_path"
    echo "🔥 Burning firmware to $device_id..."
    local response=$(curl -s -F "firmware=@$firmware_path" -F "device_id=$device_id" "${API_BASE}/burn/firmware")
    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "Firmware burning started"
        echo "$response" | jq '.'
    else
        warning "Burn command sent (check server for details)"
        echo "$response"
    fi
}

# Burn GSI image
gms-rt-burn-gsi() {
    local gsi_path="$1"
    local device_id="$2"
    [ -z "$gsi_path" ] && error "GSI path required. Usage: gms-rt-burn-gsi <gsi.img> <device_id>"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-burn-gsi <gsi.img> <device_id>"
    [ ! -f "$gsi_path" ] && error "GSI file not found: $gsi_path"
    echo "🔥 Burning GSI to $device_id..."
    local response=$(curl -s -F "gsi=@$gsi_path" -F "device_id=$device_id" "${API_BASE}/burn/gsi")
    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "GSI burning started"
        echo "$response" | jq '.'
    else
        warning "Burn command sent (check server for details)"
        echo "$response"
    fi
}

# Burn serial number
gms-rt-burn-serial() {
    local device_id="$1"
    local serial="$2"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-burn-serial <device_id> <serial>"
    [ -z "$serial" ] && error "Serial required. Usage: gms-rt-burn-serial <device_id> <serial>"
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
# System Commands
# ==============================================================================

# Health check
gms-rt-system-health() {
    check_jq
    echo "🏥 Checking server health..."
    api_call "/system/health" | jq '.'
}

# System docs
gms-rt-system-docs() {
    check_jq
    echo "📚 Getting API documentation..."
    api_call "/system/docs" | jq '.'
}

# ==============================================================================
# Configuration Commands
# ==============================================================================

# Validate configuration
gms-rt-config-validate() {
    check_jq
    echo "✅ Validating configuration..."
    api_call "/config/validate" | jq '.'
}

# Get config values
gms-rt-config-values() {
    check_jq
    echo "📋 Getting config values..."
    api_call "/config/values" | jq '.'
}

# Read config
gms-rt-config-read() {
    check_jq
    echo "📖 Reading configuration..."
    api_call "/config/read" | jq '.'
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

# Set username
gms-rt-users-set-username() {
    local username="$1"
    [ -z "$username" ] && error "Username required. Usage: gms-rt-users-set-username <username>"
    check_jq
    echo "👤 Setting username to $username..."
    local data="{\"username\":\"$username\"}"
    local response=$(api_call "/users/set-username" "POST" "$data")
    echo "$response" | jq '.'
}

# List users
gms-rt-users-list() {
    check_jq
    echo "👥 Listing all users..."
    api_call "/users/list" | jq '.'
}

# ==============================================================================
# Device Commands
# ==============================================================================

# List devices
gms-rt-devices-list() {
    check_jq
    echo "📱 Listing devices..."
    api_call "/devices/list" | jq '.'
}

# User locked devices
gms-rt-devices-user-locked() {
    check_jq
    echo "🔒 Getting user-locked devices..."
    api_call "/devices/user-locked" | jq '.'
}

# Connect WiFi
gms-rt-devices-connect-wifi() {
    local devices="$1"
    local ssid="$2"
    local password="$3"

    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-connect-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"
    [ -z "$ssid" ] && error "SSID必填. 用法: gms-rt-devices-connect-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"
    [ -z "$password" ] && error "密码必填. 用法: gms-rt-devices-connect-wifi DEVICE1 [DEVICE2 ...] <ssid> <password>"

    check_jq
    echo "📶 连接WiFi: $ssid..."

    local device_array=$(convert_devices_to_json "$devices")
    local data=$(echo "{\"devices\":$device_array,\"ssid\":\"$ssid\",\"password\":\"$password\"}" | jq -c '.')
    local response=$(api_call "/devices/connect-wifi" "POST" "$data")

    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "WiFi连接已启动"
        echo "$response" | jq '.'
    else
        error "WiFi连接失败"
        echo "$response" | jq '.'
    fi
}

# Execute shell command
gms-rt-devices-shell() {
    local device_id="$1"
    [ -z "$device_id" ] && error "设备ID必填. 用法: gms-rt-devices-shell DEVICE_ID"
    check_jq
    echo "💻 打开设备Shell: $device_id..."
    local data="{\"serial_no\":\"$device_id\"}"
    local response=$(api_call "/devices/shell" "POST" "$data")
    echo "$response" | jq '.'
}

# Show device screen
gms-rt-devices-screen() {
    local devices="$1"
    [ -z "$devices" ] && error "设备ID必填. 用法: gms-rt-devices-screen DEVICE1 [DEVICE2 ...]"
    check_jq
    echo "📺 显示设备屏幕..."

    local data=$(build_devices_json_data "$devices")

    local response=$(api_call "/devices/screen" "POST" "$data")
    echo "$response" | jq '.'
}

# Terminal push command
gms-rt-terminal-push() {
    local command="$1"
    [ -z "$command" ] && error "Command required. Usage: gms-rt-terminal-push <command>"
    check_jq
    echo "⌨️  Pushing command to terminal..."
    local data="{\"command\":\"$command\"}"
    local response=$(api_call "/terminal/push" "POST" "$data")
    echo "$response" | jq '.'
}

# OpenGrok search
gms-rt-opengrok-search() {
    local query="$1"
    local full="${2:-false}"
    [ -z "$query" ] && error "Query required. Usage: gms-rt-opengrok-search <query> [full]"
    check_jq
    echo "🔍 Searching OpenGrok for: $query..."
    local data="{\"query\":\"$query\",\"full\":$full}"
    local response=$(api_call "/opengrok/search" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Test Commands
# ==============================================================================

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

# Clean test environment
gms-rt-test-clean() {
    check_jq
    echo "🧹 Cleaning test environment..."
    local response=$(api_call "/test/clean" "POST" "{}")
    echo "$response" | jq '.'
}

# Get current test logs
gms-rt-test-logs-current() {
    local output_file="${1:-test_logs_$(date +%Y%m%d_%H%M%S).log}"
    echo "📥 Downloading current test logs to $output_file..."
    curl -s "${API_BASE}/test/logs/current" -o "$output_file"
    if [ $? -eq 0 ]; then
        success "Logs downloaded to $output_file"
    else
        error "Failed to download logs"
    fi
}

# Batch download logs
gms-rt-test-logs-batch() {
    local files="$1"
    [ -z "$files" ] && error "Files required. Usage: gms-rt-test-logs-batch <FILES_ARRAY>"
    check_jq
    echo "📦 Batch downloading logs..."
    local data="{\"files\":$files}"
    local response=$(api_call "/test/logs/batch" "POST" "$data")
    echo "$response" | jq '.'
}

# Save current logs
gms-rt-test-logs-save-current() {
    check_jq
    echo "💾 Saving current logs..."
    local response=$(api_call "/test/logs/save-current" "POST" "{}")
    echo "$response" | jq '.'
}

# List test logs
gms-rt-test-logs-list() {
    check_jq
    echo "📋 Listing test logs..."
    api_call "/test/logs/list" | jq '.'
}

# Stream test logs
gms-rt-test-logs-stream() {
    echo "📡 Streaming test logs (Ctrl+C to stop)..."
    curl -N "${API_BASE}/test/logs/stream"
}

# ==============================================================================
# Report Commands
# ==============================================================================

# Analyze report source
gms-rt-reports-analyze-source() {
    local test_name="$1"
    local error_message="${2:-}"
    [ -z "$test_name" ] && error "Test name required. Usage: gms-rt-reports-analyze-source <test_name> [error_message]"
    check_jq
    echo "🔍 Analyzing test source: $test_name..."
    local data="{\"test_name\":\"$test_name\""
    [ -n "$error_message" ] && data="$data,\"error_message\":\"$error_message\""
    data="$data}"
    local response=$(api_call "/reports/analyze-source" "POST" "$data")
    echo "$response" | jq '.'
}

# View report
gms-rt-reports-view() {
    local report_timestamp="$1"
    [ -z "$report_timestamp" ] && error "Report timestamp required. Usage: gms-rt-reports-view <report_timestamp>"
    check_jq
    echo "📊 Viewing report: $report_timestamp..."
    api_call "/reports/view?report_timestamp=$report_timestamp" | jq '.'
}

# Download report
gms-rt-reports-download() {
    local report_timestamp="$1"
    [ -z "$report_timestamp" ] && error "Report timestamp required. Usage: gms-rt-reports-download <report_timestamp>"
    check_jq
    echo "📥 Downloading report: $report_timestamp..."
    api_call "/reports/download/$report_timestamp" -o "report_${report_timestamp}.zip"
}

# Delete report
gms-rt-reports-delete() {
    local report_timestamp="$1"
    [ -z "$report_timestamp" ] && error "Report timestamp required. Usage: gms-rt-reports-delete <report_timestamp>"
    check_jq
    echo "🗑️  Deleting report: $report_timestamp..."
    local data="{\"report_timestamp\":\"$report_timestamp\"}"
    local response=$(api_call "/reports/delete" "DELETE" "$data")
    echo "$response" | jq '.'
}

# Analyze report
gms-rt-reports-analyze() {
    local report_timestamp="$1"
    local use_ai="${2:-true}"
    [ -z "$report_timestamp" ] && error "Report timestamp required. Usage: gms-rt-reports-analyze <report_timestamp> [use_ai]"
    check_jq
    echo "🔍 Analyzing report: $report_timestamp..."
    local data="{\"report_timestamp\":\"$report_timestamp\",\"use_ai\":$use_ai}"
    local response=$(api_call "/reports/analyze" "POST" "$data")
    echo "$response" | jq '.'
}

# AI analyze report
gms-rt-reports-analyze-ai() {
    local report_timestamp="$1"
    [ -z "$report_timestamp" ] && error "Report timestamp required. Usage: gms-rt-reports-analyze-ai <report_timestamp>"
    check_jq
    echo "🤖 AI analyzing report: $report_timestamp..."
    local data="{\"report_timestamp\":\"$report_timestamp\"}"
    local response=$(api_call "/reports/analyze-ai" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Desktop Commands
# ==============================================================================

# Get VNC status
gms-rt-desktop-vnc-status() {
    check_jq
    echo "🖥️  Getting VNC status..."
    api_call "/desktop/vnc/status" | jq '.'
}

# Start desktop VNC
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

# Stop desktop VNC
gms-rt-desktop-vnc-stop() {
    check_jq
    echo "🛑 Stopping desktop VNC..."
    local response=$(api_call "/desktop/vnc/stop" "POST" "{}")
    echo "$response" | jq '.'
}

# Validate desktop
gms-rt-desktop-validate() {
    local host="$1"
    local password="${2:-}"
    [ -z "$host" ] && error "Host required. Usage: gms-rt-desktop-validate <host> [password]"
    check_jq
    echo "✅ Validating desktop: $host..."
    local data="{\"host\":\"$host\""
    [ -n "$password" ] && data="$data,\"password\":\"$password\""
    data="$data}"
    local response=$(api_call "/desktop/validate" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# USB/IP Commands
# ==============================================================================

# Start ADB forward
gms-rt-adb-forward-start() {
    local device_host="$1"
    local device_password="$2"
    [ -z "$device_host" ] && error "Device host required. Usage: gms-rt-adb-forward-start <device_host> <device_password>"
    [ -z "$device_password" ] && error "Device password required. Usage: gms-rt-adb-forward-start <device_host> <device_password>"
    check_jq
    echo "🔌 Starting ADB forward..."
    local data="{\"device_host\":\"$device_host\",\"device_password\":\"$device_password\"}"
    local response=$(api_call "/adb-forward/start" "POST" "$data")
    echo "$response" | jq '.'
}

# Stop ADB forward
gms-rt-adb-forward-stop() {
    local device_host="$1"
    [ -z "$device_host" ] && error "Device host required. Usage: gms-rt-adb-forward-stop <device_host>"
    check_jq
    echo "🛑 Stopping ADB forward..."
    local data="{\"device_host\":\"$device_host\"}"
    local response=$(api_call "/adb-forward/stop" "POST" "$data")
    echo "$response" | jq '.'
}

# Get USB/IP status
gms-rt-usbip-status() {
    check_jq
    echo "📡 Getting USB/IP status..."
    api_call "/usbip/status" | jq '.'
}

# Auto install USB/IP
gms-rt-usbip-auto-install() {
    check_jq
    echo "🔧 Auto-installing USB/IP..."
    local response=$(api_call "/usbip/auto-install" "POST" "{}")
    echo "$response" | jq '.'
}

# ==============================================================================
# SSH Commands
# ==============================================================================

# Check SSHD status
gms-rt-ssh-sshd-check() {
    check_jq
    echo "🔍 Checking SSHD status..."
    api_call "/ssh/sshd-check" | jq '.'
}

# Install SSHD
gms-rt-ssh-sshd-install() {
    check_jq
    echo "🔧 Installing SSHD..."
    local response=$(api_call "/ssh/sshd-install" "POST" "{}")
    echo "$response" | jq '.'
}

# Check SSH route
# ==============================================================================
# VPN Commands
# ==============================================================================

# Get VPN status
gms-rt-vpn-status() {
    check_jq
    echo "📡 Getting VPN status..."
    api_call "/vpn/status" | jq '.'
}

# Connect VPN
gms-rt-vpn-connect() {
    check_jq
    echo "🔗 Connecting to VPN..."
    local response=$(api_call "/vpn/connect" "POST" "{}")
    echo "$response" | jq '.'
}

# Disconnect VPN
gms-rt-vpn-disconnect() {
    check_jq
    echo "🔌 Disconnecting VPN..."
    local response=$(api_call "/vpn/disconnect" "POST" "{}")
    echo "$response" | jq '.'
}

# ==============================================================================
# File Commands
# ==============================================================================

# Upload file
gms-rt-files-upload() {
    local file_path="$1"
    local target_path="${2:-}"
    [ -z "$file_path" ] && error "File path required. Usage: gms-rt-files-upload <file_path> [target_path]"
    [ ! -f "$file_path" ] && error "File not found: $file_path"

    check_jq
    echo "📤 Uploading file: $file_path..."

    if [ -n "$target_path" ]; then
        curl -X POST "${API_BASE}/files/upload" \
            -F "file=@$file_path" \
            -F "path=$target_path" \
            | jq '.'
    else
        curl -X POST "${API_BASE}/files/upload" \
            -F "file=@$file_path" \
            | jq '.'
    fi
}

# Install APK
gms-rt-files-install() {
    local file_path="$1"
    local device_id="$2"
    [ -z "$file_path" ] && error "File path required. Usage: gms-rt-files-install <file_path> <device_id>"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-files-install <file_path> <device_id>"
    [ ! -f "$file_path" ] && error "File not found: $file_path"

    check_jq
    echo "📦 Installing APK: $file_path to $device_id..."

    curl -X POST "${API_BASE}/files/install" \
        -F "file=@$file_path" \
        -F "device_id=$device_id" \
        | jq '.'
}

# Get upload progress
gms-rt-files-progress() {
    local upload_id="${1:-}"
    check_jq
    echo "📊 Getting upload progress..."
    local url="${SERVER_URL}/files/progress"
    [ -n "$upload_id" ] && url="$url?upload_id=$upload_id"
    api_call "/files/progress" | jq '.'
}

# List files
gms-rt-files-list() {
    local path="${1:-/tmp}"
    check_jq
    echo "📁 Listing files in: $path..."
    local data="{\"path\":\"$path\"}"
    local response=$(api_call "/files/list" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# Burn Commands
# ==============================================================================

# Burn firmware
gms-rt-burn-firmware() {
    local firmware_path="$1"
    local devices="$2"
    local wipe_data="${3:-true}"

    [ -z "$firmware_path" ] && error "Firmware path required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data]"
    [ -z "$devices" ] && error "Devices required. Usage: gms-rt-burn-firmware <firmware_path> <devices> [wipe_data]"
    [ ! -f "$firmware_path" ] && error "Firmware file not found: $firmware_path"

    echo "🔥 Burning firmware: $firmware_path to devices: $devices..."
    echo "⏳ Uploading firmware (this may take a few minutes)..."

    # Get terminal width for progress bars
    local term_width=${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}
    local bar_width=$((term_width * 60 / 100))

    # Upload with progress bar and capture response
    local tmp_response=$(mktemp)

    # Set COLUMNS for curl progress bar width (60% of terminal)
    export COLUMNS=$bar_width
    curl -# -X POST "${API_BASE}/burn/firmware" \
        -F "firmware_file=@$firmware_path" \
        -F "devices=$devices" \
        -F "wipe_data=$wipe_data" \
        -o "$tmp_response" \
        -w "\nHTTP_STATUS:%{http_code}\n"

    unset COLUMNS
    local http_status=$(grep "HTTP_STATUS:" "$tmp_response" | cut -d: -f2)
    local response=$(cat "$tmp_response" | grep -v "HTTP_STATUS:")

    rm -f "$tmp_response"

    echo ""  # Add newline after progress bar

    # Check response
    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "Firmware burn completed successfully"
        echo "$response" | jq '.'
    else
        error "Firmware burn failed (HTTP $http_status)"
        echo "$response" | jq '.' 2>/dev/null || echo "$response"
        return 1
    fi
}

# Burn GSI
gms-rt-burn-gsi() {
    local gsi_path="$1"
    local devices="$2"
    local wipe_data="${3:-true}"

    [ -z "$gsi_path" ] && error "GSI path required. Usage: gms-rt-burn-gsi <gsi_path> <devices> [wipe_data]"
    [ -z "$devices" ] && error "Devices required. Usage: gms-rt-burn-gsi <gsi_path> <devices> [wipe_data]"
    [ ! -f "$gsi_path" ] && error "GSI file not found: $gsi_path"

    check_jq
    echo "🔥 Burning GSI: $gsi_path to devices: $devices..."
    echo "⏳ Uploading and burning (this may take a few minutes)..."

    local response=$(curl -s -X POST "${API_BASE}/burn/gsi" \
        -F "gsi_image=@$gsi_path" \
        -F "devices=$devices" \
        -F "wipe_data=$wipe_data")

    if echo "$response" | jq -e '.success' > /dev/null; then
        success "GSI burn completed successfully"
        echo "$response" | jq '.'
    else
        error "GSI burn failed"
        echo "$response" | jq '.' 2>/dev/null || echo "$response"
    fi
}

# Burn serial
gms-rt-burn-serial() {
    local device_id="$1"
    local new_serial="$2"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-burn-serial <device_id> <new_serial>"
    [ -z "$new_serial" ] && error "New serial required. Usage: gms-rt-burn-serial <device_id> <new_serial>"
    check_jq
    echo "🔥 Burning serial $new_serial to $device_id..."
    local data="{\"device_id\":\"$device_id\",\"new_serial\":\"$new_serial\"}"
    local response=$(api_call "/burn/serial" "POST" "$data")
    echo "$response" | jq '.'
}

# ==============================================================================
# System Commands
# ==============================================================================

# WebSocket connection
gms-rt-system-websocket() {
    local client_id="${1:-test_client_$(date +%s)}"
    echo "🔗 Connecting to WebSocket with client_id: $client_id..."
    echo "Use this in your WebSocket client: ws://${SERVER_URL}/system/websocket/${client_id}"
}

# ==============================================================================
# Help Function
# ==============================================================================

gms-rt-system-help() {
    cat << EOF
${BLUE}GMS Remote Test API Helper (FastAPI Port 5001)${NC}
========================================

${YELLOW}System:${NC}
  gms-rt-system-health      - Check server health
  gms-rt-system-docs        - Get API documentation

${YELLOW}Configuration:${NC}
  gms-rt-config-validate    - Validate configuration
  gms-rt-config-values      - Get frontend config values
  gms-rt-config-read        - Read full configuration
  gms-rt-config-update      - Update configuration

${YELLOW}User Management:${NC}
  gms-rt-users-current      - Get current user info
  gms-rt-users-detect       - Auto-detect username
  gms-rt-users-set-username - Set username manually
  gms-rt-users-list         - List all users

${YELLOW}Device Management:${NC}
  gms-rt-devices-list                - List all connected devices
  gms-rt-devices-bootloader-lock     - Lock bootloader
  gms-rt-devices-bootloader-unlock   - Unlock bootloader
  gms-rt-devices-bootloader-status   - Check bootloader status
  gms-rt-devices-user-locked         - List user-locked devices
  gms-rt-devices-reboot              - Reboot devices
  gms-rt-devices-remount             - Remount RW
  gms-rt-devices-connect-wifi        - Connect to WiFi
  gms-rt-devices-shell               - Execute shell command
  gms-rt-devices-screen              - Show device screen

${YELLOW}Desktop VNC:${NC}
  gms-rt-desktop-vnc-status   - Check VNC status
  gms-rt-desktop-vnc-start     - Start VNC
  gms-rt-desktop-vnc-stop      - Stop VNC
  gms-rt-desktop-validate      - Validate desktop host

${YELLOW}USB/IP Connection:${NC}
  gms-rt-adb-forward-start    - Start ADB port forwarding
  gms-rt-adb-forward-stop     - Stop ADB port forwarding
  gms-rt-usbip-status          - Check USB/IP status
  gms-rt-usbip-start          - Start USB/IP connection
  gms-rt-usbip-stop           - Stop USB/IP connection
  gms-rt-usbip-auto-install    - Auto-install USB/IP

${YELLOW}Test Management:${NC}
  gms-rt-test-start           - Start a test
  gms-rt-test-stop            - Stop currently running test
  gms-rt-test-clean           - Clean test environment
  gms-rt-test-status          - Check test status
  gms-rt-test-suites          - List available test suites
  gms-rt-test-logs-current    - Download current log
  gms-rt-test-logs-batch      - Batch download logs
  gms-rt-test-logs-save-current - Save current logs
  gms-rt-test-logs-list       - List test logs
  gms-rt-test-logs-stream     - Stream logs in real-time

${YELLOW}Reports:${NC}
  gms-rt-reports-latest       - Get latest test report
  gms-rt-reports-list         - List all test reports
  gms-rt-reports-files        - Get report files
  gms-rt-reports-analyze      - Analyze report
  gms-rt-reports-view         - View report
  gms-rt-reports-download     - Download report
  gms-rt-reports-delete       - Delete report
  gms-rt-reports-analyze-source - Analyze test source
  gms-rt-reports-analyze-ai   - AI analyze report

${YELLOW}SSH Management:${NC}
  gms-rt-ssh-route             - Check SSH route
  gms-rt-ssh-ping              - Test SSH connectivity
  gms-rt-ssh-sshd-check        - Check SSHD status
  gms-rt-ssh-sshd-install      - Install SSHD

${YELLOW}VPN Management:${NC}
  gms-rt-vpn-status           - Check VPN status
  gms-rt-vpn-connect          - Connect to VPN
  gms-rt-vpn-disconnect       - Disconnect VPN

${YELLOW}File Management:${NC}
  gms-rt-files-upload         - Upload file
  gms-rt-files-install        - Upload and install APK
  gms-rt-files-progress       - Get upload progress
  gms-rt-files-list           - List files

${YELLOW}Firmware Burning:${NC}
  gms-rt-burn-firmware        - Burn firmware image
  gms-rt-burn-gsi             - Burn GSI image
  gms-rt-burn-serial          - Burn serial number

${YELLOW}Other:${NC}
  gms-rt-terminal-push        - Push command to terminal
  gms-rt-opengrok-search      - Search OpenGrok code
  gms-rt-system-websocket     - WebSocket connection info

${YELLOW}Examples:${NC}
  # List devices
  gms-rt-devices-list

  # Lock bootloader
  gms-rt-devices-bootloader-lock '["DEVICE-1", "DEVICE-2"]'

  # Start desktop VNC
  gms-rt-desktop-vnc-start

  # Start test
  gms-rt-test-start '["DEVICE-1"]' "CTS" "CtsPermissionTestCases"

  # Stream logs
  gms-rt-test-logs-stream

  # Check reports
  gms-rt-reports-list

${YELLOW}Performance Notes:${NC}
  - All skill commands match API paths exactly
  - Multi-device operations use parallel execution (75-85% faster)

Server: ${GREEN}$SERVER_URL${NC}
Docs:   ${GREEN}${SERVER_URL}/docs${NC}
Help:   ${GREEN}${SERVER_URL}/api/system/help${NC}
EOF
}

# Main command dispatcher
# Only execute when run directly, not when sourced
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [ $# -eq 0 ]; then
        gms-rt-system-help
    else
        "$@"
    fi
fi
