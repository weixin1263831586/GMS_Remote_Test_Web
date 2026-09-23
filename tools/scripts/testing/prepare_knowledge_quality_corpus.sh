#!/usr/bin/env bash
# Prepare a disposable android-internals-wiki clone + fresh index for the
# knowledge-quality CI job (and any local golden-query replay).
#
# CI 的普通 unit job 不部署 tools/android-internals-wiki（gitignored），
# Golden Query 语料级测试会静默 skip——"看起来在跑 knowledge tests，实际
# 召回质量零验证"（评审 P1）。本脚本为专用 knowledge-quality job 服务：
#
#   clone（pinned revision，fail-closed）→ 临时 DB 全量 reindex → 跑 golden recall
#
# 环境变量：
#   GMS_WIKI_PINNED_REVISION  必填。上游 commit/短 SHA（评审要求 pinned），
#                             pin 变更必须走评审提交。
#   GMS_DATA_ROOT             索引落根（android_internals.sqlite3 写在
#                             $GMS_DATA_ROOT/knowledge/external/ 下；默认
#                             mktemp -d，与本地共享索引隔离）。
# 输出：打印 GMS_DATA_ROOT 实际值，供后续步骤复用。
set -Eeuo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
WIKI_DIR="${GMS_WIKI_DIR:-${REPO_DIR}/tools/android-internals-wiki}"
PINNED="${GMS_WIKI_PINNED_REVISION:-}"
UPSTREAM_URL="${GMS_WIKI_UPSTREAM_URL:-https://github.com/Gracker/android-internals-wiki}"

if [ -z "${PINNED}" ]; then
    echo "[knowledge-quality] GMS_WIKI_PINNED_REVISION is required (pinned upstream revision)" >&2
    exit 1
fi

if [ ! -d "${WIKI_DIR}/.git" ]; then
    echo "[knowledge-quality] cloning ${UPSTREAM_URL} into ${WIKI_DIR}"
    git clone --no-checkout "${UPSTREAM_URL}" "${WIKI_DIR}"
fi

git -C "${WIKI_DIR}" fetch --quiet origin
# pinned revision 不存在 → 立即失败（不允许静默漂移到 HEAD）。
git -C "${WIKI_DIR}" checkout --quiet --force "${PINNED}"
actual="$(git -C "${WIKI_DIR}" rev-parse HEAD)"
case "${actual}" in
    "${PINNED}"|"${PINNED}"*) ;;  # 短 SHA 前缀匹配
    *) echo "[knowledge-quality] HEAD ${actual} does not match pin ${PINNED}" >&2; exit 1 ;;
esac
echo "[knowledge-quality] wiki clone at pinned revision ${actual}"

if [ -z "${GMS_DATA_ROOT:-}" ]; then
    GMS_DATA_ROOT="$(mktemp -d /tmp/gms-knowledge-quality.XXXXXX)"
fi
mkdir -p "${GMS_DATA_ROOT}/knowledge/external"
export GMS_DATA_ROOT

python3 - "${WIKI_DIR}" <<'PY'
"""Fresh full reindex into the temp data root (replaces shared-state risk)."""
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo_root))

from features.knowledge.external.android_internals import (
    AndroidInternalsProvider,
    index_db_path,
)

provider = AndroidInternalsProvider(str(repo_root))
reason = provider.validate()
if reason:
    print(f"[knowledge-quality] clone invalid: {reason}", file=sys.stderr)
    raise SystemExit(1)
result = provider.reindex()
db = index_db_path()
print(
    "[knowledge-quality] reindexed "
    f"{result['doc_count']} pages at revision {result['source_revision'][:12]} "
    f"-> {db}"
)
if not db.is_file() or result["doc_count"] <= 0:
    print("[knowledge-quality] reindex produced no pages", file=sys.stderr)
    raise SystemExit(1)
PY

echo "GMS_DATA_ROOT=${GMS_DATA_ROOT}"
