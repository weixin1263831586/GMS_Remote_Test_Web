"""Redmine 证据快照抓取服务（只读 evidence pipeline）。

本模块与看板摘要
通道（工单摘要的 2,000 字符截断）完全分离：

- 直接请求 Redmine REST ``/issues/{id}.json?include=...``，原样保存返回的
  UTF-8 JSON 字节并计算 SHA-256；
- 从原始响应构建规范化 manifest（不截断任何 journal notes）；
- 按 download 策略受控下载附件原件（流式、限重定向、逐附件审计）；
- 文本/图片生成派生物，但派生失败绝不影响原件保存。

对 Redmine 的所有请求都是 GET；认证使用 owner runtime config 中的
``redmine_auth``（API Key 优先，Basic 兼容）。凭据只进入请求头，不进入日志、
异常文本或任何持久化字段。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import config_manager as redmine_config_manager
from .store import owner_evidence_store


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- limits

DOWNLOAD_POLICIES = ("none", "analyzable", "all")

SNAPSHOT_ISSUE_TIMEOUT_SECONDS = 30
ATTACHMENT_CONNECT_TIMEOUT_SECONDS = 10
ATTACHMENT_READ_TIMEOUT_SECONDS = 120
MAX_REDIRECTS = 3
# 派生并发限制只控制内存/CPU，不限制成员大小、数量或完整处理时间。
DERIVE_CONCURRENCY = 4


def _env_limit(name: str, default: int, minimum: int = 1) -> int:
    """资源预算取环境变量覆盖值；非法或非正值回退默认。"""
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return max(minimum, value) if value > 0 else default


# 资源边界（可配置硬上限）：全文取证能力保留，但磁盘/CPU/解压展开量
# 必须有可配置硬上限。附件按声明大小预检 + 下载流内双保险；派生文本按
# 字符预算截停；RAR/7z 外部解包进程受时长限制。触顶一律走 error note +
# partial/missing 显性化（未扫描的部分绝不当作不存在），而非静默截断。
MAX_ATTACHMENT_BYTES = _env_limit("GMS_EVIDENCE_MAX_ATTACHMENT_BYTES", 2 * 1024**3)
DERIVED_TEXT_MAX_CHARS = _env_limit("GMS_EVIDENCE_DERIVED_TEXT_MAX_CHARS", 256_000_000)
EXTRACT_TIMEOUT_SECONDS = _env_limit("GMS_EVIDENCE_EXTRACT_TIMEOUT_SECONDS", 1800)
# OCR 单次调用时长预算（0 = 不限时是明确的 CPU 风险，必须兜底）；
# tesseract 超时抛 RuntimeError，由派生层降级 derived_status=missing。
OCR_TIMEOUT_SECONDS = _env_limit("GMS_EVIDENCE_OCR_TIMEOUT_SECONDS", 120)
# OCR 单图总耗时预算：大图 tile 化后逐片 120s 的累计可达小时级；
# 超过总预算停止剩余 tile，已识别文本保留并显式标注截断。
OCR_TOTAL_TIMEOUT_SECONDS = _env_limit("GMS_EVIDENCE_OCR_TOTAL_TIMEOUT_SECONDS", 300)
OCR_MAX_IMAGE_PIXELS = _env_limit("GMS_EVIDENCE_OCR_MAX_IMAGE_PIXELS", 16_000_000)

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

# 派生文本里的成员分隔标记（行级），search 用它还原
# ``attachment:<file>.zip!/<member>`` 引用与行号。
_ZIP_MEMBER_MARKER_RE = re.compile(r"^<<<zip-member:(.*?)>>>$")
_ZIP_MEMBER_V2_RE = re.compile(
    r"(?m)^<<<zip-member-v2:([A-Za-z0-9_-]+):([0-9]+)>>>\n"
)


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
            from ..utils import create_basic_auth_header

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


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------- entrypoints

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
