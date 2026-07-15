#!/bin/bash

GMS_SOFTWARE_ROOT="${GMS_SOFTWARE_ROOT:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

export JAVA_HOME="${GMS_SOFTWARE_ROOT}/jdk-11"
# The bundled JDK 11 has no separate jre/ directory.
export JRE_HOME="${JAVA_HOME}"
export PATH="${GMS_SOFTWARE_ROOT}/platform-tools:${JAVA_HOME}/bin:${PATH}"

if [[ -f "${GMS_SOFTWARE_ROOT}/gts-rockchip.json" ]]; then
    export APE_API_KEY="${GMS_SOFTWARE_ROOT}/gts-rockchip.json"
fi
