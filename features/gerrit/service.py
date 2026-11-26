"""Gerrit query and aggregation helpers."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import ssl
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from features.gerrit.config import MAX_QUERY_PAGE_SIZE
from foundation.outbound import ResolvedOutboundTarget, UnsafeOutboundURL, resolve_outbound_target
from foundation.outbound_aiohttp import PinnedOutboundResolver


def resolve_gerrit_target(url: str) -> ResolvedOutboundTarget:
    """Resolve an outbound target using the operator's private-host allowlist."""
    return resolve_outbound_target(
        url,
        allowed_private_hosts={
            host.strip().lower().rstrip('.')
            for host in os.getenv('GMS_GERRIT_ALLOWED_HOSTS', '').split(',')
            if host.strip()
        },
    )


async def post_gerrit_review(
    cfg: dict[str, Any],
    *,
    change_id: str,
    patchset: str = "",
    message: str = "",
    verified: int | None = None,
) -> dict[str, Any]:
    """Publish one ATS review using configured REST, then SSH fallback."""
    if not str(change_id or "").strip():
        return {"sent": False, "error": "Gerrit change id is missing"}
    rest_error = ""
    if cfg.get("base_url") and cfg.get("rest_username") and cfg.get("rest_password"):
        result = await _post_gerrit_review_rest(
            cfg,
            change_id=str(change_id),
            revision=str(patchset or "current"),
            message=message,
            verified=verified,
        )
        if result.get("sent"):
            return result
        rest_error = str(result.get("error") or "")
    if cfg.get("ssh_host"):
        result = await _post_gerrit_review_ssh(
            cfg,
            change_id=str(change_id),
            patchset=str(patchset or ""),
            message=message,
            verified=verified,
        )
        if rest_error:
            result["rest_error"] = rest_error
        return result
    return {
        "sent": False,
        "error": rest_error or "Gerrit review transport is not configured",
    }


def _rest_ssl(cfg: dict[str, Any]):
    """REST outbound TLS 策略。

    默认校验证书（与 Basic Auth 配套，防 MITM 凭据泄露）；自签内网
    Gerrit 经 ``rest_ca_cert`` 提供私有 CA。仅当已保存配置显式写
    ``rest_verify_ssl=false`` 时才兼容关闭（新配置默认 True）。
    """
    if not bool(cfg.get("rest_verify_ssl", True)):
        return False
    ca_cert = str(cfg.get("rest_ca_cert") or "").strip()
    if ca_cert:
        return ssl.create_default_context(cafile=ca_cert)
    return True


async def _post_gerrit_review_rest(
    cfg: dict[str, Any],
    *,
    change_id: str,
    revision: str,
    message: str,
    verified: int | None,
) -> dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    url = (
        f"{base_url}/a/changes/{quote(change_id, safe='')}/revisions/"
        f"{quote(revision, safe='')}/review"
    )
    payload: dict[str, Any] = {"message": message}
    if verified is not None:
        payload["labels"] = {"Verified": int(verified)}
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        target = await asyncio.to_thread(resolve_gerrit_target, url)
    except UnsafeOutboundURL:
        return {"sent": False, "source": "rest", "error": "Gerrit target is not authorized for outbound access"}
    connector = aiohttp.TCPConnector(ssl=_rest_ssl(cfg), resolver=PinnedOutboundResolver(target))
    auth = aiohttp.BasicAuth(
        str(cfg.get("rest_username") or ""),
        str(cfg.get("rest_password") or ""),
    )
    try:
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, auth=auth
        ) as session, session.post(url, json=payload, allow_redirects=False) as response:
            if response.status >= 300:
                return {
                    "sent": False,
                    "source": "rest",
                    "error": f"REST {response.status}",
                }
        return {"sent": True, "source": "rest", "change_id": change_id}
    except Exception as exc:
        return {"sent": False, "source": "rest", "error": str(exc)}


async def _post_gerrit_review_ssh(
    cfg: dict[str, Any],
    *,
    change_id: str,
    patchset: str,
    message: str,
    verified: int | None,
) -> dict[str, Any]:
    try:
        command = await asyncio.to_thread(_gerrit_ssh_prefix, cfg)
    except UnsafeOutboundURL as exc:
        return {"sent": False, "source": "ssh", "error": str(exc)}
    command.extend(["gerrit", "review"])
    if message:
        command.extend(["--message", message])
    if verified is not None:
        command.extend(["--label", f"Verified={int(verified):+d}"])
    revision = f"{change_id},{patchset}" if patchset else change_id
    command.append(revision)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return {"sent": False, "source": "ssh", "error": "Gerrit review timed out"}
    except OSError:
        return {"sent": False, "source": "ssh", "error": "Gerrit SSH 无法启动"}
    if process.returncode != 0:
        return {
            "sent": False,
            "source": "ssh",
            "error": stderr.decode("utf-8", errors="replace").strip(),
        }
    return {
        "sent": True,
        "source": "ssh",
        "change_id": change_id,
        "output": stdout.decode("utf-8", errors="replace").strip(),
    }


def _select_profile(cfg: dict[str, Any], profile_id: str) -> dict[str, Any]:
    profiles = cfg.get("dashboard_profiles") or []
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile
    return profiles[0] if profiles else {"id": "open", "name": "打开的变更", "query": "status:open"}


def _owners_for_department_profile(cfg: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    owners = [str(owner or "").strip() for owner in profile.get("owners") or [] if str(owner or "").strip()]
    if profile.get("id") == "all":
        for department in cfg.get("department_profiles") or []:
            if department.get("id") == "all":
                continue
            owners.extend(str(owner or "").strip() for owner in department.get("owners") or [] if str(owner or "").strip())
    return list(dict.fromkeys(owners))


async def _query_gerrit_via_ssh(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int = 500,
    *,
    commit_message: bool = False,
) -> dict[str, Any]:
    page_size = max(1, min(int(page_size or 500), MAX_QUERY_PAGE_SIZE))
    max_total = None if max_changes is None else max(1, int(max_changes))
    start = 0
    all_items: list[dict[str, Any]] = []
    last_stats: dict[str, Any] = {}
    while True:
        remaining = page_size if max_total is None else min(page_size, max_total - len(all_items))
        if remaining <= 0:
            break
        result = await _query_gerrit_via_ssh_once(
            cfg, _query_for_ssh(query, remaining), start=start, commit_message=commit_message,
        )
        if result.get("error"):
            return {**result, "items": all_items, "stats": result.get("stats") or last_stats}
        items = result.get("items") or []
        stats = result.get("stats") or {}
        all_items.extend(items)
        last_stats = stats
        if not items or not stats.get("moreChanges"):
            break
        start += len(items)
    return {"items": all_items, "stats": {**last_stats, "rowCount": len(all_items)}, "error": "", "source": "ssh"}


def _gerrit_ssh_prefix(cfg: dict[str, Any]) -> list[str]:
    host = str(cfg.get("ssh_host") or "").strip()
    port = int(cfg.get("ssh_port") or 29418)
    if not host or any(char in host for char in "/@?#[]") or any(char.isspace() for char in host):
        raise UnsafeOutboundURL("Gerrit SSH 主机无效")
    literal = f"[{host}]" if ":" in host else host
    try:
        target = resolve_gerrit_target(f"https://{literal}:{port}/")
    except UnsafeOutboundURL as exc:
        raise UnsafeOutboundURL("Gerrit SSH 目标未获出站授权，请配置 GMS_GERRIT_ALLOWED_HOSTS") from exc
    host_key_alias = target.hostname if port == 22 else f"[{target.hostname}]:{port}"
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", f"HostName={target.addresses[0]}", "-o", f"HostKeyAlias={host_key_alias}",
        "-o", "ProxyCommand=none", "-o", "ProxyJump=none", "-o", "ControlPath=none",
        "-p", str(port),
    ]
    if cfg.get("ssh_user"):
        cmd.extend(["-l", str(cfg["ssh_user"])])
    identity_file = str(cfg.get("ssh_identity_file") or "").strip()
    if identity_file:
        cmd.extend(["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(identity_file)])
    cmd.extend(["--", target.hostname])
    return cmd


async def _query_gerrit_via_ssh_once(
    cfg: dict[str, Any], query: str, *, start: int = 0, commit_message: bool = False,
) -> dict[str, Any]:
    try:
        cmd = await asyncio.to_thread(_gerrit_ssh_prefix, cfg)
    except UnsafeOutboundURL as exc:
        return {"items": [], "stats": {}, "error": str(exc), "source": "ssh"}
    # OpenSSH joins remote arguments into a command string. Protect the
    # Gerrit query's own quotes from the server's SSH argv parser.
    remote_args = _ssh_gerrit_query_args(query, start=start, commit_message=commit_message)
    cmd.extend([*remote_args[:-1], shlex.quote(remote_args[-1])])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return {"items": [], "stats": {}, "error": "Gerrit SSH 无法启动", "source": "ssh"}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        return {"items": [], "stats": {}, "error": "Gerrit SSH query timed out", "source": "ssh"}
    if proc.returncode != 0:
        return {"items": [], "stats": {}, "error": stderr.decode("utf-8", errors="ignore").strip()}
    items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    error = ""
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "stats":
            stats = obj
        elif obj.get("type") == "error":
            error = str(obj.get("message") or "Gerrit query failed")
        else:
            items.append(obj)
    return {"items": items, "stats": stats, "error": error, "source": "ssh"}


async def query_gerrit_dual_mode(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int | None = None,
    *,
    commit_message: bool = False,
) -> dict[str, Any]:
    rest_error = ""
    effective_page_size = int(page_size or cfg.get("query_page_size") or cfg.get("defaults", {}).get("query_page_size") or 500)
    # 仅当配置了 REST 凭据时才尝试 REST；否则匿名请求对带 nginx HTTP 认证的实例
    # 每次都 401，徒增往返再 fallback SSH。
    if cfg.get("base_url") and cfg.get("rest_username") and cfg.get("rest_password"):
        result = await _query_gerrit_via_rest(
            cfg, query, max_changes=max_changes, page_size=effective_page_size,
            commit_message=commit_message,
        )
        if not result.get("error"):
            return result
        rest_error = result.get("error") or ""
    if cfg.get("ssh_host"):
        result = await _query_gerrit_via_ssh(
            cfg, query, max_changes=max_changes, page_size=effective_page_size,
            commit_message=commit_message,
        )
        if rest_error:
            result["rest_error"] = rest_error
        return result
    if rest_error:
        rest_error = f"{rest_error}; 未配置 SSH fallback，请配置 REST 账号/HTTP Password 或 ssh_host/ssh_user"
    return {"items": [], "stats": {}, "error": rest_error or "Gerrit REST/SSH 未配置", "rest_error": rest_error, "source": ""}


async def _query_gerrit_via_rest(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int = 500,
    *,
    commit_message: bool = False,
) -> dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    max_total = None if max_changes is None else max(1, int(max_changes))
    page_size = max(1, min(int(page_size or 500), MAX_QUERY_PAGE_SIZE))
    api_prefix = "/a" if cfg.get("rest_username") and cfg.get("rest_password") else ""
    auth = None
    if api_prefix:
        auth = aiohttp.BasicAuth(str(cfg["rest_username"]), str(cfg["rest_password"]))
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        target = await asyncio.to_thread(resolve_gerrit_target, base_url)
    except UnsafeOutboundURL:
        return {"items": [], "stats": {}, "source": "rest", "error": "Gerrit target is not authorized for outbound access"}
    connector = aiohttp.TCPConnector(ssl=_rest_ssl(cfg), resolver=PinnedOutboundResolver(target))
    headers = {"Accept": "application/json"}
    items: list[dict[str, Any]] = []
    start = 0
    more_changes = False
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, auth=auth, headers=headers) as session:
            while max_total is None or len(items) < max_total:
                remaining = page_size if max_total is None else min(page_size, max_total - len(items))
                if remaining <= 0:
                    break
                params = [
                    ("q", _query_without_limit(query)),
                    ("n", str(remaining)),
                    ("S", str(start)),
                    ("o", "DETAILED_ACCOUNTS"),
                    ("o", "LABELS"),
                    ("o", "SUBMITTABLE"),
                ]
                if commit_message:
                    params.extend((("o", "CURRENT_REVISION"), ("o", "CURRENT_COMMIT")))
                url = f"{base_url}{api_prefix}/changes/?{urlencode(params)}"
                async with session.get(url, allow_redirects=False) as response:
                    if response.status >= 300:
                        return {"items": [], "stats": {}, "error": f"REST {response.status}", "source": "rest"}
                    text = await response.text()
                    rows = _decode_gerrit_rest_json(text)
                if not isinstance(rows, list):
                    return {"items": [], "stats": {}, "error": "REST response is not a list", "source": "rest"}
                items.extend(rows)
                more_changes = bool(rows and rows[-1].get("_more_changes"))
                if not more_changes:
                    break
                start += len(rows)
    except Exception as exc:
        return {"items": [], "stats": {}, "error": str(exc), "source": "rest"}
    return {
        "items": items, "stats": {"rowCount": len(items), "moreChanges": more_changes},
        "error": "", "source": "rest",
    }


def _decode_gerrit_rest_json(text: str) -> Any:
    clean = text.lstrip()
    if clean.startswith(")]}'"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else ""
    return json.loads(clean or "[]")


def _query_without_limit(query: str) -> str:
    return " ".join(part for part in str(query or "").split() if not part.lower().startswith("limit:")).strip()


def _query_with_limit(query: str, limit: int) -> str:
    clean = _query_without_limit(query)
    return f"{clean} limit:{max(1, min(int(limit or 100), 5000))}".strip()


def _query_for_ssh(query: str, limit: int) -> str:
    parts = [
        part for part in str(query or "").split()
        if part.lower() != "status:any"
    ]
    return _query_with_limit(" ".join(parts), limit)


def _ssh_gerrit_query_args(
    query: str, *, start: int = 0, commit_message: bool = False,
) -> list[str]:
    """Build the remote argv after the SSH target.

    ``--start`` is a gerrit flag. Putting it inside the query string makes
    later pages search for the literal text instead of paginating.
    ``commit_message`` asks for the current patch set body so callers can
    attribute a hit to a specific upstream SHA or bug number.
    """
    args = ["gerrit", "query", "--format=JSON", "--submit-records"]
    if commit_message:
        args.extend(["--current-patch-set", "--commit-message"])
    if start > 0:
        args.extend(["--start", str(int(start))])
    args.extend(["--", query])
    return args


def _effective_history_limit(profile: dict[str, Any], defaults: dict[str, Any]) -> int | None:
    raw = profile.get("max_history_changes")
    if raw in (None, ""):
        raw = defaults.get("max_history_changes")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = int(profile.get("query_limit") or defaults.get("query_limit") or 500)
    if parsed <= 0:
        return None
    return parsed


def _extract_query_limit(query: str) -> int | None:
    for part in str(query or "").split():
        if part.lower().startswith("limit:"):
            try:
                return max(1, min(int(part.split(":", 1)[1]), 5000))
            except (TypeError, ValueError):
                return None
    return None
