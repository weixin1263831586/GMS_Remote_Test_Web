#!/bin/bash
set -euo pipefail

# 禁用输出缓冲，确保实时输出
export PYTHONUNBUFFERED=1
export SCRIPT_LOG_FILE="/tmp/gms_test_$(date +%Y%m%d_%H%M%S).log"

# 使用 unbuffered tee 或者直接输出
LOG_FILE="$SCRIPT_LOG_FILE"

export PATH="$HOME/Software/platform-tools:$PATH"

# 未显式设置时从固定凭据文件加载 GTS 认证。
if [[ -z "${APE_API_KEY:-}" && -f "$HOME/Software/gts-rockchip.json" ]]; then
    export APE_API_KEY="$HOME/Software/gts-rockchip.json"
fi

# 运行状态
REMOTE_HOST=""
REMOTE_USER=""
SUITE_PATH=""
SUITE_PREFIX=""
TEST_COMMAND=""
DEVICE_ARGS=""
MODE="run"
PASS_COUNT=0
FAIL_COUNT=0
RESULT_TIMESTAMP=""
RESULT_DIR=""
RUN_STARTED_EPOCH=0
RETRY_FAIL="false"
COPY_TO_REMOTE="false"
PROCESS_GROUP_ID=""  # 进程组ID，用于多用户隔离

# 工具函数
log() { echo -e "$*" | tee -a "$LOG_FILE"; }
die() { log "❌ $*"; exit 1; }

show_help() {
cat <<EOF
用法:
  $0 <cts|gsi|gts|sts|vts|apts> [模块] [用例]
  $0 <cts|gsi|gts|sts|vts|apts> retry <RESULT_TIMESTAMP>

必需参数:
  --test-suite path         测试套件完整路径(如：/home/user/GMS-Suite/android-cts-16_r3-1/android-cts/tools)
  --local-server user@host  本地主机

可选参数:
  --device-args ARGS        设备参数, 格式：[-s DEVICE1] 或 [--shard-count 2 -s DEVICE1 -s DEVICE2...]
  --no-retry                禁用失败自动重试
  --copy-remote             测试结果拷贝到远端
  --pgid ID                 进程组ID，用于多用户隔离（内部使用）
  --help                    显示帮助

示例:
  $0 cts CtsSecurityTestCases --device-args '-s DEVICE1' --test-suite "$HOME/GMS-Suite/android-cts/tools" --local-server "$USER@$(hostname -I | awk '{print $1}')"
  $0 cts retry 2026.01.12_14.36.17.772_8696 --device-args '-s DEVICE1' --test-suite "$HOME/GMS-Suite/android-cts/tools" --local-server "$USER@$(hostname -I | awk '{print $1}')"

支持测试类型: cts, gsi, gts, sts, vts, apts
EOF
}

## 参数解析
parse_args() {
    local args=()
    DEVICE_ARGS=""
    log "🔧 开始解析命令行参数 ($# 个)"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help)
                show_help
                exit 0
                ;;
            --no-retry)
                RETRY_FAIL="false"
                log "✅ 禁用自动重试"
                shift
                ;;
            --local-server)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--local-server 缺少参数（格式: user@host）"
                fi
                local_server="$1"
                if [[ "$local_server" != *@* ]]; then
                    die "--local-server 格式错误，应为 user@host"
                fi
                REMOTE_USER="${local_server%@*}"
                REMOTE_HOST="${local_server#*@}"
                shift
                ;;
            --test-suite)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--test-suite 缺少路径参数"
                fi
                SUITE_PATH="$1"
                shift
                ;;
            --device-args)
                shift
                if [[ $# -gt 0 ]]; then
                    DEVICE_ARGS="$1"
                    shift
                else
                    die "--device-args 缺少参数"
                fi
                while [[ $# -gt 0 ]] && [[ ! "$1" =~ ^-- ]]; do
                    DEVICE_ARGS+=" $1"
                    shift
                done
                if [[ -z "$DEVICE_ARGS" ]]; then
                    die "缺少设备参数，请使用 --device-args 指定设备"
                fi
                if [[ ! "$DEVICE_ARGS" =~ -s[[:space:]]+[^[:space:]]+ ]]; then
                    die "⚠️ 设备参数格式可能不正确，应为: -s DEVICE1 [-s DEVICE2 ...]"
                fi
                ;;
            --copy-remote)
                COPY_TO_REMOTE="true"
                log "✅ 启用结果拷贝到远程"
                shift
                ;;
            --pgid)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--pgid 缺少ID参数"
                fi
                PROCESS_GROUP_ID="$1"
                shift
                ;;
            -*)
                die "未知参数: $1"
                ;;
            *)
                args+=("$1")
                shift
                ;;
        esac
    done

    if (( ${#args[@]} < 1 )); then
        die "缺少测试类型"
    fi
    if [[ -z "$SUITE_PATH" ]]; then
        die "缺少必需参数: --test-suite"
    fi
    # --local-server 仅在 --copy-remote 回传结果时需要，普通运行不再强制要求。
    if [[ "$COPY_TO_REMOTE" == "true" ]] && { [[ -z "$REMOTE_HOST" ]] || [[ -z "$REMOTE_USER" ]]; }; then
        die "启用 --copy-remote 时必须提供 --local-server（格式: user@host）"
    fi

    Test_Type="${args[0],,}"
    Test_Module="${args[1]:-}"
    Test_Case="${args[2]:-}"

    if [[ "${Test_Module,,}" == "retry" ]]; then
        MODE="retry"
        RESULT_TIMESTAMP="$Test_Case"
        if [[ -z "$RESULT_TIMESTAMP" ]]; then
            die "retry 必须指定 RESULT_TIMESTAMP"
        fi
        Test_Module=""; Test_Case=""
        log "🔄 Retry模式: $RESULT_TIMESTAMP"
    else
        MODE="run"
    fi

    case "${Test_Type}" in
        cts)
            SUITE_PREFIX="cts"
            TEST_COMMAND="cts"
            ;;
        gsi)
            SUITE_PREFIX="cts"
            TEST_COMMAND="cts-on-gsi"
            ;;
        gts)
            SUITE_PREFIX="gts"
            TEST_COMMAND="gts"
            ;;
        gts-root)
            SUITE_PREFIX="gts"
            TEST_COMMAND="gts-root"
            ;;
        sts)
            SUITE_PREFIX="sts"
            TEST_COMMAND="sts-dynamic-full"
            ;;
        vts)
            SUITE_PREFIX="vts"
            TEST_COMMAND="vts"
            ;;
        apts)
            SUITE_PREFIX="gts"
            TEST_COMMAND="apts"
            ;;
        *)
            die "不支持的测试类型: $Test_Type (目前仅支持: cts, gsi, gts, gts-root, sts, vts, apts)"
            ;;
    esac
}

## 执行测试
run_tradefed() {
    local mode="${1:-run}"
    cd "$SUITE_PATH" || die "无法进入测试套件目录 $SUITE_PATH"

    local tradefed_bin="./$SUITE_PREFIX-tradefed"
    [[ -x "$tradefed_bin" ]] || die "未找到 tradefed 可执行文件: $tradefed_bin"

    local -a command=("$tradefed_bin" run commandAndExit)
    if [[ "$mode" == "retry" ]]; then
        [[ -n "$RESULT_TIMESTAMP" ]] || die "retry 模式缺少 RESULT_TIMESTAMP"

        # 根据测试类型使用不同的retry参数
        case "$Test_Type" in
            vts)
                # VTS使用 --retry 参数，需要找到对应的session ID
                local session_id=""

                log "🔍 查找VTS session ID..."
                local list_output=""
                list_output=$("./$SUITE_PREFIX-tradefed" list results 2>/dev/null | grep -F "$RESULT_TIMESTAMP") || true

                if [[ -n "$list_output" ]]; then
                    # 提取session ID（第一列）
                    session_id=$(echo "$list_output" | awk '{print $1}')
                    log "✓ 找到session ID: $session_id"
                else
                    die "未找到VTS session ID，请检查结果目录: $RESULT_TIMESTAMP"
                fi

                # 构建VTS retry命令
                command+=(retry --retry "$session_id")
                log "🔄 VTS Retry模式, session ID: $session_id, 结果目录: $RESULT_TIMESTAMP"
                ;;
            *)
                # CTS/GTS/STS使用 --retry-result-dir 参数
                command+=(retry --retry-result-dir "$RESULT_TIMESTAMP")
                log "🔄 Retry模式, 结果目录: $RESULT_TIMESTAMP"
                ;;
        esac
    else
        command+=("$TEST_COMMAND")
        if [[ -n "$Test_Module" ]]; then
            command+=(-m "$Test_Module")
            if [[ -n "$Test_Case" ]]; then
                command+=(-t "$Test_Case")
            fi
        fi
    fi
    local -a device_args=()
    read -r -a device_args <<< "$DEVICE_ARGS"
    command+=("${device_args[@]}" --disable-reboot)

    local command_display=""
    printf -v command_display '%q ' "${command[@]}"
    log "📋 测试命令: ${command_display% }"
    log "⏱️ 开始时间: $(date)"
    RUN_STARTED_EPOCH=$(date +%s)

    # 如果设置了进程组ID，将其导出为环境变量，便于进程识别和管理
    if [[ -n "$PROCESS_GROUP_ID" ]]; then
        export GMS_TEST_PGID="$PROCESS_GROUP_ID"
        log "🔖 进程组ID: GMS_TEST_PGID=$PROCESS_GROUP_ID"
    fi

    # 执行命令并实时输出
    # 同时记录到日志文件（追加模式）
    "${command[@]}" 2>&1 | tee -a "$LOG_FILE"
    local exit_code=${PIPESTATUS[0]}

    log "⏱️ 结束时间: $(date)"
    log "📊 退出代码: $exit_code"
    # P2-5：0 模块匹配时结构化透传根因，避免被"未找到 RESULT DIRECTORY"掩盖。
    if grep -Fq "No matched tradefed modules" "$LOG_FILE"; then
        log "❌ module not found in suite: tradefed matched 0 modules"
        return 2
    fi
    return $exit_code
}

result_matches_current_run() {
    local xml_file="$1"
    [[ -f "$xml_file" ]] || return 1

    # 仅接受命令参数与当前任务一致的结果。
    if [[ -n "$TEST_COMMAND" ]]; then
        local match_cmd="$TEST_COMMAND"
        # sts-dynamic-full 在结果 XML 中可能仅记录为 sts-dynamic，
        # 用更短的前缀匹配避免漏掉有效结果。
        if [[ "$TEST_COMMAND" == sts-dynamic-* ]]; then
            match_cmd="sts-dynamic"
        fi
        grep -Fq "command_line_args=\"$match_cmd" "$xml_file" || return 1
    fi
    if [[ -n "$Test_Module" ]] && ! grep -Fq -- "-m $Test_Module" "$xml_file"; then
        return 1
    fi
    if [[ -n "$Test_Case" ]] && ! grep -Fq -- "-t $Test_Case" "$xml_file"; then
        return 1
    fi

    local device_tokens=()
    local index serial
    read -r -a device_tokens <<< "$DEVICE_ARGS"
    for ((index = 0; index < ${#device_tokens[@]}; index++)); do
        if [[ "${device_tokens[$index]}" == "-s" ]] && ((index + 1 < ${#device_tokens[@]})); then
            serial="${device_tokens[$((index + 1))]}"
            grep -Fq "$serial" "$xml_file" || return 1
        fi
    done
    return 0
}

resolve_result_dir() {
    local result_dir=""
    result_dir=$(awk -F': ' '/RESULT DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    if [[ -n "$result_dir" && -d "$result_dir" ]]; then
        printf '%s\n' "$result_dir"
        return 0
    fi

    local suite_root results_root candidate
    suite_root=$(cd "$SUITE_PATH/.." && pwd)
    results_root="$suite_root/results"
    if [[ -n "$RESULT_TIMESTAMP" && -d "$results_root/$RESULT_TIMESTAMP" ]]; then
        printf '%s\n' "$results_root/$RESULT_TIMESTAMP"
        return 0
    fi
    [[ -d "$results_root" && "$RUN_STARTED_EPOCH" -gt 0 ]] || return 1

    while IFS= read -r candidate; do
        if result_matches_current_run "$candidate"; then
            dirname "$candidate"
            return 0
        fi
    done < <(
        find "$results_root" -mindepth 2 -maxdepth 2 -type f -name test_result.xml \
            -newermt "@$((RUN_STARTED_EPOCH - 2))" -printf '%T@ %p\n' 2>/dev/null \
            | sort -nr | sed 's/^[^ ]* //'
    )
    return 1
}

## 重新测试
retry_if_needed() {
    (( FAIL_COUNT == 0 )) && return 0
    [[ "$RETRY_FAIL" != "true" ]] && return 0

    if run_tradefed "retry"; then
        log "✅ retry成功"
        return 0
    else
        log "❌ 自动重试失败，回退完整重跑..."
        run_tradefed "run"
    fi
}

## 解析结果
analyze_result() {
    log "🔍 解析结果..."
    cd "$SUITE_PATH" || die "无法进入测试套件目录 $SUITE_PATH"

    local result_dir=""
    result_dir=$(resolve_result_dir) || die "未找到 RESULT DIRECTORY"
    RESULT_DIR="$result_dir"
    if ! grep -Fq "RESULT DIRECTORY: $result_dir" "$LOG_FILE"; then
        log "RESULT DIRECTORY: $result_dir"
    fi
    log "📁 结果目录: $result_dir"
    RESULT_TIMESTAMP=$(basename "$result_dir")

    if [[ -f "$result_dir/test_result.xml" ]]; then
        PASS_COUNT=$(grep -o 'pass="[0-9]*"' "$result_dir/test_result.xml" | head -1 | sed 's/pass="//; s/"//')
        FAIL_COUNT=$(grep -o 'failed="[0-9]*"' "$result_dir/test_result.xml" | head -1 | sed 's/failed="//; s/"//')
    else
        PASS_COUNT=$(awk '/^PASSED[[:space:]]+:/ {print $2}' "$LOG_FILE")
        FAIL_COUNT=$(awk '/^FAILED[[:space:]]+:/ {print $2}' "$LOG_FILE")
    fi
    log "📊 测试结果: PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
}

## 远程拷贝
copy_to_remote_server() {
    if [[ "$COPY_TO_REMOTE" != "true" ]]; then
        log "📤 远程拷贝已禁用"
        return 0
    fi

    local logs_dir result_dir suite_root
    logs_dir=$(awk -F': ' '/LOG DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    result_dir="${RESULT_DIR:-}"
    if [[ -z "$result_dir" ]]; then
        result_dir=$(resolve_result_dir) || die "未找到 RESULT DIRECTORY"
    fi
    suite_root=$(cd "$SUITE_PATH/.." && pwd)
    if [[ -z "$logs_dir" && -d "$suite_root/logs/$(basename "$result_dir")" ]]; then
        logs_dir="$suite_root/logs/$(basename "$result_dir")"
    fi
    [[ -d "$logs_dir" && -d "$result_dir" ]] || die "未找到 RESULT DIRECTORY"
    log "📁 日志目录: ${logs_dir:-<none>}"
    log "📁 结果目录: ${result_dir:-<none>}"

    local timestamp=$(basename "$result_dir")
    [[ -n "$timestamp" ]] || die "无法获取 RESULT_TIMESTAMP"

    local remote_target_dir="/home/$REMOTE_USER/gms_test_results/$timestamp"
    log "🌐 本地主机: ${REMOTE_USER}@${REMOTE_HOST}:${remote_target_dir}"

    # 可选路由：运行期绝不提权修改主机网络。如果需要跨网段回传，
    # 由主机管理员在启动 Worker 前配置持久路由；环境变量仅用于校验前置条件。
    # 路由目标网络和网关均由环境变量配置。
    if [[ -n "${GMS_COPY_ROUTE_NETWORK:-}" && -n "${GMS_COPY_ROUTE_GATEWAY:-}" ]]; then
        if ! ip route show "${GMS_COPY_ROUTE_NETWORK}" | grep -Fq "via ${GMS_COPY_ROUTE_GATEWAY}"; then
            log "❌ 缺少回传路由: ${GMS_COPY_ROUTE_NETWORK} via ${GMS_COPY_ROUTE_GATEWAY}"
            log "❌ 请由主机管理员配置持久路由后重试；Worker 不允许在任务期间修改网络。"
            return 1
        fi
    else
        log "ℹ️ 未配置 GMS_COPY_ROUTE_NETWORK/GMS_COPY_ROUTE_GATEWAY，跳过回传路由添加"
    fi

    # 验证 SSH 连接
    if ! ssh -o BatchMode=yes -o ConnectTimeout=5 \
            "${REMOTE_USER}@${REMOTE_HOST}" "echo 'OK' >/dev/null" 2>/dev/null; then
        log "❌ 无法连接远程服务器（检查网络和SSH免密）"
        return 1
    fi

    # 创建远程目录
    ssh "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p '$remote_target_dir'" 2>&1 | tee -a "$LOG_FILE"

    log "📤 开始拷贝: $remote_target_dir"
    for src in "$logs_dir" "$result_dir"; do
        if [[ -d "$src" ]]; then
            rsync -avz --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
                "$src/" \
                "${REMOTE_USER}@${REMOTE_HOST}:${remote_target_dir}/" \
                2>&1 | tee -a "$LOG_FILE"
        fi
    done
    log "✅ 拷贝完成: ${REMOTE_USER}@${REMOTE_HOST}:${remote_target_dir}"
}

## 主函数
# P2-5 前置校验：模块名在 testcases/ 下不存在时立即失败（精确/大小写
# 不敏感），附相近候选；避免走完 tradefed 会话建立→清理流程后才报
# "未找到 RESULT DIRECTORY"。
validate_module() {
    [[ -z "$Test_Module" || "$MODE" == "retry" ]] && return 0
    local testcases_dir
    testcases_dir="$(cd "$SUITE_PATH/.." && pwd)/testcases"
    [[ -d "$testcases_dir" ]] || return 0
    if [[ -d "$testcases_dir/$Test_Module" ]] || [[ -f "$testcases_dir/$Test_Module.config" ]]; then
        return 0
    fi
    local match
    match=$(ls "$testcases_dir" 2>/dev/null | grep -iFx "$Test_Module" | head -1 || true)
    if [[ -n "$match" ]]; then
        # 大小写不敏感存在同名模块：放行并提示正确大小写，由 tradefed 正常执行。
        log "⚠️ 模块名大小写与 testcases/ 不一致: $Test_Module → $match"
        Test_Module="$match"
        return 0
    fi
    match=$(ls "$testcases_dir" 2>/dev/null | grep -iF "$Test_Module" | head -5 || true)
    if [[ -n "$match" ]]; then
        die "module not found in suite: $Test_Module. 相近候选模块: $(echo "$match" | tr '\n' ', ' | sed 's/,$//')"
    fi
    die "module not found in suite: $Test_Module（testcases/ 下无精确或大小写不敏感匹配）"
}

main() {
    parse_args "$@"

    log "🚀 测试类型: $Test_Type"
    log "📦 测试模块: $Test_Module"
    log "🧪 测试用例: $Test_Case"
    log "📱 测试设备: $DEVICE_ARGS"
    log "📁 测试套件: $SUITE_PATH"
    if [[ -n "$REMOTE_HOST" ]]; then
        log "🌐 本地主机: ${REMOTE_USER}@${REMOTE_HOST}"
    fi
    log "📋 日志文件: $LOG_FILE"
    log "========================================"

    validate_module

    if [[ "$MODE" == "retry" ]]; then
        run_tradefed "retry"
        copy_to_remote_server
        exit $?
    fi
    
    if run_tradefed "run"; then
        analyze_result
        retry_if_needed
        copy_to_remote_server
        if (( FAIL_COUNT > 0 )); then
            log "⚠️ GMS 测试执行完成，报告包含失败项 (PASS: $PASS_COUNT FAIL: $FAIL_COUNT)"
        else
            log "✅ GMS 测试执行完成，报告无失败项"
        fi
    else
        log "❌ GMS 测试执行失败"
        copy_to_remote_server
        exit 1
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
