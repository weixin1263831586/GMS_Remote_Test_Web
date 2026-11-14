"""用户注册的 SDK 源码源存储（「添加SDK」管理流的持久层）。

管理员在 Web「用户管理 → Agent 接入」面板选择编译服务器并绑定某个 SDK
根目录后，记录落到 ``configs/secrets/sdk_sources/user_sources.json``
（canonical 路径见 :func:`foundation.config_paths.sdk_user_sources_path`）：

- 与静态 ``sdk_sources.providers`` 配置合并进入 registry（见
  :func:`features.system.source_provider.initialize_source_runtime`），
  不改写部署 config.json；
- SSH 口令经 :mod:`foundation.secrets` Fernet 加密存储，任何读路径都
  不回明文；文件 0600，写入走 原子 tmp + ``os.replace``；
- 跨进程互斥：threading.RLock + fcntl 文件锁（Web 双 worker / 独立
  daily-brief Worker 并发写同一文件）；
- 记录仅含连接元数据与校验状态；``last_verified_at`` / ``last_verified_ok``
  由探测端点刷新（每次刷新列表时检查 SDK 路径是否仍存在）。

安全边界：本存储只被 human-admin 的 manage API 读写；agent token 不能
触碰（见 source_manage_api 的守卫）。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from foundation.secrets import decrypt_secret, encrypt_secret


logger = logging.getLogger(__name__)

STORE_VERSION = 1
SOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# 目录名 → Android 版本的识别模式（大小写不敏感）：Android17 /
# android-16 / aosp-15 等常见命名；bp2a 级别代号太冷僻不猜。
# guess_android_version 只做展示提示（列表"Android 版本"列 + 添加
# 弹框预填），识别不到返回空串；不参与检索行为。目录名不含
# Android/aosp 字样时不回退纯数字（避免把 0623 这类日期误判成版本）。
_ANDROID_VERSION_RE = re.compile(
    r"(?:android|aosp)[-_. ]*(1[0-9])|(?<![0-9.])(1[0-9])(?![0-9.])",
    re.IGNORECASE,
)


def guess_android_version(repo_root: str) -> str:
    base = repo_root.rstrip("/").rsplit("/", 1)[-1]
    if not re.search(r"android|aosp", base, re.IGNORECASE):
        return ""
    match = _ANDROID_VERSION_RE.search(base)
    if not match:
        return ""
    return f"Android{match.group(1) or match.group(2)}"

# 单文件防御性上限：远超真实部署（几十个 SDK 根），防失控写入。
MAX_SOURCES = 128


class UserSourceStoreError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _clean_text(value: Any, *, field: str, max_length: int = 256) -> str:
    text = str(value or "").strip()
    if not text:
        raise UserSourceStoreError(f"{field} 不能为空")
    if len(text) > max_length:
        raise UserSourceStoreError(f"{field} 过长（>{max_length}）")
    if any(ch in text for ch in ("\x00", "\n", "\r")):
        raise UserSourceStoreError(f"{field} 含非法字符")
    return text


def validate_source_id(source_id: Any) -> str:
    text = _clean_text(source_id, field="源 ID", max_length=64)
    if not SOURCE_ID_RE.fullmatch(text):
        raise UserSourceStoreError(
            "源 ID 只能由字母/数字/中划线/下划线组成，且以字母或数字开头，"
            "大小写保持原样"
        )
    return text


def validate_repo_root(repo_root: Any) -> str:
    """远端 SDK 根目录：必须是绝对 POSIX 路径，拒绝 ``..`` 与空段注入。"""
    text = _clean_text(repo_root, field="SDK 路径", max_length=512)
    if not text.startswith("/"):
        raise UserSourceStoreError("SDK 路径必须是远端绝对路径（以 / 开头）")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise UserSourceStoreError("SDK 路径非法（不允许 .. 或空段）")
    normalized = "/" + "/".join(parts)
    return normalized.rstrip("/") or "/"


def validate_connection(host: Any, username: Any, port: Any) -> dict[str, Any]:
    ssh_host = _clean_text(host, field="主机", max_length=255)
    if any(ch.isspace() for ch in ssh_host) or "@" in ssh_host:
        raise UserSourceStoreError("主机名非法（不能包含空白或 @）")
    ssh_username = _clean_text(username, field="用户名", max_length=64)
    if any(ch.isspace() for ch in ssh_username):
        raise UserSourceStoreError("用户名非法（不能包含空白）")
    try:
        ssh_port = int(port or 22)
    except (TypeError, ValueError) as exc:
        raise UserSourceStoreError("SSH 端口必须是整数") from exc
    if not 1 <= ssh_port <= 65535:
        raise UserSourceStoreError("SSH 端口必须在 1-65535 之间")
    return {"ssh_host": ssh_host, "ssh_username": ssh_username, "ssh_port": ssh_port}


class UserSourceStore:
    """文件后端的用户 SDK 源注册表；record 即 public dict（不含明文口令）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._thread_lock = threading.RLock()

    # ---------- 底层 I/O ----------

    def _locked(self):
        return _file_lock(self.path)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise UserSourceStoreError(
                f"SDK 源存储不可读：{type(exc).__name__}", status_code=503
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._handle_corrupt(
                raw, "SDK 源存储文件损坏（JSON 解析失败）", exc,
            )
        if not isinstance(data, dict):
            self._handle_corrupt(raw, "SDK 源存储结构损坏（顶层不是对象）")
        version = data.get("version")
        # Schema 严格校验（2026-11-14 审核意见）：语法合法但结构错误
        # （version 缺失/不识别、sources 是数组、记录不是对象、记录
        # source_id 与键不一致）同样不能按空存储继续——后续 add 会用
        # 空数据覆盖原文件，已注册源与加密口令不可恢复地丢失。
        if not isinstance(version, int) or isinstance(version, bool):
            self._handle_corrupt(raw, "SDK 源存储结构损坏（version 缺失或非法）")
        if version != STORE_VERSION:
            self._handle_corrupt(
                raw,
                f"SDK 源存储版本不识别（version={version}，当前支持 "
                f"{STORE_VERSION}）",
            )
        sources = data.get("sources")
        if not isinstance(sources, dict):
            self._handle_corrupt(
                raw, "SDK 源存储结构损坏（sources 不是对象）",
            )
        for key, value in sources.items():
            if not isinstance(value, dict) or not isinstance(key, str):
                self._handle_corrupt(
                    raw, "SDK 源存储结构损坏（记录不是对象）",
                )
            if str(value.get("source_id") or "") != key:
                self._handle_corrupt(
                    raw,
                    f"SDK 源存储结构损坏（记录 '{key}' 的 source_id 与键不一致）",
                )
        return sources

    def _handle_corrupt(
        self, raw: str, message: str,
        exc: Exception | None = None,
    ) -> None:
        """结构损坏统一处理：保留 .corrupt 原文备份 + 503 阻断写入。"""
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        try:
            try:
                descriptor = os.open(
                    backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(raw)
                logger.error(
                    "用户 SDK 源存储损坏，已备份到 %s；写入被阻断", backup
                )
        except OSError:
            logger.error("用户 SDK 源存储损坏，且备份失败；写入被阻断")
        raise UserSourceStoreError(
            f"{message}，已阻断写入以保护原文件；"
            "请人工检查 .corrupt 备份后修复或删除该文件",
            status_code=503,
        ) from exc

    def _write(self, sources: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:  # pragma: no cover - 平台不支持 chmod 时忽略
            pass
        payload = json.dumps(
            {"version": STORE_VERSION, "sources": sources},
            ensure_ascii=False,
            indent=1,
        )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                with _suppressed_oserror():
                    tmp.unlink()

    # ---------- 公共读写 ----------

    def list_sources(self) -> list[dict[str, Any]]:
        with self._thread_lock, self._locked():
            return list(self._load().values())

    def get(self, source_id: str) -> dict[str, Any]:
        with self._thread_lock, self._locked():
            record = self._load().get(source_id)
        if record is None:
            raise UserSourceStoreError(
                f"SDK 源 '{source_id}' 未在用户注册表中", status_code=404
            )
        return dict(record)

    def get_password(self, source_id: str) -> str:
        """解密口令；仅 provider 构建与校验路径使用，绝不进日志/响应。"""
        record = self.get(source_id)
        token = str(record.get("ssh_password_enc") or "")
        if not token:
            return ""
        try:
            return decrypt_secret(token)
        except Exception as exc:
            raise UserSourceStoreError(
                "SDK 源口令解密失败（密钥文件是否变更？请删除后重新添加）",
                status_code=503,
            ) from exc

    def add(
        self,
        *,
        source_id: str,
        provider: str = "ssh_git",
        repo_root: str,
        ssh_host: str = "",
        ssh_port: int = 22,
        ssh_username: str = "",
        ssh_password: str = "",
        default_revision: str = "HEAD",
        android_version: str = "",
        discover_repos: bool = True,
        discover_ttl_hours: int = 24,
        label: str = "",
        added_by: str = "",
    ) -> dict[str, Any]:
        sid = validate_source_id(source_id)
        root = validate_repo_root(repo_root)
        now_iso = _utcnow_iso()
        # Android 版本：空值时从目录名自动识别（Android17/aosp-android-16
        # 等），识别不到存空串；手动传入值原样保留（128 字符防御上限）。
        version = str(android_version or "").strip()[:128] or guess_android_version(root)
        if provider == "local_git":
            # 本机 Git 仓库：无 SSH 连接段；revision 绑定语义与静态配置一致。
            record: dict[str, Any] = {
                "source_id": sid,
                "provider": "local_git",
                "repo_root": root,
                "default_revision": str(default_revision or "HEAD").strip()[:128],
                "android_version": version,
                "label": str(label or "").strip()[:120],
                "added_by": str(added_by or "").strip()[:64],
                "added_at": now_iso,
            }
        elif provider == "ssh_git":
            connection = validate_connection(ssh_host, ssh_username, ssh_port)
            record = {
                "source_id": sid,
                "provider": "ssh_git",
                "repo_root": root,
                "default_revision": str(default_revision or "HEAD").strip()[:128],
                "android_version": version,
                "discover_repos": bool(discover_repos),
                "discover_ttl_hours": max(1, int(discover_ttl_hours or 24)),
                "label": str(label or "").strip()[:120],
                "added_by": str(added_by or "").strip()[:64],
                "added_at": now_iso,
                **connection,
            }
        else:
            raise UserSourceStoreError(f"不支持的源类型：{provider}")
        if ssh_password:
            record["ssh_password_enc"] = encrypt_secret(ssh_password)
        with self._thread_lock, self._locked():
            sources = self._load()
            if sid in sources:
                raise UserSourceStoreError(
                    f"SDK 源 '{sid}' 已存在；如需更换路径请先删除", status_code=409
                )
            if len(sources) >= MAX_SOURCES:
                raise UserSourceStoreError(
                    f"用户 SDK 源数量已达上限（{MAX_SOURCES}）", status_code=409
                )
            sources[sid] = record
            self._write(sources)
        return dict(record)

    def delete(self, source_id: str) -> dict[str, Any]:
        with self._thread_lock, self._locked():
            sources = self._load()
            record = sources.pop(source_id, None)
            if record is None:
                raise UserSourceStoreError(
                    f"SDK 源 '{source_id}' 未在用户注册表中", status_code=404
                )
            self._write(sources)
        return dict(record)

    def mark_verified(
        self, source_id: str, *, ok: bool, verified_at: str | None = None
    ) -> None:
        with self._thread_lock, self._locked():
            sources = self._load()
            record = sources.get(source_id)
            if record is None:
                return
            record["last_verified_at"] = verified_at or _utcnow_iso()
            record["last_verified_ok"] = bool(ok)
            sources[source_id] = record
            self._write(sources)


# ---------- 模块级辅助 ----------

import contextlib  # noqa: E402  (置于常量后，保持单段 import 语义清晰)


@contextlib.contextmanager
def _suppressed_oserror():
    try:
        yield
    except OSError:
        pass


@contextlib.contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def user_source_to_provider_config(record: dict[str, Any], *, password: str = "") -> dict[str, Any]:
    """把用户记录转换成 ``ProviderConfig`` 关键字兼容的配置项。

    ``password`` 是调用方（registry 合并处）经 :meth:`get_password` 还原的
    明文口令——与静态配置 ``ssh_password`` 的进程内语义一致，但永不进入
    list_sources/响应/日志。local_git 记录只映射仓库路径与 revision。
    """
    provider = str(record.get("provider") or "ssh_git")
    item: dict[str, Any] = {
        "source_id": record.get("source_id"),
        "provider": provider,
        "repo_root": record.get("repo_root") or "",
        "default_revision": record.get("default_revision") or "HEAD",
        "android_version": str(record.get("android_version") or ""),
    }
    if provider == "ssh_git":
        item.update({
            "ssh_host": record.get("ssh_host") or "",
            "ssh_port": record.get("ssh_port") or 22,
            "ssh_username": record.get("ssh_username") or "",
            "ssh_use_key_auth": False,
            "ssh_password": password,
            "repos": (),
            "discover_repos": bool(record.get("discover_repos", True)),
            "discover_ttl_hours": record.get("discover_ttl_hours") or 24,
        })
    return item


__all__ = [
    "MAX_SOURCES",
    "SOURCE_ID_RE",
    "UserSourceStore",
    "UserSourceStoreError",
    "user_source_to_provider_config",
    "validate_connection",
    "validate_repo_root",
    "validate_source_id",
]
