# Fixed system command domain; sourced by gms-remote-test.sh.
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
    local encoded_skill target temporary temp_dir exit_code archive_magic
    if [[ ! "$skill_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
            || [ "$skill_name" = "." ] || [ "$skill_name" = ".." ]; then
        error "Invalid skill name: use 1-128 letters, digits, dot, underscore, or hyphen"
        return "$GMS_RT_EXIT_USAGE"
    fi
    encoded_skill=$(_urlencode "$skill_name")
    target="${skill_name}-skills.zip"
    temp_dir=$(mktemp -d "./.gms-skills.XXXXXX") || {
        error "Failed to create private download directory"
        return "$GMS_RT_EXIT_OPERATION"
    }
    chmod 700 "$temp_dir"
    temporary="$temp_dir/archive.zip"
    echo "📁 Downloading skills directory as ZIP..."
    echo "URL: ${API_BASE}/system/skills?skill_name=${encoded_skill}"
    echo "Saving to: ${target}"
    # 走 api_call：Bearer 模式下 CURL_AUTH_ARGS 被清空，裸 curl 只传
    # AUTH 参数必 401；api_call 同时给出 000→网络错误码与 cookie 锁。
    api_call "/system/skills?skill_name=${encoded_skill}" GET "" \
        -o "$temporary" >/dev/null
    exit_code=$?
    if [ "$exit_code" -eq 0 ]; then
        archive_magic=$(LC_ALL=C od -An -N2 -tx1 "$temporary" 2>/dev/null | tr -d '[:space:]')
        if [ ! -s "$temporary" ] || [ "$archive_magic" != "504b" ]; then
            rm -rf -- "$temp_dir"
            error "Downloaded skills payload is not a ZIP archive"
            return "$GMS_RT_EXIT_OPERATION"
        fi
        if ! mv -f -- "$temporary" "$target"; then
            rm -rf -- "$temp_dir"
            error "Failed to atomically replace $target"
            return "$GMS_RT_EXIT_OPERATION"
        fi
        rmdir -- "$temp_dir"
        success "Skills ZIP downloaded successfully"
        ls -lh "$target"
    else
        rm -rf -- "$temp_dir"
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
    gms_agent="$_gms_runtime_dir/gms-agent"
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

            local api_response
            api_response=$(api_call "/terminal/open" 2>/dev/null)
            local api_status=$?
            if [ "$api_status" -ne 0 ] || [ -z "$api_response" ]; then
                error "Failed to connect to API server at ${SERVER_URL}"
                echo ""
                echo "💡 Troubleshooting:"
                echo "   1. Check if the API server is running: systemctl status gms-web-app"
                echo "   2. Verify server URL: echo \$GMS_REMOTE_TEST_SERVER"
                echo "   3. Test connection with the configured CA/TLS settings: gms-rt-terminal-open"
                return "$api_status"
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
# Agent-facing environment self-check
# ==============================================================================

# One-shot read-only environment report for agents: credential mode, identity,
# server health, device inventory and locally visible test suites. Every
# section degrades gracefully; hints carry actionable next steps so an agent
# can recover (e.g. re-enroll) without a human walkthrough.
gms-rt-system-selfcheck() {
    check_jq || return 1
    local auth_json health_json devices_json console_json hints_json suites_json
    local auth_ok=false health_ok=false devices_ok=false console_ok=false
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
        hints+=("server ${SERVER_URL} unreachable or unhealthy: verify the web app is running and select its installed profile with GMS_RT_PROFILE=<PROFILE>; otherwise configure GMS_CURL_CA_CERT=/path/to/controller-ca.pem")
    fi

    # --- device inventory (optional scope) --------------------------------------
    devices_ok=true
    devices_json=$(api_call "/devices/list" 2>/dev/null)
    _gms_rt_selfcheck_is_json "$devices_json" || { devices_ok=false; devices_json='[]'; }
    if [ "$devices_ok" != true ]; then
        hints+=("device inventory unavailable: the current credential may lack the devices.read scope")
    fi

    # --- Controller-local serial evidence ---------------------------------------
    console_ok=true
    console_json=$(api_call "/devices/console/ports" 2>/dev/null)
    _gms_rt_selfcheck_is_json "$console_json" || { console_ok=false; console_json='{}'; }
    if [ "$console_ok" != true ]; then
        hints+=("serial console inventory unavailable: verify devices.read scope and Controller pyudev/dialout readiness")
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
        --argjson serial_console "$console_json" \
        --argjson suites "$suites_json" \
        --argjson hints "$hints_json" \
        --argjson auth_ok "$auth_ok" \
        --argjson health_ok "$health_ok" \
        --argjson devices_ok "$devices_ok" \
        --argjson serial_console_ok "$console_ok" \
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
            serial_console: (if $serial_console_ok
                then {ok: true, ports: ($serial_console.data.ports // [])}
                else {ok: false} end),
            local_suites: $suites,
            hints: $hints
        }'
}
