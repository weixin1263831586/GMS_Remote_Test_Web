"""周报总结聚合 API。

聚合 Redmine 个人看板 (workload 统计) 与 Gerrit 个人看板 (personal 统计 +
review-queue) 数据，按用户给定的起止区间生成结构化周报数据，供前端渲染为
Markdown 报告。

归属人取当前登录用户/默认 owner，与各个人看板页一致。任一数据源缺凭证或
未配置时，该项降级为 null + error，不影响另一项与整体返回。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from foundation.responses import error_response, success_response

router = APIRouter()


def _last_week_range(today: date | None = None) -> tuple[date, date]:
    """返回上一个完整自然周的 (周一, 周日)。"""
    today = today or date.today()
    # 本周一 (weekday(): 周一=0)
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _iso_week_keys(start: date, end: date) -> set[str]:
    """区间 [start, end] 覆盖到的所有 ISO 周 ('YYYY-Www')。"""
    keys: set[str] = set()
    cur = start
    while cur <= end:
        iso = cur.isocalendar()
        keys.add(f"{iso.year}-W{iso.week:02d}")
        cur += timedelta(days=1)
    return keys


def _coerce_dt(value: Any) -> datetime | None:
    """容忍 ISO8601 / 空格分隔 / 仅日期 等多种写法 (含时区与小数秒)。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    # fromisoformat 在 3.11+ 已支持 'Z' 与大多数时区写法；这里兜底处理 'Z'。
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(fmt)], fmt)
        except ValueError:
            continue
    return None


def _in_range(value: Any, start: date, end: date) -> bool:
    """value 落在 [start, end] 全天区间内 (含首尾)。"""
    dt = _coerce_dt(value)
    if not dt:
        return False
    return start <= dt.date() <= end


async def _collect_redmine(request: Request, start: date, end: date, name: str = "") -> dict[str, Any]:
    # 复用 workload 端点：自带缓存、owner 解析、live counts。
    # name 非空时按指定人查询（部门周报）；为空时按当前登录用户。
    from features.redmine.statistics_api import get_workload_statistics

    payload = await get_workload_statistics(request, stale_days=None, list_limit=50, name=name, refresh=False)
    if not payload.get("success"):
        return {"available": False, "error": payload.get("error") or "Redmine 统计不可用"}
    data = payload.get("data") or {}

    week_keys = _iso_week_keys(start, end)
    resolved_this_period = sum(
        item.get("count", 0)
        for item in (data.get("resolved_weekly") or [])
        if item.get("week") in week_keys
    )

    lists = data.get("lists") or {}
    return {
        "available": True,
        "resolved_this_period": resolved_this_period,
        "total_owned": data.get("total_owned", 0),
        "open_count": data.get("open_count", 0),
        "waiting_my_reply": data.get("waiting_my_reply", 0),
        "no_reply_3_days": data.get("no_reply_3_days", 0),
        "owner_names": (data.get("meta") or {}).get("owner_names") or [],
        "lists": {
            "open_issues": lists.get("open_issues") or [],
            "waiting_my_reply": lists.get("waiting_my_reply") or [],
            "no_reply_3_days": lists.get("no_reply_3_days") or [],
        },
    }


async def _collect_gerrit(request: Request, start: date, end: date, owner: str = "") -> dict[str, Any]:
    from features.gerrit.api import (
        get_gerrit_personal_statistics,
        get_review_queue_count,
    )

    personal = await get_gerrit_personal_statistics(request, profile_id="", owner=owner, refresh=False)
    if not personal.get("success"):
        return {"available": False, "error": personal.get("error") or "Gerrit 统计不可用"}
    data = personal.get("data") or {}
    summary = data.get("summary") or {}
    lists = data.get("lists") or {}

    merged = lists.get("merged") or []
    opened = lists.get("open") or []

    merged_in_range = [
        c for c in merged
        if _in_range(c.get("updated") or c.get("created"), start, end)
    ]
    new_in_range = [
        c for c in opened
        if _in_range(c.get("created"), start, end)
    ]

    review_queue_count: Any = None
    try:
        rq = await get_review_queue_count(request, owner=owner, refresh=False)
        if rq.get("success"):
            review_queue_count = ((rq.get("data") or {}).get("count"))
    except Exception:
        pass

    return {
        "available": True,
        "merged_this_period": len(merged_in_range),
        "new_this_period": len(new_in_range),
        "pending_review": summary.get("pending_review_count", 0),
        "open_count": summary.get("open_count", 0),
        "total_count": summary.get("total_count", 0),
        "review_queue_count": review_queue_count,
        "owner": data.get("owner") or "",
        "lists": {
            "merged": merged_in_range,
            "new": new_in_range,
        },
    }


@router.get("/api/reports/weekly-report")
async def get_weekly_report(
    request: Request,
    start: str = Query(""),
    end: str = Query(""),
):
    resolved = _resolve_range(start, end)
    if not isinstance(resolved, tuple):
        return resolved  # error_response
    start_date, end_date, is_default = resolved

    redmine = await _collect_redmine(request, start_date, end_date)
    gerrit = await _collect_gerrit(request, start_date, end_date)
    member = {"owner": "", "name": "我自己", "redmine": redmine, "gerrit": gerrit}
    _member_themes(member)

    data = {
        "range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "label": "上周" if is_default else "自定义",
        },
        "redmine": redmine,
        "gerrit": gerrit,
        "themes": member["themes"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return success_response(data=data, message="周报已生成")


# ---------------------------------------------------------------------------
# Top 主题提取：从工单/提交标题中抽取关键标签 (芯片型号 / Android 版本 /
# 测试套件 / 业务关键词)，按出现频次排序，作为周报精炼版的主题清单。
# ---------------------------------------------------------------------------

# 芯片型号：RKxxxx / PXxx / rv1126 等
_CHIP_RE = re.compile(r"\b(RK\s?\d{4,}|PX\d{2,}|RV\s?\d{4}|rv1126)", re.IGNORECASE)
# Android 版本：Android16 / 安卓 16 / A14 等
_ANDROID_RE = re.compile(r"(Android\s*\d{1,2}|安卓\s*\d{1,2}|A(?:ndroid)?\s?\d{1,2})", re.IGNORECASE)
# 测试套件模块：CtsXxx / GtsXxx / VtsXxx / STS / BTS / Mainline / VBA / EDLA / GMS Express
_SUITE_RE = re.compile(
    r"\b((?:Cts|Gts|Vts|Sts|Bts)[A-Za-z0-9_]*|Mainline|VBA|EDLA|GMS\s?Express|APEX|VBMeta)",
    re.IGNORECASE,
)

_NORMALIZE = {
    "安卓": "Android",
}


_NORMALIZE_TAG = {
    # 统一常见同义/大小写写法
    "mainline": "Mainline",
    "apex": "APEX",
    "vbmeta": "VBMeta",
    "bts": "BTS",
    "sts": "STS",
    "vba": "VBA",
    "edla": "EDLA",
}


def _extract_themes(subjects: list[str], top_n: int = 8) -> list[dict[str, Any]]:
    """从标题列表抽取 Top 主题标签。返回 [{tag, count}]，按 count 降序。"""
    counter: dict[str, int] = {}
    for raw in subjects or []:
        text = str(raw or "")
        for pattern in (_CHIP_RE, _ANDROID_RE, _SUITE_RE):
            for match in pattern.findall(text):
                tag = re.sub(r"\s+", "", str(match))
                for src, dst in _NORMALIZE.items():
                    tag = tag.replace(src, dst)
                low = tag.lower()
                if low in _NORMALIZE_TAG:
                    tag = _NORMALIZE_TAG[low]
                elif low.startswith(("rk", "px", "rv")):
                    tag = low.upper()
                elif low.startswith("android"):
                    tag = "Android" + re.sub(r"[^0-9]", "", tag)
                counter[tag] = counter.get(tag, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"tag": tag, "count": cnt} for tag, cnt in ranked[:top_n]]


def _member_themes(member: dict[str, Any]) -> dict[str, Any]:
    """为单个成员的 redmine/gerrit 数据补上 Top 主题标签。"""
    rm = member.get("redmine") or {}
    gr = member.get("gerrit") or {}
    rm_subjects: list[str] = []
    if rm.get("available") is not False:
        rm_lists = rm.get("lists") or {}
        rm_subjects = [
            it.get("subject", "")
            for key in ("waiting_my_reply", "no_reply_3_days", "open_issues")
            for it in (rm_lists.get(key) or [])
        ]
    gr_subjects: list[str] = []
    if gr.get("available") is not False:
        gr_lists = gr.get("lists") or {}
        gr_subjects = [it.get("subject", "") for it in (gr_lists.get("merged") or [])]
        gr_subjects += [it.get("subject", "") for it in (gr_lists.get("new") or [])]
    member["themes"] = {
        "redmine": _extract_themes(rm_subjects),
        "gerrit": _extract_themes(gr_subjects),
    }
    return member


def _resolve_range(start: str, end: str) -> tuple[date, date, bool] | Any:
    """解析起止日期，返回 (start, end, is_default) 或 error_response。"""
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    default_start, default_end = _last_week_range()
    is_default = False
    if start_date is None and end_date is None:
        start_date, end_date = default_start, default_end
        is_default = True
    else:
        if start_date is None:
            start_date = default_start
        if end_date is None:
            end_date = default_end
        if end_date < start_date:
            return error_response("结束日期不能早于开始日期", status_code=400)
    return start_date, end_date, is_default


def _resolve_department_members(request: Request, profile_id: str) -> list[dict[str, str]]:
    """从 Gerrit 部门 profile 解析成员列表 [{owner(邮箱), name(中文姓名)}]。

    中文姓名优先 redmine_user_map，其次 personal_profiles.name，最后邮箱前缀。
    """
    from features.gerrit.api import (
        _dashboard_config_for_request,
        _redmine_users_for_request,
        _owners_for_department_profile,
    )
    from features.gerrit.config import select_gerrit_department_profile

    cfg = _dashboard_config_for_request(request)
    profile = select_gerrit_department_profile(cfg, profile_id)
    owners = _owners_for_department_profile(cfg, profile)

    name_by_email: dict[str, str] = {}
    try:
        for entry in _redmine_users_for_request(request):
            email = str(entry.get("email") or "").strip().lower()
            name = str(entry.get("name") or "").strip()
            if email and name:
                name_by_email[email] = name
    except Exception:
        pass
    for p in cfg.get("personal_profiles") or []:
        email = str(p.get("owner") or "").strip().lower()
        name = str(p.get("name") or "").strip()
        if email and name and email not in name_by_email:
            name_by_email[email] = name

    members = []
    for owner_text in owners:
        key = str(owner_text or "").strip().lower()
        name = name_by_email.get(key) or str(owner_text or "").split("@")[0] or owner_text
        members.append({"owner": owner_text, "name": name})
    return members


@router.get("/api/reports/weekly-report/department")
async def get_weekly_report_department(
    request: Request,
    profile_id: str = Query(""),
    owner: str = Query(""),  # 指定单个成员邮箱；为空则返回成员名单供前端选择
    start: str = Query(""),
    end: str = Query(""),
):
    members = _resolve_department_members(request, profile_id)
    if not owner:
        # 仅返回成员名单 + 可用 profile，供前端下拉选择
        return success_response(data={
            "members": members,
            "profile_id": profile_id,
        }, message="成员名单")

    resolved = _resolve_range(start, end)
    if not isinstance(resolved, tuple):
        return resolved  # error_response
    start_date, end_date, is_default = resolved

    # 找到选中成员的中文姓名（用于 Redmine name 查询）
    selected = next((m for m in members if m["owner"] == owner), None)
    member_name = (selected or {}).get("name") or ""

    redmine = await _collect_redmine(request, start_date, end_date, name=member_name)
    gerrit = await _collect_gerrit(request, start_date, end_date, owner=owner)
    member = {
        "owner": owner,
        "name": member_name or owner,
        "redmine": redmine,
        "gerrit": gerrit,
    }
    _member_themes(member)

    data = {
        "range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "label": "上周" if is_default else "自定义",
        },
        "member": member,
        "members": members,  # 便于前端切换成员时无需再请求一次
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return success_response(data=data, message="部门成员周报已生成")


# ---------------------------------------------------------------------------
# AI 周报总结：读取代表性 Redmine 工单的完整内容(描述+回复日志)，用本地 AI
# 生成"本周做了什么 / 解决了什么问题"的总结段落。
# ---------------------------------------------------------------------------

def _issue_body_for_ai(issue: dict[str, Any]) -> str:
    """把单个工单的标题/状态/描述/回复日志压成 AI 友好的文本块。"""
    issue_id = issue.get("issue_id") or "?"
    subject = (issue.get("subject") or "").strip()
    status = (issue.get("status_name") or "").strip()
    priority = (issue.get("priority_name") or "").strip()
    desc = (issue.get("description") or "").strip()
    journals = issue.get("journals_json") or []
    notes = []
    for j in journals:
        note = str(j.get("notes") or "").strip()
        if note:
            who = str(j.get("user") or j.get("author") or "").strip()
            notes.append(f"[{who}] {note}" if who else note)
    parts = [f"#{issue_id} {subject}".strip()]
    if status:
        parts.append(f"状态：{status}")
    if priority:
        parts.append(f"优先级：{priority}")
    if desc:
        parts.append(f"描述：{desc[:500]}")
    if notes:
        # 最近几条回复最能体现"解决了什么"
        parts.append("近期回复：\n" + "\n".join(notes[-6:]))
    return "\n".join(parts)


def _collect_representative_issues(
    request: Request,
    redmine: dict[str, Any],
    start: date,
    end: date,
    owner_names: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """取代表性工单的完整详情(描述+journals)，供 AI 总结。

    覆盖三类：
    1. 本周内关闭的工单(closed_on 落在区间，且属于该 owner) —— 最能体现"解决了什么"；
    2. 超期未回复 / 待我回复 / 近期更新的开放工单。
    只取少量(limit)，避免把上千工单全喂给 AI。
    """
    from features.redmine.api import get_redmine_service_for_request
    from features.redmine.users import _name_keys

    lists = redmine.get("lists") or {}
    candidate_ids: list[int] = []
    seen: set[int] = set()
    for key in ("no_reply_3_days", "waiting_my_reply", "open_issues"):
        for it in lists.get(key) or []:
            try:
                iid = int(it.get("issue_id") or it.get("id") or 0)
            except (ValueError, TypeError):
                continue
            if iid and iid not in seen:
                seen.add(iid)
                candidate_ids.append(iid)

    issues: list[dict[str, Any]] = []
    try:
        repo = get_redmine_service_for_request(request).repository

        # 1) 本周关闭的工单：按 owner + closed_on 区间直接在 SQL 过滤。
        #    closed_on 可能是 '2026-06-26' 或 '2026-06-26T07:16:08'，用字符串区间
        #    比较 (>= start, < end+1day) 兼容两种格式。
        owner_keys: set = set()
        for n in owner_names or []:
            owner_keys.update(_name_keys(n))
        end_next = (end + timedelta(days=1)).isoformat()
        try:
            with repo.connect() as conn:
                if owner_keys:
                    # 按 assigned_to_name 做宽匹配（姓名可能带空格/不同写法）
                    like_clauses = " OR ".join(
                        ["assigned_to_name LIKE ?"] * len(owner_keys)
                    )
                    params = [f"%{k}%" for k in owner_keys]
                    rows = conn.execute(
                        f"SELECT * FROM redmine_agent_issues "
                        f"WHERE is_resolved = 1 AND closed_on IS NOT NULL AND closed_on != '' "
                        f"AND closed_on >= ? AND closed_on < ? "
                        f"AND ({like_clauses}) "
                        f"ORDER BY closed_on DESC LIMIT 50",
                        [start.isoformat(), end_next, *params],
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM redmine_agent_issues "
                        "WHERE is_resolved = 1 AND closed_on IS NOT NULL AND closed_on != '' "
                        "AND closed_on >= ? AND closed_on < ? "
                        "ORDER BY closed_on DESC LIMIT 50",
                        [start.isoformat(), end_next],
                    ).fetchall()
            for row in rows:
                decoded = repo._decode_row(row)
                decoded["_category"] = "closed_this_period"
                issues.append(decoded)
        except Exception:
            pass

        # 2) 开放/跟进类：从 workload lists 取完整行
        for iid in candidate_ids:
            if len(issues) >= limit * 2:
                break
            row = repo.get_issue(iid)
            if row:
                issues.append(row)
    except Exception:
        pass
    return issues[:limit * 2]


@router.post("/api/reports/weekly-report/ai-summary")
async def get_weekly_report_ai_summary(request: Request):
    """读取代表性工单完整内容，调用本地 AI 生成周报总结段落。

    Body: {"start","end","owner","redmine","gerrit"}  (前端把已生成的周报数据回传)
    """
    import json as _json

    body = {}
    try:
        raw = await request.body()
        body = _json.loads(raw or b"{}")
    except Exception:
        body = {}

    start = str(body.get("start") or "")
    end = str(body.get("end") or "")
    owner = str(body.get("owner") or "")
    redmine = body.get("redmine") or {}
    gerrit = body.get("gerrit") or {}

    resolved = _resolve_range(start, end)
    if not isinstance(resolved, tuple):
        return resolved
    start_date, end_date, is_default = resolved

    # 若前端没回传 redmine/gerrit，则现取一份
    if not redmine:
        member_name = str(body.get("name") or "")
        redmine = await _collect_redmine(request, start_date, end_date, name=member_name)
    if not gerrit:
        gerrit = await _collect_gerrit(request, start_date, end_date, owner=owner)

    # 取代表性工单完整内容(含本周关闭 + 跟进中)
    owner_names = (redmine.get("owner_names") or []) if redmine.get("available") is not False else []
    if body.get("name"):
        owner_names = [str(body.get("name"))] + [n for n in owner_names if n != body.get("name")]
    rep_issues = _collect_representative_issues(
        request, redmine, start_date, end_date, owner_names=owner_names, limit=10
    ) if redmine.get("available") is not False else []

    # 组装 Gerrit 合并提交标题(主要)
    gr_merged = [c for c in ((gerrit.get("lists") or {}).get("merged") or [])
                 if not re.match(r"^(bump|version bump|bump version|cherry pick|revert)", str(c.get("subject") or ""), re.I)]
    gr_new = [c for c in ((gerrit.get("lists") or {}).get("new") or [])
              if not re.match(r"^(bump|version bump|bump version|cherry pick|revert)", str(c.get("subject") or ""), re.I)]

    issue_blocks = "\n\n".join(
        f"【{'本周已关闭' if it.get('_category') == 'closed_this_period' else '跟进中'}】\n{_issue_body_for_ai(it)}"
        for it in rep_issues
    )
    merged_blocks = "\n".join(f"- {c.get('number','?')} {c.get('subject','')}".strip() for c in gr_merged[:10])
    new_blocks = "\n".join(f"- {c.get('number','?')} {c.get('subject','')}".strip() for c in gr_new[:6])

    user_prompt = (
        f"你是团队成员，请基于以下本周({start_date} ~ {end_date})的工作数据，"
        f"用中文写一份周报的「本周工作总结」，重点说明完成了什么、解决了什么具体问题。\n\n"
        f"要求：\n"
        f"1. 分点陈述，每点一句话说清做了什么/解决了什么，避免空泛。\n"
        f"2. 【本周已关闭】的工单代表已解决的问题，要具体说清问题本身和解决结果。\n"
        f"3. 【跟进中】的工单说明当前进展，可简要提及现状。\n"
        f"4. 合并的代码提交归纳为若干项，不要逐条罗列版本号类机械提交。\n"
        f"5. 语气平实，像人写的周报，不要写「根据数据」之类元话语。\n\n"
        f"=== 本周合并的提交(主要) ===\n{merged_blocks or '(无)'}\n\n"
        f"=== 本周新增/进行中 ===\n{new_blocks or '(无)'}\n\n"
        f"=== 本周跟进/处理的 Redmine 工单(含描述与近期回复) ===\n{issue_blocks or '(无)'}\n"
    )

    # 调本地 AI
    try:
        from features.reports.dependencies import dependencies
        from features.assistant.universal_ai import get_universal_analyzer
    except Exception as e:  # pragma: no cover
        return error_response(f"AI 模块加载失败: {e}", status_code=500)

    factory = dependencies.universal_analyzer_factory or get_universal_analyzer
    try:
        analyzer = factory()
    except Exception as e:
        return error_response(f"AI 分析器初始化失败: {e}", status_code=500)

    system_prompt = "你是资深 Android 系统工程师，擅长把零散的工单/提交记录归纳成清晰的中文周报。"
    # 优先使用本地模型 glm_local；若未启用则回退到通用主 provider
    result = analyzer.generate(
        user_prompt=user_prompt, system_prompt=system_prompt, max_tokens=1500,
        preferred_provider="glm_local"
    )
    if not result.get("success"):
        return error_response(result.get("error") or "AI 生成失败", status_code=502)

    return success_response(data={
        "summary": result.get("content") or "",
        "provider": result.get("provider") or "",
        "range": {"start": start_date.isoformat(), "end": end_date.isoformat(),
                  "label": "上周" if is_default else "自定义"},
        "issue_count": len(rep_issues),
    }, message="AI 周报总结已生成")
