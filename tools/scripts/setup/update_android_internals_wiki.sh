#!/usr/bin/env bash
# Clone or fast-forward the local android-internals-wiki checkout.
#
# tools/android-internals-wiki/ is a local clone of a third-party knowledge
# base — it is gitignored in this repository and must never be committed.
# Run this script after a fresh clone (or to update an existing checkout).
#
# Remote naming (single canonical scheme, see tools/README.md provenance):
#   upstream = Gracker/android-internals-wiki  — pull updates from here
#   origin   = weixin1263831586 fork           — push local optimizations
#
# 保持单一 remote 语义：upstream 始终用于拉取 Gracker，origin 始终指向
# 可推送的项目 fork，避免 fresh clone 与 existing-clone 产生不同布局。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../../.." && pwd -P)"
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
else
    echo "[INFO] Cloning ${UPSTREAM_URL} into ${WIKI_DIR}"
    git clone --origin upstream "${UPSTREAM_URL}" "${WIKI_DIR}"
fi

# 只有在 clone 已存在后才访问 remote；这也保证 fresh-clone 分支可达。
ensure_remote upstream "${UPSTREAM_URL}"
ensure_remote origin "${FORK_URL}"
current_upstream="$(git -C "${WIKI_DIR}" remote get-url upstream)"
if [ "${current_upstream}" != "${UPSTREAM_URL}" ]; then
    git -C "${WIKI_DIR}" remote set-url upstream "${UPSTREAM_URL}"
    echo "[INFO] Normalized remote upstream -> ${UPSTREAM_URL}"
fi
current_origin="$(git -C "${WIKI_DIR}" remote get-url origin)"
if [ "${current_origin}" != "${FORK_URL}" ]; then
    git -C "${WIKI_DIR}" remote set-url origin "${FORK_URL}"
    echo "[INFO] Normalized remote origin -> ${FORK_URL}"
fi

git -C "${WIKI_DIR}" fetch upstream
git -C "${WIKI_DIR}" remote set-head upstream --auto >/dev/null
upstream_head="$(git -C "${WIKI_DIR}" symbolic-ref --short refs/remotes/upstream/HEAD)"
git -C "${WIKI_DIR}" merge --ff-only "${upstream_head}"

echo "[OK] android-internals-wiki ready: ${WIKI_DIR}"
