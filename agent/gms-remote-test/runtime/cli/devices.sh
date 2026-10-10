# Fixed command domain; sourced by gms-remote-test.sh.
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
    local device_id=""
    local worker_id=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                printf 'Usage: gms-rt-devices-console [port_key] [--tail 1..10000] [--date YYYYMMDD] [--device SERIAL [--worker WORKER_ID]]\n'
                return 0
                ;;
            --device)
                shift
                [ "$#" -gt 0 ] || { error "--device requires a serial"; return "$GMS_RT_EXIT_USAGE"; }
                device_id="$1"
                ;;
            --device=*) device_id="${1#*=}" ;;
            --worker)
                shift
                [ "$#" -gt 0 ] || { error "--worker requires a Worker ID"; return "$GMS_RT_EXIT_USAGE"; }
                worker_id="$1"
                ;;
            --worker=*) worker_id="${1#*=}" ;;
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
    if [ -n "$port_key" ] && [ -n "$device_id" ]; then
        error "port_key and --device are mutually exclusive"
        return "$GMS_RT_EXIT_USAGE"
    fi
    if [ -n "$worker_id" ] && [ -z "$device_id" ]; then
        error "--worker requires --device"
        return "$GMS_RT_EXIT_USAGE"
    fi

    local response
    if [ -n "$device_id" ]; then
        local availability="/devices/console/availability/$(_urlencode "$device_id")"
        [ -z "$worker_id" ] || availability="$availability?worker_id=$(_urlencode "$worker_id")"
        response=$(api_call "$availability") || return $?
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$response" | jq '.'
        else
            echo "$response" | jq -r '.data | "device=\(.device_id) worker=\(.worker_id) state=\(.state) available=\(.available) confidence=\(.confidence)\n\(.reason)"'
        fi
        return ${PIPESTATUS[1]}
    fi
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
        echo "$response" | jq -r '.data.ports[] | "\(.binding.label // .devname // .port_key)\t\(.online | if . then "online" else "offline" end)\t\(.devname // "-")\t\(.port_key)\tdevice=\(.binding.device_id // "unbound")\tcapture=\(.capture_active // false)\t\(.error // "")"'
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
    # 一次调用聚合设备诊断包——此前只有 fingerprint/activity/keyguard/owners
    # 四项且探针失败被 2>/dev/null 静默吞掉，null 无从区分 offline /
    # unauthorized / 属性本就不存在。现在扩成真正的取证包（ro.build.*/
    # ro.boot.* 属性、内核 cmdline、存储、电池），失败探针带原因进 errors[]，
    # 信封含 collected/total。复用 gms-rt-devices-shell（本地 adb / SSH 直连，
    # 同 gms_rt_shell 白名单语义之外的平台诊断路径）。
    # Snapshot probes are the documented read-only typed set;
    # export the typed-readonly marker so the shell gate allows only these
    # fixed probe commands in service-token mode.
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-snapshot DEVICE_ID"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    # 收紧后的 typed-readonly 白名单拒绝管道（元字符
    # 复核），因此 dumpsys+grep 探针改为在函数侧取全量输出、本地 grep。
    # 每条探针命令仍是固定字符串，探针命令面不因修复而扩大。
    local probe_file probe_err
    probe_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-snap.XXXXXX") || return "$GMS_RT_EXIT_OPERATION"
    probe_err=$(mktemp "${TMPDIR:-/tmp}/gms-rt-snap.XXXXXX") || { rm -f "$probe_file"; return "$GMS_RT_EXIT_OPERATION"; }
    _snapshot_probe() {
        # subshell-safe: 计数与输出经 stdout 返回，失败记录直接追加到
        # probe_file（子 shell 内的文件写入对外可见）。stderr 不再丢弃，
        # 截断后作为失败原因 —— 这是 offline/unauthorized 的唯一线索。
        local output rc probe_error
        : >"$probe_err"
        output=$(GMS_RT_TYPED_READONLY=1 gms-rt-devices-shell "$device_id" "$1" 2>"$probe_err")
        rc=$?
        if [ "$rc" -ne 0 ]; then
            probe_error=$(tr '\t\n\r' '   ' <"$probe_err" | head -c 200)
            [ -n "$probe_error" ] || probe_error="command failed (exit $rc)"
            printf '%s\t%s\t%s\n' "$1" "$rc" "$probe_error" >>"$probe_file"
            printf '%s' ""
            return 0
        fi
        # 过滤器只能是不含管道/引号的简单词列表（$2、$3 顺序应用），未给出
        # 时沿用旧的 head -3 默认。未加引号展开不会重新解析 `|`，因此不
        # 支持 "grep x | head" 形式 —— 用两个过滤器位代替。
        if [ -n "${2:-}" ]; then output=$(printf '%s\n' "$output" | $2); fi
        if [ -n "${3:-}" ]; then output=$(printf '%s\n' "$output" | $3); fi
        if [ -z "${2:-}" ] && [ -z "${3:-}" ]; then
            output=$(printf '%s\n' "$output" | head -3)
        fi
        printf '%s' "$output"
    }
    local prop_fp activity keyguard owners build_props boot_props cmdline storage battery
    prop_fp=$(_snapshot_probe "getprop ro.build.fingerprint")
    activity=$(_snapshot_probe "dumpsys activity activities" "grep -m1 topResumedActivity")
    keyguard=$(_snapshot_probe "dumpsys window" "grep -m1 mDreamingLockscreen")
    owners=$(_snapshot_probe "dpm list-owners")
    build_props=$(_snapshot_probe "getprop" "grep -F [ro.build." "head -30")
    boot_props=$(_snapshot_probe "getprop" "grep -F [ro.boot." "head -50")
    cmdline=$(_snapshot_probe "cat /proc/cmdline")
    storage=$(_snapshot_probe "df -h / /data" "head -5")
    battery=$(_snapshot_probe "dumpsys battery" "head -20")
    rm -f "$probe_err"
    # Keep in sync with the probe list above (9 probes).
    local total=9 collected=0 value
    for value in "$prop_fp" "$activity" "$keyguard" "$owners" \
        "$build_props" "$boot_props" "$cmdline" "$storage" "$battery"; do
        [ -n "$value" ] && collected=$((collected + 1))
    done
    local errors_json
    errors_json=$(jq -Rn '
        [inputs
        | select(length > 0)
        | split("\t")
        | {probe: .[0], exit_code: ((.[1] // "0") | tonumber), error: (.[2] // "command failed")}]' \
        <"$probe_file")
    rm -f "$probe_file"
    jq -n \
        --arg device "$device_id" \
        --argjson collected "$collected" \
        --argjson total "$total" \
        --argjson errors "$errors_json" \
        --arg fingerprint "$prop_fp" \
        --arg activity "$activity" \
        --arg keyguard "$keyguard" \
        --arg owners "$owners" \
        --arg build_props "$build_props" \
        --arg boot_props "$boot_props" \
        --arg cmdline "$cmdline" \
        --arg storage "$storage" \
        --arg battery "$battery" \
        '{
            device: $device,
            collected: $collected,
            total: $total,
            errors: $errors,
            fingerprint: ($fingerprint | if length > 0 then . else null end),
            focused_activity: ($activity | if length > 0 then . else null end),
            lockscreen: ($keyguard | if length > 0 then . else null end),
            device_owners: ($owners | if length > 0 then . else null end),
            build_props: ($build_props | if length > 0 then . else null end),
            boot_props: ($boot_props | if length > 0 then . else null end),
            kernel_cmdline: ($cmdline | if length > 0 then . else null end),
            storage: ($storage | if length > 0 then . else null end),
            battery: ($battery | if length > 0 then . else null end)
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
        consume_response=$(api_call "/auth/approval-tokens/consume" POST "$consume_data")
        local consume_status=$?
        unset consume_data approval_token
        if [ "$consume_status" -ne 0 ] || \
           ! echo "$consume_response" | jq -e '.success == true' >/dev/null 2>&1; then
            error "审批令牌校验失败: $(extract_api_error "$consume_response")"
            [ "$consume_status" -eq 0 ] || return "$consume_status"
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
        # 与 MCP _validate_shell_command 对齐：允许且仅允许一个受限管道
        # "readonly | grep|wc|head|tail ..."。切分后两侧分别过同一套
        # 只读白名单与元字符检查（管道符本身不进入任一侧），修复
        # "MCP 放行、CLI 拒绝" 的工具契约漂移。
        local _ro_check="$shell_command"
        local _ro_pipe_tail=""
        if [[ "$shell_command" == *"|"* ]]; then
            case "$shell_command" in
                *\|*\|*)
                    error "只读路径至多允许一个管道"
                    return "$GMS_RT_EXIT_PERMISSION"
                    ;;
            esac
            _ro_check="${shell_command%%|*}"
            _ro_pipe_tail="${shell_command#*|}"
            # 两侧去首尾空白（对应 MCP 切分后的 segment.strip()）。
            _ro_check="${_ro_check#"${_ro_check%%[![:space:]]*}"}"
            _ro_check="${_ro_check%"${_ro_check##*[![:space:]]}"}"
            _ro_pipe_tail="${_ro_pipe_tail#"${_ro_pipe_tail%%[![:space:]]*}"}"
            _ro_pipe_tail="${_ro_pipe_tail%"${_ro_pipe_tail##*[![:space:]]}"}"
            if [ -z "$_ro_check" ] || [ -z "$_ro_pipe_tail" ]; then
                error "空管道段（'||' 链式被拒绝）"
                return "$GMS_RT_EXIT_PERMISSION"
            fi
            # 管道尾段仅允许过滤类二进制（MCP _SHELL_FILTER_BINARIES）；
            # grep/wc/head/tail 本身已在下方无条件只读白名单内。
            local _ro_tail_first=${_ro_pipe_tail%%[[:space:]]*}
            case "$_ro_tail_first" in
                grep|wc|head|tail) ;;
                *) error "管道尾段仅允许 grep/wc/head/tail: '$_ro_tail_first'"; return "$GMS_RT_EXIT_PERMISSION" ;;
            esac
        fi
        # Typed-readonly allowlist (mirror of the MCP adapter's structured
        # allowlist). Binaries with mutating subcommands (settings/cmd/am/
        # pm/dpm/content/device_config/wm/logcat/dmesg/dumpsys) are verified
        # per-subcommand below — the first token alone is NOT sufficient
        # (a forged marker previously let `settings put` through when only
        # the leading binary was checked).
        local _ro_first
        _ro_first=${_ro_check%% *}
        # Binaries whose read-only surface is unconditional.
        case "$_ro_first" in
            getprop|ls|cat|ps|pidof|stat|uptime|vmstat|df|id|printenv|grep|head|tail|wc|pgrep) ;;
            settings)
                case "$_ro_check" in
                    "settings get "*) ;;
                    *) error "Service-token 只读白名单仅允许 'settings get'"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            wm)
                case "$_ro_check" in
                    "wm size"|"wm density") ;;
                    *) error "Service-token 只读白名单仅允许 'wm size'/'wm density'"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            dumpsys)
                # Must stay in lockstep with _SHELL_DUMPSYS_MUTATING in
                # runtime/mcp_server.py (17 words). Any word added there
                # MUST be added here too — this gate is the CLI mirror of
                # the MCP typed-readonly allowlist.
                case " $_ro_check " in
                    *" unplug "*|*" reset "*|*" disable "*|*" enable "*|*" kill "*|*" force-stop "*|*" set "*|\
                    *" whitelist "*|*" set-debug-app "*|*" suspend "*|*" resume "*|*" reset-role "*|\
                    *" plug "*|*" charge "*|*" nocharge "*|*" persist "*|*" import "*)
                        error "dumpsys 参数可能改变设备状态，需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            logcat)
                case "$_ro_check" in
                    *-c*|*" -f"*) error "logcat -c/-f 属破坏性参数，需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            dmesg)
                case "$_ro_check" in
                    *-c*|*-C*) error "dmesg -c/-C 清空内核环形缓冲，需一次性审批令牌"; return "$GMS_RT_EXIT_PERMISSION" ;;
                esac ;;
            device_config)
                case "$_ro_check" in
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
                case "$_ro_check" in
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
        # Shell metacharacters — per-segment mirror of the MCP gate: the
        # command is split on at most one '|' above, then each side must
        # pass the same forbidden set as _SHELL_FORBIDDEN_CHARS in
        # runtime/mcp_server.py. The command string is finally parsed by the
        # device-side `sh` (adb shell), so quote/glob/backslash/whitespace
        # metachars can smuggle a second command just like `;` does.
        local _ro_segment
        for _ro_segment in "$_ro_check" "$_ro_pipe_tail"; do
            [ -n "$_ro_segment" ] || continue
            case "$_ro_segment" in
                *[\\\"\;\|\&\>\<\`\$\(\)\{\}\[\]\'\*\?]*)
                    error "Service-token 只读路径禁止 shell 元字符: $_ro_segment"
                    return "$GMS_RT_EXIT_PERMISSION"
                    ;;
            esac
            if [[ "$_ro_segment" == *[$'\t\r\n']* ]]; then
                error "Service-token 只读路径禁止制表符/换行符: $_ro_segment"
                return "$GMS_RT_EXIT_PERMISSION"
            fi
        done
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

# Read-only device diagnosis front door for agents (ADR-free local gap:
# MCP exposes gms_rt_shell, but the bare CLI previously had no equivalent
# unattended path). Delegates to gms-rt-devices-shell with the typed-
# readonly marker, so the SAME fixed allowlist gate applies — the marker
# alone never widens the command surface. Each invocation appends a local
# audit line (best-effort: audit failures never block diagnosis).
_gms_rt_diag_audit() {
    local audit_dir="${XDG_STATE_HOME:-${HOME}/.local/state}/gms-remote-test"
    local audit_file="$audit_dir/diag-audit.log"
    mkdir -p "$audit_dir" 2>/dev/null || return 0
    local safe_command
    safe_command=$(printf '%s' "$2" | tr '\t\n' '  ')
    {
        flock 9
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "${GMS_RT_PROFILE:-default}" "$1" "$safe_command" "$3" >&9
    } 9>>"$audit_file" 2>/dev/null || return 0
}

gms-rt-devices-diag() {
    local device_id="$1"
    [ -z "$device_id" ] && { error "设备ID必填. 用法: gms-rt-devices-diag DEVICE_ID COMMAND"; return "$GMS_RT_EXIT_USAGE"; }
    shift
    [ "$#" -gt 0 ] || { error "缺少诊断命令. 用法: gms-rt-devices-diag DEVICE_ID 'getprop ro.build.fingerprint'"; return "$GMS_RT_EXIT_USAGE"; }
    local diag_command="$*"
    _gms_rt_diag_audit "$device_id" "$diag_command" "start"
    local status=0
    # Marker name MUST match what the gate inside gms-rt-devices-shell
    # actually reads (GMS_RT_TYPED_READONLY — same name the MCP adapter
    # passes via env_extra). A leading-underscore "private" variant is a
    # different variable and silently unlocks nothing; that mismatch used
    # to leave the snapshot probes denied-and-nulled in service-token mode.
    # The marker routes to the fixed read-only allowlist only — it never
    # bypasses the approval path for mutating commands.
    GMS_RT_TYPED_READONLY=1 gms-rt-devices-shell "$device_id" "$diag_command" || status=$?
    _gms_rt_diag_audit "$device_id" "$diag_command" "exit=$status"
    return "$status"
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

    # 临时文件名不含用户输入：远端 sh 会解释拼接后的命令，旧实现把本地文件名
    # 拼进 scp/ssh 远端命令，含引号的文件名可注入测试主机命令。
    # $$ 在 source（函数模式）下对所有调用相同，追加 $RANDOM 供并发去重。
    local tmp_remote="/tmp/gms-rt-push-$$-$RANDOM"

    echo "📤 Step 1/2: Transferring $filename to test host..."
    scp -P "$port" "$local_path" "$user@$host:$tmp_remote" || { error "文件传输失败"; return 1; }

    echo "📤 Step 2/2: Pushing to device $device_id:$remote_path (via $user@$host)..."
    local push_result=0
    local quoted_device quoted_tmp quoted_remote_path
    quoted_device=$(_shell_quote "$device_id")
    quoted_tmp=$(_shell_quote "$tmp_remote")
    quoted_remote_path=$(_shell_quote "$remote_path")
    ssh -p "$port" "$user@$host" "adb -s ${quoted_device} push ${quoted_tmp} ${quoted_remote_path}" || push_result=$?
    ssh -p "$port" "$user@$host" "rm -f ${quoted_tmp}" 2>/dev/null
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
