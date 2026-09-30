# Fixed command domain; sourced by gms-remote-test.sh.
# ==============================================================================
# Redmine Evidence Commands (read-only evidence chain)
# ==============================================================================

gms-rt-redmine-issue-fetch() {
    local issue_ref=""
    local download="all"
    local refresh=1
    local do_wait=0
    local dry_run=0
    local max_wait=300
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-redmine-issue-fetch <issue_id_or_url> [--download none|analyzable|all] [--refresh|--no-refresh] [--wait] [--max-wait SECONDS] [--dry-run]"
                echo "  Create/refresh a full evidence snapshot (raw JSON, journals, attachments)."
                echo "  --refresh is the default; --no-refresh may reuse a recent ready snapshot (response carries cache_hit)."
                echo "  --dry-run validates the issue reference, read access, base_url, and credentials without creating a snapshot."
                return 0
                ;;
            --download)
                shift
                [ $# -gt 0 ] || { error "--download requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    none|analyzable|all) download="$1" ;;
                    *) error "--download accepts none, analyzable, or all"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            --no-refresh) refresh=0 ;;
            --refresh) refresh=1 ;;
            --suite-path)
                shift
                [ $# -gt 0 ] || { error "--suite-path requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                suite_path="$1"
                ;;
            --wait) do_wait=1 ;;
            --dry-run) dry_run=1 ;;
            --max-wait)
                shift
                [ $# -gt 0 ] || { error "--max-wait requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                max_wait="$1"
                ;;
            *)
                [ -z "$issue_ref" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                issue_ref="$1"
                ;;
        esac
        shift
    done
    [ -z "$issue_ref" ] && { error "Issue ID or URL required. Usage: gms-rt-redmine-issue-fetch <issue_id_or_url> [options]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local payload response snapshot_id
    payload=$(jq -cn --argjson refresh "$refresh" --arg download "$download" \
        --argjson dry_run "$dry_run" \
        '{refresh: $refresh, download: $download, dry_run: $dry_run}')
    response=$(api_call "/redmine-agent/issues/$(_urlencode "$issue_ref")/evidence" "POST" "$payload")
    local call_status=$?
    if [ "$call_status" -ne 0 ]; then
        # api_call 已把服务器错误 body 打到 stdout；这里给出人话根因
        #（scope/凭据/issue 不存在），不再让错误详情被 jq 过滤吞掉。
        error "Evidence snapshot creation failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    if [ "$dry_run" = "1" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$response" | jq '.'
        else
            success "Preconditions met: issue reference, Redmine base_url, and credentials are valid."
        fi
        return 0
    fi
    snapshot_id=$(echo "$response" | jq -r '.data.snapshot_id // empty')
    if [ -z "$snapshot_id" ]; then
        error "Failed to create evidence snapshot for '$issue_ref'"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ "$do_wait" != "1" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            jq -cn --arg snapshot_id "$snapshot_id" \
                '{success: true, snapshot_id: $snapshot_id, status: "queued", poll: "gms-rt-redmine-issue-show '$snapshot_id'"}'
        else
            success "Evidence snapshot queued: $snapshot_id"
            echo "Poll with: gms-rt-redmine-issue-show $snapshot_id"
        fi
        return 0
    fi
    local waited=0 interval=3
    while true; do
        local status_resp status
        status_resp=$(api_call "/redmine-agent/evidence/$snapshot_id" "GET")
        status=$(echo "$status_resp" | jq -r '.data.status // empty')
        case "$status" in
            ready|partial|failed) break ;;
        esac
        [ -z "$status" ] && { error "Failed to read snapshot status"; return "$GMS_RT_EXIT_OPERATION"; }
        [ "$waited" -ge "$max_wait" ] && break
        sleep "$interval"
        waited=$((waited + interval))
    done
    gms-rt-redmine-issue-show "$snapshot_id"
}

gms-rt-redmine-issue-show() {
    local snapshot_id="$1"
    [ -z "$snapshot_id" ] && { error "Snapshot ID or issue ID required. Usage: gms-rt-redmine-issue-show <snapshot_id | issue_id> (--issue forces issue-id interpretation)"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local force_issue=0
    if [ "$2" = "--issue" ] || [ "$1" = "--issue" ]; then
        # 显式声明第一个参数是 issue_id。
        if [ "$1" = "--issue" ]; then
            snapshot_id="$2"
        fi
        force_issue=1
    fi
    # 第一个参数历史上必须是 snapshot_id，传 issue_id
    # 会得到费解的 "Snapshot not readable"。约定：ev_ 前缀或含连字符视为
    # snapshot_id；纯数字视为 issue_id 并解析最新快照；--issue/--snapshot
    # 可显式消歧。
    case "$snapshot_id" in
        --snapshot)
            snapshot_id="$2"
            ;;
        --issue)
            snapshot_id="$2"
            force_issue=1
            ;;
    esac
    if [ "$force_issue" = "1" ] || [[ "$snapshot_id" =~ ^[0-9]+$ ]]; then
        local latest_resp resolved status_line
        latest_resp=$(api_call "/redmine-agent/issues/$(_urlencode "$snapshot_id")/evidence/latest" "GET")
        local latest_status=$?
        if [ "$latest_status" -ne 0 ]; then
            error "$(extract_api_error "$latest_resp")"
            return "$latest_status"
        fi
        status_line=$(echo "$latest_resp" | jq -r '.data.status // ""')
        if [ "$status_line" != "ready" ]; then
            warning "latest snapshot for issue $snapshot_id is '$status_line' (not ready); showing it anyway"
        fi
        snapshot_id=$(echo "$latest_resp" | jq -r '.data.snapshot_id // empty')
        [ -z "$snapshot_id" ] && { error "Failed to resolve latest snapshot"; return "$GMS_RT_EXIT_OPERATION"; }
    fi
    local status_resp description_resp
    status_resp=$(api_call "/redmine-agent/evidence/$snapshot_id" "GET")
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        description_resp=$(api_call "/redmine-agent/evidence/$snapshot_id/issue" "GET")
        # Combine: status payload + description head
        echo "$status_resp" | jq --argjson desc "$(echo "$description_resp" | jq -c '.data // {}')" \
            '.data + {description: ($desc.description // {text: ""})}'
        return $?
    fi
    if echo "$status_resp" | jq -e '.success' > /dev/null; then
        echo "$status_resp" | jq -r '.data | "snapshot: \(.snapshot_id)\nissue: \(.issue_id)\nstatus: \(.status) complete=\(.complete)\njournals: \(.journal_count)  attachments: \(.attachment_count)/\(.downloaded_count) downloaded\nfetched_at: \(.fetched_at)  source_updated: \(.source_updated_on)\nsha256: \(.content_sha256)"'
        # failed/partial 快照必须把服务端 errors[] 透传出来（MCP
        # 可见而 CLI 不可见会导致排障绕路）。
        if echo "$status_resp" | jq -e '.data.status == "failed" or .data.status == "partial" or ((.data.errors // []) | length > 0)' >/dev/null; then
            echo "errors:"
            echo "$status_resp" | jq -r '.data.errors[]? | "  [\(.stage // "issue")] \(.message // .)"'
        fi
        description_resp=$(api_call "/redmine-agent/evidence/$snapshot_id/issue" "GET")
        echo "$description_resp" | jq -r '.data | "\nsubject: \(.subject)\nstatus: \(.status)  tracker: \(.tracker)\n\n--- description (first 2000 chars, use gms-rt-artifact-read for more) ---\n\(.description.text[:2000])"'
    else
        error "Snapshot not readable: $(extract_api_error "$status_resp")"
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

gms-rt-redmine-journals() {
    local snapshot_id="$1"
    local limit=50
    local cursor=""
    local argument
    shift 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            --cursor)
                shift
                [ $# -gt 0 ] || { error "--cursor requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                cursor="$1"
                ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$snapshot_id" ] && { error "Snapshot ID required. Usage: gms-rt-redmine-journals <snapshot_id> [--limit N] [--cursor C]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/redmine-agent/evidence/$snapshot_id/journals?limit=$limit"
    [ -n "$cursor" ] && url="$url&cursor=$(_urlencode "$cursor")"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "total: \(.total) returned: \(.returned) next_cursor: \(.next_cursor // "-")", (.journals[] | "[journal:\(.id)] \(.user_name) @ \(.created_on)\n\(.notes)\n---")'
}

gms-rt-redmine-attachments() {
    local snapshot_id="$1"
    [ -z "$snapshot_id" ] && { error "Snapshot ID required. Usage: gms-rt-redmine-attachments <snapshot_id>"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/redmine-agent/evidence/$snapshot_id/artifacts" "GET" | jq '.'
        return $?
    fi
    api_call "/redmine-agent/evidence/$snapshot_id/artifacts" "GET" | jq -r '.data | "total: \(.total)", (.artifacts[] | "[\(.artifact_id)] attachment:\(.attachment_id) \(.original_filename) kind=\(.kind) status=\(.status) size=\(.size_bytes) sha256=\(.sha256[0:16])\(.error // "" | if . != "" then "  error: \(.)" else "" end)")'
}

gms-rt-redmine-attachment-download() {
    local artifact_id="$1"
    local output="${2:-}"
    [ -z "$artifact_id" ] && { error "Artifact ID required. Usage: gms-rt-redmine-attachment-download <artifact_id> [output_path]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    [ -n "$output" ] || output="gms-evidence-$artifact_id.bin"
    local curl_status status_file tmp_output
    status_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-dl-status.XXXXXX") || return "$GMS_RT_EXIT_OPERATION"
    tmp_output=$(mktemp "${TMPDIR:-/tmp}/gms-rt-dl-body.XXXXXX") || { rm -f -- "$status_file"; return "$GMS_RT_EXIT_OPERATION"; }
    _refresh_tls_args
    _ensure_auth_cookie_jar || { rm -f -- "$status_file" "$tmp_output"; return "$GMS_RT_EXIT_OPERATION"; }
    # 先落临时文件：非 200 时响应体是 JSON 错误（如 scope_required），
    # 不能写进目标输出再整文件删除（否则错误详情会丢失）。
    curl "${CURL_TLS_ARGS[@]}" "${CURL_BEARER_ARGS[@]}" "${CURL_AUTH_ARGS[@]}" -sS \
        -o "$tmp_output" -w '%{http_code}' --max-time "$CURL_TIMEOUT" \
        "${API_BASE}/redmine-agent/artifacts/$artifact_id/download" > "$status_file"
    curl_status=$(cat "$status_file" 2>/dev/null)
    rm -f -- "$status_file"
    case "$curl_status" in
        200)
            mv -f -- "$tmp_output" "$output" || {
                rm -f -- "$tmp_output"
                error "Failed to write $output"
                return "$GMS_RT_EXIT_OPERATION"
            }
            ;;
        000|'')
            rm -f -- "$tmp_output"
            error "Download failed (network error)"
            return "$GMS_RT_EXIT_NETWORK"
            ;;
        *)
            local error_body exit_code
            error_body=$(cat "$tmp_output" 2>/dev/null || true)
            rm -f -- "$tmp_output"
            exit_code=$(_http_exit_code "$curl_status")
            error "Download failed (HTTP $curl_status): $(extract_api_error "$error_body")"
            _record_api_exit_code "$exit_code"
            return "$exit_code"
            ;;
    esac
    local size sha
    size=$(stat -c '%s' "$output" 2>/dev/null || echo 0)
    sha=$(sha256sum "$output" 2>/dev/null | awk '{print $1}' || echo "")
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg artifact_id "$artifact_id" --arg path "$output" \
            --arg size "$size" --arg sha256 "$sha" \
            '{success: true, artifact_id: $artifact_id, saved_path: $path, size_bytes: ($size | tonumber), sha256: $sha256}'
    else
        success "Saved $output ($size bytes)"
        [ -n "$sha" ] && echo "sha256: $sha"
    fi
}

# Pre-flight credential check: reports whether the
# owner account behind the current credential has Redmine credentials
# configured. Equivalent to GET /redmine-agent/config/credentials; never
# returns secret material.
# 当天待处理 triage（waiting_my_reply / no_reply_3_days 去重）。
# 只读：数据来自个人看板 workload 统计，本命令不做任何业务判断。
gms-rt-redmine-triage() {
    local stale_days=""
    local list_limit=""
    local refresh=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-redmine-triage [--stale-days N] [--list-limit N] [--refresh]"
                echo "  List today's pending Redmine issues (waiting_my_reply + no_reply_3_days, deduped)."
                echo "  Read-only; source of truth is the personal dashboard workload statistics."
                return 0
                ;;
            --stale-days)
                shift
                [ $# -gt 0 ] || { error "--stale-days requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                stale_days="$1"
                ;;
            --list-limit)
                shift
                [ $# -gt 0 ] || { error "--list-limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                list_limit="$1"
                ;;
            --refresh) refresh=1 ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    check_jq || return 1

    local query=""
    [ -n "$stale_days" ] && query="${query}&stale_days=${stale_days}"
    [ -n "$list_limit" ] && query="${query}&list_limit=${list_limit}"
    [ "$refresh" = "1" ] && query="${query}&refresh=true"
    [ -n "$query" ] && query="?${query#&}"

    local response call_status
    response=$(api_call "/redmine-agent/daily-brief/triage${query}" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Redmine triage failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    local configured
    configured=$(echo "$response" | jq -r '.data.configured // true')
    if [ "$configured" = "false" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            echo "$response" | jq '.'
        else
            error "Redmine credentials not configured for this owner account."
            warning "Ask the enrolling account owner to configure them in the Web UI settings page."
        fi
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.data'
    else
        echo "$response" | jq -r '"generated_at: \(.data.generated_at)  snapshot: \(.data.snapshot_hash[0:12])",
            "waiting_my_reply: \(.data.counts.waiting_my_reply)  no_reply_3_days: \(.data.counts.no_reply_3_days)  total: \(.data.counts.total)"'
        echo "$response" | jq -r '.data.issues[] |
            "\(.priority)  #\(.issue_id)  [\((.buckets // []) | join(","))]  unreplied=\(.unreplied_days // 0)  \(.subject)"'
    fi
    return 0
}

gms-rt-redmine-history-search() {
    local query=""
    local limit=""
    local exclude_issue_id=""
    local resolved_only=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-redmine-history-search <query> [--limit N] [--exclude-issue-id N] [--resolved-only]"
                echo "  Search ALL historical Redmine issues (local archive + Redmine site search) for"
                echo "  same/similar problems and reusable fixes. Read-only, scoped to the owner account."
                return 0
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            --exclude-issue-id)
                shift
                [ $# -gt 0 ] || { error "--exclude-issue-id requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                exclude_issue_id="$1"
                ;;
            --resolved-only) resolved_only=1 ;;
            -*)
                # Allow the query to start with a dash (rare) only via explicit
                # value forms; otherwise reject unknown options.
                error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
            *)
                [ -z "$query" ] && query="$1" || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                ;;
        esac
        shift
    done
    [ -n "$query" ] || { error "Query required. Usage: gms-rt-redmine-history-search <query> [options]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq || return 1

    local qs="?q=$(_urlencode "$query")"
    [ -n "$limit" ] && qs="${qs}&limit=${limit}"
    [ -n "$exclude_issue_id" ] && qs="${qs}&exclude_issue_id=${exclude_issue_id}"
    [ "$resolved_only" = "1" ] && qs="${qs}&resolved_only=true"

    local response call_status
    response=$(api_call "/redmine-agent/history/search${qs}" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Redmine history search failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.data'
    else
        echo "$response" | jq -r '.data.items[] |
            "\(.is_resolved == true | if . then "[已解决]" else "[未解决]" end)  #\(.issue_id)  [\(.source // "local_db")]  \(.subject)"' \
            2>/dev/null || echo "$response" | jq '.data'
        echo "$response" | jq -r '.data.items[] | select(.solution != "" and .solution != null) |
            "  #\(.issue_id) fix: \(.solution)"' 2>/dev/null || true
    fi
    return 0
}

gms-rt-redmine-credentials-status() {
    check_jq || return 1
    local response call_status
    response=$(api_call "/redmine-agent/config/credentials" "GET")
    call_status=$?
    if [ "$call_status" -ne 0 ]; then
        error "Credentials status check failed: $(extract_api_error "$response")"
        return "$call_status"
    fi
    local configured username api_key_configured
    configured=$(echo "$response" | jq -r '.data.configured // false')
    username=$(echo "$response" | jq -r '.data.username // ""')
    api_key_configured=$(echo "$response" | jq -r '.data.api_key_configured // false')
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg configured "$configured" --arg username "$username" \
            --arg api_key_configured "$api_key_configured" \
            '{configured: ($configured == "true"), username: $username, api_key_configured: ($api_key_configured == "true")}'
    else
        echo "configured: $configured"
        [ -n "$username" ] && echo "username:  $username"
        echo "api_key:   $api_key_configured"
    fi
    if [ "$configured" != "true" ]; then
        warning "Owner account has no Redmine credentials; evidence fetch will fail."
        warning "Ask the enrolling account owner to configure them in the Web UI settings page"
        warning "(agent shares the owner storage; POST /config/credentials is human-only)."
        return "$GMS_RT_EXIT_PERMISSION"
    fi
    return 0
}

gms-rt-artifact-read() {
    local artifact_id="$1"
    local offset=0
    local limit=65536
    shift 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --offset)
                shift
                [ $# -gt 0 ] || { error "--offset requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                offset="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$artifact_id" ] && { error "Artifact ID required. Usage: gms-rt-artifact-read <artifact_id> [--offset N] [--limit N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/redmine-agent/artifacts/$artifact_id/text?offset=$offset&limit=$limit" "GET" | jq '.'
        return $?
    fi
    api_call "/redmine-agent/artifacts/$artifact_id/text?offset=$offset&limit=$limit" "GET" | jq -r '.data | "chars \(.offset)+\(.returned_chars)/\(.total_chars)\(if .truncated then " (truncated; use --offset)" else "" end)\n\n\(.text)"'
}

gms-rt-redmine-artifact-image() {
    local artifact_id="$1"
    [ -z "$artifact_id" ] && { error "Artifact ID required. Usage: gms-rt-redmine-artifact-image <artifact_id>"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    # 供 MCP image tool 使用：返回 base64 + 元数据；人类终端请用 download 命令。
    api_call "/redmine-agent/artifacts/$artifact_id/image" "GET" | jq '.'
}

gms-rt-artifact-search() {
    local snapshot_id=""
    local query=""
    local limit=50
    local positional=()
    # 契约：<snapshot_id> --query QUERY；同时兼容两个位置参数。
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --query)
                shift
                [ $# -gt 0 ] || { error "--query requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                query="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    if [ -z "$snapshot_id" ] && [ "${#positional[@]}" -ge 1 ]; then
        snapshot_id="${positional[0]}"
    fi
    if [ -z "$query" ] && [ "${#positional[@]}" -ge 2 ]; then
        query="${positional[1]}"
    fi
    [ -z "$snapshot_id" ] || [ -z "$query" ] && { error "Usage: gms-rt-artifact-search <snapshot_id> <query|--query QUERY> [--limit N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/redmine-agent/evidence/$snapshot_id/search?q=$(_urlencode "$query")&limit=$limit"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "query: \(.query) matches: \(.total)\(if .limited then " (limited)" else "" end)", (.matches[] | "[\(.kind)\(.journal_id // .artifact_id // "")] \(.snippet)")'
}

gms-rt-apk-analyze-attachment() {
    local snapshot_id="$1"
    local artifact_id="$2"
    shift 2 2>/dev/null || true
    [ -z "$snapshot_id" ] || [ -z "$artifact_id" ] && { error "Usage: gms-rt-apk-analyze-attachment <snapshot_id> <artifact_id>"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    # artifact_id 已是全局唯一 opaque ID；snapshot_id 仅用于人工核对。
    local response
    response=$(api_call "/redmine-agent/artifacts/$artifact_id/apk-analysis" "POST")
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        echo "$response" | jq '.'
        return $?
    fi
    if echo "$response" | jq -e '.success' > /dev/null; then
        echo "$response" | jq -r '.data | "task: \(.task_id)\nstatus: \(.status)\nsource: issue \(.source_ref.issue_id) sha256=\(.source_ref.sha256[0:16])"'
        echo "Poll with: gms-rt-apk-status $(echo "$response" | jq -r '.data.task_id')"
    else
        error "Failed to import artifact into APK analysis"
        echo "$response" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
}

gms-rt-apk-source-read() {
    local task_id="$1"
    local path="$2"
    local offset=0
    local limit=400
    shift 2 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --offset)
                shift
                [ $# -gt 0 ] || { error "--offset requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                offset="$1"
                ;;
            --limit)
                shift
                [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                limit="$1"
                ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$task_id" ] || [ -z "$path" ] && { error "Usage: gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/apk/source-read/$task_id?path=$(_urlencode "$path")&offset=$offset&limit=$limit"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "lines \(.offset)+\(.returned_lines)/\(.total_lines)\(if .truncated then " (truncated)" else "" end)", (.lines[] | "\(.line)\t\(.text)")'
}

gms-rt-sdk-sources() {
    check_jq
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/sdk/sources" "GET" | jq '.'
        return $?
    fi
    api_call "/sdk/sources" "GET" | jq -r '.data.sources[] | "\(.source_id) (provider=\(.provider), default_revision=\(.default_revision // "-"))"'
}

gms-rt-sdk-search() {
    local source="" revision="" query=""
    local limit=50 path_filter=""
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-sdk-search --source ID --revision REV --query TEXT [--path FILTER] [--limit N]"
                return 0
                ;;
            --source) shift; [ $# -gt 0 ] || { error "--source requires a value"; return "$GMS_RT_EXIT_USAGE"; }; source="$1" ;;
            --revision) shift; [ $# -gt 0 ] || { error "--revision requires a value"; return "$GMS_RT_EXIT_USAGE"; }; revision="$1" ;;
            --query) shift; [ $# -gt 0 ] || { error "--query requires a value"; return "$GMS_RT_EXIT_USAGE"; }; query="$1" ;;
            --path) shift; [ $# -gt 0 ] || { error "--path requires a value"; return "$GMS_RT_EXIT_USAGE"; }; path_filter="$1" ;;
            --limit) shift; [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }; limit="$1" ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$source" ] || [ -z "$revision" ] || [ -z "$query" ] && { error "Usage: gms-rt-sdk-search --source ID --revision REV --query TEXT"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/sdk/search?source=$(_urlencode "$source")&revision=$(_urlencode "$revision")&query=$(_urlencode "$query")&limit=$limit"
    [ -n "$path_filter" ] && url="$url&path_filter=$(_urlencode "$path_filter")"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "source: \(.source_id) commit: \(.commit)\nmatches: \(.total)\(if .limited then " (limited)" else "" end)", (.matches[] | "\(.path):\(.line): \(.snippet)\n  result_id: \(.result_id)")'
}

gms-rt-sdk-read() {
    # read 只接受自包含 opaque result_id；source/path/commit
    # 由 result_id 载荷携带，客户端不再拼接自由路径。
    local result_id=""
    local offset=0 limit=400
    # 位置参数形式：gms-rt-sdk-read SDK_RESULT_ID（同上契约）。
    if [ $# -ge 1 ]; then
        case "$1" in
            -*) : ;;
            *) result_id="$1"; shift ;;
        esac
    fi
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --result-id) shift; [ $# -gt 0 ] || { error "--result-id requires a value"; return "$GMS_RT_EXIT_USAGE"; }; result_id="$1" ;;
            --offset) shift; [ $# -gt 0 ] || { error "--offset requires a value"; return "$GMS_RT_EXIT_USAGE"; }; offset="$1" ;;
            --limit) shift; [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }; limit="$1" ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$result_id" ] && {
        error "Usage: gms-rt-sdk-read SDK_RESULT_ID [--offset N] [--limit N]"
        error "       (result_id comes from gms-rt-sdk-search matches; source/path/commit are bound inside)"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq
    local url="/sdk/read?result_id=$(_urlencode "$result_id")&offset=$offset&limit=$limit"
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data | "source: \(.source_id) commit: \(.commit) blob_sha256: \(.blob_sha256)\nlines \(.offset)+\(.returned_lines)/\(.total_lines)\(if .truncated then " (truncated)" else "" end)", (.lines[] | "\(.line)\t\(.text)")'
}

gms-rt-knowledge-search() {
    # ADR 0014: 外部 Android 知识源只读检索（android-internals-wiki）。
    # 命中一律是 background 背景知识：解释系统机制，不得当作 verified
    # root cause 证据，也不能替代 gms-rt-sdk-* 的源码取证。
    local query="" limit=5 android_api_level=""
    if [ $# -ge 1 ]; then
        case "$1" in
            -*) : ;;
            *) query="$1"; shift ;;
        esac
    fi
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-knowledge-search QUERY [--limit N] [--android-api-level N]"
                echo "  Query: mechanism keywords, e.g. 'LMKD PRESSURE_AFTER_KILL', 'Binder timeout'"
                return 0
                ;;
            --query) shift; [ $# -gt 0 ] || { error "--query requires a value"; return "$GMS_RT_EXIT_USAGE"; }; query="$1" ;;
            --limit) shift; [ $# -gt 0 ] || { error "--limit requires a value"; return "$GMS_RT_EXIT_USAGE"; }; limit="$1" ;;
            --android-api-level) shift; [ $# -gt 0 ] || { error "--android-api-level requires a value"; return "$GMS_RT_EXIT_USAGE"; }; android_api_level="$1" ;;
            *) error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift
    done
    [ -z "$query" ] && { error "Usage: gms-rt-knowledge-search QUERY [--limit N] [--android-api-level N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local url="/knowledge/android-internals/search?q=$(_urlencode "$query")&limit=$limit"
    if [ -n "$android_api_level" ]; then
        url="$url&android_api_level=$(_urlencode "$android_api_level")"
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    api_call "$url" "GET" | jq -r '.data |
        (.sources_status | map(select(.status != "ready"))) as $bad |
        "background hits: \(.results | length)" +
        (if ($bad | length) > 0
         then " (sources: " + ([$bad[] | "\(.source)=\(.status)"] | join(", ")) + ")"
         else "" end),
        (.results[] |
            (if .extra.section.start_line and .extra.section.start_line > 0
             then " @ L\(.extra.section.start_line)-L\(.extra.section.end_line)"
             else "" end) as $range |
            "[\(.chapter // "-")] \(.title)\($range)\n  \(.snippet)\n  versions: \(.applicable_versions // "-") · confidence: \(.confidence // "-") · verified: \(.last_verified // "-")\n  src: \(.source_path) @ \(.source_revision[0:8] // "-") · \(.license)\n")'
}
