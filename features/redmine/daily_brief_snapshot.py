"""Daily Brief 快照构建（Phase 2/5/6）。

单一事实来源：直接调用 ``RedmineAgentDB.get_workload_statistics()`` 的
``lists["waiting_my_reply"]`` / ``lists["no_reply_3_days"]``，本模块不做
任何「谁需要回复」的业务判断。

快照一经生成立即冻结（canonical JSON + SHA256）；分析阶段只读冻结数据。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from .api import get_redmine_service_for_owner
from .daily_brief_models import base_priority_score, priority_from_score
from .users import (
    display_names_from_mapping,
    find_user_mapping_for_names,
    load_redmine_user_map_for_owner,
)


logger = logging.getLogger(__name__)


# 本地时区的当天日期（brief_date / 目录都以本地时区为准）
DEFAULT_STALE_DAYS = 3
DEFAULT_LIST_LIMIT = 100


class DailyBriefIdentityError(RuntimeError):
    """无法确定 owner 对应的单个 Redmine 身份（fail-closed，不做兜底展开）。"""


async def resolve_daily_brief_owner_identity(
    owner_id: str,
    service: Any | None = None,
) -> dict[str, Any]:
    """把 owner 解析为**单个** Redmine 用户（与个人看板同语义）。

    优先级与 statistics_api 的个人视图一致：
    1. owner 配置的 Redmine 用户名在 user map 中命中 → 该映射（单人）；
    2. Redmine 当前登录用户（live）；
    3. 都失败 → DailyBriefIdentityError。

    永不把 user map 中的部门全员当作 owner（那是部门视图，不是个人晨报），
    也不在无身份时回退 None（那会让 repository 展开为全部 assignee）。
    """
    svc = service or get_redmine_service_for_owner(owner_id)

    username = ""
    user_map: list[dict[str, Any]] = []
    try:
        config = svc.agent.config_manager.load_config() or {}
        username = str(
            ((config.get("redmine_auth") or {}).get("username"))
            or ((config.get("redmine") or {}).get("username"))
            or ""
        ).strip()
    except Exception:
        username = ""
    try:
        user_map = load_redmine_user_map_for_owner(owner_id)
    except Exception:
        user_map = []

    if username and user_map:
        mapped = find_user_mapping_for_names(user_map, [username])
        if mapped:
            try:
                user_id = int(mapped.get("id") or 0) or None
            except (TypeError, ValueError):
                user_id = None
            return {"user_id": user_id, "names": display_names_from_mapping(mapped)}

    client = svc.agent._make_client()
    try:
        user = await client.get_current_user()
    finally:
        await client.close()
    if user is None:
        raise DailyBriefIdentityError(
            f"cannot resolve Redmine identity for owner {owner_id!r}: "
            "configure Redmine credentials or the user-map entry first"
        )
    first = str(getattr(user, "firstname", "") or "").strip()
    last = str(getattr(user, "lastname", "") or "").strip()
    login = str(getattr(user, "login", "") or "").strip()
    mail = str(getattr(user, "mail", "") or getattr(user, "email", "") or "").strip()
    names = list(dict.fromkeys(name for name in (
        f"{last} {first}".strip(), f"{first} {last}".strip(), mail, login,
    ) if name))
    try:
        user_id = int(user.id)
    except (TypeError, ValueError):
        user_id = None
    return {"user_id": user_id, "names": names}


async def _sync_owner_issue_snapshots(
    service: Any,
    user_id: int | None,
    *,
    list_limit: int,
    window_days: int,
) -> bool:
    """快照前把该用户的 Redmine issue 同步进本地库（与看板刷新同源）。"""
    if user_id is None:
        return False
    from .users import refresh_assignee_issue_snapshots

    client = service.agent._make_client()
    try:
        return await refresh_assignee_issue_snapshots(
            client,
            service.repository,
            user_id,
            issue_limit=max(int(list_limit or 0), 100),
            window_days=window_days,
        )
    finally:
        await client.close()



def brief_date_today(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def issue_fingerprint(entry: dict[str, Any]) -> str:
    """快照指纹：issue 内容变化（journal/附件/更新时间）则指纹变化。

    未带 fingerprint 的旧 entry 视为「必然变化」，强制重新分析。
    """
    material = {
        "issue_id": entry.get("issue_id"),
        "updated_on": entry.get("updated_on"),
        "last_external_reply_at": entry.get("last_external_reply_at"),
        "last_owner_reply_at": entry.get("last_owner_reply_at"),
        "attachment_count": entry.get("attachment_count"),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def detect_delta(
    previous_issues: list[dict[str, Any]],
    current_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delta refresh 比较（Phase 8/30/31）。

    返回需要重新分析的 issue（新增或 fingerprint 变化）与不再待处理的
    issue（no_longer_pending）。缺 fingerprint 的旧记录一律视为变化。
    """
    previous_by_id = {int(item["issue_id"]): item for item in previous_issues}
    current_by_id = {int(item["issue_id"]): item for item in current_issues}

    changed: list[dict[str, Any]] = []
    for issue_id, entry in current_by_id.items():
        old = previous_by_id.get(issue_id)
        old_fp = (old or {}).get("fingerprint")
        if old is None or not old_fp or old_fp != entry.get("fingerprint"):
            changed.append(entry)
    removed = sorted(set(previous_by_id) - set(current_by_id))
    return {"changed": changed, "no_longer_pending": removed}


def _merge_buckets(
    waiting: list[dict[str, Any]],
    stale: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """两个 bucket 按 issue_id 去重合并；同一 issue 只保留一份、bucket 叠加。"""
    merged: dict[int, dict[str, Any]] = {}
    for bucket_name, entries in (("waiting_my_reply", waiting), ("no_reply_3_days", stale)):
        for item in entries:
            try:
                issue_id = int(item.get("issue_id"))
            except (TypeError, ValueError):
                continue
            entry = merged.setdefault(
                issue_id,
                {
                    "issue_id": issue_id,
                    "buckets": [],
                    **{k: item.get(k) or "" for k in (
                        "subject", "status_name", "priority_name", "assigned_to_name",
                        "created_on", "updated_on", "last_external_reply_at",
                        "last_external_reply_by", "last_owner_reply_at",
                    )},
                    "attachment_count": int(item.get("attachment_count") or 0),
                },
            )
            if bucket_name not in entry["buckets"]:
                entry["buckets"].append(bucket_name)
            # 保留两个 bucket 中更完整的字段值（等待天数等）。
            for key in ("unreplied_days", "last_reply_side"):
                value = item.get(key)
                if value not in (None, "") and not entry.get(key):
                    entry[key] = value
    return merged


def _unreplied_days(entry: dict[str, Any], snapshot_at: datetime) -> float:
    last = str(entry.get("last_external_reply_at") or "").strip()
    if not last:
        return 0.0
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return 0.0
    return round(max(0.0, (snapshot_at - last_dt).total_seconds() / 86400.0), 1)


async def build_daily_triage_snapshot(
    owner_id: str,
    stale_days: int = DEFAULT_STALE_DAYS,
    list_limit: int = DEFAULT_LIST_LIMIT,
    organization_user_map: list[dict[str, Any]] | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    """构建并冻结当天 triage 快照（严格单人视角）。

    返回::

        {
          "brief_date": "2026-09-13",
          "generated_at": "...",
          "owner": {"id": ..., "names": [...]},
          "counts": {"waiting_my_reply": 6, "no_reply_3_days": 3, "total": 8},
          "issues": [ {issue_id, buckets, fingerprint, priority, ...}, ... ],
          "snapshot_hash": "sha256..."
        }

    ``organization_user_map`` 仅作显式覆盖用于测试，正常调用不传；
    owner 身份始终经 resolve_daily_brief_owner_identity 解析为单个用户。
    """
    service = get_redmine_service_for_owner(owner_id)

    identity = await resolve_daily_brief_owner_identity(owner_id, service)
    owner_names = identity["names"]
    if not owner_names:
        raise DailyBriefIdentityError(
            f"resolved identity for owner {owner_id!r} carries no usable names"
        )
    user_map = organization_user_map

    # 默认先同步 Redmine（本地 SQLite 只是镜像；不同步会分析过期数据）。
    # pre-sync 失败不阻断晨报（本地镜像兜底），但必须在快照里显式标记
    # source_sync_status，让 run/报告可见数据可能是过期的。
    synced = False
    source_sync_status = "skipped"  # refresh=False：明确跳过同步
    if refresh:
        source_sync_status = "sync_failed"
        try:
            stats_cfg: dict[str, Any] = {}
            try:
                stats_cfg = service.agent.config_manager.get_redmine_stats_config()
            except Exception:
                stats_cfg = {}
            synced = await _sync_owner_issue_snapshots(
                service,
                identity.get("user_id"),
                list_limit=list_limit,
                window_days=int(stats_cfg.get("window_days") or 0),
            )
            if synced:
                source_sync_status = "synced"
        except Exception:
            logger.warning("daily brief pre-sync failed for %s; using local snapshot", owner_id)

    snapshot_at = datetime.now()
    workload = service.repository.get_workload_statistics(
        owner_names=owner_names,
        stale_days=stale_days,
        list_limit=list_limit,
        organization_user_map=user_map,
    )
    lists = workload.get("lists") or {}
    waiting = list(lists.get("waiting_my_reply") or [])
    stale = list(lists.get("no_reply_3_days") or [])

    merged = _merge_buckets(waiting, stale)
    issues: list[dict[str, Any]] = []
    for entry in merged.values():
        entry["unreplied_days"] = _unreplied_days(entry, snapshot_at)
        score = base_priority_score(entry)
        entry["priority_score"] = score
        entry["priority"] = priority_from_score(score)
        entry["fingerprint"] = issue_fingerprint(entry)
        issues.append(entry)
    # 排序确定性：分数降序 → issue_id 升序。
    issues.sort(key=lambda item: (-int(item.get("priority_score") or 0), int(item["issue_id"])))

    snapshot = {
        "brief_date": brief_date_today(snapshot_at),
        "generated_at": snapshot_at.isoformat(timespec="seconds"),
        "stale_days": stale_days,
        "synced": synced,
        "source_sync_status": source_sync_status,
        "owner": {"id": owner_id, "names": owner_names, "user_id": identity.get("user_id")},
        "counts": {
            "waiting_my_reply": len(waiting),
            "no_reply_3_days": len(stale),
            "total": len(issues),
        },
        "issues": issues,
    }
    snapshot["snapshot_hash"] = hashlib.sha256(
        _canonical_json(snapshot["counts"]) .encode("utf-8")
        + _canonical_json(issues).encode("utf-8")
    ).hexdigest()
    return snapshot


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_STALE_DAYS",
    "DailyBriefIdentityError",
    "brief_date_today",
    "build_daily_triage_snapshot",
    "detect_delta",
    "issue_fingerprint",
    "resolve_daily_brief_owner_identity",
]
