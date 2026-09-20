#!/usr/bin/env bash
# Clone or fast-forward the local android-internals-wiki checkout.
#
# tools/android-internals-wiki/ is a local clone of a third-party knowledge
# base — it is gitignored in this repository and must never be committed.
# Run this script after a fresh clone (or to update an existing checkout).
#
# Remotes (see tools/README.md provenance table):
#   origin   = upstream knowledge base (Gracker) — pull updates from here
#   fork     = GMS fork (weixin1263831586) — push local optimizations here
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
WIKI_DIR="${REPO_DIR}/tools/android-internals-wiki"
UPSTREAM_URL="https://github.com/Gracker/android-internals-wiki"
FORK_URL="https://github.com/weixin1263831586/android-internals-wiki"

ensure_remote() {
    local name="$1" url="$2"
    if ! git -C "${WIKI_DIR}" remote get-url "${name}" >/dev/null 2>&1; then
        git -C "${WIKI_DIR}" remote add "${name}" "${url}"
        echo "[INFO] Added remote ${name} -> ${url}"
    fi
}

if [ -d "${WIKI_DIR}/.git" ]; then
    echo "[INFO] Updating existing clone at ${WIKI_DIR}"
    ensure_remote upstream "${UPSTREAM_URL}"
    ensure_remote fork "${FORK_URL}"
    git -C "${WIKI_DIR}" pull --ff-only upstream
else
    echo "[INFO] Cloning ${UPSTREAM_URL} into ${WIKI_DIR}"
    git clone "${UPSTREAM_URL}" "${WIKI_DIR}"
    ensure_remote fork "${FORK_URL}"
fi

echo "[OK] android-internals-wiki ready: ${WIKI_DIR}"
