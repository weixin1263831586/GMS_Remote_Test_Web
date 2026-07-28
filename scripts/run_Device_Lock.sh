#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <serial> <oem-command>" >&2
    exit 1
fi

SERIAL="$1"
OEM_COMMAND="$2"

fastboot -s "$SERIAL" oem "$OEM_COMMAND"
fastboot -s "$SERIAL" reboot fastboot || true
fastboot -s "$SERIAL" reboot
