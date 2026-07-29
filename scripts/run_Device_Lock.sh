#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <serial> <oem-command>" >&2
    exit 1
fi

SERIAL="$1"
OEM_COMMAND="$2"

wait_for_fastbootd() {
    local timeout="${FASTBOOTD_TIMEOUT_SECONDS:-60}"
    local deadline=$((SECONDS + timeout))
    local output=""
    while ((SECONDS < deadline)); do
        output="$(fastboot -s "$SERIAL" getvar is-userspace 2>&1 || true)"
        if [[ "${output,,}" == *"is-userspace: yes"* ]]; then
            return 0
        fi
        sleep 1
    done
    echo "设备 $SERIAL 未在 ${timeout}s 内进入 fastbootd" >&2
    return 1
}

fastboot -s "$SERIAL" oem "$OEM_COMMAND"
if fastboot -s "$SERIAL" reboot fastboot; then
    wait_for_fastbootd
    fastboot -s "$SERIAL" reboot
fi
