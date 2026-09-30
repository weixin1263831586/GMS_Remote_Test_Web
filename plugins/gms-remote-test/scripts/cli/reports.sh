# Fixed command domain; sourced by gms-remote-test.sh.
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
