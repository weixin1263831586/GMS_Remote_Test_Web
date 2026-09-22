"""只读 SDK 源码 provider。

统一 ``SourceProvider`` 接口；首期实现 ``local_git``：

- provider 与仓库根目录只来自管理员部署配置（config ``sdk_sources.providers``），
  客户端不能提供 base URL 或本地路径；
- revision 必须通过 ``git rev-parse --verify <rev>^{commit}`` 解析成 commit；
- 文件读取使用 ``git show <commit>:<path>``（blob 内容），保证可复现且不读
  工作树未提交内容；
- 结果 ID 为服务端签名的 opaque token（source/commit/path 的 HMAC），后续
  read 不接受自由路径；
- 拒绝 ``.git`` 私有数据与绝对路径/``..``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html as html_module
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 30
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_SEARCH_FILES = 20000
MAX_SEARCH_SCAN_BYTES = 512 * 1024 * 1024
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
READ_MAX_LINES = 4000

# codesearch（OpenGrok 1.14+ /api/v1 REST）provider 参数。
CODESEARCH_TIMEOUT_SECONDS = 30
CODESEARCH_MAX_HITS_PER_FILE = 3

# result_id 是自包含的 opaque token：``src1_<base64url(payload)>_<hmac16>``。
# payload 内编码 source_id/commit/path；HMAC 防篡改，后续 read 只接受
# result_id，不再接受自由 path/commit。
RESULT_ID_PREFIX = "src1_"
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")


class SourceProviderError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _git(repo_root: str, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    """以参数数组调用 git；任何输出异常都转成 SourceProviderError。"""
    try:
        completed = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise SourceProviderError(
            f"git 调用失败: {type(exc).__name__}", status_code=502
        ) from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip().splitlines()
        detail = stderr[0][:200] if stderr else f"git exit {completed.returncode}"
        raise SourceProviderError(f"git: {detail}", status_code=422)
    return completed.stdout


def _safe_repo_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or "\x00" in normalized:
        raise SourceProviderError("非法路径", status_code=422)
    parts = [part for part in normalized.split("/") if part not in (".", "")]
    if ".." in parts or ".git" in parts or re.match(r"^[A-Za-z]:", normalized):
        raise SourceProviderError("非法路径", status_code=422)
    normalized = "/".join(parts)
    # 只拒绝 .git 目录本身；.github/.gitignore 等合法路径不受前缀误伤。
    parts = normalized.split("/")
    if parts[0] == ".git" or ".git" in parts:
        raise SourceProviderError("拒绝读取 .git 私有数据", status_code=422)
    return normalized


@dataclass(frozen=True)
class ProviderConfig:
    source_id: str
    provider: str
    repo_root: str = ""
    default_revision: str = ""
    # codesearch 专用：OpenGrok /api/v1 base_url、项目名与 Bearer token。
    base_url: str = ""
    project: str = ""
    token: str = ""


def _codesearch_token(item: dict[str, Any]) -> str:
    """token 来源：token_env 环境变量优先（避免明文入库），其次明文 token。"""
    token_env = str(item.get("token_env") or "").strip()
    if token_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise SourceProviderError(
                f"source '{item.get('source_id')}' 的 token_env 非法", status_code=422
            )
        return os.environ.get(token_env, "").strip()
    return str(item.get("token") or "").strip()


def load_provider_configs(config: dict[str, Any]) -> list[ProviderConfig]:
    section = (config or {}).get("sdk_sources") or {}
    providers: list[ProviderConfig] = []
    seen: set[str] = set()
    for item in section.get("providers") or []:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "").strip()
        provider = str(item.get("provider") or "").strip()
        repo_root = str(item.get("repo_root") or "").strip()
        if not source_id or source_id in seen:
            continue
        if provider == "codesearch":
            base_url = str(item.get("base_url") or "").strip()
            project = str(item.get("project") or "").strip()
            if not base_url or not project:
                logger.warning(
                    "codesearch SDK source '%s' 缺少 base_url/project，已忽略", source_id
                )
                continue
            try:
                token = _codesearch_token(item)
            except SourceProviderError as exc:
                logger.warning("%s", exc)
                continue
            if not token:
                logger.warning(
                    "codesearch SDK source '%s' 无可用 token，已忽略", source_id
                )
                continue
            seen.add(source_id)
            providers.append(ProviderConfig(
                source_id=source_id,
                provider=provider,
                default_revision=str(item.get("default_revision") or "").strip(),
                base_url=base_url,
                project=project,
                token=token,
            ))
            continue
        if provider != "local_git":
            logger.warning("未知 SDK provider 类型已忽略: %s", provider)
            continue
        if not repo_root:
            continue
        seen.add(source_id)
        providers.append(
            ProviderConfig(
                source_id=source_id,
                provider=provider,
                repo_root=repo_root,
                default_revision=str(item.get("default_revision") or "").strip(),
            )
        )
    return providers


def _make_signed_id(payload: dict[str, Any], secret: bytes) -> str:
    """模块级 result_id 签名：``src1_<base64url(payload)>_<hmac16>``。"""
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()[:16]
    return f"{RESULT_ID_PREFIX}{encoded}_{signature}"


def decode_signed_id(result_id: str, secret: bytes) -> dict[str, Any]:
    """校验并解码自包含 result_id 载荷；任何失败都是 422。"""
    value = str(result_id or "").strip()
    if not value.startswith(RESULT_ID_PREFIX):
        raise SourceProviderError("result_id 格式非法", status_code=422)
    encoded, sep, signature = value[len(RESULT_ID_PREFIX):].rpartition("_")
    if not sep or not encoded or not signature:
        raise SourceProviderError("result_id 格式非法", status_code=422)
    # 非 ASCII 载荷在这里是非法输入（签名内容只可能是 base64url/hex），
    # 必须映射 422 而不是让 UnicodeEncodeError 穿透成 500。
    if not encoded.isascii():
        raise SourceProviderError("result_id 格式非法", status_code=422)
    expected = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected, signature):
        raise SourceProviderError("result_id 校验失败", status_code=422)
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceProviderError("result_id 载荷非法", status_code=422) from exc
    if not isinstance(payload, dict):
        raise SourceProviderError("result_id 载荷非法", status_code=422)
    return payload


def _strip_opengrok_path(raw_path: str, project: str) -> str:
    """/<project>/<repo path> -> <repo path>；无项目前缀时仅去掉开头斜杠。"""
    marker = f"/{project}/"
    text = str(raw_path or "")
    if marker in text:
        return text.split(marker, 1)[1]
    return text.lstrip("/")


_OPENGROK_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(value: str) -> str:
    return html_module.unescape(_OPENGROK_TAG_RE.sub("", str(value or ""))).strip()


def _opengrok_line_number(value: Any) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0
