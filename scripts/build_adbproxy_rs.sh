#!/usr/bin/env bash
set -euo pipefail

# Build the pinned adbproxy-rs source once on the Controller. Workers receive
# only the resulting static Linux package and never need GitHub or Rust.
ADBPROXY_VERSION="0.4.5"
ADBPROXY_SOURCE_COMMIT="f2beb4ff1bece8ab8f5d63c04dbfd6bf90aae8ee"
ADBPROXY_SOURCE_SHA256="347a1885fcd36cc721287d1f124370dacef8e2e1e2649d4f6c73516a87bf4d06"
ADBPROXY_TARGET="x86_64-unknown-linux-musl"
ADBPROXY_BUILDER_IMAGE="${GMS_ADB_PROXY_BUILDER_IMAGE:-rust:1.88-bookworm}"

ADBPROXY_SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADBPROXY_PROJECT_ROOT="$(cd "${ADBPROXY_SCRIPT_ROOT}/.." && pwd)"
ADBPROXY_SOURCE_ARCHIVE="${ADBPROXY_PROJECT_ROOT}/tools/adbproxy-rs/adbproxy-rs-v${ADBPROXY_VERSION}-source.tar.gz"
ADBPROXY_DIST_ROOT="${ADBPROXY_PROJECT_ROOT}/tools/adbproxy-rs/dist"
ADBPROXY_PACKAGE_NAME="adbproxy-rs-linux-x86_64-musl.tar.gz"
ADBPROXY_PACKAGE="${ADBPROXY_DIST_ROOT}/${ADBPROXY_PACKAGE_NAME}"
ADBPROXY_CHECKSUM="${ADBPROXY_PACKAGE}.sha256"

for command_name in sha256sum tar; do
    command -v "${command_name}" >/dev/null 2>&1 || {
        echo "Missing required command: ${command_name}" >&2
        exit 1
    }
done

[[ -f "${ADBPROXY_SOURCE_ARCHIVE}" ]] || {
    echo "Pinned adbproxy-rs source archive is missing: ${ADBPROXY_SOURCE_ARCHIVE}" >&2
    exit 1
}
printf '%s  %s\n' "${ADBPROXY_SOURCE_SHA256}" "${ADBPROXY_SOURCE_ARCHIVE}" |
    sha256sum --check -

ADBPROXY_BUILD_ROOT="$(mktemp -d)"
trap 'rm -rf -- "${ADBPROXY_BUILD_ROOT}"' EXIT
tar -xzf "${ADBPROXY_SOURCE_ARCHIVE}" -C "${ADBPROXY_BUILD_ROOT}"
ADBPROXY_SOURCE_ROOT="${ADBPROXY_BUILD_ROOT}/adbproxy-rs-v${ADBPROXY_VERSION}"
[[ -f "${ADBPROXY_SOURCE_ROOT}/Cargo.lock" ]] || {
    echo "Source archive is missing Cargo.lock" >&2
    exit 1
}

build_with_local_rust() {
    rustup target add "${ADBPROXY_TARGET}"
    (
        cd "${ADBPROXY_SOURCE_ROOT}"
        cargo test --locked
        cargo build --locked --release --bins --target "${ADBPROXY_TARGET}"
    )
}

build_with_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo "Rust/musl toolchain is unavailable and Docker is not installed" >&2
        exit 1
    }
    docker run --rm \
        -v "${ADBPROXY_BUILD_ROOT}:/work" \
        -w "/work/adbproxy-rs-v${ADBPROXY_VERSION}" \
        "${ADBPROXY_BUILDER_IMAGE}" \
        bash -ceu '
            trap '"'"'chown -R '"$(id -u):$(id -g)"' /work'"'"' EXIT
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends musl-tools
            rustup target add x86_64-unknown-linux-musl
            cargo test --locked
            cargo build --locked --release --bins --target x86_64-unknown-linux-musl
        '
}

if command -v cargo >/dev/null 2>&1 &&
    command -v rustup >/dev/null 2>&1 &&
    command -v musl-gcc >/dev/null 2>&1; then
    build_with_local_rust
else
    build_with_docker
fi

ADBPROXY_BINARY_ROOT="${ADBPROXY_SOURCE_ROOT}/target/${ADBPROXY_TARGET}/release"
ADBPROXY_STAGE="${ADBPROXY_BUILD_ROOT}/package"
install -d -m 755 "${ADBPROXY_STAGE}" "${ADBPROXY_DIST_ROOT}"
for binary_name in adb-proxy adb-hub adb-hubd; do
    binary_path="${ADBPROXY_BINARY_ROOT}/${binary_name}"
    [[ -x "${binary_path}" ]] || {
        echo "Build output is missing ${binary_name}" >&2
        exit 1
    }
    if command -v ldd >/dev/null 2>&1 &&
        ! ldd "${binary_path}" 2>&1 |
            grep -Eq 'not a dynamic executable|statically linked'; then
        echo "${binary_name} is not statically linked" >&2
        exit 1
    fi
    install -m 755 "${binary_path}" "${ADBPROXY_STAGE}/${binary_name}"
done

{
    printf 'project=https://github.com/Ken-u/adbproxy-rs\n'
    printf 'version=%s\n' "${ADBPROXY_VERSION}"
    printf 'source_commit=%s\n' "${ADBPROXY_SOURCE_COMMIT}"
    printf 'source_sha256=%s\n' "${ADBPROXY_SOURCE_SHA256}"
    printf 'target=%s\n' "${ADBPROXY_TARGET}"
} > "${ADBPROXY_STAGE}/BUILDINFO"

tar -C "${ADBPROXY_STAGE}" -czf "${ADBPROXY_PACKAGE}" .
package_sha256="$(sha256sum "${ADBPROXY_PACKAGE}" | awk '{print $1}')"
printf '%s  %s\n' "${package_sha256}" "${ADBPROXY_PACKAGE_NAME}" > "${ADBPROXY_CHECKSUM}"

for binary_name in adb-proxy adb-hub adb-hubd; do
    "${ADBPROXY_STAGE}/${binary_name}" --version
done
printf 'Built %s\nSHA256 %s\n' "${ADBPROXY_PACKAGE}" "${package_sha256}"
