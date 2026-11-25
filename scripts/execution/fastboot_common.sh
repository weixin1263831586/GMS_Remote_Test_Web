#!/usr/bin/env bash
# Shared fastbootd readiness checks for the independently deployed runners.
is_fastbootd() {
    local serial="$1" output
    output="$(fastboot -s "$serial" getvar is-userspace 2>&1 || true)"
    [[ "${output,,}" == *"is-userspace: yes"* ]]
}

wait_for_fastbootd() {
    local serial="$1"
    local timeout="${FASTBOOTD_TIMEOUT_SECONDS:-60}"
    local deadline=$((SECONDS + timeout))
    while ((SECONDS < deadline)); do
        if is_fastbootd "$serial"; then
            return 0
        fi
        sleep 1
    done
    echo "设备 $serial 未在 ${timeout}s 内进入 fastbootd" >&2
    return 1
}
