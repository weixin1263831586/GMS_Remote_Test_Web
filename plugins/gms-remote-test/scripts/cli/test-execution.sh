# Fixed command domain; sourced by gms-remote-test.sh.
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
    # 流式响应必须保持裸 curl（api_call 会缓冲整个响应体）。cookie 模式
    # 下只读会话文件（-b 不带 -c）：长流期间既不持 cookie 写锁，也不回写
    # jar；网络失败由 curl 退出码原样透传，不误报权限错误。
    _gms_curl_authenticated_readonly -N "${API_BASE}/test/logs/stream"
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
