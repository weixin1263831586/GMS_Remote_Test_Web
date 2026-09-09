"""Redmine 证据快照抓取服务（只读 evidence pipeline）。

2026-09-08 计划：redmine-cli-agent-implementation-plan.md §7。本模块与看板摘要
通道（analysis_issue.py 的 2,000 字符截断）完全分离：

- 直接请求 Redmine REST ``/issues/{id}.json?include=...``，原样保存返回的
  UTF-8 JSON 字节并计算 SHA-256；
- 从原始响应构建规范化 manifest（不截断任何 journal notes）；
- 按 download 策略受控下载附件原件（流式、限重定向、限大小、逐附件审计）；
- 文本/图片生成派生物，但派生失败绝不影响原件保存。

对 Redmine 的所有请求都是 GET；认证使用 owner runtime config 中的
``redmine_auth``（API Key 优先，Basic 兼容）。凭据只进入请求头，不进入日志、
异常文本或任何持久化字段。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import config_manager as redmine_config_manager
from .evidence_store import owner_evidence_store


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- limits

DOWNLOAD_POLICIES = ("none", "analyzable", "all")

SNAPSHOT_ISSUE_TIMEOUT_SECONDS = 30
ATTACHMENT_CONNECT_TIMEOUT_SECONDS = 10
ATTACHMENT_READ_TIMEOUT_SECONDS = 120
ATTACHMENT_MAX_BYTES = 256 * 1024 * 1024
SNAPSHOT_MAX_TOTAL_DOWNLOAD_BYTES = 1024 * 1024 * 1024
SNAPSHOT_MAX_ATTACHMENTS = 200
MAX_REDIRECTS = 3
SNAPSHOT_TTL_SECONDS = 300

TEXT_KIND_EXTENSIONS = {
    ".txt", ".log", ".xml", ".json", ".html", ".htm", ".csv", ".md",
    ".cfg", ".conf", ".ini", ".properties", ".java", ".py", ".sh", ".js",
    ".ts", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".kt", ".yml", ".yaml",
}
IMAGE_KIND_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp",
}
ANALYZABLE_KINDS = {"text", "log", "image", "pdf", "archive", "apk"}

_TEXT_ENCODINGS = ("utf-8", "gb18030", "utf-16", "latin-1")
_DERIVED_TEXT_MAX_BYTES = 64 * 1024 * 1024
_ISSUE_ID_OR_URL_RE = re.compile(r"^/issues/(\d+)$")


class EvidenceError(Exception):
    """抓取过程中的可审计失败（不含凭据）。"""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class EvidenceAuthError(EvidenceError):
    def __init__(self, message: str):
        super().__init__(message, status_code=401)


class EvidencePermissionError(EvidenceError):
    def __init__(self, message: str):
        super().__init__(message, status_code=403)


class EvidenceNotFoundError(EvidenceError):
    def __init__(self, message: str):
        super().__init__(message, status_code=404)


def parse_issue_ref(value: str, base_url: str) -> int:
    """接受纯数字 ID 或与配置 Redmine 同源的 /issues/<digits> URL。"""
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    parsed = urlparse(text)
    base = urlparse(str(base_url or ""))
    if parsed.scheme in {"http", "https"} and parsed.netloc == base.netloc:
        match = _ISSUE_ID_OR_URL_RE.match(parsed.path)
        if match:
            return int(match.group(1))
    raise EvidenceError(
        "issue 引用必须是纯数字 ID 或与配置 Redmine 同源的 /issues/<id> URL",
        status_code=422,
    )


# ---------------------------------------------------------------- credential

@dataclass
class _OwnerCredentials:
    api_key: str = ""
    username: str = ""
    password: str = ""

    def headers(self) -> dict[str, str]:
        if self.api_key:
            return {"X-Redmine-API-Key": self.api_key}
        if self.username and self.password:
            from .utils import create_basic_auth_header

            return create_basic_auth_header(self.username, self.password)
        return {}


def load_owner_credentials(owner_id: str) -> _OwnerCredentials:
    from foundation.secrets import decrypt_secret

    owner_config = redmine_config_manager.for_owner(owner_id)
    runtime_provider = getattr(owner_config, "manager", owner_config)
    saved = runtime_provider.get_runtime_config().get("redmine_auth") or {}

    api_key = ""
    encrypted_key = saved.get("encrypted_api_key")
    if encrypted_key:
        try:
            api_key = decrypt_secret(str(encrypted_key))
        except Exception:
            api_key = ""
    username = str(saved.get("username") or "")
    password = ""
    encrypted_password = saved.get("encrypted_password")
    if encrypted_password:
        try:
            password = decrypt_secret(str(encrypted_password))
        except Exception:
            password = ""
    return _OwnerCredentials(api_key=api_key, username=username, password=password)


def owner_base_url(owner_id: str) -> str:
    manager = redmine_config_manager.for_owner(owner_id)
    return str(manager.get_redmine_base_url() or "").strip().rstrip("/")


# ------------------------------------------------------------------- helpers

def _same_origin(first: str, second: str) -> bool:
    a, b = urlparse(first), urlparse(second)
    return (a.scheme, a.hostname, a.port or 80 if a.scheme == "http" else a.port or 443) == (
        b.scheme, b.hostname, b.port or 80 if b.scheme == "http" else b.port or 443,
    )


def _attachment_path_ok(url: str) -> bool:
    path = urlparse(url).path
    return bool(re.match(r"^/attachments/(download/)?\d+", path))


def _classify_attachment(filename: str, content_type: str) -> str:
    name = str(filename or "").lower()
    mime = str(content_type or "").lower().split(";")[0].strip()
    if name.endswith(".apk"):
        return "apk"
    if mime in IMAGE_MIME_TYPES or Path(name).suffix in IMAGE_KIND_EXTENSIONS:
        return "image"
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if Path(name).suffix in {".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".bz2", ".xz"}:
        return "archive"
    if mime.startswith("text/") or Path(name).suffix in TEXT_KIND_EXTENSIONS:
        return "text" if mime.startswith("text/") or Path(name).suffix != ".log" else "log"
    if name.endswith(".log"):
        return "log"
    if mime == "application/octet-stream":
        # Redmine 经常对日志文件声明 octet-stream；扩展名仍可判定。
        suffix = Path(name).suffix
        if suffix in TEXT_KIND_EXTENSIONS:
            return "log" if suffix == ".log" else "text"
        if suffix in IMAGE_KIND_EXTENSIONS:
            return "image"
        return "binary"
    return "unknown"


def _detect_text_encoding(data: bytes) -> str:
    for encoding in _TEXT_ENCODINGS:
        try:
            data.decode(encoding)
            return encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


@dataclass
class _FetchResult:
    snapshot_id: str = ""
    status: str = "failed"
    errors: list[dict[str, str]] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------- entrypoints

# ``EvidenceFetcher`` lives in ``evidence_fetch.py`` (split to keep both
# modules reviewable); it is re-exported here so the historical import paths
# ``features.redmine.evidence.EvidenceFetcher`` keep working.
from .evidence_fetch import EvidenceFetcher  # noqa: E402


_BACKGROUND_TASKS: set[asyncio.Task] = set()


def start_evidence_fetch(owner_id: str, snapshot: dict[str, Any]) -> asyncio.Task:
    """启动受控后台抓取任务（Controller 生命周期内跟踪）。"""
    fetcher = EvidenceFetcher(owner_id)

    async def _runner() -> None:
        try:
            await fetcher.run(snapshot)
        except EvidenceError as exc:
            logger.warning(
                "Redmine evidence fetch failed (issue=%s): %s",
                snapshot.get("issue_id"), exc,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 后台任务不得向上抛出
            logger.exception("Redmine evidence fetch crashed: %s", exc)
            # 未预期异常也必须把快照推进到 failed 终态，绝不留在 fetching。
            try:
                fetcher.store.update_snapshot(
                    str(snapshot.get("snapshot_id") or ""),
                    status="failed",
                    error_json=[{
                        "stage": "issue",
                        "message": f"抓取任务异常终止: {type(exc).__name__}",
                    }],
                )
            except Exception:  # pragma: no cover - 存储层已不可用时只记日志
                logger.exception(
                    "Failed to mark evidence snapshot %s as failed",
                    snapshot.get("snapshot_id"),
                )

    task = asyncio.create_task(_runner())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


def snapshot_completeness(snapshot: dict[str, Any], artifacts: list[dict[str, Any]] | None = None) -> dict[str, bool]:
    """按计划 §8 计算 completeness，失败绝不能标记 complete。"""
    status = str(snapshot.get("status") or "")
    errors = snapshot.get("errors") or []
    issue_ok = status in {"ready", "partial"} and bool(snapshot.get("content_sha256"))
    journals_ok = issue_ok and int(snapshot.get("journal_count") or 0) >= 0
    policy = str(snapshot.get("download_policy") or "none")
    if policy == "none" or status == "ready":
        downloads_ok = True
    else:
        downloads_ok = False
    return {
        "issue": issue_ok,
        "journals": journals_ok,
        "attachment_metadata": issue_ok,
        "requested_downloads": downloads_ok and not any(
            str(item.get("stage")) == "download" for item in errors
        ),
    }


def wait_for_snapshot(
    owner_id: str, snapshot_id: str, *, timeout_seconds: float = 120.0
) -> dict[str, Any]:
    """同步等待快照到达终态（供阻塞式 API/CLI 使用）。"""
    import time

    store = owner_evidence_store(owner_id)
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = store.get_snapshot(snapshot_id)
        if snapshot is None:
            raise EvidenceNotFoundError("snapshot 不存在")
        if str(snapshot.get("status")) in {"ready", "partial", "failed"}:
            return snapshot
        if time.monotonic() >= deadline:
            return snapshot
        time.sleep(0.2)
