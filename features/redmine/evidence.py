"""Redmine 证据快照抓取服务（只读 evidence pipeline）。

本模块与看板摘要
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
import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import config_manager as redmine_config_manager
from .evidence_store import owner_evidence_store
from .users import sanitize_owner_id


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
# 派生阶段（OCR/系统压缩包解包）在附件下载完成后的并发上限。与单图
# OCR 超时（evidence_images.OCR_TIMEOUT_SECONDS）共同把最坏派生时间从
# 串行 200×20s 压到 ~17 分钟量级；派生失败只记
# 派生状态，不做超时跳过（保证分析结论准确优先）。
DERIVE_CONCURRENCY = 4

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
# zip 内文本成员的派生文本限制。单成员解压后
# 超过阈值、成员数超上限或总量超上限都会跳过剩余成员（可审计地记录在
# artifact error 里），防止 zip 炸弹拖垮 fetch 或搜索。
ZIP_MEMBER_TEXT_MAX_BYTES = 2 * 1024 * 1024
ZIP_MEMBER_TEXT_MAX_MEMBERS = 50
ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES = 32 * 1024 * 1024
# 派生文本里的成员分隔标记（行级），search 用它还原
# ``attachment:<file>.zip!/<member>`` 引用与行号。
_ZIP_MEMBER_MARKER_RE = re.compile(r"^<<<zip-member:(.*?)>>>$")
_ZIP_MEMBER_V2_RE = re.compile(
    r"(?m)^<<<zip-member-v2:([A-Za-z0-9_-]+):([0-9]+)>>>\n"
)
_ZIP_MEMBER_MARKER_PREFIX = "<<<zip-member:"
_ZIP_MEMBER_MARKER_SUFFIX = ">>>"


def zip_member_marker(name: str) -> str:
    return f"{_ZIP_MEMBER_MARKER_PREFIX}{name}{_ZIP_MEMBER_MARKER_SUFFIX}"


def zip_member_block(name: str, text: str) -> str:
    """Frame one member so member content cannot forge the next marker."""

    encoded_name = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
    return f"<<<zip-member-v2:{encoded_name}:{len(text)}>>>\n{text}"


def split_zip_derived_text(text: str) -> list[tuple[str, str]]:
    """按成员标记切派生文本 → [(member_name, member_text), ...]。"""

    framed: list[tuple[str, str]] = []
    cursor = 0
    saw_framed_marker = False
    while match := _ZIP_MEMBER_V2_RE.search(text, cursor):
        saw_framed_marker = True
        try:
            encoded = match.group(1)
            padding = "=" * (-len(encoded) % 4)
            name = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            length = int(match.group(2))
        except (ValueError, UnicodeDecodeError):
            cursor = match.end()
            continue
        end = match.end() + length
        if end > len(text):
            break
        framed.append((name, text[match.end():end]))
        cursor = end
    if saw_framed_marker:
        return framed

    members: list[tuple[str, list[str]]] = []
    current_name = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        marker = _ZIP_MEMBER_MARKER_RE.match(line)
        if marker:
            if current_name:
                members.append((current_name, current_lines))
            current_name = marker.group(1)
            current_lines = []
        elif current_name:
            current_lines.append(line)
    if current_name:
        members.append((current_name, current_lines))
    return [(name, "\n".join(lines)) for name, lines in members]
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


def preflight_owner_fetch(owner_id: str) -> str:
    """抓取前置校验（在建快照之前确认配置，避免失败快照）。

    在建快照之前确认 owner 已配置 base_url 与 Redmine 凭据，缺失时立即
    抛 ``EvidenceError``（409/401），快速失败且不留 failed 垃圾快照。
    返回校验通过的 base_url（已 rstrip）。

    错误自带修复路径：agent 与 enroll 账号共享 owner 存储，人在 Web UI
    配置即可；凭据为 human-only（见 /config/credentials）。
    """

    base_url = owner_base_url(owner_id)
    if not base_url:
        raise EvidenceError(
            "redmine.base_url 未配置；请在 Web UI『设置』页配置 Redmine 地址后再抓取证据",
            status_code=409,
        )
    credentials = load_owner_credentials(owner_id)
    if not credentials.headers():
        raise EvidenceAuthError(
            "owner 账号未配置 Redmine 凭据；请由 enroll 该 agent 的账号在 Web UI『设置』页"
            "配置用户名/密码或 API Key（agent 与该账号共享 owner 存储），"
            "或由该账号调用 POST /api/redmine-agent/config/credentials；"
            "配置后可用 gms-rt-redmine-credentials-status 验证"
        )
    return base_url


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

# 进程内活跃抓取（sanitize(owner_id), snapshot_id）：重启对账据此区分
# "仍在执行"与"无执行者的遗留中间态"。
_ACTIVE_FETCH_SNAPSHOTS: set[tuple[str, str]] = set()

# 抓取过程的中间态：进程意外退出后，处于这些状态的快照不会再有执行者。
# queued 也纳入对账：快照创建与后台任务启动之间存在窗口，进程在该窗口
# 退出会留下永远无人认领的 queued 记录。
INTERMEDIATE_FETCH_STATUSES = ("queued", "fetching", "downloading")


def reconcile_interrupted_fetches(root: Path | None = None) -> list[dict[str, str]]:
    """启动对账：把无活跃执行者的中间态快照推进到 ``interrupted`` 终态。

    Controller 重启后 asyncio 后台抓取任务不复存在，数据库里残留的
    queued/fetching/downloading 快照会让 wait_for_snapshot 与状态轮询
    永久等待。中断标记保留已下载的附件、issue.json 与派生结果（不删除
    任何证据），重试时重新发起抓取即可；活跃执行者存在的快照不做改动。
    """
    if root is None:
        from foundation.config import settings

        root = settings.data_root / "redmine" / "by_user"
    reconciled: list[dict[str, str]] = []
    root = Path(root)
    if not root.is_dir():
        return reconciled
    from .evidence_store import EvidenceStore

    for owner_dir in sorted(root.iterdir()):
        # 只认已存在的 evidence 存储，绝不因对账创建新库。
        if not (owner_dir / "evidence.sqlite3").is_file():
            continue
        store = EvidenceStore(owner_dir)
        orphans = store.list_snapshots_by_status(set(INTERMEDIATE_FETCH_STATUSES))
        for snapshot in orphans:
            snapshot_id = str(snapshot.get("snapshot_id") or "")
            if (str(owner_dir.name), snapshot_id) in _ACTIVE_FETCH_SNAPSHOTS:
                continue
            store.update_snapshot(
                snapshot_id,
                status="interrupted",
                error_json=[
                    *(snapshot.get("errors") or []),
                    {
                        "stage": "fetch",
                        "message": (
                            "Controller 重启导致抓取中断；已下载附件与派生结果"
                            "已保留，可重新发起抓取"
                        ),
                    },
                ],
            )
            reconciled.append({"owner": str(owner_dir.name), "snapshot_id": snapshot_id})
    return reconciled


def start_evidence_fetch(owner_id: str, snapshot: dict[str, Any]) -> asyncio.Task:
    """启动受控后台抓取任务（Controller 生命周期内跟踪）。"""
    fetcher = EvidenceFetcher(owner_id)
    # 活跃登记：重启对账据此识别"有执行者"的中间态，不做中断标记。
    active_key = (sanitize_owner_id(owner_id), str(snapshot.get("snapshot_id") or ""))
    _ACTIVE_FETCH_SNAPSHOTS.add(active_key)

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

    def _forget(done_task: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(done_task)
        _ACTIVE_FETCH_SNAPSHOTS.discard(active_key)

    task.add_done_callback(_forget)
    return task


def snapshot_completeness(snapshot: dict[str, Any], artifacts: list[dict[str, Any]] | None = None) -> dict[str, bool]:
    """计算 completeness，失败绝不能标记 complete。"""
    status = str(snapshot.get("status") or "")
    errors = snapshot.get("errors") or []
    issue_ok = status in {"ready", "partial"} and bool(snapshot.get("content_sha256"))
    journals_ok = issue_ok and int(snapshot.get("journal_count") or 0) >= 0
    policy = str(snapshot.get("download_policy") or "none")
    if policy == "none" or status == "ready":
        downloads_ok = True
    else:
        downloads_ok = False
    # 派生文本（OCR/压缩包索引/转码）单独计一个维度：派生失败不再把
    # 快照整体拉成 partial，completeness 必须如实反映这部分缺口。
    derived_ok = not any(str(item.get("stage")) == "derive" for item in errors)
    return {
        "issue": issue_ok,
        "journals": journals_ok,
        "attachment_metadata": issue_ok,
        "requested_downloads": downloads_ok and not any(
            str(item.get("stage")) == "download" for item in errors
        ),
        "derived_text": derived_ok,
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
        # interrupted 是重启对账写入的终态：立即返回而不是等满超时。
        # 默认 120s 只是阻塞调用方的上限，不是抓取预算——派生阶段
        # （OCR/压缩包）最坏可达 ~17 分钟，超时返回当前快照，由调用方
        # 按状态轮询或稍后重查，证据不会丢失。
        if str(snapshot.get("status")) in {"ready", "partial", "failed", "interrupted"}:
            return snapshot
        if time.monotonic() >= deadline:
            return snapshot
        time.sleep(0.2)
