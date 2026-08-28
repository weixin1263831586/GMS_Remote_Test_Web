#!/bin/bash
set -euo pipefail

if [[ $# -ne 4 && $# -ne 6 ]]; then
    echo "Usage: $0 <serial> <unlock-command> <system.img> <misc.img> [partition image]" >&2
    exit 1
fi

SERIAL="$1"
UNLOCK_COMMAND="$2"
SYSTEM_IMG="$3"
MISC_IMG="$4"
VENDOR_PARTITION="${5:-}"
VENDOR_IMG="${6:-}"

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

is_fastbootd() {
    local output=""
    output="$(fastboot -s "$SERIAL" getvar is-userspace 2>&1 || true)"
    [[ "${output,,}" == *"is-userspace: yes"* ]]
}

# Controller/Worker code normally performs this transition so USB/IP can
# re-bind the new gadget identity.  Keep the fallback for direct script use.
if ! is_fastbootd; then
    fastboot -s "$SERIAL" oem "$UNLOCK_COMMAND"
    fastboot -s "$SERIAL" reboot fastboot
    wait_for_fastbootd
fi

fastboot -s "$SERIAL" delete-logical-partition product || true
fastboot -s "$SERIAL" delete-logical-partition product_a || true
fastboot -s "$SERIAL" delete-logical-partition product_b || true

if [[ -n "$SYSTEM_IMG" ]]; then
    fastboot -s "$SERIAL" flash system "$SYSTEM_IMG"
fi
fastboot -s "$SERIAL" flash misc "$MISC_IMG"

if [[ -n "$VENDOR_IMG" ]]; then
    fastboot -s "$SERIAL" flash "$VENDOR_PARTITION" "$VENDOR_IMG"
fi

if [[ "${GMS_GSI_DEFER_REBOOT:-0}" != "1" ]]; then
    fastboot -s "$SERIAL" reboot
fi
