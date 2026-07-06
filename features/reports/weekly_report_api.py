"""周报总结聚合 API。

聚合 Redmine 个人看板 (workload 统计)、Gerrit 个人看板 (personal 统计 +
review-queue)、Android17 移植计划（腾讯文档）以及 GMS 本地认证测试进展，
按用户给定的起止区间生成结构化周报数据，供前端渲染为 Markdown 报告。

归属人取当前登录用户/默认 owner，与各个人看板页一致。任一数据源缺凭证或
未配置时，该项降级为可用标记 false + error，不影响其他项与整体返回。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from fastapi import APIRouter, Query, Request

from foundation.config import settings
from foundation.responses import error_response, success_response
from foundation.time import parse_datetime

# gms_test 复用「📋 测试结果列表」数据源：tradefed list results（文本表格，几 KB），
# 不再 etree.parse 整个 CTS XML（1.5GB）。这些函数/对象在调用时才取（避免启动期循环导入）。
from features.test_execution import runtime as te_runtime
from features.test_execution.suite_helpers import _get_available_test_suites
from features.test_execution.suites import get_default_suites_path
from features.test_execution.tradefed import execute_tradefed_command, parse_tradefed_list_results

logger = logging.getLogger(__name__)
router = APIRouter()


ANDROID17_SHEET_URL = "https://docs.qq.com/sheet/DQnVLa3NVeHdISXpy?tab=BB08J2"


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


def _in_range(value: Any, start: date, end: date) -> bool:
    """value 落在 [start, end] 全天区间内 (含首尾)。"""
    dt = parse_datetime(value)
    if not dt:
        return False
    return start <= dt.date() <= end


def _bool_param(value: str | None) -> bool:
    """解析前端传来的 scope 开关参数（1/true/on/yes 均视为启用）。"""
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


# ---------------------------------------------------------------------------
# Android17 腾讯文档解析
# ---------------------------------------------------------------------------

def _parse_tencent_docs_ssr(html: str) -> list[dict[str, Any]]:
    """解析腾讯文档 SSR 画布的 record JSON，按列坐标重建成表格行。

    首页 SSR 会把表格首屏渲染成 Canvas 重放记录（``const record=...``），
    里面包含文本索引坐标。我们用列中心点把每个文本片段归到对应列，再按行
    合并成字典。
    """
    m = re.search(r'const record="([^"]+)"', html)
    if not m:
        return []
    decoded = urllib.parse.unquote(m.group(1))
    try:
        obj = json.loads(decoded)
    except Exception:
        return []

    texts = (obj.get("flyweight") or {}).get("texts") or []
    actions = obj.get("actions") or ""
    q_cmds = re.findall(r'q\[(\d+),([\d.]+),([\d.]+)\]', actions)
    placements = [(float(x), float(y), int(idx), texts[int(idx)]) for idx, x, y in q_cmds if int(idx) < len(texts)]
    placements.sort(key=lambda t: (t[1], t[0]))

    # 识别表头列中心点（表头约 y=39.4 附近）
    header_items = [p for p in placements if abs(p[1] - 39.4) < 12]
    header_items.sort(key=lambda p: p[0])
    col_centers = [(x, txt) for x, _, _, txt in header_items]

    if not col_centers:
        return []

    def nearest_col(x: float) -> tuple[float, str] | None:
        best = None
        best_d = 1e9
        for cx, name in col_centers:
            d = abs(x - cx)
            if d < best_d:
                best_d, best = d, (cx, name)
        return best

    # 按 y 坐标分组成行
    rows_raw: list[list[tuple[float, float, int, str]]] = []
    cur: list[tuple[float, float, int, str]] = []
    last_y: float | None = None
    for x, y, idx, txt in placements:
        if last_y is None or abs(y - last_y) < 12:
            cur.append((x, y, idx, txt))
        else:
            rows_raw.append(cur)
            cur = [(x, y, idx, txt)]
        last_y = y
    if cur:
        rows_raw.append(cur)

    headers = [name for _, name in col_centers]
    result: list[dict[str, Any]] = []
    for row_cells in rows_raw:
        by_col: dict[str, list[str]] = {}
        for x, y, idx, txt in row_cells:
            matched = nearest_col(x)
            if not matched:
                continue
            name = matched[1]
            by_col.setdefault(name, []).append(txt)
        if not by_col:
            continue
        row = {k: " ".join(v).strip() for k, v in by_col.items()}
        # 至少包含“负责人”列才视为有效数据行
        if "负责人" in headers and row.get("负责人"):
            result.append(row)
    return result


def _owner_matches(name: str, target: str) -> bool:
    """宽松匹配负责人姓名（忽略空格/大小写/前后缀）。"""
    a = re.sub(r"\s+", "", name or "").lower()
    b = re.sub(r"\s+", "", target or "").lower()
    return a == b or (len(a) >= 2 and a in b) or (len(b) >= 2 and b in a)


def _extract_date_tokens(text: str) -> list[date]:
    """从文本中提取所有日期（支持 YYMMDD 前缀 / YYYY/MM/DD / YYYY-M-D）。"""
    out: list[date] = []
    if not text:
        return out
    # YYMMDD 常见于【260630】这种腾讯文档日期前缀
    current_year = date.today().year
    for m in re.finditer(r"【?(\d{2})(\d{2})(\d{2})】?", text):
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + yy
        # 简单校验：年份应接近当前年份
        if year > current_year + 1 or year < current_year - 5:
            continue
        try:
            out.append(date(year, mm, dd))
        except ValueError:
            pass
    for m in re.finditer(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return out


def _task_completed_last_week(row: dict[str, Any], start: date, end: date) -> bool:
    """判断该行是否在上周内完成/验证。

    规则：状态为 Close/已完成/验证OK/验证完成，或进展字段里包含上周日期
    的完成/验证标记。
    """
    status = str(row.get("状态") or "").lower()
    done_keywords = {"close", "已完成", "验证ok", "验证完成", "完成", "已验证"}
    if any(k in status for k in done_keywords):
        return True
    progress = str(row.get("进展【提醒：请按规范提交代码】") or "")
    dates = _extract_date_tokens(progress)
    if any(start <= d <= end for d in dates):
        if any(k in progress for k in done_keywords | {"已提交", "提交", "验证", "完成"}):
            return True
    return False


async def _collect_android17(start: date, end: date, owner: str = "黄超群") -> dict[str, Any]:
    """抓取腾讯文档 Android17 移植计划，返回指定负责人的上周已完成任务。"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(ANDROID17_SHEET_URL, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
    except Exception as exc:
        return {"available": False, "error": f"无法访问腾讯文档: {exc}"}

    rows = _parse_tencent_docs_ssr(html)
    if not rows:
        return {"available": False, "error": "未能从腾讯文档解析到表格数据（可能需要登录或首屏未渲染）"}

    matched: list[dict[str, Any]] = []
    for row in rows:
        responsible = str(row.get("负责人") or "")
        if not _owner_matches(responsible, owner):
            continue
        if _task_completed_last_week(row, start, end):
            matched.append({
                "category": row.get("分类", ""),
                "task": row.get("任务", ""),
                "description": row.get("说明", ""),
                "priority": row.get("优先级", ""),
                "deadline": row.get("需求完成时间", ""),
                "progress": row.get("进展【提醒：请按规范提交代码】", ""),
                "status": row.get("状态", ""),
            })

    return {
        "available": True,
        "owner": owner,
        "title": "Android17_SDK移植适配工作",
        "count": len(matched),
        "tasks": matched,
    }


# ---------------------------------------------------------------------------
# GMS_Test 进展聚合（复用 tradefed list results，不再扫描本地 results 目录）
# ---------------------------------------------------------------------------


def _parse_result_timestamp(basename: str) -> datetime | None:
    """解析 Tradefed 结果目录时间戳，支持 2026.07.02_21.27.07.425_5532 等。"""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})", basename)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
        )
    except ValueError:
        return None


# 模块（测试套件）识别：把 suite_plan / suite_name 归一成周报展示用的模块名。
# GMS 完整模块清单（用于 AI 周报提示识别「未测试模块」）。
# 各模块 plan 形态：
#   CTS / CTS-ON-GSI(烧Google签名system.img) / CTS-Verifier(手动测试,plan=cts-verifier)
#   GTS / GTS-ROOT(烧vendor_boot-debug.img,plan=gts-root) / GTS-Interactive(plan=gts-interactive,Android13+新增)
#   STS(plan=sts-dynamic-full,userdebug固件) / VTS(烧签名system.img+vendor_boot-debug.img)
# 注：GTS-Verifier 仅 Android 13 需要，Android 17 不再纳入。GTS-ROOT/CTS-ON-GSI 是
# GTS/CTS 的子套件，单独成列。未命中映射时回退为 suite_name 大写。
_ALL_GMS_MODULES = ["CTS", "CTS-ON-GSI", "GTS", "GTS-ROOT", "GTS-Interactive", "STS", "VTS"]

_MODULE_MAP = {
    "cts": "CTS",
    "cts-verifier": "CTS-Verifier",
    "cts-on-gsi": "CTS-ON-GSI",
    "cts_on_gsi": "CTS-ON-GSI",
    "gts": "GTS",
    "gts-root": "GTS-ROOT",
    "gts-interactive": "GTS-Interactive",
    "vts": "VTS",
    "sts-dynamic-full": "STS",
    "sts": "STS",
}


def _gms_module(suite_plan: str | None, suite_name: str | None) -> str:
    """从 suite_plan（优先）/ suite_name 推断展示用模块名。"""
    key = (suite_plan or "").strip().lower()
    if key in _MODULE_MAP:
        return _MODULE_MAP[key]
    if "cts-on-gsi" in key or "cts_on_gsi" in key:
        return "CTS-ON-GSI"
    if key.endswith("-root"):
        return (suite_name or "GTS").upper() + "-ROOT"
    return (suite_name or "未知").upper()


# 芯片平台识别：从 device_serial / build_fingerprint 提取 RKxxxx 主型号。
# tradefed list results 的 device_serial 形如 "RK3576GMS2, RK357603"，同一正则即可命中。
# 只取前 4 位数字（RK3572/RK3576/RK3562/RK3326 等主型号），忽略 GMS1-5、03、05 等板级后缀。
_PLATFORM_RE = re.compile(r"(RK\d{4})", re.IGNORECASE)


def _gms_platform_from_device(device_serial: str) -> str:
    """从 tradefed list results 的 device_serial 推断芯片平台（如 RK3576）。"""
    m = _PLATFORM_RE.search(device_serial or "")
    return m.group(1).upper() if m else "未知"


def _run_tradefed_list_results(suite: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """对单个套件跑 `tradefed list results`，返回解析后的 session 列表（失败返回 []）。

    同步函数，供 asyncio.to_thread 调度。每个套件独占一条 SSH 连接（invoke_shell
    不可复用），try/finally 保证归还连接池，单套件失败不影响整体。
    """
    ssh = te_runtime.ssh_manager.get_connection(config)
    if not ssh:
        return []
    try:
        # tradefed_bin 必传 full_path，否则 execute_tradefed_command 会回退到
        # find_tradefed_binary 做 SSH 全盘搜索，单次可挂 ~42s。
        output, _error, code = execute_tradefed_command(
            ssh,
            suite_path=suite.get("tools_path") or "",
            tradefed_bin=suite.get("full_path") or "",
        )
        logger.warning("[Weekly][GMS] %s list results code=%s output_len=%s output_head=%s",
            suite.get('version') or suite.get('test_type'), code, len(output),
            output[:500].replace('\n', ' | '))
        if code != 0:
            return []
        parsed = parse_tradefed_list_results(output)
        logger.warning("[Weekly][GMS] %s parsed results=%s", suite.get('version') or suite.get('test_type'), len(parsed.get('results') or []))
        return parsed.get("results") or []
    except Exception:
        logger.debug("[Weekly] tradefed list results 失败: %s", suite.get("version"), exc_info=True)
        return []
    finally:
        te_runtime.ssh_manager.return_connection(ssh)


async def _collect_gms_test(start: date, end: date) -> dict[str, Any]:
    """按「芯片平台 × 测试模块」聚合区间内最新 GMS 认证测试进展。

    复用「📋 测试结果列表」的数据源（tradefed list results 文本表格），不再
    etree.parse 每个 CTS XML（曾因 1.5GB XML 拖慢周报 ~24s）。各套件并发执行，
    每个 (platform, module) 仅保留区间内时间戳最新的一次结果，给出"该平台该
    模块当前还剩多少 fail"的当前态视图。
    """
    config = te_runtime.config_manager.load_config()
    base_path = config.get("suites_path") or get_default_suites_path(config)
    suites = _get_available_test_suites(config, base_path)
    logger.warning("[Weekly][GMS] suites found: %s", [s.get('version') or s.get('test_type') for s in suites])
    if not suites:
        return {"available": False, "error": f"GMS 套件目录为空或不存在: {base_path}"}

    # 主机可达性早退：借一条连接探测。get_connection 底层 connect/banner/auth 各 10s
    # 超时，死机 ≤~10s 返回 None（不会出现 42s —— 那是 find_tradefed_binary 的全盘
    # find 搜索，本路径直接传 full_path 跳过）。
    probe = te_runtime.ssh_manager.get_connection(config)
    if not probe:
        return {"available": False, "error": "测试主机不可达（无法建立 SSH 连接）"}
    te_runtime.ssh_manager.return_connection(probe)

    # 并发 fan-out：Semaphore 限到 SSH 连接池大小（默认 5），防超额借连接。
    sem = asyncio.Semaphore(min(5, len(suites)))

    async def run_one(suite: dict[str, Any]) -> list[dict[str, Any]]:
        async with sem:
            sessions = await asyncio.to_thread(_run_tradefed_list_results, suite, config)
            logger.warning("[Weekly][GMS] %s sessions=%s", suite.get('version') or suite.get('test_type'), len(sessions or []))
            return sessions

    sessions_per_suite = await asyncio.gather(*(run_one(s) for s in suites))

    # (platform, module) -> 最新结果 dict
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for suite, sessions in zip(suites, sessions_per_suite):
        for s in sessions or []:
            dt = _parse_result_timestamp(s.get("result_directory") or "")
            logger.warning("[Weekly][GMS] raw session: plan=%s suite_type=%s platform=%s ts=%s dt=%s in_range=%s",
                s.get('test_plan'), suite.get('test_type'), _gms_platform_from_device(s.get('device_serial') or ''),
                s.get('result_directory'), dt, start <= dt.date() <= end if dt else None)
            if not dt or not (start <= dt.date() <= end):
                continue
            try:
                p, f = int(s.get("pass") or 0), int(s.get("fail") or 0)
            except (TypeError, ValueError):
                continue
            total = p + f
            if total == 0:
                continue
            platform = _gms_platform_from_device(s.get("device_serial") or "")
            module = _gms_module(s.get("test_plan"), suite.get("test_type"))
            logger.warning("[Weekly][GMS] accepted: platform=%s module=%s pass=%s fail=%s", platform, module, p, f)
            key = (platform, module)
            prev = latest.get(key)
            if prev is None or dt > prev["_dt"]:
                latest[key] = {
                    "_dt": dt,
                    "platform": platform,
                    "module": module,
                    "total": total,
                    "pass": p,
                    "fail": f,
                    "pass_rate": f"{p / total * 100:.2f}%" if total else "-",
                    "remaining": f,
                    "latest_ts": s.get("result_directory") or "",
                    "device": s.get("device_serial") or "",
                    "suite_version": suite.get("version") or "",
                    # tradefed list results 不输出 Android release 版本；UI/AI 均不依赖此字段。
                    "android_version": "",
                }

    if not latest:
        return {
            "available": True,
            "count": 0,
            "platforms": [],
            "message": f"{start} ~ {end} 期间未在 {base_path} 找到 tradefed list results 会话",
        }

    # 按 platform 分组，每组内 module 按失败数降序（剩余 fail 多的排前，最需关注）。
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for item in latest.values():
        item.pop("_dt", None)
        by_platform.setdefault(item["platform"], []).append(item)
    # 过滤掉「未知」平台：device_serial 无法解析出 RK 型号时的噪音聚合，
    # 会让 AI 总结出现「未知平台 GMS 测试」这种无意义行。
    by_platform.pop("未知", None)
    platforms: list[dict[str, Any]] = []
    for platform, modules in by_platform.items():
        modules.sort(key=lambda m: (-(m.get("fail") or 0), m.get("module") or ""))
        total_cases = sum(m.get("total", 0) for m in modules)
        total_fail = sum(m.get("fail", 0) for m in modules)
        tested_names = {m.get("module") for m in modules if m.get("module")}
        untested = [m for m in _ALL_GMS_MODULES if m not in tested_names]
        platforms.append({
            "platform": platform,
            "modules": modules,
            "module_count": len(modules),
            "total_cases": total_cases,
            "total_fail": total_fail,
            "pass_rate": f"{(total_cases - total_fail) / total_cases * 100:.2f}%" if total_cases else "-",
            "untested_modules": untested,
        })
    platforms.sort(key=lambda p: p["platform"])

    return {
        "available": True,
        "count": sum(p["module_count"] for p in platforms),
        "platform_count": len(platforms),
        "total_cases": sum(p["total_cases"] for p in platforms),
        "total_fail": sum(p["total_fail"] for p in platforms),
        "platforms": platforms,
    }


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
    owner_names = (data.get("meta") or {}).get("owner_names") or ([name] if name else [])
    # 本周已解决工单明细：用于周报「已解决/待解决」分类展示。
    # 从本地 DB 按 owner + closed_on 区间直接查（与 _collect_representative_issues 同逻辑，
    # 但只取摘要字段，避免把整行喂给前端）。
    resolved_list = _resolved_issues_summary(request, owner_names, start, end, limit=30)

    return {
        "available": True,
        "resolved_this_period": resolved_this_period,
        "total_owned": data.get("total_owned", 0),
        "open_count": data.get("open_count", 0),
        "waiting_my_reply": data.get("waiting_my_reply", 0),
        "no_reply_3_days": data.get("no_reply_3_days", 0),
        "owner_names": owner_names,
        "lists": {
            "open_issues": lists.get("open_issues") or [],
            "waiting_my_reply": lists.get("waiting_my_reply") or [],
            "no_reply_3_days": lists.get("no_reply_3_days") or [],
            "resolved_this_period": resolved_list,
        },
    }


def _resolved_issues_summary(
    request: Request, owner_names: list[str], start: date, end: date, limit: int = 30
) -> list[dict[str, Any]]:
    """返回本周已关闭工单的摘要列表（issue_id / subject / closed_on）。"""
    try:
        from features.redmine.api import get_redmine_service_for_request
        from features.redmine.users import _name_keys

        owner_keys: set = set()
        for n in owner_names or []:
            owner_keys.update(_name_keys(n))
        end_next = (end + timedelta(days=1)).isoformat()
        repo = get_redmine_service_for_request(request).repository
        with repo.connect() as conn:
            if owner_keys:
                like_clauses = " OR ".join(["assigned_to_name LIKE ?"] * len(owner_keys))
                params = [f"%{k}%" for k in owner_keys]
                rows = conn.execute(
                    f"SELECT issue_id, subject, closed_on FROM redmine_agent_issues "
                    f"WHERE is_resolved = 1 AND closed_on IS NOT NULL AND closed_on != '' "
                    f"AND closed_on >= ? AND closed_on < ? "
                    f"AND ({like_clauses}) "
                    f"ORDER BY closed_on DESC LIMIT ?",
                    [start.isoformat(), end_next, *params, limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT issue_id, subject, closed_on FROM redmine_agent_issues "
                    "WHERE is_resolved = 1 AND closed_on IS NOT NULL AND closed_on != '' "
                    "AND closed_on >= ? AND closed_on < ? "
                    "ORDER BY closed_on DESC LIMIT ?",
                    [start.isoformat(), end_next, limit],
                ).fetchall()
        return [
            {"issue_id": r["issue_id"], "subject": r["subject"] or "", "closed_on": r["closed_on"] or ""}
            for r in rows
        ]
    except Exception:
        return []


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
    include_redmine: str = Query("1"),
    include_gerrit: str = Query("1"),
    include_android17: str = Query("0"),
    include_gms_test: str = Query("0"),
):
    resolved = _resolve_range(start, end)
    if not isinstance(resolved, tuple):
        return resolved  # error_response
    start_date, end_date, is_default = resolved

    # 四路采集互不依赖，asyncio.gather 并发（耗时 ≈ 最慢一路，而非四者之和）。
    # gms_test 现在走 tradefed list results（paramiko socket IO，每次阻塞系统调用
    # 释放 GIL），不再是旧的 etree.parse 1.5GB CPU 密集任务，故可与 redmine/gerrit
    # 安全同池并发，不会像旧版那样把 redmine 从 0.02s 拖到 96s。
    want_rm = _bool_param(include_redmine)
    want_gr = _bool_param(include_gerrit)
    want_a17 = _bool_param(include_android17)
    want_gms = _bool_param(include_gms_test)
    coros = []
    names = []
    import time as _t
    async def _timed(nm, c):
        _s = _t.time()
        try:
            r = await c
        except Exception:
            logger.warning("TIMING %s ERR %.2fs", nm, _t.time()-_s); raise
        logger.warning("TIMING %s %.2fs", nm, _t.time()-_s)
        return r
    if want_rm:  coros.append(_timed("redmine",  _collect_redmine(request, start_date, end_date))); names.append("redmine")
    if want_gr:  coros.append(_timed("gerrit",   _collect_gerrit(request, start_date, end_date))); names.append("gerrit")
    if want_a17: coros.append(_timed("android17", _collect_android17(start_date, end_date))); names.append("android17")
    if want_gms: coros.append(_timed("gms_test", _collect_gms_test(start_date, end_date))); names.append("gms_test")
    _gs = _t.time()
    results = await asyncio.gather(*coros) if coros else []
    logger.warning("TIMING gather_total %.2fs", _t.time()-_gs)
    ri = 0
    redmine   = results[ri] if want_rm  else {"available": None}; ri += int(want_rm)
    gerrit    = results[ri] if want_gr  else {"available": None}; ri += int(want_gr)
    android17 = results[ri] if want_a17 else {"available": None}; ri += int(want_a17)
    gms_test  = results[ri] if want_gms else {"available": None}

    member = {
        "owner": "",
        "name": "我自己",
        "redmine": redmine,
        "gerrit": gerrit,
        "android17": android17,
        "gms_test": gms_test,
    }
    _member_themes(member)

    data = {
        "range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "label": "上周" if is_default else "自定义",
        },
        "redmine": redmine,
        "gerrit": gerrit,
        "android17": android17,
        "gms_test": gms_test,
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

# 机械性提交（版本号/拣选/回退），周报正文与 AI 总结统一过滤，避免逐条罗列噪音。
_NOISE_CHANGE_RE = re.compile(
    r"^(bump|version bump|bump version|cherry pick|revert)", re.IGNORECASE
)


def _drop_noise_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剔除机械性代码提交（Bump version / Cherry pick / Revert）。"""
    return [c for c in (changes or []) if not _NOISE_CHANGE_RE.match(str(c.get("subject") or ""))]

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
    include_redmine: str = Query("1"),
    include_gerrit: str = Query("1"),
    include_android17: str = Query("0"),
    include_gms_test: str = Query("0"),
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

    # 四路并发采集（见 get_weekly_report 同款注释）。
    want_rm = _bool_param(include_redmine)
    want_gr = _bool_param(include_gerrit)
    want_a17 = _bool_param(include_android17)
    want_gms = _bool_param(include_gms_test)
    coros = []
    if want_rm:  coros.append(_collect_redmine(request, start_date, end_date, name=member_name))
    if want_gr:  coros.append(_collect_gerrit(request, start_date, end_date, owner=owner))
    if want_a17: coros.append(_collect_android17(start_date, end_date, owner=member_name or "黄超群"))
    if want_gms: coros.append(_collect_gms_test(start_date, end_date))
    results = await asyncio.gather(*coros) if coros else []
    ri = 0
    redmine   = results[ri] if want_rm  else {"available": None}; ri += int(want_rm)
    gerrit    = results[ri] if want_gr  else {"available": None}; ri += int(want_gr)
    android17 = results[ri] if want_a17 else {"available": None}; ri += int(want_a17)
    gms_test  = results[ri] if want_gms else {"available": None}

    member = {
        "owner": owner,
        "name": member_name or owner,
        "redmine": redmine,
        "gerrit": gerrit,
        "android17": android17,
        "gms_test": gms_test,
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

    Body: {"start","end","owner","name","redmine","gerrit","android17","gms_test"}
    前端把已生成的周报数据回传；缺失项由后端现取兜底。
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
    android17 = body.get("android17") or {}
    gms_test = body.get("gms_test") or {}

    resolved = _resolve_range(start, end)
    if not isinstance(resolved, tuple):
        return resolved
    start_date, end_date, is_default = resolved

    member_name = str(body.get("name") or "")

    # 各数据源是否对 AI 可见：前端勾选才会回传真实数据（available 为 True/False），
    # 未勾选时是 {"available": null} 占位，AI 不应纳入。redmine/gerrit 为周报核心，
    # 兜底现取；android17/gms_test 仅当前端回传了真实数据时才喂给 AI。
    def _has_real_data(payload: dict) -> bool:
        return bool(payload) and payload.get("available") in (True, False)

    if not _has_real_data(redmine):
        redmine = await _collect_redmine(request, start_date, end_date, name=member_name)
    if not _has_real_data(gerrit):
        gerrit = await _collect_gerrit(request, start_date, end_date, owner=owner)
    a17_available = _has_real_data(android17)
    # GMS 认证进展是周报第四层核心内容：即使前端未勾选 GMS_Test 也兜底现取，
    # 否则 AI 拿不到数据会漏写第四层（依赖用户记得勾选易出错）。
    if not _has_real_data(gms_test):
        try:
            gms_test = await _collect_gms_test(start_date, end_date)
        except Exception:
            gms_test = {"available": False, "error": "GMS 测试数据采集失败"}
    gms_available = _has_real_data(gms_test)

    # 取代表性工单完整内容(含本周关闭 + 跟进中)
    owner_names = (redmine.get("owner_names") or []) if redmine.get("available") is not False else []
    if body.get("name"):
        owner_names = [str(body.get("name"))] + [n for n in owner_names if n != body.get("name")]
    rep_issues = _collect_representative_issues(
        request, redmine, start_date, end_date, owner_names=owner_names, limit=10
    ) if redmine.get("available") is not False else []

    # 组装 Gerrit 合并提交标题(主要)
    gr_merged = _drop_noise_changes((gerrit.get("lists") or {}).get("merged") or [])
    gr_new = _drop_noise_changes((gerrit.get("lists") or {}).get("new") or [])

    issue_blocks = "\n\n".join(
        f"【{'本周已关闭' if it.get('_category') == 'closed_this_period' else '跟进中'}】\n{_issue_body_for_ai(it)}"
        for it in rep_issues
    )
    merged_blocks = "\n".join(f"- {c.get('number','?')} {c.get('subject','')}".strip() for c in gr_merged[:10])
    new_blocks = "\n".join(f"- {c.get('number','?')} {c.get('subject','')}".strip() for c in gr_new[:6])

    # Android17 移植任务（本周完成的）
    a17_tasks = (android17.get("tasks") or []) if (a17_available and android17.get("available")) else []
    a17_blocks = "\n".join(
        f"- [{t.get('category','')}] {t.get('task','')}".strip()
        + (f"（{t.get('progress','')[:80]}）" if t.get("progress") else "")
        for t in a17_tasks[:15]
    )

    # GMS 认证测试：平台 × 模块 当前进展（每组合取区间内最新一次），
    # 同时补全未测试模块清单供 AI 在总结中引用。
    gms_lines: list[str] = []
    for p in (gms_test.get("platforms") or []) if (gms_available and gms_test.get("available")) else []:
        gms_lines.append(f"- {p.get('platform')}：{p.get('module_count')} 个模块，总用例 {p.get('total_cases')}，剩余失败 {p.get('total_fail')}，通过率 {p.get('pass_rate')}")
        for m in (p.get("modules") or []):
            gms_lines.append(f"    · {m.get('module')}：总 {m.get('total')}，失败 {m.get('fail')}，通过率 {m.get('pass_rate')}（{m.get('latest_ts','')}）")
        untested = p.get("untested_modules") or []
        if untested:
            gms_lines.append(f"    · 未测试模块：{', '.join(untested)}")
    gms_blocks = "\n".join(gms_lines)

    user_prompt = (
        f"你是团队成员，请基于以下本周({start_date} ~ {end_date})的工作数据，"
        f"用中文写一份周报的「本周工作总结」。\n\n"
        f"【核心要求——必须严格按下面四个一级标题的顺序分层输出，顺序不可调换，禁止流水账式逐条罗列】\n"
        f"每层内部的同类项必须归纳合并，每条要带「做了什么 + 结论/结果」，"
        f"不要把多个工单/提交拆成独立一条平铺。\n\n"
        f"## 一、Gerrit提交\n"
        f"把本周合并的代码提交按主题归纳为 1-3 条（如加密级别适配、SELinux 权限、"
        f"预装应用 targetSdk 升级等），每条一句话说清改了什么、达成什么；"
        f"纯版本号/拣选/回退类机械提交一律忽略。\n\n"
        f"## 二、Android17 SDK适配\n"
        f"本周完成的移植/验证任务（NTFS、DEQP、FRP、性能模式等），每项说明做了哪类移植及验证结果。\n"
        f"重要：若下方「Android17 移植任务」清单为空，但「合并/新增提交」中含明显属于 Android17 "
        f"移植的条目（如 vold NTFS 支持、ntfsfix 权限、FIXED_PERFORMANCE、Android17 checkout 等），"
        f"必须依据这些提交推断本周移植进展并写入本节，不得写「本周暂无闭环任务」这类与提交证据矛盾的结论。\n"
        f"只有当既无移植任务清单、又无相关提交时，才写「本周无 Android17 移植任务」。\n\n"
        f"## 三、Redmine工单\n"
        f"分两个子层：\n"
        f"- 「已解决（按类别归纳）」：把已关闭工单按性质分组（如客户交付指导类、"
        f"系统/网络故障类、测试 Fail 修复类、硬件/底层异常类），每类一句话总结共性问题和结果，"
        f"不要逐个工单罗列。\n"
        f"- 「跟进中（按问题性质分组）」：进行中工单按性质分组（认证测试 Fail、"
        f"系统功能异常、编译/咨询等），每组合并描述；可在每条末尾用括号注明工单号。\n"
        f"若有反复强调的重点难点（如 APEX 签名跨 SDK 误用），单独拎出一段说明技术结论。\n"
        f"注意：不要罗列 Redmine 工单的「最新进展/回复内容」，只按已解决/跟进中分类。\n\n"
        f"## 四、GMS 认证进展\n"
        f"每个芯片平台写成一段独立的话。格式要求：\n"
        f"- 已测试模块按 'XX模块N个fail' 列出，未测试模块按 'XX模块未测试' 列出，"
        f"平台名、模块名、数字之间不加空格；\n"
        f"- 整段话合并为一句，示例：「XX平台GMS测试，VTS模块58个fail，"
        f"STS模块1个fail，CTS、CTS-ON-GSI、GTS、GTS-Root、GTS-Interactive模块未测试。」\n"
        f"- 输出时必须完全照搬示例格式，禁止在平台名、模块名与数字之间插入空格。\n"
        f"- 只描述下方数据中确实出现的平台与模块；未出现的模块不要硬编。\n"
        f"- 严禁使用『扫尾阶段』『攻坚阶段』等定性表述，只陈述客观事实。\n"
        f"- 风险研判单独一句，仅指出当前遗留 fail 最多或覆盖缺失最明显的模块作为后续重点，"
        f"不要出现『推进各平台未测试模块的全面覆盖』这类泛泛而谈的收尾句，"
        f"禁止对项目整体进度做主观推断。\n"
        f"- 多个平台之间必须换行分隔，每平台独立成段。\n"
        f"注意：即使上述 GMS 认证测试进展数据整体为「(无)」，也必须保留「## 四、GMS 认证进展」标题，"
        f"并写明「本周无 GMS 认证测试数据」，不许整节省略。\n\n"
        f"【格式硬约束】\n"
        f"1. 一级标题用 \"## \" 开头；要点用顶格 \"- \" 开头，子项缩进两个空格。\n"
        f"2. 禁止使用 \"•\" 字符，禁止用 \"1. 2. 3.\" 数字编号；同一要点内部需要换行时，"
        f"使用 Markdown 软换行（行尾两个空格），禁止把完整词组拆到下一行。\n"
        f"3. 语气平实，像人写的周报，不要写「根据数据」「如下所示」之类元话语。\n"
        f"4. 每个一级标题之间空一行。\n\n"
        f"=== 本周合并的提交(主要) ===\n{merged_blocks or '(无)'}\n\n"
        f"=== 本周新增/进行中 ===\n{new_blocks or '(无)'}\n\n"
        f"=== 本周跟进/处理的 Redmine 工单(含描述与近期回复) ===\n{issue_blocks or '(无)'}\n\n"
        f"=== 本周完成的 Android17 移植任务 ===\n{a17_blocks or '(无)'}\n\n"
        f"=== GMS 认证测试进展（平台 × 模块当前态）===\n{gms_blocks or '(无)'}\n"
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

    system_prompt = "你是资深 Android 系统工程师，擅长把零散的工单、代码提交、移植任务与 GMS 认证测试结果归纳成清晰的中文周报。"
    # 优先使用本地模型 glm_local；若未启用则回退到通用主 provider
    result = analyzer.generate(
        user_prompt=user_prompt, system_prompt=system_prompt, max_tokens=2500,
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
