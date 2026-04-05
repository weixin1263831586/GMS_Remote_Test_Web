#!/bin/bash
# ==============================================================================
# GMS Remote Test API Helper Script (FastAPI Port 5001)
# Version: 2026.04.05-100000
# ==============================================================================

# Default configuration
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
    exit 1
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

    if [ -n "$data" ]; then
        curl -sX "${method}" "${API_BASE}${endpoint}" \
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

# List all connected devices
gms-rt-devices() {
    check_jq
    echo "📱 Listing connected devices..."
    api_call "/devices/list" | jq '.'
}

# Get device details
gms-rt-device-details() {
    local device_id="$1"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-device-details <DEVICE_ID>"
    check_jq
    echo "📱 Getting details for $device_id..."
    api_call "/devices/details/$device_id" | jq '.'
}

# Get device info for multiple devices (parallel)
gms-rt-devices-info() {
    local devices="$1"
    [ -z "$devices" ] && error "Devices required. Usage: gms-rt-devices-info \"[DEVICE-1,DEVICE-2]\""
    check_jq
    echo "📱 Getting device info (parallel execution)..."
    local data="{\"devices\":$devices}"
    local response=$(api_call "/devices/info" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Device info retrieved"
        echo "$response" | jq '.'
    else
        error "Failed to get device info"
    fi
}

# Reboot multiple devices (parallel)
gms-rt-devices-reboot() {
    local devices="$1"
    [ -z "$devices" ] && error "Devices required. Usage: gms-rt-devices-reboot \"[DEVICE-1,DEVICE-2]\""
    check_jq
    echo "🔄 Rebooting devices (parallel execution)..."
    local data="{\"devices\":$devices}"
    local response=$(api_call "/devices/reboot" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Devices rebooted"
        echo "$response" | jq '.'
    else
        error "Failed to reboot devices"
    fi
}

# Remount multiple devices (parallel)
gms-rt-devices-remount() {
    local devices="$1"
    [ -z "$devices" ] && error "Devices required. Usage: gms-rt-devices-remount \"[DEVICE-1,DEVICE-2]\""
    check_jq
    echo "🔄 Remounting devices (parallel execution)..."
    local data="{\"devices\":$devices}"
    local response=$(api_call "/devices/remount" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Devices remounted"
        echo "$response" | jq '.'
    else
        error "Failed to remount devices"
    fi
}

# Lock bootloader
gms-rt-device-lock() {
    local device_id="$1"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-device-lock <DEVICE_ID>"
    check_jq
    echo "🔒 Locking bootloader for $device_id..."
    local data="{\"device_id\":\"$device_id\"}"
    local response=$(api_call "/devices/bootloader-lock" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Bootloader locked"
        echo "$response" | jq '.'
    else
        error "Failed to lock bootloader"
    fi
}

# Unlock bootloader
gms-rt-device-unlock() {
    local device_id="$1"
    [ -z "$device_id" ] && error "Device ID required. Usage: gms-rt-device-unlock <DEVICE_ID>"
    check_jq
    echo "🔓 Unlocking bootloader for $device_id..."
    local data="{\"device_id\":\"$device_id\"}"
    local response=$(api_call "/devices/bootloader-unlock" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "Bootloader unlocked"
        echo "$response" | jq '.'
    else
        error "Failed to unlock bootloader"
    fi
}

# ==============================================================================
# Desktop VNC Commands
# ==============================================================================

# Start desktop VNC
gms-rt-vnc-start() {
    local host="$1"
    local username="${2:-hcq}"
    [ -z "$host" ] && error "Host required. Usage: gms-rt-vnc-start <host> [username]"
    check_jq
    echo "🖥️  Starting VNC for $host..."
    local data="{\"host\":\"$host\",\"username\":\"$username\"}"
    local response=$(api_call "/desktop/vnc/start" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "VNC started"
        echo "$response" | jq '.'
    else
        error "Failed to start VNC"
    fi
}

# Stop desktop VNC
gms-rt-vnc-stop() {
    check_jq
    echo "🖥️  Stopping VNC..."
    local response=$(api_call "/desktop/vnc/stop" "POST")
    if echo "$response" | jq -e '.success' > /dev/null; then
        success "VNC stopped"
    else
        warning "Failed to stop VNC or not running"
    fi
}

# Check VNC status
gms-rt-vnc-status() {
    check_jq
    echo "🖥️  Checking VNC status..."
    api_call "/desktop/vnc/status" | jq '.'
}

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

# Stream test logs in real-time
gms-rt-stream-logs() {
    echo "📡 Streaming test logs (Ctrl+C to stop)..."
    curl -N "${API_BASE}/test/logs/stream"
}

# Download current log
gms-rt-log-download() {
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
gms-rt-latest-report() {
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
gms-rt-list-reports() {
    check_jq
    echo "📋 Listing all reports..."
    local response=$(api_call "/reports/list")
    local count=$(echo "$response" | jq '.reports | length')
    echo "Found $count report(s):"
    echo "$response" | jq -r '.reports[] | "\(.timestamp // "N/A") | \(.client_id // "N/A") | \(.test_type // "N/A") | \(.result // "N/A")"'
}

# Get report files
gms-rt-report-files() {
    local timestamp="$1"
    [ -z "$timestamp" ] && error "Timestamp required. Usage: gms-rt-report-files <TIMESTAMP>"
    check_jq
    echo "📄 Fetching report files for $timestamp..."
    api_call "/reports/files/$timestamp" | jq '.'
}

# Analyze report
gms-rt-report-analyze() {
    local timestamp="$1"
    [ -z "$timestamp" ] && error "Timestamp required. Usage: gms-rt-report-analyze <TIMESTAMP>"
    check_jq
    echo "📊 Analyzing report $timestamp..."
    api_call "/reports/analyze/$timestamp" | jq '.'
}

# ==============================================================================
# Configuration Commands
# ==============================================================================

# Get current config
gms-rt-config() {
    check_jq
    echo "⚙️  Getting current configuration..."
    api_call "/config/read" | jq '.'
}

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

# Test network connectivity
gms-rt-network-ping() {
    local test_host_ip="$1"
    local client_ip="$2"
    [ -z "$test_host_ip" ] && error "Test host IP required. Usage: gms-rt-network-ping <test_host_ip> <client_ip>"
    [ -z "$client_ip" ] && error "Client IP required. Usage: gms-rt-network-ping <test_host_ip> <client_ip>"
    check_jq
    echo "🌐 Testing network connectivity..."
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
# File Management Commands
# ==============================================================================

# Upload file
gms-rt-file-upload() {
    local file_path="$1"
    local target_path="${2:-/tmp}"
    [ -z "$file_path" ] && error "File path required. Usage: gms-rt-file-upload <file> [target_path]"
    [ ! -f "$file_path" ] && error "File not found: $file_path"
    echo "📤 Uploading file $file_path to $target_path..."
    local response=$(curl -s -F "file=@$file_path" -F "file_path=$target_path" "${API_BASE}/files/upload")
    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        success "File uploaded successfully"
        echo "$response" | jq '.'
    else
        warning "Upload completed (check server for details)"
        echo "$response"
    fi
}

# List files
gms-rt-file-list() {
    local path="${1:-/}"
    check_jq
    echo "📁 Listing files in $path..."
    local data="{\"path\":\"$path\"}"
    local response=$(api_call "/files/list" "POST" "$data")
    if echo "$response" | jq -e '.success' > /dev/null 2>/dev/null; then
        echo "$response" | jq '.'
    else
        echo "$response"
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
gms-rt-health() {
    check_jq
    echo "🏥 Checking server health..."
    api_call "/system/health" | jq '.'
}

# Show API docs URL
gms-rt-docs() {
    echo "📚 Interactive API Documentation:"
    echo "   ${GREEN}${SERVER_URL}/docs${NC}"
    echo ""
    echo "📖 API Help:"
    echo "   ${GREEN}${SERVER_URL}/api/help${NC}"
}

# ==============================================================================
# Help Function
# ==============================================================================

gms-rt-help() {
    cat << EOF
${BLUE}GMS Remote Test API Helper (FastAPI Port 5001)${NC}
========================================

${YELLOW}System:${NC}
  gms-rt-health             - Check server health
  gms-rt-docs               - Show API documentation URLs

${YELLOW}Device Management:${NC}
  gms-rt-devices            - List all connected devices
  gms-rt-devices-info "[...]" - Get device info (parallel, 75-85% faster)
  gms-rt-devices-reboot "[...]" - Reboot devices (parallel)
  gms-rt-devices-remount "[...]" - Remount RW (parallel)
  gms-rt-device-lock <id>   - Lock bootloader
  gms-rt-device-unlock <id> - Unlock bootloader

${YELLOW}Desktop VNC:${NC}
  gms-rt-vnc-start <host> [username] - Start VNC
  gms-rt-vnc-stop           - Stop VNC
  gms-rt-vnc-status         - Check VNC status
  gms-rt-desktop-validate <host> - Validate desktop host

${YELLOW}USB/IP Connection:${NC}
  gms-rt-usbip-start <user@ip> [password]
                                - Start USB/IP connection
  gms-rt-usbip-stop         - Stop USB/IP connection
  gms-rt-usbip-status       - Check USB/IP status

${YELLOW}Test Management:${NC}
  gms-rt-test-start <device> [type] [module] [case] [suite]
                            - Start a test (default: CTS, CtsPermissionTestCases)
  gms-rt-test-stop          - Stop currently running test
  gms-rt-test-status        - Check test status
  gms-rt-test-monitor       - Monitor test progress in real-time
  gms-rt-stream-logs       - Stream test logs in real-time (plain text)
  gms-rt-log-download [file] - Download current log

${YELLOW}Reports:${NC}
  gms-rt-latest-report      - Get latest test report
  gms-rt-list-reports       - List all test reports
  gms-rt-report-files <ts>  - Get report files for timestamp
  gms-rt-report-analyze <ts> - Analyze report

${YELLOW}Network & Client:${NC}
  gms-rt-client-info        - Get client IP information
  gms-rt-client-record <ip> [username] - Record client info
  gms-rt-client-detect <ip> [username] [password] - Auto-detect username
  gms-rt-network-ping <test_host> <client> - Test network connectivity

${YELLOW}Configuration:${NC}
  gms-rt-config             - Get current configuration
  gms-rt-config-update <key> <value> - Update config value
  gms-rt-config-validate    - Validate configuration

${YELLOW}VPN Management:${NC}
  gms-rt-vpn-connect        - Connect to VPN (no parameters needed)
  gms-rt-vpn-disconnect     - Disconnect VPN
  gms-rt-vpn-status         - Check VPN status

${YELLOW}File Management:${NC}
  gms-rt-file-upload <file> [target_path] - Upload file
  gms-rt-file-list [path]   - List files in path

${YELLOW}Firmware Burning:${NC}
  gms-rt-burn-firmware <firmware.img> <device_id> - Burn firmware
  gms-rt-burn-gsi <gsi.img> <device_id> - Burn GSI image
  gms-rt-burn-serial <device_id> <serial> - Burn serial number

${YELLOW}Examples:${NC}
  # List devices
  gms-rt-devices

  # Get device info for multiple devices (parallel)
  gms-rt-devices-info "[\"DEVICE-1\", \"DEVICE-2\"]"

  # Start desktop VNC
  gms-rt-vnc-start "172.16.14.233" "hcq"

  # Test network connectivity
  gms-rt-network-ping "172.16.14.233" "172.16.14.68"

  # Connect to VPN (no parameters needed)
  gms-rt-vpn-connect

  # Check VPN status
  gms-rt-vpn-status

  # Start USB/IP connection
  gms-rt-usbip-start "user@192.168.1.100" "password"

  # Start CTS test on device
  gms-rt-test-start "RK3588-DEVICE" "CTS" "CtsPermissionTestCases"

  # Monitor test progress
  gms-rt-test-monitor

  # Stream logs in real-time
  gms-rt-stream-logs

  # Get latest report
  gms-rt-latest-report

  # Update config
  gms-rt-config-update "device_host" "user@192.168.1.100"

  # Upload file
  gms-rt-file-upload "/path/to/file.txt" "/tmp"

${YELLOW}Performance Notes:${NC}
  - Multi-device operations are 75-85% faster with parallel execution
  - Use batch operations for better performance

Server: ${GREEN}$SERVER_URL${NC}
Docs:   ${GREEN}${SERVER_URL}/docs${NC}
Help:   ${GREEN}${SERVER_URL}/api/help${NC}
EOF
}

# Main command dispatcher
if [ $# -eq 0 ]; then
    gms-rt-help
else
    "$@"
fi
