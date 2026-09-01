#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/env.sh"

required=(
    "${JAVA_HOME}/bin/java"
    "${GMS_SOFTWARE_ROOT}/platform-tools/adb"
    "${GMS_SOFTWARE_ROOT}/platform-tools/fastboot"
)
for executable in "${required[@]}"; do
    if [[ ! -x "${executable}" ]]; then
        echo "Missing required GMS host tool: ${executable}" >&2
        exit 1
    fi
done
if [[ ! -f "${GMS_SOFTWARE_ROOT}/gts-rockchip.json" ]]; then
    echo "Missing GTS API credential: ${GMS_SOFTWARE_ROOT}/gts-rockchip.json" >&2
    exit 1
fi
if [[ "$(stat -c '%a' "${GMS_SOFTWARE_ROOT}/gts-rockchip.json")" != "600" ]]; then
    echo "GTS API credential must have mode 0600" >&2
    exit 1
fi

"${JAVA_HOME}/bin/java" -version
python3 --version
"${GMS_SOFTWARE_ROOT}/platform-tools/adb" version
"${GMS_SOFTWARE_ROOT}/platform-tools/fastboot" --version

for optional_tool in aapt aapt2; do
    if optional_path="$(command -v "${optional_tool}" 2>/dev/null)"; then
        "${optional_path}" version
    else
        echo "Optional Android Build-Tools command not configured: ${optional_tool}"
    fi
done

echo "GMS host tools verified under ${GMS_SOFTWARE_ROOT}"
