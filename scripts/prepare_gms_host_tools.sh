#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 PROJECT_ROOT" >&2
    exit 2
fi

PROJECT_ROOT="$(readlink -f "$1")"
HOST_TOOLS="${PROJECT_ROOT}/tools/GMS-Host-Tools"
JDK_ROOT="${HOST_TOOLS}/jdk-11"
PLATFORM_ARCHIVE="${HOST_TOOLS}/platform-tools-gms-linux.zip"
GOOGLE_PLATFORM_TOOLS_VERSION="37.0.1"
GOOGLE_PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools_r${GOOGLE_PLATFORM_TOOLS_VERSION}-linux.zip"
GOOGLE_PLATFORM_TOOLS_SHA256="d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1"
TEMURIN_JDK_VERSION="11.0.32.1+1"
TEMURIN_JDK_URL="https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.32.1%2B1/OpenJDK11U-jdk_x64_linux_hotspot_11.0.32.1_1.tar.gz"
TEMURIN_JDK_SHA256="5c3f68887c325d36d852ba534303e1f5f1f5cae7d6cc1e951d73e0d8e98a058d"

valid_sha256() {
    [[ "$1" =~ ^[0-9a-fA-F]{64}$ ]]
}

download_verified() {
    local label="$1" url="$2" expected="$3" output="$4"
    local allowed_protocols="=https"
    [[ -n "${url}" ]] || {
        echo "${label} is missing; configure its artifact URL" >&2
        return 1
    }
    valid_sha256 "${expected}" || {
        echo "${label} requires an exact 64-character SHA256" >&2
        return 1
    }
    if [[ "${url}" != https://* ]]; then
        if [[ "${url}" == file://* && "${GMS_HOST_TOOLS_ALLOW_FILE:-0}" == "1" ]]; then
            allowed_protocols="=https,file"
        elif [[ "${url}" == http://* && "${GMS_HOST_TOOLS_ALLOW_HTTP:-0}" == "1" ]]; then
            allowed_protocols="=https,http"
        else
            echo "${label} URL must use HTTPS" >&2
            return 1
        fi
    fi
    local curl_args=(
        --fail --location --silent --show-error
        --proto "${allowed_protocols}" --proto-redir "${allowed_protocols}"
        --output "${output}"
    )
    if [[ -n "${GMS_HOST_TOOLS_CA_CERT:-}" ]]; then
        curl_args+=(--cacert "${GMS_HOST_TOOLS_CA_CERT}")
    fi
    curl "${curl_args[@]}" "${url}"
    printf '%s  %s\n' "${expected,,}" "${output}" | sha256sum --check --status || {
        echo "${label} SHA256 verification failed" >&2
        return 1
    }
}

sha256_matches() {
    local path="$1" expected="$2"
    [[ -f "${path}" ]] || return 1
    printf '%s  %s\n' "${expected,,}" "${path}" \
        | sha256sum --check --status
}

validate_platform_tools_archive() {
    local archive="$1" member
    unzip -tq "${archive}" >/dev/null
    for member in \
        platform-tools/source.properties \
        platform-tools/adb \
        platform-tools/fastboot \
        platform-tools/lib64/libc++.so; do
        unzip -Z1 "${archive}" | grep -Fxq "${member}" || {
            echo "Android platform-tools artifact is missing ${member}" >&2
            return 1
        }
    done
    if unzip -Z1 "${archive}" \
        | grep -Eq '^platform-tools/(aapt|aapt2)$'; then
        echo "Android platform-tools artifact must remain the original Google package; configure aapt/aapt2 separately" >&2
        return 1
    fi
}

mkdir -p "${HOST_TOOLS}"
WORK_DIR="$(mktemp -d "${HOST_TOOLS}/.prepare.XXXXXX")"
trap 'rm -rf "${WORK_DIR}"' EXIT

# 与 platform-tools 相同的解析策略：manifest/脚本内置 Temurin 11 固定版本
# 默认值；环境变量仅作 override，且 URL 与 SHA256 必须成对提供。
if [[ -n "${GMS_HOST_TOOLS_JDK_URL:-}" || \
        -n "${GMS_HOST_TOOLS_JDK_SHA256:-}" ]]; then
    jdk_url="${GMS_HOST_TOOLS_JDK_URL:-}"
    jdk_sha256="${GMS_HOST_TOOLS_JDK_SHA256:-}"
    [[ -n "${jdk_url}" ]] || {
        echo "JDK 11 override requires both URL and SHA256" >&2
        exit 1
    }
else
    jdk_url="${TEMURIN_JDK_URL}"
    jdk_sha256="${TEMURIN_JDK_SHA256}"
fi
valid_sha256 "${jdk_sha256}" || {
    echo "JDK 11 artifact requires an exact 64-character SHA256" >&2
    exit 1
}

if [[ ! -x "${JDK_ROOT}/bin/java" ]]; then
    JDK_ARCHIVE="${WORK_DIR}/jdk.tar.gz"
    download_verified \
        "JDK 11 artifact" \
        "${jdk_url}" \
        "${jdk_sha256}" \
        "${JDK_ARCHIVE}"
    mkdir -p "${WORK_DIR}/jdk-extract"
    tar --extract --gzip --file "${JDK_ARCHIVE}" \
        --directory "${WORK_DIR}/jdk-extract" --no-same-owner --no-same-permissions
    mapfile -t java_bins < <(find "${WORK_DIR}/jdk-extract" -type f -path '*/bin/java')
    [[ ${#java_bins[@]} -eq 1 ]] || {
        echo "JDK artifact must contain exactly one bin/java" >&2
        exit 1
    }
    extracted_jdk="${java_bins[0]%/bin/java}"
    [[ -f "${extracted_jdk}/release" && -d "${extracted_jdk}/legal" ]] || {
        echo "JDK artifact is missing release/legal metadata" >&2
        exit 1
    }
    mkdir -p "${JDK_ROOT}"
    rsync -a --delete "${extracted_jdk}/" "${JDK_ROOT}/"
    java_version_output="$("${JDK_ROOT}/bin/java" -version 2>&1)" || {
        echo "Installed JDK 11 runtime failed to execute; verify the artifact matches this host architecture" >&2
        exit 1
    }
    java_major="$(printf '%s\n' "${java_version_output}" \
        | sed -n 's/[^"]*"\([0-9][0-9]*\).*/\1/p' | head -n1)"
    [[ "${java_major}" == "11" ]] || {
        echo "Installed JDK 11 runtime reports Java major '${java_major:-unknown}', expected 11 (Temurin ${TEMURIN_JDK_VERSION})" >&2
        exit 1
    }
fi

if [[ -n "${GMS_HOST_TOOLS_PLATFORM_URL:-}" || \
        -n "${GMS_HOST_TOOLS_PLATFORM_SHA256:-}" ]]; then
    platform_url="${GMS_HOST_TOOLS_PLATFORM_URL:-}"
    platform_sha256="${GMS_HOST_TOOLS_PLATFORM_SHA256:-}"
    [[ -n "${platform_url}" ]] || {
        echo "Android platform-tools override requires both URL and SHA256" >&2
        exit 1
    }
else
    platform_url="${GOOGLE_PLATFORM_TOOLS_URL}"
    platform_sha256="${GOOGLE_PLATFORM_TOOLS_SHA256}"
fi
valid_sha256 "${platform_sha256}" || {
    echo "Android platform-tools artifact requires an exact 64-character SHA256" >&2
    exit 1
}

if ! sha256_matches "${PLATFORM_ARCHIVE}" "${platform_sha256}"; then
    downloaded_platform="${WORK_DIR}/platform-tools.zip"
    download_verified \
        "Android platform-tools artifact" \
        "${platform_url}" \
        "${platform_sha256}" \
        "${downloaded_platform}"
    validate_platform_tools_archive "${downloaded_platform}"
    install -m 0644 "${downloaded_platform}" "${PLATFORM_ARCHIVE}"
else
    validate_platform_tools_archive "${PLATFORM_ARCHIVE}"
fi

echo "GMS Host Tools artifacts are ready."
