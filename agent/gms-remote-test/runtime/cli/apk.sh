# Fixed command domain; sourced by gms-remote-test.sh.
# ==============================================================================
# APK Analysis Commands (suite module -> decompiled source)
# ==============================================================================

gms-rt-apk-resolve() {
    local module_query=""
    local suite_types=""
    local prefer="apk"
    local suite_path=""
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-resolve <module_query> [--types cts,cts-v,vts,gts,sts] [--prefer apk|jar] [--suite-path PATH]"
                echo "  module_query  Test module keyword, e.g. CtsCamera"
                echo "  --types       Comma-separated suite types (default cts,cts-v,vts,gts,sts)"
                echo "  --prefer      Preferred artifact type: apk (default) or jar"
                echo "  --suite-path  Explicit suite root (local host only); scans only that suite"
                return 0
                ;;
            --suite-path)
                shift
                [ $# -gt 0 ] || { error "--suite-path requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                suite_path="$1"
                ;;
            --types)
                shift
                [ $# -gt 0 ] || { error "--types requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                suite_types="$1"
                ;;
            --prefer)
                shift
                [ $# -gt 0 ] || { error "--prefer requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    apk|jar) prefer="$1" ;;
                    *) error "--prefer accepts apk or jar"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            *)
                [ -z "$module_query" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                module_query="$1"
                ;;
        esac
        shift
    done
    [ -z "$module_query" ] && { error "Module query required. Usage: gms-rt-apk-resolve <module_query> [--types cts,cts-v,vts,gts,sts] [--prefer apk|jar]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local url="/test/suites/modules/apk?query=$(_urlencode "$module_query")&prefer=$prefer"
    [ -n "$suite_types" ] && url="$url&suite_types=$(_urlencode "$suite_types")"
    [ -n "$suite_path" ] && url="$url&suite_path=$(_urlencode "$suite_path")"

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    local response
    response=$(api_call "$url" "GET")
    if echo "$response" | jq -e '.success' > /dev/null; then
        echo "$response" | jq -r '.data | "module: \(.module)\ntype: \(.suite_type) \(.suite_version)\nartifact: \(.file_name)\nsuite_path: \(.analyze_suite_path)\nanalyze_path: \(.analyze_path)"'
    else
        error "No APK/JAR artifact resolved for '$module_query'"
        echo "$response" | jq '.'
    fi
}

gms-rt-apk-analyze() {
    local module_query=""
    local suite_types=""
    local prefer="apk"
    local suite_path=""
    local do_wait=0
    local max_wait=300
    local argument
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-analyze <module_query> [--types cts,cts-v,vts,gts,sts] [--prefer apk|jar] [--suite-path PATH] [--wait] [--max-wait SECONDS]"
                echo "  Resolve a test module to its APK/JAR in the latest suites, copy it into an"
                echo "  analysis task, and start jadx decompilation. --wait polls until completed."
                return 0
                ;;
            --types)
                shift
                [ $# -gt 0 ] || { error "--types requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                suite_types="$1"
                ;;
            --prefer)
                shift
                [ $# -gt 0 ] || { error "--prefer requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    apk|jar) prefer="$1" ;;
                    *) error "--prefer accepts apk or jar"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
            --wait) do_wait=1 ;;
            --max-wait)
                shift
                [ $# -gt 0 ] || { error "--max-wait requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                max_wait="$1"
                ;;
            *)
                [ -z "$module_query" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                module_query="$1"
                ;;
        esac
        shift
    done
    [ -z "$module_query" ] && { error "Module query required. Usage: gms-rt-apk-analyze <module_query> [--types cts,cts-v,vts,gts,sts] [--prefer apk|jar] [--suite-path PATH] [--wait] [--max-wait SECONDS]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    # Step 1: resolve module keyword -> suite artifact
    local url="/test/suites/modules/apk?query=$(_urlencode "$module_query")&prefer=$prefer"
    [ -n "$suite_types" ] && url="$url&suite_types=$(_urlencode "$suite_types")"
    [ -n "$suite_path" ] && url="$url&suite_path=$(_urlencode "$suite_path")"
    local resolve_resp
    resolve_resp=$(api_call "$url" "GET")
    if ! echo "$resolve_resp" | jq -e '.success' > /dev/null; then
        error "Module resolution failed"
        echo "$resolve_resp" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
    local suite_path analyze_path file_name module_name
    suite_path=$(echo "$resolve_resp" | jq -r '.data.analyze_suite_path')
    analyze_path=$(echo "$resolve_resp" | jq -r '.data.analyze_path')
    file_name=$(echo "$resolve_resp" | jq -r '.data.file_name')
    module_name=$(echo "$resolve_resp" | jq -r '.data.module')
    [ "$GMS_RT_QUIET" != "1" ] && info "Resolved '$module_query' -> $module_name ($file_name)"

    # Step 2: copy the artifact from the suite into an analysis task
    local copy_data copy_resp task_id start_resp
    copy_data=$(jq -cn --arg suite_path "$suite_path" --arg path "$analyze_path" '{suite_path: $suite_path, path: $path}')
    copy_resp=$(api_call "/test/suites/apk/analyze" "POST" "$copy_data")
    if ! echo "$copy_resp" | jq -e '.success' > /dev/null; then
        error "Failed to copy suite artifact into an analysis task"
        echo "$copy_resp" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
    task_id=$(echo "$copy_resp" | jq -r '.data.task_id')

    # Step 3: start jadx decompilation
    start_resp=$(api_call "/apk/analyze/$task_id" "POST")
    if ! echo "$start_resp" | jq -e '.success' > /dev/null; then
        error "Failed to start decompilation for task $task_id"
        echo "$start_resp" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi

    if [ "$do_wait" != "1" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            jq -cn --arg task_id "$task_id" --arg module "$module_name" --arg file "$file_name" \
                '{success: true, task_id: $task_id, module: $module, file: $file, status: "analyzing"}'
        else
            success "Decompilation started for $module_name (task $task_id)"
            echo "Poll with: gms-rt-apk-status $task_id"
        fi
        return 0
    fi

    # Step 4: poll until completed/error or --max-wait is exceeded
    local waited=0 interval=5 status="" status_resp
    while true; do
        status_resp=$(api_call "/apk/status/$task_id" "GET")
        status=$(echo "$status_resp" | jq -r '.data.status // empty')
        if [ -z "$status" ]; then
            error "Failed to read analysis status for task $task_id"
            echo "$status_resp" | jq '.'
            return "$GMS_RT_EXIT_OPERATION"
        fi
        case "$status" in
            completed|error) break ;;
        esac
        [ "$waited" -ge "$max_wait" ] && break
        sleep "$interval"
        waited=$((waited + interval))
    done

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg task_id "$task_id" --arg module "$module_name" --arg file "$file_name" --arg status "$status" \
            '{success: ($status == "completed"), task_id: $task_id, module: $module, file: $file, status: $status}'
        [ "$status" = "completed" ] && return 0
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ "$status" = "completed" ]; then
        success "Decompilation completed for $module_name (task $task_id)"
        echo "Browse: gms-rt-apk-source $task_id"
        return 0
    fi
    error "Decompilation not completed (status: ${status:-unknown}, waited ${waited}s)"
    echo "Task: $task_id (continue polling: gms-rt-apk-status $task_id)"
    return "$GMS_RT_EXIT_OPERATION"
}

# One task id -> status; no task id -> the full task list.
gms-rt-apk-status() {
    local task_id="${1:-}"
    shift 2>/dev/null || true
    [ "$#" -gt 0 ] && { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    local response
    if [ -z "$task_id" ]; then
        if [ "$GMS_RT_OUTPUT" = "json" ]; then
            api_call "/apk/tasks" "GET" | jq '.'
            return $?
        fi
        response=$(api_call "/apk/tasks" "GET")
        if echo "$response" | jq -e '.success' > /dev/null; then
            local count
            count=$(echo "$response" | jq '.data.total')
            if [ "$count" = "0" ]; then
                success "No APK analysis tasks"
                return 0
            fi
            success "Found $count APK analysis task(s)"
            printf "%-40s %-14s %-9s %s\n" "TASK" "STATUS" "PROGRESS" "FILE"
            echo "$response" | jq -r '.data.tasks[] | "\(.task_id)\t\(.status)\t\(.progress)\t\(.filename)"' |
                while IFS=$'\t' read -r tid status progress filename; do
                    printf "%-40s %-14s %-9s %s\n" "$tid" "$status" "$progress" "$filename"
                done
        else
            error "Failed to list APK analysis tasks"
            echo "$response" | jq '.'
            return "$GMS_RT_EXIT_OPERATION"
        fi
        return 0
    fi
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "/apk/status/$task_id" "GET" | jq '.'
        return $?
    fi
    response=$(api_call "/apk/status/$task_id" "GET")
    if echo "$response" | jq -e '.success' > /dev/null; then
        echo "$response" | jq -r '.data | "task: \(.task_id)\nstatus: \(.status)\nprogress: \(.progress)\nfile: \(.filename)" + (if .error then "\nerror: \(.error)" else "" end)'
    else
        error "Failed to get APK analysis status"
        echo "$response" | jq '.'
    fi
}

# Manifest view; --permissions selects the declared-permissions subset.
gms-rt-apk-manifest() {
    local task_id=""
    local permissions=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-manifest <task_id> [--permissions]"
                echo "  --permissions  List only the permissions declared by the artifact"
                return 0
                ;;
            --permissions) permissions=1 ;;
            *)
                [ -z "$task_id" ] && task_id="$1" || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                ;;
        esac
        shift
    done
    [ -z "$task_id" ] && { error "Task ID required. Usage: gms-rt-apk-manifest <task_id> [--permissions]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq
    if [ "$permissions" = "1" ]; then
        api_call "/apk/permissions/$task_id" "GET" | jq '.'
        return $?
    fi
    api_call "/apk/manifest/$task_id" "GET" | jq '.'
}

gms-rt-apk-source() {
    local task_id=""
    local path=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-source <task_id> [path]"
                echo "  path  Relative path inside the decompiled sources (default: tree root)"
                echo "File contents: gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N]"
                return 0
                ;;
            *)
                [ -z "$task_id" ] && task_id="$1" || {
                    [ -z "$path" ] || { error "Unexpected argument: $1"; return "$GMS_RT_EXIT_USAGE"; }
                    path="$1"
                }
                ;;
        esac
        shift
    done
    [ -z "$task_id" ] && { error "Task ID required. Usage: gms-rt-apk-source <task_id> [path]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local url="/apk/source/$task_id"
    [ -n "$path" ] && url="$url?path=$(_urlencode "$path")"

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        api_call "$url" "GET" | jq '.'
        return $?
    fi
    local response
    response=$(api_call "$url" "GET")
    if ! echo "$response" | jq -e '.success' > /dev/null; then
        error "Failed to browse decompiled source"
        echo "$response" | jq '.'
        return "$GMS_RT_EXIT_OPERATION"
    fi
    echo "$response" | jq -r '.data.items[] | "\(.type)\t\(.path)"' |
        while IFS=$'\t' read -r type item_path; do
            printf "%-4s %s\n" "$type" "$item_path"
        done
}

# Unified source lookup: filename (name), file content (content), or Java
# symbol definition (symbol).
gms-rt-apk-search() {
    local task_id=""
    local query=""
    local mode="name"
    local limit=""
    local path_filter=""
    local symbol_line=0
    local positional=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: gms-rt-apk-search <task_id> <query> [--mode name|content|symbol] [--limit N] [--path FILTER] [--line N]"
                echo "  --mode name     Search decompiled file names by substring (default)"
                echo "  --mode content  Search decompiled file content (path:line:column + snippet)"
                echo "  --mode symbol   Locate a best-effort Java symbol definition"
                return 0
                ;;
            --mode)
                shift
                [ $# -gt 0 ] || { error "--mode requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                case "$1" in
                    name|content|symbol) mode="$1" ;;
                    *) error "--mode accepts name, content, or symbol"; return "$GMS_RT_EXIT_USAGE" ;;
                esac
                ;;
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
            --path)
                shift
                [ $# -gt 0 ] || { error "--path requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                path_filter="$1"
                ;;
            --line)
                shift
                [ $# -gt 0 ] || { error "--line requires a value"; return "$GMS_RT_EXIT_USAGE"; }
                symbol_line="$1"
                ;;
            *) positional+=("$1") ;;
        esac
        shift
    done
    if [ -z "$task_id" ] && [ "${#positional[@]}" -ge 1 ]; then
        task_id="${positional[0]}"
    fi
    if [ -z "$query" ] && [ "${#positional[@]}" -ge 2 ]; then
        query="${positional[1]}"
    fi
    [ -z "$task_id" ] || [ -z "$query" ] && { error "Usage: gms-rt-apk-search <task_id> <query> [--mode name|content|symbol] [--limit N] [--path FILTER] [--line N]"; return "$GMS_RT_EXIT_USAGE"; }
    check_jq

    local url response
    case "$mode" in
        name)
            [ -z "$path_filter" ] || { error "--path applies to content/symbol modes only"; return "$GMS_RT_EXIT_USAGE"; }
            [ "$symbol_line" = "0" ] || { error "--line applies to symbol mode only"; return "$GMS_RT_EXIT_USAGE"; }
            limit="${limit:-20}"
            url="/apk/search/$task_id?q=$(_urlencode "$query")&limit=$limit"
            if [ "$GMS_RT_OUTPUT" = "json" ]; then
                api_call "$url" "GET" | jq '.'
                return $?
            fi
            response=$(api_call "$url" "GET")
            if echo "$response" | jq -e '.success' > /dev/null; then
                echo "$response" | jq -r '.data.items[] | .path'
            else
                error "Failed to search decompiled source files"
                echo "$response" | jq '.'
                return "$GMS_RT_EXIT_OPERATION"
            fi
            ;;
        content)
            [ "$symbol_line" = "0" ] || { error "--line applies to symbol mode only"; return "$GMS_RT_EXIT_USAGE"; }
            limit="${limit:-50}"
            url="/apk/source-search/$task_id?q=$(_urlencode "$query")&limit=$limit"
            [ -n "$path_filter" ] && url="$url&path_filter=$(_urlencode "$path_filter")"
            if [ "$GMS_RT_OUTPUT" = "json" ]; then
                api_call "$url" "GET" | jq '.'
                return $?
            fi
            api_call "$url" "GET" | jq -r '.data | "query: \(.query) matches: \(.total)\(if .limited then " (limited)" else "" end) scanned_files: \(.scanned_files)", (.matches[] | "\(.path):\(.line):\(.column): \(.snippet)")'
            ;;
        symbol)
            [ -z "$limit" ] || { error "--limit applies to name/content modes only"; return "$GMS_RT_EXIT_USAGE"; }
            url="/apk/definition/$task_id?symbol=$(_urlencode "$query")&line=$symbol_line"
            [ -n "$path_filter" ] && url="$url&path=$(_urlencode "$path_filter")"
            api_call "$url" "GET" | jq '.'
            ;;
    esac
}

gms-rt-apk-download() {
    local task_id="$1"
    local output="$2"
    [ -z "$task_id" ] && { error "Task ID required. Usage: gms-rt-apk-download <task_id> [output.zip]"; return "$GMS_RT_EXIT_USAGE"; }
    [ -z "$output" ] && output="${task_id}_decompiled.zip"
    echo "⬇ Downloading decompiled source for $task_id -> $output"
    # Binary payload: stream straight to the target file via curl extra args.
    if ! api_call "/apk/download/$task_id" "GET" "" -o "$output" -sS; then
        rm -f -- "$output"
        error "Download failed (HTTP error); check the task id and that analysis completed"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    if [ ! -s "$output" ]; then
        rm -f -- "$output"
        error "Download failed: $output is empty"
        return "$GMS_RT_EXIT_OPERATION"
    fi
    success "Saved: $output ($(du -h "$output" | cut -f1))"
}
