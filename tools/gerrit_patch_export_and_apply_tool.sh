#!/bin/bash

# ============ Configuration ============
GERRIT_USER="chaoqun.huang"
GERRIT_HOST="10.10.10.29"
GERRIT_PORT="29418"
# ======================================

# Gerrit project name prefix (Gerrit URL includes this prefix; manifest name does not)
GERRIT_PROJECT_PREFIX="android/"

# Map: Gerrit project name -> SDK local path
declare -A PROJECT_PATH_MAP

_build_project_path_map() {
    local android_root="$1"
    [[ ${#PROJECT_PATH_MAP[@]} -gt 0 ]] && return 0
    echo "[INFO] Building project -> SDK path map..."
    # Use tab as delimiter to avoid parsing issues with special characters in paths
    while IFS=$'\t' read -r projectname sdk_path; do
        PROJECT_PATH_MAP["$projectname"]="$sdk_path"
    done < <(
        find "$android_root" -maxdepth 5 -name ".git" -type d 2>/dev/null | while read -r gitdir; do
            local dir
            dir=$(dirname "$gitdir")
            local rel="${dir#$android_root/}"
            local pn=""
            for remote_name in $(git -C "$dir" remote 2>/dev/null); do
                pn=$(git -C "$dir" config --get "remote.$remote_name.projectname" 2>/dev/null || true)
                [[ -n "$pn" ]] && break
            done
            [[ -n "$pn" ]] && printf '%s\t%s\n' "$pn" "$rel"
        done
    )
    echo "[INFO] Mapped ${#PROJECT_PATH_MAP[@]} projects"
}

# Convert Gerrit query project name to SDK path
_project_to_sdk_path() {
    local gerrit_project="$1"
    local manifest_name="${gerrit_project#$GERRIT_PROJECT_PREFIX}"
    if [[ -n "${PROJECT_PATH_MAP[$manifest_name]:-}" ]]; then
        echo "${PROJECT_PATH_MAP[$manifest_name]}"
    else
        echo "$manifest_name"
    fi
}

# Show help
show_help() {
cat <<EOF
Usage: $0 <command> [options]

Commands:
  export <topic> [patches_dir]      Export patches
  apply <patches_dir> [dry-run]     Apply patches
  list <patches_dir>                List patches
  help                              Show help

Environment:
  PARALLEL_JOBS    Parallel download count (default: 4)

Examples:
  $0 export Android15_Security_Patch
  $0 apply patches_Android15_Security_Patch
  $0 list patches_Android15_Security_Patch
EOF
}

# Worker function to download a single patch (for parallel execution)
# Uses independent temp dirs to avoid contention between patches in the same project
_fetch_and_format_patch() {
    local project="$1"
    local ref="$2"
    local patch_file="$3"
    local patch_dir="$4"
    local gerrit_url="$5"
    local change_id="$6"
    # Use change_id as temp dir suffix to ensure uniqueness
    local tmp_dir="${patch_dir}/.tmp_git_${change_id}"

    # Clean up any leftover temp dir
    rm -rf "$tmp_dir"

    if ! git init --bare "$tmp_dir" >/dev/null 2>&1; then
        echo "[ERROR]   git init failed for $project"
        return 1
    fi

    # Fetch single ref, only the commit and its parent
    local fetch_output
    fetch_output=$(git -C "$tmp_dir" fetch --depth=2 "$gerrit_url/$project" "$ref" 2>&1)
    local fetch_rc=$?

    if [[ $fetch_rc -ne 0 ]]; then
        echo "[ERROR]   Failed to fetch ref: $ref for change: $change_id"
        echo "[ERROR]   $fetch_output"
        rm -rf "$tmp_dir"
        return 1
    fi

    # Generate patch via format-patch
    if ! git -C "$tmp_dir" format-patch -1 FETCH_HEAD --stdout > "${patch_dir}/${patch_file}"; then
        echo "[ERROR]   format-patch failed for change: $change_id"
        rm -rf "$tmp_dir"
        return 1
    fi

    rm -rf "$tmp_dir"
    echo "[INFO]   ✅ Patch saved: $patch_file"
    return 0
}

# Export patches
export_patches() {
    local topic="$1"
    local output_path="$2"
    local tmp_file="$output_path/patches.json"
    local android_root
    android_root="$(pwd)"

    local start_time
    start_time=$(date +%s)

    echo "[INFO] ========== Start exporting patches =========="
    echo "[INFO] Topic: $topic"
    echo "[INFO] Output Path: $output_path"

    mkdir -p "$output_path"

    # Build project -> SDK path map
    _build_project_path_map "$android_root"

    echo "[INFO] Querying Gerrit for topic: $topic ..."
    ssh -p "${GERRIT_PORT}" "${GERRIT_USER}@${GERRIT_HOST}" \
        gerrit query "topic:${topic}" --format=JSON --patch-sets --files > "$tmp_file"

    echo "[INFO] Parsing patches..."
    # Use tab delimiter to avoid subject parsing issues
    # Filter newlines in subject to prevent line-based read errors
    # Single jq call: collect -> sort -> output
    local patches
    patches=$(jq -s -r '
        [.[] | select(.project != null)] |
        sort_by(.project, (.number // 0 | tonumber)) |
        .[] |
        .subject = (.subject | gsub("[\\n\\r]"; " ")) |
        [.project, .number, .patchSets[-1].ref, .subject] |
        @tsv
    ' "$tmp_file")

    if [[ -z "$patches" ]]; then
        echo "[ERROR] No patches found for topic: $topic"
        rm -f "$tmp_file"
        return 1
    fi

    # Count total
    local total_count
    total_count=$(echo "$patches" | wc -l)
    echo "[INFO] Found $total_count patches to export"

    local gerrit_url="ssh://${GERRIT_USER}@${GERRIT_HOST}:${GERRIT_PORT}"
    local pids=()
    local pid_files=()

    # Track per-project patch counters
    declare -A project_counters
    local current_idx=0
    local success_count=0
    local fail_count=0
    local PARALLEL_JOBS=${PARALLEL_JOBS:-4}

    echo "[INFO] Parallel jobs: $PARALLEL_JOBS"

    # Use tab as IFS to safely parse subjects with spaces
    while IFS=$'\t' read -r project change_id ref patch_subject; do
        [[ -z "$project" ]] && continue
        ((current_idx++))

        echo "[INFO] [$current_idx/$total_count] Exporting: $project, change: $change_id"
        echo "[INFO]   Subject: $patch_subject"

        if [[ "$ref" == "null" || -z "$ref" ]]; then
            echo "[WARN] Skipping $change_id: no valid ref found"
            ((fail_count++))
            continue
        fi

        # Convert Gerrit project name to SDK local path
        local sdk_path
        sdk_path=$(_project_to_sdk_path "$project")
        local patch_dir="${output_path}/${sdk_path}"
        mkdir -p "$patch_dir"
        echo "[INFO]   SDK path: $sdk_path"

        # Get or initialize current project's patch counter
        if [[ -z "${project_counters[$project]}" ]]; then
            project_counters[$project]=1
        fi

        # Generate standard Git patch filename
        local clean_subject
        clean_subject=$(echo "$patch_subject" | sed 's/[^a-zA-Z0-9._-]/-/g; s/--*/-/g; s/^-\|-$//g' | cut -c1-80 | tr '[:upper:]' '[:lower:]')
        local patch_number
        patch_number=$(printf "%04d" "${project_counters[$project]}")
        local patch_file="${patch_number}-${clean_subject}.patch"

        echo "[INFO]   Fetching patch: $patch_file"

        # Parallel download: run in background
        (
            _fetch_and_format_patch "$project" "$ref" "$patch_file" "$patch_dir" "$gerrit_url" "$change_id"
        ) &

        pids+=($!)
        pid_files+=("$project/$patch_file")

        ((project_counters[$project]++))

        # Throttle: wait for the oldest process when at concurrency limit
        while [[ ${#pids[@]} -ge $PARALLEL_JOBS ]]; do
            wait "${pids[0]}"
            local rc=$?
            if [[ $rc -eq 0 ]]; then
                ((success_count++))
            else
                echo "[ERROR] Failed: ${pid_files[0]}"
                ((fail_count++))
            fi
            # Remove completed process
            pids=("${pids[@]:1}")
            pid_files=("${pid_files[@]:1}")
        done
    done < <(echo "$patches")

    # Wait for remaining background tasks
    echo "[INFO] Waiting for remaining downloads to complete..."
    for i in "${!pids[@]}"; do
        if wait "${pids[$i]}"; then
            ((success_count++))
        else
            echo "[ERROR] Failed: ${pid_files[$i]}"
            ((fail_count++))
        fi
    done

    # Clean up any leftover temp dirs
    find "$output_path" -name ".tmp_git_*" -type d -exec rm -rf {} + 2>/dev/null

    echo "[INFO] Patches saved in: $output_path"
    echo "[INFO] Downloaded $((success_count + fail_count)) patches (success: $success_count, failed: $fail_count)"

    local end_time
    end_time=$(date +%s)
    local elapsed=$((end_time - start_time))
    echo "[INFO] Elapsed: ${elapsed}s"

    # Clean up temp file
    rm -f "$tmp_file"

    echo "[INFO] ========== Export complete =========="
}

# Apply patches
apply_patches() {
    local patches_path
    patches_path="$(readlink -f "$1")"
    local android_root
    android_root="$(pwd)"
    local dry_run="$2"
    local total=0
    local applied=0
    local failed=0

    echo "[INFO] Android root : $android_root"
    echo "[INFO] Patches path : $patches_path"
    echo "[INFO] ========== Start applying patches =========="

    while read -r patch; do
        ((total++))

        local patch_file
        patch_file="$(readlink -f "$patch")"
        local patch_name
        patch_name="$(basename "$patch_file")"
        local patch_dir
        patch_dir="$(dirname "$patch_file")"

        # Extract SDK relative path from patch directory (strip patches root prefix)
        local repo_rel_path
        repo_rel_path="${patch_dir#$patches_path/}"
        local repo_path="$android_root/$repo_rel_path"

        echo "[INFO] -------------------------------------"
        echo "[INFO] Patch : $patch_name"
        echo "[INFO] Repo  : $repo_path"

        if [[ ! -d "$repo_path/.git" ]]; then
            echo "[ERROR] ❌ Not a git repo, skipping"
            ((failed++))
            continue
        fi

        if [[ "$dry_run" == "dry-run" ]]; then
            if git -C "$repo_path" am --dry-run "$patch_file" >/dev/null 2>&1; then
                echo "[INFO] 🧪 dry-run OK"
                ((applied++))
            else
                echo "[ERROR] ❌ dry-run FAILED"
                ((failed++))
            fi
        else
            if git -C "$repo_path" am "$patch_file"; then
                echo "[INFO] ✅ applied"
                ((applied++))
            else
                echo "[ERROR] ❌ FAILED, abort"
                git -C "$repo_path" am --abort
                ((failed++))
            fi
        fi
    done < <(find "$patches_path" -name "*.patch" | sort)

    echo "[INFO] ========== Apply complete =========="
    echo "[INFO] Total: $total, Success: $applied, Failed: $failed"
}

list_patches() {
    local patches_path
    patches_path="$(readlink -f "$1")"
    local total_patches=0
    local total_projects=0

    echo "[INFO] Patches root: $patches_path"
    echo ""

    [[ ! -d "$patches_path" ]] && {
        echo "[ERROR] Directory not found: $patches_path"
        return 1
    }

    # Find all project directories containing patches
    mapfile -t project_dirs < <(find "$patches_path" -name "*.patch" -exec dirname {} \; | sort -u)
    for project_dir in "${project_dirs[@]}"; do
        local rel_project
        rel_project="${project_dir#$patches_path/}"

        mapfile -t patches < <(find "$project_dir" -maxdepth 1 -name "*.patch" | sort)
        echo "📁 $rel_project : ${#patches[@]} patches"

        for p in "${patches[@]}"; do
            echo "📄 ${p#$patches_path/}"
            ((total_patches++))
        done

        ((total_projects++))
        echo ""
    done

    echo "======📊 Total: $total_projects projects, $total_patches patches ======"
}

case "${1:-}" in
    export)
        export_patches "${2:-}" "${3:-./patches_$2}"
        ;;
    apply)
        apply_patches "${2:-}" "${3:-}"
        ;;
    list)
        list_patches "${2:-}"
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        show_help
        ;;
esac
