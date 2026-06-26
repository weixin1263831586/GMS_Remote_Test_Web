"""Gerrit query and aggregation helpers."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlencode

import aiohttp

from features.gerrit.config import MAX_QUERY_PAGE_SIZE


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


async def _query_gerrit_via_ssh(cfg: dict[str, Any], query: str, max_changes: int | None = None, page_size: int = 500) -> dict[str, Any]:
    page_size = max(1, min(int(page_size or 500), MAX_QUERY_PAGE_SIZE))
    max_total = None if max_changes is None else max(1, int(max_changes))
    start = 0
    all_items: list[dict[str, Any]] = []
    last_stats: dict[str, Any] = {}
    while True:
        remaining = page_size if max_total is None else min(page_size, max_total - len(all_items))
        if remaining <= 0:
            break
        result = await _query_gerrit_via_ssh_once(cfg, _query_for_ssh(query, remaining, start=start))
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


async def _query_gerrit_via_ssh_once(cfg: dict[str, Any], query: str) -> dict[str, Any]:
    target = cfg["ssh_host"]
    if cfg.get("ssh_user"):
        target = f"{cfg['ssh_user']}@{target}"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(cfg["ssh_port"]),
    ]
    identity_file = str(cfg.get("ssh_identity_file") or "").strip()
    if identity_file:
        cmd.extend(["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(identity_file)])
    cmd.extend([
        target,
        "gerrit",
        "query",
        "--format=JSON",
        "--submit-records",
        query,
    ])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
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


async def _query_gerrit_dual_mode(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    rest_error = ""
    effective_page_size = int(page_size or cfg.get("query_page_size") or cfg.get("defaults", {}).get("query_page_size") or 500)
    # 仅当配置了 REST 凭据时才尝试 REST；否则匿名请求对带 nginx HTTP 认证的实例
    # 每次都 401，徒增往返再 fallback SSH。
    if cfg.get("base_url") and cfg.get("rest_username") and cfg.get("rest_password"):
        result = await _query_gerrit_via_rest(cfg, query, max_changes=max_changes, page_size=effective_page_size)
        if not result.get("error"):
            return result
        rest_error = result.get("error") or ""
    if cfg.get("ssh_host"):
        result = await _query_gerrit_via_ssh(cfg, query, max_changes=max_changes, page_size=effective_page_size)
        if rest_error:
            result["rest_error"] = rest_error
        return result
    if rest_error and not cfg.get("ssh_host"):
        rest_error = f"{rest_error}; 未配置 SSH fallback，请配置 REST 账号/HTTP Password 或 ssh_host/ssh_user"
    return {"items": [], "stats": {}, "error": rest_error or "Gerrit REST/SSH 未配置", "rest_error": rest_error, "source": ""}


async def _query_gerrit_via_rest(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int = 500,
) -> dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    max_total = None if max_changes is None else max(1, int(max_changes))
    page_size = max(1, min(int(page_size or 500), MAX_QUERY_PAGE_SIZE))
    api_prefix = "/a" if cfg.get("rest_username") and cfg.get("rest_password") else ""
    auth = None
    if api_prefix:
        auth = aiohttp.BasicAuth(str(cfg["rest_username"]), str(cfg["rest_password"]))
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(ssl=bool(cfg.get("rest_verify_ssl", False)))
    headers = {"Accept": "application/json"}
    items: list[dict[str, Any]] = []
    start = 0
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
                url = f"{base_url}{api_prefix}/changes/?{urlencode(params)}"
                async with session.get(url, allow_redirects=True) as response:
                    text = await response.text()
                    if response.status >= 400:
                        return {"items": [], "stats": {}, "error": f"REST {response.status}: {text[:300]}", "source": "rest"}
                    rows = _decode_gerrit_rest_json(text)
                if not isinstance(rows, list):
                    return {"items": [], "stats": {}, "error": "REST response is not a list", "source": "rest"}
                items.extend(rows)
                if not rows or not rows[-1].get("_more_changes"):
                    break
                start += len(rows)
    except Exception as exc:
        return {"items": [], "stats": {}, "error": str(exc), "source": "rest"}
    return {"items": items, "stats": {"rowCount": len(items)}, "error": "", "source": "rest"}


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


def _query_for_ssh(query: str, limit: int, start: int = 0) -> str:
    parts = [
        part for part in str(query or "").split()
        if part.lower() != "status:any"
    ]
    clean = _query_with_limit(" ".join(parts), limit)
    if start > 0:
        clean = f"{clean} --start {int(start)}"
    return clean


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

