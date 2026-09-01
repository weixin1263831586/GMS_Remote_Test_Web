#!/bin/bash

GMS_SOFTWARE_ROOT="${GMS_SOFTWARE_ROOT:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

export JAVA_HOME="${GMS_SOFTWARE_ROOT}/jdk-11"
# The bundled JDK 11 has no separate jre/ directory.
export JRE_HOME="${JAVA_HOME}"
export PATH="${GMS_SOFTWARE_ROOT}/platform-tools:${JAVA_HOME}/bin:${PATH}"

# aapt/aapt2 belong to Android Build-Tools, not Platform-Tools. Operators can
# expose an independently installed Build-Tools directory when needed.
if [[ -n "${GMS_ANDROID_BUILD_TOOLS_DIR:-}" && \
        -d "${GMS_ANDROID_BUILD_TOOLS_DIR}" ]]; then
    export PATH="${GMS_ANDROID_BUILD_TOOLS_DIR}:${PATH}"
fi

# Google/APE credentials never live in the repository or this bundle.
# The Worker installer copies the operator-supplied service-account file to
# ${SOFTWARE_ROOT}/gts-rockchip.json (mode 0600); APE_API_KEY only points at
# that deployed copy when it exists.
if [[ -f "${GMS_SOFTWARE_ROOT}/gts-rockchip.json" ]]; then
    export APE_API_KEY="${GMS_SOFTWARE_ROOT}/gts-rockchip.json"
fi
