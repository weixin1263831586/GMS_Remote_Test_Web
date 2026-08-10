#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 INSTALL_BIN_DIR" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRATE_ROOT="${PROJECT_ROOT}/tools/gms-worker-native"
INSTALL_BIN_DIR="$1"
ARCH="$(uname -m)"
DIST_ROOT="${CRATE_ROOT}/dist/${ARCH}"

if [[ ! -x "${DIST_ROOT}/gms-process-inventory" || \
      ! -x "${DIST_ROOT}/gms-usbip-control" ]]; then
    echo "Required prebuilt native Worker tools are unavailable: ${DIST_ROOT}" >&2
    exit 1
fi

if [[ ! -f "${DIST_ROOT}/SHA256SUMS" ]]; then
    echo "Native Worker checksum manifest is missing: ${DIST_ROOT}/SHA256SUMS" >&2
    exit 1
fi
(cd "${DIST_ROOT}" && sha256sum -c SHA256SUMS)
mkdir -p "${INSTALL_BIN_DIR}"
install -m 755 "${DIST_ROOT}/gms-process-inventory" \
    "${INSTALL_BIN_DIR}/gms-process-inventory"
install -m 755 "${DIST_ROOT}/gms-usbip-control" \
    "${INSTALL_BIN_DIR}/gms-usbip-control"
