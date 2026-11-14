"""SDK 源码源用户注册管理 API（「添加SDK」弹框后端）。

与只读检索面（``source_api`` 的 ``/api/sdk/*``，sdk.read）分离的管理面：

- ``GET    /api/sdk/manage/sources``             用户注册源 + 实时存在性校验
- ``GET    /api/sdk/manage/servers``             可选编译服务器清单（build_servers.json）
- ``POST   /api/sdk/manage/probe``               绑定前探测远端 SDK 路径
- ``POST   /api/sdk/manage/sources``             校验 + 注册 + 热重载 registry（单路径兼容）
- ``POST   /api/sdk/manage/sources/batch``       批量注册：一次 SSH 连接探测多条路径
- ``DELETE /api/sdk/manage/sources/{source_id}`` 注销 + 热重载

安全模型（ADR 0012）：全部 human-only——读操作
``require_human_principal_when_auth_required``，写操作
``require_elevated_admin``（与配对码创建同级）；agent token /
machine capability 一律 403。SSH 口令只从编译服务器部署配置
（env_password）、请求一次性提供或既有加密记录解析，永不回传/记日志。

SSH 执行走 ADR 0004 唯一层（``SSHManager`` + ``ssh_executor``），远端命令
只读且路径经 ``shlex.quote``。列表刷新时的存在性校验会顺手更新
``last_verified_at/ok``（满足"每次刷新都要检查该 SDK 路径是否还存在"）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from features.auth import (
    require_elevated_admin,
    require_human_principal_when_auth_required,
)
from foundation.config import settings
from foundation.config_paths import build_servers_path
from foundation.errors import handle_api_errors

from .source_provider import reload_source_runtime, user_source_store
from .source_user_store import (
    UserSourceStoreError,
    guess_android_version,
    validate_repo_root,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sdk/manage")

# 远端探测/校验超时与上限（与 ssh_git provider 的保守值同风格）。
# 206 等多人共用编译服务器在编译高峰（实测 wlq/lby 同时全量编译）ssh
# 命令延迟会从 2s 飙到 26s+，30s 预算不够——探测放宽到 60s。
_PROBE_TIMEOUT_SECONDS = 60
_VERIFY_TIMEOUT_SECONDS = 25
# 探测命令超时后的自动重试次数：编译高峰偶发抖动，一次重试把偶发失败
# 从 "整单 502 用户重填" 变成后台自动恢复。
_PROBE_TIMEOUT_RETRIES = 1
# SDK 根目录名 → source_id 的清洗规则（与 store 的 SOURCE_ID_RE 呼应）。
# 保留目录名原始大小写（7_Android17_1017 → 7_Android17_1017），只清洗
# 非法字符：Agent 检索的 source 参数与用户认知的目录名保持一致。
_SOURCE_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_VERIFY_MIN_GIT_DEPTH = 4
# 批量注册（POST /sources/batch）：一次 SSH 连接顺序探测全部路径，
# 超时预算按路径数放宽但封顶。206 认证阶段实测 60s+，逐路径各连一次
# 会把认证成本放大 N 倍——批量模式只连一次。
_BATCH_MAX_PATHS = 16
_BATCH_PROBE_PER_PATH_SECONDS = 30
_BATCH_PROBE_MAX_TIMEOUT_SECONDS = 300

# auto 模式的校验结果新鲜窗口：窗口内的 last_verified_* 直接作为缓存
# 状态返回（列表立即响应）；超窗转后台校验。编译服务器失联时前端仍能
# 立即看到"校验过期 + 上次结果"，不被 25s×N 的 SSH 拖死。
_VERIFY_FRESH_SECONDS = 300
# 后台校验去重：同一进程内同一源已有校验在跑时不重复发（列表刷新可能
# 被用户连续点）。持锁做记录/摘除，线程本身在全局池上执行。
_VERIFY_BACKGROUND_LOCK = threading.Lock()
_VERIFY_IN_FLIGHT: set[str] = set()
_VERIFY_EXECUTOR: ThreadPoolExecutor | None = None


def _error_response(exc: Exception) -> JSONResponse:
    status = getattr(exc, "status_code", 500)
    return JSONResponse(
        status_code=status, content={"success": False, "error": str(exc)}
    )


# ---------- 远端只读探测（ADR 0004 层） ----------


# probe 子仓探测深度：maxdepth 2 命中即停（秒级，编译高峰也不拖）。
# 精确子仓计数已弃用——检索链路（source_ssh_git discover_repos +
# source_repo_rank）运行时自己全量枚举排序，probe 的 count 只是
# "选没选对目录"的展示反馈，浅层证据足够。
_PROBE_GIT_DEPTH = 2


def _probe_command(repo_root: str) -> str:
    """远端单条只读探测：存在性 / 直接 git 仓 / repo 工作区 / Android 树。

    输出用标记行（DIR/GIT/REPO_OK + SUB_GIT_OK），命令恒成功退出，解析
    标记。find 限 maxdepth 2 且 ``-quit`` 命中即停：任何深度下探都可能
    被编译高峰期的磁盘负载拖到分钟级（2026-11-14 实测 206 上 60s 超时
    ×重试 = 校验卡 2 分钟），浅层足够回答"这是不是 SDK/代码树"。
    Android 源码树（build/envsetup.sh）为单仓 AOSP 提供注册门禁证据。
    """
    root = shlex.quote(repo_root)
    quick_find = (
        f"find {root} -maxdepth {_PROBE_GIT_DEPTH} "
        f"\\( -name .repo -o -name out \\) -prune -o -type d -name .git "
        f"-print -quit 2>/dev/null | grep -q . && echo SUB_GIT_OK"
    )
    return (
        f"test -d {root} && echo DIR_OK; "
        f"test -d {root}/.git && echo GIT_OK; "
        f"test -e {root}/.repo/manifest.xml && echo REPO_OK; "
        f"test -e {root}/build/envsetup.sh && echo ANDROID_OK; "
        f"test -e {root}/build/make/envsetup.sh && echo ANDROID_OK; "
        f"{quick_find}"
    )


def _verify_command(repo_root: str) -> str:
    """远端轻量存在性校验：目录存在 且 有任一 git 证据即成功（退出码语义）。

    证据：直接 ``.git``、``.repo/manifest.xml``，或 maxdepth 4 内任意子仓
    （``-print -quit`` 命中即停，秒级返回）。
    """
    root = shlex.quote(repo_root)
    quick_find = (
        f"find {root} -maxdepth {_VERIFY_MIN_GIT_DEPTH} "
        f"\\( -name .repo -o -name out \\) -prune -o -type d -name .git "
        f"-print -quit 2>/dev/null"
    )
    return (
        f"test -d {root} && {{ test -d {root}/.git "
        f"|| test -e {root}/.repo/manifest.xml "
        f"|| {{ {quick_find} | grep -q . ; }} ; }}"
    )


def _parse_probe(stdout: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    return {
        "path_exists": "DIR_OK" in lines,
        "is_git_repo": "GIT_OK" in lines,
        "is_repo_workspace": "REPO_OK" in lines,
        "is_android_tree": "ANDROID_OK" in lines,
        # 浅层子仓证据（maxdepth 2 命中即停）：只做布尔判定，不再计数。
        "has_sub_git_repo": "SUB_GIT_OK" in lines,
        "git_repo_count": 1 if "SUB_GIT_OK" in lines else 0,
    }


def _probe_summary(probe: dict[str, Any]) -> str:
    if not probe["path_exists"]:
        return "路径不存在"
    if probe["is_repo_workspace"]:
        return "repo 工作区（SDK 源码树）"
    if probe["is_android_tree"]:
        return "Android 源码树（单仓，含 build/envsetup.sh）"
    if probe["has_sub_git_repo"]:
        return "目录下发现 git 子仓（多仓代码树）"
    if probe["is_git_repo"]:
        return "单 git 仓库"
    return "目录存在，但未发现任何 git 仓库"


def _probe_via_target(
    target: str,
    repo_root: str,
    provided_password: str,
    *,
    verify_only: bool = False,
    source_id: str = "",
) -> dict[str, Any]:
    """手动输入 target（user@host[:port]）的探测封装。"""
    connection = _parse_target(target)
    password = _resolve_password_by_target(
        connection["host"], connection["username"], provided_password,
        source_id=source_id,
    )
    synthetic = {
        "id": "(manual)",
        "host": connection["host"],
        "port": connection["port"],
        "username": connection["username"],
        "auth_type": "",
    }
    return _probe_via_server(
        synthetic, repo_root, password,
        verify_only=verify_only, source_id=source_id,
    )


# ---------- SDK 根目录发现（远端候选下拉） ----------

# find 限 home 下两层 + 标记测试；输出 CAND|kind|path 行，命令恒成功。
# 编译高峰期远端枚举可达 30s+（与探测同源的负载抖动），预算同 60s。
_DISCOVER_TIMEOUT_SECONDS = 60
_DISCOVER_MAX_CANDIDATES = 200


def _discover_command() -> str:
    """两层扫描：home 直下的一级目录里找 repo 工作区 / Android 源码树，
    二级目录里只找同样两类；剪枝 out/ 与隐藏目录。首行输出 ``HOME|<home>``，
    供解析端按**相对深度**过滤（嵌套 home 如 ``/data/users/x`` 的一级
    目录已有 4 个斜杠，不能按绝对斜杠数剪枝）。

    Android 源码树证据 = ``build/envsetup.sh`` 或 ``build/make/envsetup.sh``
    （AOSP 单仓/非 repo 工作区都有）。不再把"任意 .git 目录"当候选：
    206 上 ``gms_tools`` 这类 CI 工具仓曾被误列成 SDK（2026-11-15 用户
    反馈），SDK 检索对它们跑全量子仓枚举只会白耗时。"""
    return (
        'echo "HOME|$HOME"; '
        'for d in "$HOME"/*/; do '
        'd="${d%/}"; case "$(basename "$d")" in out|.*|*.img) continue;; esac; '
        'if [ -e "$d/.repo/manifest.xml" ]; then echo "REPO|$d"; '
        'elif [ -e "$d/build/envsetup.sh" ] || [ -e "$d/build/make/envsetup.sh" ]; then echo "ANDROID|$d"; '
        'else for s in "$d"/*/; do s="${s%/}"; '
        'case "$(basename "$s")" in out|.*|*.img) continue;; esac; '
        'if [ -e "$s/.repo/manifest.xml" ]; then echo "REPO|$s"; '
        'elif [ -e "$s/build/envsetup.sh" ] || [ -e "$s/build/make/envsetup.sh" ]; then echo "ANDROID|$s"; '
        'fi; done; fi; done | '
        f"head -n {_DISCOVER_MAX_CANDIDATES}"
    )


def _parse_discover(stdout: str) -> list[dict[str, Any]]:
    """REPO/ANDROID 都保留（repo 工作区 / Android 源码树），按相对深度过滤。

    深度防御按相对 home 的层级（≤2 层）判定；HOME 行缺失时回退为
    绝对斜杠数 ≤3 的保守上限（老格式兼容）。

    候选收紧（2026-11-15）：只认 ``.repo/manifest.xml``（repo 工作区）与
    ``build/envsetup.sh``（Android 源码树）两类证据；裸 ``.git`` 目录
    （gms_tools 这类 CI 工具仓、备份）不再列为候选——它们不是 SDK 树，
    添加后 SDK 检索的子仓枚举/排序纯属浪费。"旧命令的 GIT/SUB 输出
    不再被解析（新命令已不产生）。"""
    remote_home = ""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        kind, path = parts[0].strip(), parts[1].strip()
        if kind == "HOME" and path.startswith("/"):
            remote_home = path.rstrip("/")
            continue
        if kind not in ("REPO", "ANDROID") or not path.startswith("/"):
            continue
        path = path.rstrip("/")
        if remote_home and (path == remote_home or path.startswith(remote_home + "/")):
            depth = path[len(remote_home):].count("/")  # 相对层级：一级=1，二级=2
            max_depth = 2
        else:
            depth = path.count("/")  # 无 HOME 行：老式保守过滤
            max_depth = 3
        if path in seen or depth > max_depth:
            continue
        seen.add(path)
        label = {
            "REPO": "repo 工作区",
            "ANDROID": "Android 源码树",
        }[kind]
        candidates.append({"path": path, "kind": kind, "kind_label": label})
    candidates.sort(key=lambda item: (0 if item["kind"] == "REPO" else 1, item["path"]))
    return candidates


def _discover_via_target(
    target: str, provided_password: str
) -> list[dict[str, Any]]:
    """列出远端 home（≤2 层）下的 SDK 根目录候选（repo 工作区 / git 仓）。"""
    connection = _parse_target(target)
    password = _resolve_password_by_target(
        connection["host"], connection["username"], provided_password
    )
    synthetic = {
        "id": "(manual)",
        "host": connection["host"],
        "port": connection["port"],
        "username": connection["username"],
        "auth_type": "",
    }
    stdout, exit_code = _run_remote_probe(
        host=synthetic["host"],
        port=synthetic["port"],
        username=synthetic["username"],
        password=password,
        use_key_auth=not password,
        repo_root="/",
        command=_discover_command(),
        timeout=_DISCOVER_TIMEOUT_SECONDS,
    )
    if exit_code != 0:
        raise UserSourceStoreError(
            "远端目录枚举失败（请检查连接与权限）", status_code=502
        )
    candidates = _parse_discover(stdout)
    if not candidates:
        raise UserSourceStoreError(
            f"在 {connection['username']}@{connection['host']} 的 home 下"
            "（≤2 层）未发现 Android 源码树（repo 工作区或含 "
            "build/envsetup.sh 的目录）；请确认该机器上有 SDK 代码树，"
            "或改用手动输入完整路径",
            status_code=404,
        )
    return candidates


def _probe_local_path(repo_root: str) -> dict[str, Any]:
    """本机 local_git 路径的本地探测（不 SSH；仅存在性 + git 证据）。"""
    import os

    root = validate_repo_root(repo_root)
    exists = os.path.isdir(root)
    probe = {
        "path_exists": exists,
        "is_git_repo": exists and os.path.isdir(os.path.join(root, ".git")),
        "is_repo_workspace": exists
        and os.path.isfile(os.path.join(root, ".repo", "manifest.xml")),
        "is_android_tree": exists
        and (
            os.path.isfile(os.path.join(root, "build", "envsetup.sh"))
            or os.path.isfile(os.path.join(root, "build", "make", "envsetup.sh"))
        ),
        "has_sub_git_repo": False,
        "git_repo_count": 0,
    }
    if exists and not probe["is_git_repo"] and not probe["is_repo_workspace"]:
        # 浅层找子仓证据（maxdepth 2、命中即停），与远端 _probe_command 对齐。
        for current, dirs, _files in os.walk(root):
            depth = current[len(root):].count(os.sep)
            if depth >= _PROBE_GIT_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in (".repo", "out")]
            if ".git" in dirs:
                probe["has_sub_git_repo"] = True
                probe["git_repo_count"] = 1
                break
    probe["summary"] = _probe_summary(probe)
    return probe


def _ssh_connection_config(
    host: str, port: int, username: str, password: str, use_key_auth: bool
) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "username": username,
        "use_key_auth": use_key_auth,
        "private_key_path": "~/.ssh/id_rsa",
        "password": password,
    }


def _run_remote_probe(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_key_auth: bool,
    repo_root: str,
    command: str,
    timeout: int,
) -> tuple[str, int]:
    """一条只读远端命令；连接/执行异常统一 502，命令退出码原样返回。

    超时自动重试 :data:`_PROBE_TIMEOUT_RETRIES` 次（206 等编译服务器
    sshd 偶发抖动，journald 实测重试即成功）；连接失败不重试（凭据/
    网络问题，重试同样失败且拉长用户等待）。
    """
    from .ssh import SSHManager

    last_detail = ""
    for attempt in range(1 + _PROBE_TIMEOUT_RETRIES):
        manager = SSHManager(pool_size=1)
        config = _ssh_connection_config(host, port, username, password, use_key_auth)
        ssh = manager.create_connection(config)
        if ssh is None:
            raise UserSourceStoreError(
                f"SSH 连接失败：{username}@{host}:{port}（请检查网络、用户名与口令）",
                status_code=502,
            )
        try:
            result = manager.execute_command(ssh, command, timeout=timeout)
        except Exception as exc:
            logger.warning("SDK 源远端探测执行失败 %s@%s: %s", username, host, exc)
            raise UserSourceStoreError(
                f"SSH 命令执行失败：{type(exc).__name__}", status_code=502
            ) from exc
        finally:
            try:
                ssh.close()
            except Exception:
                pass
        # ssh_executor 超时/异常不抛出，而是返回 code=-1 的结果；必须显式
        # 502，否则空 stdout 会被解析成「路径不存在」误导用户。
        if getattr(result, "timed_out", False) or result.code == -1:
            last_detail = (result.stderr or "超时或连接异常").strip()[:120]
            if attempt < _PROBE_TIMEOUT_RETRIES:
                logger.warning(
                    "SDK 源远端命令超时（%s@%s，第 %d 次），自动重试",
                    username, host, attempt + 1,
                )
                continue
            raise UserSourceStoreError(
                f"SSH 命令执行失败（{last_detail}）；"
                "已自动重试仍超时，请稍后重试或检查服务器负载",
                status_code=502,
            )
        return result.stdout, result.code
    raise UserSourceStoreError(  # pragma: no cover - 循环必经 return/raise
        f"SSH 命令执行失败（{last_detail}）", status_code=502
    )


# ---------- 编译服务器清单（只读解析，不引 build feature） ----------


def _load_build_servers() -> list[dict[str, Any]]:
    path = build_servers_path(settings.project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return []
    listing = []
    for item in servers:
        if not isinstance(item, dict) or not str(item.get("host") or "").strip():
            continue
        auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
        listing.append(
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "host": str(item.get("host") or "").strip(),
                "port": int(item.get("port") or 22),
                "username": str(item.get("username") or "").strip(),
                # 只暴露认证方式；口令来源（环境变量名）不外泄。
                "auth_type": str(auth.get("type") or ""),
                "auth_env": str(auth.get("env") or "").strip(),
            }
        )
    return listing


def _parse_target(target: str) -> dict[str, Any]:
    """解析手动输入的连接目标：``user@10.0.0.9`` / ``user@host:2222``。

    返回 {host, port, username}；用户名不设缺省（root 连接太危险），
    留空直接 422。
    """
    text = str(target or "").strip()
    if not text:
        raise UserSourceStoreError("请输入编译服务器地址（如 user@10.0.0.9）")
    # 去掉可能粘贴进来的 scheme 与空白
    text = text.replace("ssh://", "").strip()
    if any(ch.isspace() for ch in text) or "@" in text.replace("@", "", 1):
        raise UserSourceStoreError("服务器地址格式非法（应为 user@host[:port]）")
    username, sep, hostport = text.rpartition("@")
    if not sep:
        raise UserSourceStoreError("请使用 user@host 格式（如 user@10.0.0.9）")
    if "@" in hostport:
        raise UserSourceStoreError("服务器地址格式非法（多个 @）")
    host, port_str = hostport, ""
    if ":" in hostport:
        host, port_str = hostport.rsplit(":", 1)
    host = host.strip()
    username = username.strip()
    if not host or not username:
        raise UserSourceStoreError("服务器地址缺少主机名或用户名")
    if any(ch.isspace() for ch in host) or any(ch.isspace() for ch in username):
        raise UserSourceStoreError("主机名/用户名不能包含空白")
    port = 22
    if port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise UserSourceStoreError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise UserSourceStoreError("端口必须在 1-65535 之间")
    return {"host": host, "port": port, "username": username}


def _get_server(server_id: str) -> dict[str, Any]:
    for item in _load_build_servers():
        if item["id"] == server_id:
            return item
    raise UserSourceStoreError(
        f"编译服务器 '{server_id}' 不在 build_servers 配置中", status_code=404
    )


def _resolve_password(
    server: dict[str, Any], provided: str, *, source_id: str = ""
) -> str:
    """口令来源：请求一次性提供 > 编译服务器 env_password > 既有加密记录。"""
    if provided:
        return provided
    if server.get("auth_env"):
        return os.getenv(server["auth_env"], "")
    if source_id:
        try:
            return user_source_store().get_password(source_id)
        except UserSourceStoreError:
            return ""
    return ""


def _resolve_password_by_target(
    host: str, username: str, provided: str, *, source_id: str = ""
) -> str:
    """按 host+username 匹配 build_servers 的 env_password；否则回退既有记录。"""
    if provided:
        return provided
    for item in _load_build_servers():
        if item["host"] == host and item["username"] == username:
            password = _resolve_password(item, "")
            if password:
                return password
            break
    if source_id:
        try:
            return user_source_store().get_password(source_id)
        except UserSourceStoreError:
            return ""
    return ""


def _probe_via_server(
    server: dict[str, Any], repo_root: str, provided_password: str,
    *, verify_only: bool = False, source_id: str = "",
) -> dict[str, Any]:
    """连接编译服务器并探测/校验路径；返回探测结果或抛 UserSourceStoreError。

    口令解析：请求一次性提供 > server 自带 auth_env > 按 host+username
    匹配 build_servers（列表刷新的合成 server 没有 auth_env 字段，必须
    走这个回退）> 既有加密记录。解析不到才回退密钥认证——本机常备
    口令短语保护的私钥，空口令强走 key auth 只会稳定失败。
    """
    validate_repo_root(repo_root)
    password = _resolve_password(server, provided_password, source_id=source_id)
    if not password and provided_password == "":
        password = _resolve_password_by_target(
            str(server.get("host") or ""), str(server.get("username") or ""),
            "", source_id=source_id,
        )
    # 无口令时尝试密钥认证（SSHManager 会回退 agent/默认密钥）。
    use_key_auth = not password
    command = _verify_command(repo_root) if verify_only else _probe_command(repo_root)
    timeout = _VERIFY_TIMEOUT_SECONDS if verify_only else _PROBE_TIMEOUT_SECONDS
    try:
        stdout, exit_code = _run_remote_probe(
            host=server["host"],
            port=int(server["port"] or 22),
            username=server["username"],
            password=password,
            use_key_auth=use_key_auth,
            repo_root=repo_root,
            command=command,
            timeout=timeout,
        )
    except UserSourceStoreError as exc:
        if not verify_only:
            raise
        # 校验模式：连接失败 = 不可用（返回 not_ok 而不是 5xx，让刷新列
        # 表在单台服务器失联时仍能展示其余条目）。
        return {"verified": False, "reason": str(exc), "probe": {}}
    if verify_only:
        return {
            "verified": exit_code == 0,
            "reason": (
                "" if exit_code == 0 else "路径不存在或未发现 git 仓库"
            ),
            "probe": {},
        }
    probe = _parse_probe(stdout)
    probe["summary"] = _probe_summary(probe)
    return probe


def _derive_source_id(repo_root: str) -> str:
    base = repo_root.rstrip("/").rsplit("/", 1)[-1]
    sanitized = _SOURCE_ID_SANITIZE_RE.sub("-", base).strip("-_") or "sdk"
    return sanitized[:48]


# ---------- 批量探测 / 注册（弹框多选） ----------


def _batch_probe_command(repo_roots: list[str]) -> str:
    """单连接批量探测：对每条路径输出 ``BPR|<idx>|<标记串>`` 行。

    标记串与单路径 probe 的标记字母一致（DIR/GIT/REPO/SUB），只是
    按路径索引归组。for 循环只做只读 test，不做任何 find（批量场景
    下浅层子仓证据会放大远端 IO，注册门禁只看 GIT/REPO/SUB 任一）。
    """
    tests: list[str] = []
    for idx, repo_root in enumerate(repo_roots):
        root = shlex.quote(repo_root)
        marks = (
            f"test -d {root} && echo DIR; "
            f"test -d {root}/.git && echo GIT; "
            f"test -e {root}/.repo/manifest.xml && echo REPO; "
            f"test -e {root}/build/envsetup.sh && echo ANDROID; "
            f"test -e {root}/build/make/envsetup.sh && echo ANDROID"
        )
        tests.append(f'marks="$( {marks} )"; echo "BPR|{idx}|$marks"')
    return " ; ".join(tests)


def _parse_batch_probe(stdout: str) -> list[dict[str, Any]]:
    """按 ``BPR|<idx>|<标记>`` 行还原每条路径的单路径 probe 结构。"""
    probes: dict[int, dict[str, Any]] = {}
    for line in stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or parts[0].strip() != "BPR":
            continue
        try:
            idx = int(parts[1].strip())
        except ValueError:
            continue
        marks = set(parts[2].split())
        probes[idx] = {
            "path_exists": "DIR" in marks,
            "is_git_repo": "GIT" in marks,
            "is_repo_workspace": "REPO" in marks,
            "is_android_tree": "ANDROID" in marks,
            "has_sub_git_repo": "SUB" in marks,
            "git_repo_count": 1 if "SUB" in marks else 0,
        }
    return [probes[idx] for idx in sorted(probes)]


def _batch_probe_via_target(
    target: str, repo_roots: list[str], provided_password: str
) -> list[dict[str, Any]]:
    """一次 SSH 连接顺序探测全部路径（认证阶段 60s+ 的 206 只付一次）。"""
    connection = _parse_target(target)
    password = _resolve_password_by_target(
        connection["host"], connection["username"], provided_password
    )
    synthetic = {
        "id": "(manual)",
        "host": connection["host"],
        "port": connection["port"],
        "username": connection["username"],
        "auth_type": "",
    }
    timeout = min(
        _BATCH_PROBE_MAX_TIMEOUT_SECONDS,
        _BATCH_PROBE_PER_PATH_SECONDS * max(1, len(repo_roots)),
    )
    stdout, exit_code = _run_remote_probe(
        host=synthetic["host"],
        port=synthetic["port"],
        username=synthetic["username"],
        password=password,
        use_key_auth=not password,
        repo_root="/",
        command=_batch_probe_command(repo_roots),
        timeout=timeout,
    )
    if exit_code != 0:
        raise UserSourceStoreError(
            "远端批量探测失败（请检查连接与权限）", status_code=502
        )
    probes = _parse_batch_probe(stdout)
    if len(probes) != len(repo_roots):
        raise UserSourceStoreError(
            "远端批量探测输出不完整（命令被截断？），请重试", status_code=502
        )
    for probe in probes:
        probe["summary"] = _probe_summary(probe)
    return probes


def _unique_source_id(store: Any, repo_root: str) -> str:
    """目录名派生 source_id；重名追加 -2/-3…（批量添加同名目录时）。"""
    base = _derive_source_id(repo_root)
    source_id = base
    suffix = 2
    while True:
        try:
            store.get(source_id)
        except UserSourceStoreError:
            return source_id
        source_id = f"{base}-{suffix}"
        suffix += 1


# ---------- 路由 ----------


@router.get("/servers")
@handle_api_errors
def list_manage_servers(request: Request):
    require_human_principal_when_auth_required(request)
    return {"success": True, "data": {"servers": _load_build_servers()}}


@router.get("/sources")
@handle_api_errors
def list_user_sdk_sources(request: Request, verify: str = "auto"):
    """用户注册源 + 存在性校验（校验状态与列表响应解耦）。

    local_git（本机）走本地探测；ssh_git 走远端 verify。

    校验模式（2026-11-14 审核意见：多台编译服务器失联时，同步校验会让
    列表卡 25s×N，用户必须仍能立即查看/管理/删除已注册源）：

    - ``auto``（默认）：``last_verified_at`` 在新鲜窗口内的源直接返回
      缓存结果（``verify_state=fresh``）；从未校验（pending）或超窗
      （stale）的源转**后台线程**校验并落盘，本响应立即返回；前端下次
      刷新即可看到最新结果。
    - ``sync``：显式强校验（「刷新」按钮），同步等待全部校验完成——
      旧行为，单源失败不阻断其余条目。

    ``verify_state`` 表达结果时鲜性：fresh（窗口内）/ stale（超窗，附
    上次结果）/ pending（从未校验）；``live.verified`` 表达结果本身。
    """
    require_human_principal_when_auth_required(request)
    store = user_source_store()
    records = store.list_sources()
    servers_by_host: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in records:
        if record.get("provider") != "ssh_git":
            continue
        key = (
            str(record.get("ssh_host") or ""),
            int(record.get("ssh_port") or 22),
            str(record.get("ssh_username") or ""),
        )
        if key not in servers_by_host:
            # 合成探测目标：与注册记录相同的连接参数（等价 build server）。
            servers_by_host[key] = {
                "id": "(registered)",
                "host": key[0],
                "port": key[1],
                "username": key[2],
                "auth_type": "",
            }
    results: list[dict[str, Any]] = [dict(record) for record in records]
    sync_mode = str(verify or "auto").strip().lower() == "sync"
    now = time.time()
    background: list[int] = []

    def _verify(index: int) -> dict[str, Any]:
        record = records[index]
        source_id = str(record.get("source_id") or "")
        repo_root = str(record.get("repo_root") or "/invalid")
        if record.get("provider") == "local_git":
            probe = _probe_local_path(repo_root)
            verified = bool(probe["path_exists"] and (
                probe["git_repo_count"] or probe["is_git_repo"]
                or probe["is_repo_workspace"]
            ))
            live = {"verified": verified, "reason": "" if verified else probe["summary"]}
        else:
            key = (
                str(record.get("ssh_host") or ""),
                int(record.get("ssh_port") or 22),
                str(record.get("ssh_username") or ""),
            )
            probe = _probe_via_server(
                servers_by_host[key],
                repo_root,
                "",
                verify_only=True,
                source_id=source_id,
            )
            live = {"verified": probe["verified"], "reason": probe["reason"]}
        try:
            store.mark_verified(source_id, ok=live["verified"])
        except UserSourceStoreError as exc:  # 写失败不阻断展示
            logger.warning("SDK 源校验状态写入失败：%s", exc)
        return live

    for index, item in enumerate(results):
        verified_at = str(item.get("last_verified_at") or "")
        age: float | None = None
        if verified_at:
            try:
                age = now - datetime.strptime(
                    verified_at, "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                age = None
        if age is None:
            state = "pending"
        elif age > _VERIFY_FRESH_SECONDS:
            state = "stale"
        else:
            state = "fresh"
        item["verify_state"] = state
        if state == "fresh":
            item["live"] = {
                "verified": bool(item.get("last_verified_ok")),
                "reason": "",
            }
        elif sync_mode:
            item["live"] = _verify(index)
            item["verify_state"] = "fresh"
        else:
            item["live"] = None  # 后台校验中；前端按"校验中/校验过期"展示
            background.append(index)

    if background:
        _schedule_background_verify(
            [records[index] for index in background],
            servers_by_host,
        )
    return {
        "success": True,
        "data": {"sources": [_public_record(item) for item in results]},
    }


def _schedule_background_verify(
    records: list[dict[str, Any]],
    servers_by_host: dict[tuple[str, int, str], dict[str, Any]],
) -> None:
    """把 pending/stale 源的校验投到后台线程，结果写回存储。

    与请求生命周期解耦：列表响应不被 SSH 超时拖住；同一源已有校验在
    跑时跳过（用户连续点刷新不会叠出并发 SSH）；线程内的异常只记日志
    ——下一次刷新会因 last_verified_at 未更新再次进入后台校验。
    """
    global _VERIFY_EXECUTOR

    store = user_source_store()
    with _VERIFY_BACKGROUND_LOCK:
        todo: list[dict[str, Any]] = []
        for record in records:
            source_id = str(record.get("source_id") or "")
            if not source_id or source_id in _VERIFY_IN_FLIGHT:
                continue
            _VERIFY_IN_FLIGHT.add(source_id)
            todo.append(record)
        if not todo:
            return
        if _VERIFY_EXECUTOR is None:
            _VERIFY_EXECUTOR = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="sdk-verify",
            )

    def _run(record: dict[str, Any]) -> None:
        source_id = str(record.get("source_id") or "")
        repo_root = str(record.get("repo_root") or "/invalid")
        try:
            if record.get("provider") == "local_git":
                probe = _probe_local_path(repo_root)
                verified = bool(probe["path_exists"] and (
                    probe["git_repo_count"] or probe["is_git_repo"]
                    or probe["is_repo_workspace"]
                ))
            else:
                key = (
                    str(record.get("ssh_host") or ""),
                    int(record.get("ssh_port") or 22),
                    str(record.get("ssh_username") or ""),
                )
                server = servers_by_host.get(key)
                if server is None:
                    return
                probe = _probe_via_server(
                    server, repo_root, "",
                    verify_only=True, source_id=source_id,
                )
                verified = bool(probe["verified"])
            try:
                store.mark_verified(source_id, ok=verified)
            except UserSourceStoreError as exc:
                logger.warning("SDK 源后台校验写入失败：%s", exc)
        except Exception:
            logger.warning(
                "SDK 源 %s 后台校验失败；下次刷新重试", source_id,
                exc_info=True,
            )
        finally:
            with _VERIFY_BACKGROUND_LOCK:
                _VERIFY_IN_FLIGHT.discard(source_id)

    for record in todo:
        _VERIFY_EXECUTOR.submit(_run, record)


@router.post("/discover")
@handle_api_errors
def discover_sdk_roots(request: Request, req: dict[str, Any]):
    """枚举远端 home 下的 SDK 根目录候选（下拉数据源）。"""
    require_human_principal_when_auth_required(request)
    try:
        candidates = _discover_via_target(
            str(req.get("target") or ""), str(req.get("password") or "")
        )
    except UserSourceStoreError as exc:
        return _error_response(exc)
    return {"success": True, "data": {"candidates": candidates}}


@router.post("/discover-local")
@handle_api_errors
def discover_local_sdk_roots(request: Request):
    """本机（local_git）候选：home 一级目录 + 项目根同级，含 git 证据。"""
    require_human_principal_when_auth_required(request)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    roots = [Path.home()]
    try:
        project_dir = Path(settings.project_root).parent
        if project_dir.is_dir():
            roots.append(project_dir)
    except (TypeError, OSError):
        pass
    for base in roots:
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries[:400]:
            if not entry.is_dir() or entry.name.startswith(".") or entry.name == "out":
                continue
            path_str = str(entry)
            if path_str in seen:
                continue
            is_repo = (entry / ".repo" / "manifest.xml").is_file()
            is_android = (entry / "build" / "envsetup.sh").is_file() or (
                entry / "build" / "make" / "envsetup.sh"
            ).is_file()
            if not (is_repo or is_android):
                # 与远端 discover 同口径：裸 git 目录（CI 工具仓等）
                # 不是 SDK 树，不进候选。
                continue
            seen.add(path_str)
            candidates.append({
                "path": path_str,
                "kind": "REPO" if is_repo else "ANDROID",
                "kind_label": "repo 工作区" if is_repo else "Android 源码树",
            })
            if len(candidates) >= _DISCOVER_MAX_CANDIDATES:
                break
        if len(candidates) >= _DISCOVER_MAX_CANDIDATES:
            break
    candidates.sort(key=lambda item: (0 if item["kind"] == "REPO" else 1, item["path"]))
    return {"success": True, "data": {"candidates": candidates}}


@router.post("/probe")
@handle_api_errors
def probe_sdk_path(request: Request, req: dict[str, Any]):
    require_human_principal_when_auth_required(request)
    provider = str(req.get("provider") or "ssh_git").strip()
    repo_root = str(req.get("repo_root") or "").strip()
    target = str(req.get("target") or "").strip()
    try:
        if provider == "local_git":
            probe = _probe_local_path(repo_root)
        elif target:
            probe = _probe_via_target(
                target, repo_root, str(req.get("password") or "")
            )
        else:
            server = _get_server(str(req.get("server_id") or "").strip())
            probe = _probe_via_server(
                server, repo_root, str(req.get("password") or "")
            )
    except UserSourceStoreError as exc:
        return _error_response(exc)
    probe["android_version"] = guess_android_version(repo_root)
    return {"success": True, "data": {"probe": probe}}


@router.post("/sources")
@handle_api_errors
def register_sdk_source(request: Request, req: dict[str, Any]):
    require_elevated_admin(request)
    try:
        record, error = _register_single_source(request, req)
        if error is not None:
            return _error_response(error)
        state = reload_source_runtime()
    except (RuntimeError, OSError) as exc:  # 热重载/磁盘异常
        logger.exception("SDK 源注册后置步骤失败")
        return _error_response(exc)
    record["registry_state"] = state
    return {"success": True, "data": {"source": _public_record(record)}}


def _register_single_source(
    request: Request, req: dict[str, Any], *, probe_override: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, UserSourceStoreError | None]:
    """校验 + 注册单条 SDK 源；返回 ``(record, None)`` 或 ``(None, error)``。

    批量端点复用本函数：``probe_override`` 传入单连接批量探测的结果时
    跳过逐条 SSH 探测（206 认证阶段 60s+，逐条各连一次成本翻 N 倍）。
    抛出的异常原样向上传播；本函数只把业务校验失败（4xx）打包成
    error 返回，方便批量流程逐条收集。
    """
    try:
        provider = str(req.get("provider") or "ssh_git").strip()
        repo_root = str(req.get("repo_root") or "").strip()
        target = str(req.get("target") or "").strip()
        password = str(req.get("password") or "")
        stored_password = password
        if provider == "local_git":
            server: dict[str, Any] = {}
            probe = probe_override or _probe_local_path(repo_root)
        elif target:
            connection = _parse_target(target)
            server = {
                "id": "(manual)",
                "host": connection["host"],
                "port": connection["port"],
                "username": connection["username"],
            }
            probe = probe_override or _probe_via_target(target, repo_root, password)
            # env/既有记录解析出的口令也要随记录加密存储，否则下次
            # 刷新校验时拿不到凭据。
            stored_password = _resolve_password_by_target(
                connection["host"], connection["username"], password
            )
        else:
            server = _get_server(str(req.get("server_id") or "").strip())
            probe = probe_override or _probe_via_server(server, repo_root, password)
        if not probe.get("path_exists"):
            raise UserSourceStoreError(
                f"路径不存在：{repo_root}", status_code=422
            )
        if not (
            probe.get("git_repo_count") or probe.get("is_git_repo")
            or probe.get("is_repo_workspace") or probe.get("is_android_tree")
        ):
            raise UserSourceStoreError(
                f"未在 {repo_root} 发现 Android 源码树（{_probe_summary(probe)}）；"
                "请确认这是 SDK/代码树根目录",
                status_code=422,
            )
        source_id = str(req.get("source_id") or "").strip() or _derive_source_id(
            repo_root
        )
        record = user_source_store().add(
            source_id=source_id,
            provider=provider,
            repo_root=repo_root,
            default_revision=str(req.get("default_revision") or "").strip()
            or ("main" if provider == "local_git" else "HEAD"),
            android_version=str(req.get("android_version") or "").strip(),
            ssh_host=str(server.get("host") or ""),
            ssh_port=int(server.get("port") or 22),
            ssh_username=str(server.get("username") or ""),
            ssh_password=stored_password,
            discover_repos=bool(req.get("discover_repos", provider == "ssh_git")),
            discover_ttl_hours=int(req.get("discover_ttl_hours") or 24),
            label=str(req.get("label") or "").strip(),
            added_by=getattr(request.state, "username", "") or "",
        )
        user_source_store().mark_verified(source_id, ok=True)
        record["probe"] = probe
        return record, None
    except UserSourceStoreError as exc:
        return None, exc


@router.post("/sources/batch")
@handle_api_errors
def register_sdk_sources_batch(request: Request, req: dict[str, Any]):
    """批量注册：一次 SSH 连接顺序探测全部路径，逐条注册。

    远端认证阶段实测可达 60s+（206 sshd），逐路径各连一次会把认证
    成本放大 N 倍；批量模式只付一次连接成本。单条失败（重名/非 SDK
    目录/路径不存在）不阻断其余条目，结果逐条回传供前端展示。
    """
    require_elevated_admin(request)
    provider = str(req.get("provider") or "ssh_git").strip()
    target = str(req.get("target") or "").strip()
    password = str(req.get("password") or "")
    paths_raw = req.get("repo_roots")
    repo_roots = (
        [str(p or "").strip() for p in paths_raw]
        if isinstance(paths_raw, list) else []
    )
    repo_roots = [p for p in repo_roots if p]
    if not repo_roots:
        return _error_response(UserSourceStoreError("请至少选择一条 SDK 路径"))
    if len(repo_roots) != len(set(repo_roots)):
        return _error_response(UserSourceStoreError(
            "路径列表存在重复项", status_code=422
        ))
    if len(repo_roots) > _BATCH_MAX_PATHS:
        return _error_response(UserSourceStoreError(
            f"单批最多 {_BATCH_MAX_PATHS} 条路径", status_code=422
        ))
    try:
        for repo_root in repo_roots:
            validate_repo_root(repo_root)
    except UserSourceStoreError as exc:
        return _error_response(exc)
    if provider == "local_git":
        probes = [_probe_local_path(p) for p in repo_roots]
    else:
        try:
            probes = _batch_probe_via_target(target, repo_roots, password)
        except UserSourceStoreError as exc:
            return _error_response(exc)
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    base_req = {k: v for k, v in req.items() if k not in ("repo_roots", "source_id")}
    for repo_root, probe in zip(repo_roots, probes):
        one_req = dict(base_req)
        one_req["provider"] = provider
        one_req["target"] = target
        one_req["password"] = password
        one_req["repo_root"] = repo_root
        # 批量模式忽略共享 source_id，一律按目录名生成；重名自动加
        # -2/-3 后缀（同机多个同名分支目录并存）。
        one_req["source_id"] = _unique_source_id(user_source_store(), repo_root)
        record, error = _register_single_source(
            request, one_req, probe_override=probe if provider != "local_git" else None
        )
        if record is not None:
            created.append(_public_record(record))
        else:
            failed.append({"repo_root": repo_root, "error": str(error)})
    try:
        state = reload_source_runtime()
    except (RuntimeError, OSError) as exc:  # 热重载/磁盘异常
        logger.exception("SDK 源批量注册后热重载失败")
        return _error_response(exc)
    for item in created:
        item["registry_state"] = state
    return {"success": True, "data": {"created": created, "failed": failed}}


@router.delete("/sources/{source_id}")
@handle_api_errors
def delete_sdk_source(request: Request, source_id: str):
    require_elevated_admin(request)
    try:
        user_source_store().delete(source_id)
        state = reload_source_runtime()
    except UserSourceStoreError as exc:
        return _error_response(exc)
    except (RuntimeError, OSError) as exc:
        logger.exception("SDK 源删除后热重载失败")
        return _error_response(exc)
    return {
        "success": True,
        "data": {"deleted": True, "id": source_id, "registry_state": state},
    }


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    """去掉加密口令字段的管理视图（口令永不回传）。"""
    public = {k: v for k, v in record.items() if not k.endswith("_enc")}
    return public


__all__ = ["router"]
