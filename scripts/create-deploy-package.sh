#!/usr/bin/env bash
set -euo pipefail

# CI 发布包入口：清理配置、排除运行数据并生成校验和与签名。
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${1:-${PROJECT_DIR}/dist}"
VERSION="${2:-$(date +%Y%m%d_%H%M%S)}"

exec "${PROJECT_DIR}/install.sh" package \
    --dist-dir "${DIST_DIR}" \
    --version "${VERSION}"
