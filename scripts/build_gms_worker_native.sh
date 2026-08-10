#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRATE_ROOT="${PROJECT_ROOT}/tools/gms-worker-native"
TARGET="${GMS_WORKER_NATIVE_TARGET:-}"

if ! command -v cargo >/dev/null 2>&1; then
    echo "cargo is required to build GMS Worker native tools" >&2
    exit 1
fi

cargo_args=(--release --locked)
target_root="${CRATE_ROOT}/target/release"
dist_name="$(uname -m)"
if [[ -n "${TARGET}" ]]; then
    cargo_args+=(--target "${TARGET}")
    target_root="${CRATE_ROOT}/target/${TARGET}/release"
    dist_name="${TARGET}"
fi

(
    cd "${CRATE_ROOT}"
    if [[ -z "${TARGET}" && "$(uname -s)" == "Linux" && \
          "${GMS_WORKER_NATIVE_STATIC:-true}" =~ ^(1|true|yes|on)$ ]]; then
        for binary in gms-process-inventory gms-usbip-control; do
            cargo rustc "${cargo_args[@]}" --bin "${binary}" -- \
                -C target-feature=+crt-static
        done
    else
        cargo build "${cargo_args[@]}"
    fi
)

dist_root="${CRATE_ROOT}/dist/${dist_name}"
mkdir -p "${dist_root}"
for binary in gms-process-inventory gms-usbip-control; do
    install -m 755 "${target_root}/${binary}" "${dist_root}/${binary}"
done
(
    cd "${dist_root}"
    sha256sum gms-process-inventory gms-usbip-control > SHA256SUMS
)
echo "GMS Worker native tools built in ${dist_root}"
