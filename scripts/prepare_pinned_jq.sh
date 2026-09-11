#!/usr/bin/env bash
# Stage the pinned jq binary served by GET /api/system/tools/jq.
#
# The endpoint hands this file to air-gapped build
# servers, so it must be the official static-ish jq release binary — NOT a
# distro build that dynamically links libjq.so.1 (such a file only works on
# hosts that already have libjq installed, which defeats the purpose).
#
# Provenance (also recorded in .gitignore):
#   https://github.com/jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64
#   SHA-256 020468de7539ce70ef1bceaf7cde2e8c4f2ca6c3afb84642aabc5c97d9fc2a0d
#
# Usage:
#   scripts/prepare_pinned_jq.sh                 # download + verify + install
#   GMS_JQ_SOURCE=/path/to/jq-linux-amd64 scripts/prepare_pinned_jq.sh
#                                              # verify a pre-downloaded file
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${PROJECT_ROOT}/tools/jq-linux-amd64"
JQ_VERSION="1.8.1"
EXPECTED_SHA256="020468de7539ce70ef1bceaf7cde2e8c4f2ca6c3afb84642aabc5c97d9fc2a0d"
URL="https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-amd64"

temporary="$(mktemp "${TARGET}.tmp.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT

if [ -n "${GMS_JQ_SOURCE:-}" ]; then
    echo "[pinned-jq] verifying operator-provided file: ${GMS_JQ_SOURCE}"
    cp -- "${GMS_JQ_SOURCE}" "$temporary"
else
    echo "[pinned-jq] downloading jq ${JQ_VERSION} (linux-amd64) from ${URL}"
    curl -fsSL "$URL" -o "$temporary"
fi

actual="$(sha256sum "$temporary" | awk '{print $1}')"
if [ "$actual" != "$EXPECTED_SHA256" ]; then
    echo "[pinned-jq] SHA-256 mismatch: expected ${EXPECTED_SHA256}, got ${actual}" >&2
    exit 1
fi
chmod 755 "$temporary"

# The served file must actually run on a bare Linux x86-64 host (no libjq).
if ! "$temporary" --version >/dev/null 2>&1; then
    echo "[pinned-jq] staged file fails to execute (--version); refusing to install" >&2
    exit 1
fi
echo "[pinned-jq] staged binary reports: $("$temporary" --version)"

mkdir -p "$(dirname "$TARGET")"
install -m 755 "$temporary" "$TARGET"
echo "[pinned-jq] installed ${TARGET} (jq ${JQ_VERSION}, sha256 ${EXPECTED_SHA256})"
