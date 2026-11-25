#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 PROJECT_ROOT RUN_HOME" >&2
    exit 2
fi

PROJECT_ROOT="$(readlink -f "$1")"
RUN_HOME="$(readlink -f "$2")"
HOST_TOOLS_METADATA="${PROJECT_ROOT}/tools/GMS-Host-Tools"
HOST_TOOLS="${HOST_TOOLS_METADATA}"
COMPILED_RUNTIME=""
if [[ -x "${PROJECT_ROOT}/gms-runtime" ]]; then
    COMPILED_RUNTIME="${PROJECT_ROOT}/gms-runtime"
    HOST_TOOLS="${GMS_DATA_ROOT:-${PROJECT_ROOT}/data}/host-tools"
elif [[ -x "${PROJECT_ROOT}/compiled-runtime/gms-runtime" ]]; then
    COMPILED_RUNTIME="${PROJECT_ROOT}/compiled-runtime/gms-runtime"
fi
SOFTWARE_ROOT="${RUN_HOME}/Software"

[[ -f "${PROJECT_ROOT}/scripts/deployment/worker/prepare_gms_host_tools.sh" ]] || {
    echo "Invalid project root: ${PROJECT_ROOT}" >&2
    exit 2
}
if [[ "${GMS_WORKER_SOFTWARE_PREPARED:-0}" != 1 || ! -x "${HOST_TOOLS}/jdk-11/bin/java" || ! -s "${HOST_TOOLS}/jdk-11/lib/modules" || ! -f "${HOST_TOOLS}/platform-tools-gms-linux.zip" ]]; then
    GMS_HOST_TOOLS_ROOT="${HOST_TOOLS}" bash "${PROJECT_ROOT}/scripts/deployment/worker/prepare_gms_host_tools.sh" "${PROJECT_ROOT}"
fi
[[ -x "${HOST_TOOLS}/jdk-11/bin/java" && -s "${HOST_TOOLS}/jdk-11/lib/modules" ]] || {
    echo "jdk-11 not provisioned; run scripts/deployment/worker/prepare_gms_host_tools.sh (pinned Temurin 11 download, see tools/GMS-Host-Tools/manifest.json)" >&2
    exit 1
}
[[ -f "${HOST_TOOLS}/platform-tools-gms-linux.zip" ]] || {
    echo "platform-tools not provisioned; check GMS_HOST_TOOLS_PLATFORM_URL" >&2
    exit 1
}
[[ -f "${PROJECT_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4.tar.gz" ]] || {
    echo "Missing bundled scrcpy tarball" >&2
    exit 1
}

mkdir -p "${SOFTWARE_ROOT}/GMS-Host-Tools"
rm -rf "${SOFTWARE_ROOT}/jdk-11" "${SOFTWARE_ROOT}/platform-tools"
rsync -a "${HOST_TOOLS}/jdk-11/" "${SOFTWARE_ROOT}/jdk-11/"


if [[ -n "${COMPILED_RUNTIME}" ]]; then
    "${COMPILED_RUNTIME}" extract-zip "${HOST_TOOLS}/platform-tools-gms-linux.zip" "${SOFTWARE_ROOT}"
else
    python3 "${PROJECT_ROOT}/scripts/deployment/worker/extract_zip_preserve_mode.py" \
        "${HOST_TOOLS}/platform-tools-gms-linux.zip" "${SOFTWARE_ROOT}"
fi
rm -rf "${SOFTWARE_ROOT}/scrcpy-linux-x86_64-v3.3.4"
tar -xzf "${PROJECT_ROOT}/tools/scrcpy-linux-x86_64-v3.3.4.tar.gz" \
    -C "${SOFTWARE_ROOT}"
install -m 755 "${HOST_TOOLS_METADATA}/env.sh" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/env.sh"
install -m 755 "${HOST_TOOLS_METADATA}/verify.sh" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/verify.sh"
install -m 644 "${HOST_TOOLS_METADATA}/README.md" \
    "${SOFTWARE_ROOT}/GMS-Host-Tools/README.md"

# Credentials never come from the repository tree (they are untracked and
# forbidden in Git); the operator must supply GMS_GTS_CREDENTIAL_FILE.
credential="${GMS_GTS_CREDENTIAL_FILE:-}"
if [[ -n "${credential}" && -f "${credential}" ]]; then
    if [[ ! "${credential}" -ef "${SOFTWARE_ROOT}/gts-rockchip.json" ]]; then
        install -m 600 "${credential}" "${SOFTWARE_ROOT}/gts-rockchip.json"
    else
        chmod 600 "${SOFTWARE_ROOT}/gts-rockchip.json"
    fi
elif [[ ! -f "${SOFTWARE_ROOT}/gts-rockchip.json" ]]; then
    echo "GTS credential is not configured" >&2
    exit 1
fi

if [[ -n "${COMPILED_RUNTIME}" ]]; then
    "${COMPILED_RUNTIME}" configure-host-tools "${RUN_HOME}/.bashrc"
else
    python3 "${PROJECT_ROOT}/scripts/deployment/worker/configure_gms_host_tools.py" "${RUN_HOME}/.bashrc"
fi
GMS_ADB_PROXY_INSTALL_DIR="${RUN_HOME}/.local/bin" \
    "${PROJECT_ROOT}/scripts/native/install_adbproxy_rs.sh"
install -d -m 700 "${RUN_HOME}/.local/state/gms-adbproxy"
"${SOFTWARE_ROOT}/GMS-Host-Tools/verify.sh"
