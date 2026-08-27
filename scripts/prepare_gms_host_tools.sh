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

mkdir -p "${HOST_TOOLS}"
WORK_DIR="$(mktemp -d "${HOST_TOOLS}/.prepare.XXXXXX")"
trap 'rm -rf "${WORK_DIR}"' EXIT

if [[ ! -x "${JDK_ROOT}/bin/java" ]]; then
    JDK_ARCHIVE="${WORK_DIR}/jdk.tar.gz"
    download_verified \
        "JDK 11 artifact" \
        "${GMS_HOST_TOOLS_JDK_URL:-}" \
        "${GMS_HOST_TOOLS_JDK_SHA256:-}" \
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
fi

if [[ ! -f "${PLATFORM_ARCHIVE}" ]]; then
    downloaded_platform="${WORK_DIR}/platform-tools.zip"
    download_verified \
        "Android platform-tools artifact" \
        "${GMS_HOST_TOOLS_PLATFORM_URL:-}" \
        "${GMS_HOST_TOOLS_PLATFORM_SHA256:-}" \
        "${downloaded_platform}"
    unzip -tq "${downloaded_platform}" >/dev/null
    install -m 0644 "${downloaded_platform}" "${PLATFORM_ARCHIVE}"
fi

echo "GMS Host Tools artifacts are ready."
