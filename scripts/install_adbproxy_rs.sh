#!/usr/bin/env bash
set -euo pipefail

# The Controller builds this package from the pinned local source. Installation
# is intentionally offline so remote Workers never depend on an external host.
ADBPROXY_VERSION="0.4.5"
ADBPROXY_ARCHIVE="adbproxy-rs-linux-x86_64-musl.tar.gz"
ADBPROXY_INSTALL_DIR="${GMS_ADB_PROXY_INSTALL_DIR:-${HOME}/.local/bin}"
ADBPROXY_SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADBPROXY_PROJECT_ROOT="$(cd "${ADBPROXY_SCRIPT_ROOT}/.." && pwd)"
ADBPROXY_BUNDLED_PACKAGE="${ADBPROXY_PROJECT_ROOT}/tools/adbproxy-rs/dist/${ADBPROXY_ARCHIVE}"
ADBPROXY_BUNDLED_CHECKSUM="${ADBPROXY_BUNDLED_PACKAGE}.sha256"

case "$(uname -s):$(uname -m)" in
    Linux:x86_64|Linux:amd64) ;;
    *)
        echo "adbproxy-rs ${ADBPROXY_VERSION} only publishes a Linux x86_64 binary" >&2
        exit 1
        ;;
esac

for command_name in sha256sum tar; do
    command -v "${command_name}" >/dev/null 2>&1 || {
        echo "Missing required command: ${command_name}" >&2
        exit 1
    }
done

ADBPROXY_TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "${ADBPROXY_TEMP_ROOT}"' EXIT
ADBPROXY_PACKAGE="${ADBPROXY_TEMP_ROOT}/${ADBPROXY_ARCHIVE}"

if [[ -n "${GMS_ADB_PROXY_ARCHIVE_FILE:-}" ]]; then
    ADBPROXY_INPUT_PACKAGE="${GMS_ADB_PROXY_ARCHIVE_FILE}"
    ADBPROXY_EXPECTED_SHA256="${GMS_ADB_PROXY_ARCHIVE_SHA256:-}"
    [[ -n "${ADBPROXY_EXPECTED_SHA256}" ]] || {
        echo "GMS_ADB_PROXY_ARCHIVE_SHA256 is required with a custom archive" >&2
        exit 1
    }
else
    ADBPROXY_INPUT_PACKAGE="${ADBPROXY_BUNDLED_PACKAGE}"
    [[ -f "${ADBPROXY_BUNDLED_CHECKSUM}" ]] || {
        echo "Bundled adbproxy-rs checksum is missing: ${ADBPROXY_BUNDLED_CHECKSUM}" >&2
        exit 1
    }
    ADBPROXY_EXPECTED_SHA256="$(awk 'NR == 1 {print $1}' "${ADBPROXY_BUNDLED_CHECKSUM}")"
fi

[[ -f "${ADBPROXY_INPUT_PACKAGE}" ]] || {
    echo "Bundled adbproxy-rs package is missing: ${ADBPROXY_INPUT_PACKAGE}" >&2
    echo "Run scripts/build_adbproxy_rs.sh on the Controller first." >&2
    exit 1
}
[[ "${ADBPROXY_EXPECTED_SHA256}" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "Invalid adbproxy-rs SHA256 value" >&2
    exit 1
}
install -m 600 "${ADBPROXY_INPUT_PACKAGE}" "${ADBPROXY_PACKAGE}"
printf '%s  %s\n' "${ADBPROXY_EXPECTED_SHA256}" "${ADBPROXY_PACKAGE}" |
    sha256sum --check -
tar -xzf "${ADBPROXY_PACKAGE}" -C "${ADBPROXY_TEMP_ROOT}"
install -d -m 755 "${ADBPROXY_INSTALL_DIR}"
for binary_name in adb-proxy adb-hub adb-hubd; do
    binary_path="${ADBPROXY_TEMP_ROOT}/${binary_name}"
    [[ -f "${binary_path}" ]] || {
        echo "Release archive is missing ${binary_name}" >&2
        exit 1
    }
    install -m 755 "${binary_path}" "${ADBPROXY_INSTALL_DIR}/${binary_name}"
done

installed_version="$("${ADBPROXY_INSTALL_DIR}/adb-proxy" --version)"
[[ "${installed_version}" == *"${ADBPROXY_VERSION}"* ]] || {
    echo "Installed adb-proxy version mismatch: ${installed_version}" >&2
    exit 1
}
printf '%s\n' "${installed_version}"
