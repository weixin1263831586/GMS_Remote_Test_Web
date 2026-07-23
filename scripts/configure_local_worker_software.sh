#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 PROJECT_ROOT RUN_HOME" >&2
    exit 2
fi

PROJECT_ROOT="$(readlink -f "$1")"
RUN_HOME="$(readlink -f "$2")"
HOST_TOOLS="${PROJECT_ROOT}/tools/GMS-Host-Tools"
SOFTWARE_ROOT="${RUN_HOME}/Software"

[[ -f "${PROJECT_ROOT}/app.py" ]] || {
    echo "Invalid project root: ${PROJECT_ROOT}" >&2
    exit 2
}
[[ -d "${HOST_TOOLS}/jdk-11" ]] || {
    echo "Missing bundled jdk-11" >&2
    exit 1
}
[[ -f "${HOST_TOOLS}/platform-tools-gms-linux.zip" ]] || {
    echo "Missing bundled platform-tools archive" >&2
    exit 1
}
[[ -d "${PROJECT_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4" ]] || {
    echo "Missing bundled scrcpy" >&2
    exit 1
}

mkdir -p "${SOFTWARE_ROOT}/GMS-Host-Tools"
rm -rf "${SOFTWARE_ROOT}/jdk-11" "${SOFTWARE_ROOT}/platform-tools"
rsync -a "${HOST_TOOLS}/jdk-11/" "${SOFTWARE_ROOT}/jdk-11/"

python3 - "${SOFTWARE_ROOT}/jdk-11/lib" <<'PY'
import sys
from pathlib import Path

lib_dir = Path(sys.argv[1])
parts = sorted(lib_dir.glob("modules.part.*"))
if parts:
    with (lib_dir / "modules").open("wb") as output:
        for part in parts:
            output.write(part.read_bytes())
    for part in parts:
        part.unlink()
elif not (lib_dir / "modules").is_file():
    raise SystemExit("Missing JDK modules and modules.part.*")
PY

python3 "${PROJECT_ROOT}/scripts/extract_zip_preserve_mode.py" \
    "${HOST_TOOLS}/platform-tools-gms-linux.zip" "${SOFTWARE_ROOT}"
rsync -a --delete \
    "${PROJECT_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4/" \
    "${SOFTWARE_ROOT}/scrcpy-linux-x86_64-v3.3.4/"
install -m 755 "${HOST_TOOLS}/env.sh" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/env.sh"
install -m 755 "${HOST_TOOLS}/verify.sh" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/verify.sh"
install -m 644 "${HOST_TOOLS}/README.md" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/README.md"

credential="${GMS_GTS_CREDENTIAL_FILE:-${HOST_TOOLS}/gts-rockchip.json}"
if [[ -f "${credential}" ]]; then
    install -m 600 "${credential}" "${SOFTWARE_ROOT}/gts-rockchip.json"
elif [[ ! -f "${SOFTWARE_ROOT}/gts-rockchip.json" ]]; then
    echo "GTS credential is not configured" >&2
    exit 1
fi

python3 "${PROJECT_ROOT}/scripts/configure_gms_host_tools.py" \
    "${RUN_HOME}/.bashrc"
"${SOFTWARE_ROOT}/GMS-Host-Tools/verify.sh"
