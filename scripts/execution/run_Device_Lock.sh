#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <serial> <oem-command|->" >&2
    exit 1
fi

SERIAL="$1"
OEM_COMMAND="$2"

. "$(dirname -- "${BASH_SOURCE[0]}")/fastboot_common.sh"

# OEM_COMMAND 为 "-" 表示 oem 命令已在 Python 侧执行（含 unrecognized
# 兜底重试），脚本只负责 fastboot 重启衔接。
if [[ "$OEM_COMMAND" != "-" ]]; then
    fastboot -s "$SERIAL" oem "$OEM_COMMAND"
fi
if fastboot -s "$SERIAL" reboot fastboot; then
    wait_for_fastbootd "$SERIAL"
    fastboot -s "$SERIAL" reboot
fi
