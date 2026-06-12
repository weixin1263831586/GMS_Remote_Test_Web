"""RedmineAgent APIs and page."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from core.redmine_agent import RedmineAgent
from core.redmine_agent_db import RedmineAgentDB
from modules.redmine_agent_scheduler import get_scheduler_config


router = APIRouter(prefix="/api/redmine-agent")
page_router = APIRouter()

_db = RedmineAgentDB()
_agent = RedmineAgent(_db)
_run_lock = asyncio.Lock()
_active_task: Optional[asyncio.Task] = None
_active_run_id: Optional[str] = None
_db.mark_stale_running_runs()


async def _start_task(coro_factory, run_id: str, message: str) -> dict:
    """Shared helper to launch a background task with lock protection."""
    global _active_task, _active_run_id
    async with _run_lock:
        if _active_task and not _active_task.done():
            return {"success": False, "error": "RedmineAgent already running", "run_id": _active_run_id}
        _active_task = asyncio.create_task(coro_factory())
        _active_run_id = run_id
        return {"success": True, "message": message, "run_id": run_id}


async def start_redmine_agent_run(hours: int = 24, max_issues: int = 20, mode: str = "manual") -> dict:
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    return await _start_task(
        lambda: _agent.run(hours=hours, max_issues=max_issues, run_id=run_id, mode=mode),
        run_id,
        "RedmineAgent started",
    )


async def start_redmine_agent_sync(max_analyze: int = 20) -> dict:
    run_id = "sync-" + datetime.now().strftime("%Y%m%d%H%M%S")
    return await _start_task(
        lambda: _agent.sync_all_assigned_issues(analyze_new=True, max_analyze=max_analyze),
        run_id,
        "RedmineAgent sync started",
    )


# ------------------------------------------------------------------
# Existing endpoints (enhanced)
# ------------------------------------------------------------------

@router.post("/runs")
async def create_run(
    hours: int = Query(48, ge=1, le=168),
    max_issues: int = Query(20, ge=1, le=100),
):
    return await start_redmine_agent_run(hours=hours, max_issues=max_issues, mode="manual")


@router.get("/status")
async def get_status():
    running = bool(_active_task and not _active_task.done())
    result = None
    if _active_task and _active_task.done():
        try:
            result = _active_task.result()
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
    return {"success": True, "data": {"running": running, "active_run_id": _active_run_id, "last_result": result}}


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100)):
    return {"success": True, "data": {"items": _db.list_runs(limit)}}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = _db.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "run not found"})
    return {"success": True, "data": {"run": run, "issues": _db.list_run_issues(run_id)}}


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: int):
    issue = _db.get_issue(issue_id)
    if not issue:
        return JSONResponse(status_code=404, content={"success": False, "error": "issue not found"})
    # Enrich with structured fields
    ai = issue.get("ai_json") or {}
    enriched = {
        **issue,
        "title": issue.get("subject") or ai.get("title", ""),
        "problem_description": issue.get("problem_description") or RedmineAgent.extract_description(issue),
        "error_info": issue.get("error_info") or RedmineAgent.extract_error_from_failures(issue.get("failures_json", [])),
        "error_analysis": issue.get("error_analysis") or ai.get("root_cause_guess", ""),
        "solution": issue.get("solution") or ai.get("solution", ""),
        "patch_direction": issue.get("patch_direction") or ai.get("patch_direction", ""),
    }
    return {"success": True, "data": enriched}


@router.get("/issues/{issue_id}/document")
async def get_issue_document(issue_id: int):
    issue = _db.get_issue(issue_id)
    if not issue:
        return PlainTextResponse("issue not found", status_code=404)
    return PlainTextResponse(issue.get("doc_content") or "", media_type="text/markdown")


# ------------------------------------------------------------------
# New endpoints
# ------------------------------------------------------------------

@router.get("/issues")
async def list_issues(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    priority: str = Query(""),
    category: str = Query(""),
    search: str = Query(""),
    sort: str = Query("updated_on"),
    order: str = Query("desc"),
):
    issues = _db.list_all_issues(limit=limit, offset=offset, status=status, priority=priority, category=category, search=search, sort=sort, order=order)
    total = _db.count_issues(status=status, priority=priority, category=category, search=search)
    return {"success": True, "data": {"items": issues, "total": total, "limit": limit, "offset": offset}}


@router.get("/issues/search")
async def search_issues(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    return {"success": True, "data": {"items": _db.search_issues(q, limit)}}


@router.get("/statistics")
async def get_statistics():
    return {"success": True, "data": _db.get_issue_statistics()}


async def _resolve_owner_names() -> List[str]:
    names: List[str] = []
    try:
        client = _agent._make_client()
        user = await client.get_current_user()
        first = str(getattr(user, "firstname", "") or "").strip()
        last = str(getattr(user, "lastname", "") or "").strip()
        login = str(getattr(user, "login", "") or "").strip()
        mail = str(getattr(user, "mail", "") or getattr(user, "email", "") or "").strip()
        display_name = f"{last} {first}".strip() or f"{first} {last}".strip()
        names.extend([
            display_name,
            mail or login,
        ])
    except Exception:
        pass

    return list(dict.fromkeys(name for name in names if name))


@router.get("/statistics/workload")
async def get_workload_statistics(
    stale_days: int = Query(3, ge=1, le=30),
    list_limit: int = Query(30, ge=1, le=100),
):
    owner_names = await _resolve_owner_names()
    # Collect extra names from run history for matching only, not display
    extra_names = []
    try:
        for run in _db.list_runs(10):
            assigned_to = str(run.get("assigned_to") or "").strip()
            if assigned_to:
                extra_names.append(assigned_to)
    except Exception:
        pass
    all_names = owner_names + [n for n in extra_names if n]
    data = _db.get_workload_statistics(owner_names=all_names, stale_days=stale_days, list_limit=list_limit, display_names=owner_names)
    return {"success": True, "data": data}


@router.post("/sync")
async def trigger_sync(max_analyze: int = Query(20, ge=1, le=100)):
    return await start_redmine_agent_sync(max_analyze=max_analyze)


@router.post("/issues/{issue_id}/fetch")
async def fetch_and_analyze_issue(issue_id: int):
    """Fetch a single issue from Redmine and analyze it."""
    existing = _db.get_issue(issue_id)
    if existing and existing.get("analysis_status") == "done":
        return {"success": True, "data": {"action": "exists", "issue": existing}}
    run_id = f"fetch-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return await _start_task(
        lambda: _agent.analyze_issue(_agent._make_client(), issue_id, run_id),
        run_id,
        f"Fetching #{issue_id} from Redmine",
    )


@router.get("/reports/latest")
async def get_latest_report():
    run = _db.get_latest_run()
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "no completed runs"})
    return {"success": True, "data": {"run": run, "issues": _db.list_run_issues(run.get("run_id", ""))}}


@router.get("/config")
async def get_config():
    return {"success": True, "data": get_scheduler_config()}


# ------------------------------------------------------------------
# Web UI
# ------------------------------------------------------------------
# Web UI
# ------------------------------------------------------------------

@page_router.get("/redmine-agent", response_class=HTMLResponse)
async def redmine_agent_page():
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedmineAgent</title>
  <style>
    :root { color-scheme: dark; --bg:#0b0d12; --panel:#131720; --panel2:#191f2b; --border:#2b3342; --text:#e8edf7; --muted:#96a1b5; --primary:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; --high:#ef4444; --medium:#f59e0b; --low:#6b7280; }
    * { box-sizing: border-box; }
    body { margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    header { display:flex; align-items:center; gap:16px; padding:8px 16px; border-bottom:1px solid var(--border); background:var(--panel); }
    .header-title { font-size:18px; font-weight:700; white-space:nowrap; margin:0; }
    .header-right { display:flex; align-items:center; gap:12px; flex-shrink:0; flex-wrap:wrap; }
    .muted { color:var(--muted); font-size:13px; line-height:1.6; }
    button { height:30px; border:0; border-radius:6px; padding:0 10px; color:white; background:var(--primary); font-weight:650; cursor:pointer; font-size:13px; }
    button.secondary { background:#30394a; }
    button.warn { background:#b45309; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .btn-group { display:flex; gap:6px; flex-wrap:wrap; }

    /* Tabs — inline in header, left side */
    .tabs { display:flex; gap:0; }
    .tab { padding:8px 16px; cursor:pointer; font-size:13px; font-weight:600; color:var(--muted); border-bottom:2px solid transparent; transition:all .15s; }
    .tab:hover { color:var(--text); }
    .tab.active { color:var(--primary); border-bottom-color:var(--primary); }

    /* Panels */
    .tab-content { display:none; padding:16px; }
    .tab-content.active { display:block; }

    /* Filter bar */
    .filter-bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .filter-bar input, .filter-bar select { height:28px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:0 8px; font-size:13px; }
    .filter-bar input { width:240px; }
    .filter-bar select { width:100px; }

    /* Issue cards */
    .issue-card { border:1px solid var(--border); border-radius:8px; padding:14px; margin-bottom:14px; background:#101620; }
    .issue-card h3 { margin:0 0 10px; font-size:16px; display:flex; align-items:center; gap:8px; }
    .issue-card h3 a { color:#7bb0ff; text-decoration:none; }
    .issue-card h3 a:hover { text-decoration:underline; }

    .chips { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0 10px; }
    .chip { font-size:11px; color:#dbeafe; background:#1d3558; padding:3px 7px; border-radius:999px; }
    .chip.high { background:#5c1d1d; color:#fca5a5; }
    .chip.medium { background:#5c3a0a; color:#fde68a; }
    .chip.ok { background:#14412a; color:#86efac; }

    .field { margin-bottom:12px; }
    .field-label { font-size:12px; font-weight:700; color:var(--muted); margin-bottom:4px; text-transform:uppercase; letter-spacing:.5px; }
    .field-content { font-size:14px; line-height:1.6; }
    .field-content.error-section { background:#1a0a0a; border:1px solid #3b1111; border-radius:5px; padding:8px 10px; font-family:'Cascadia Code','Fira Code',monospace; font-size:12px; white-space:pre-wrap; word-break:break-word; color:#fca5a5; }
    .field-content.code-section { background:#0a0f1a; border:1px solid #112040; border-radius:5px; padding:8px 10px; font-family:'Cascadia Code','Fira Code',monospace; font-size:12px; white-space:pre-wrap; word-break:break-word; color:#93c5fd; }
    .field-content.solution-section { background:#0a1a0e; border:1px solid #113320; border-radius:5px; padding:8px 10px; font-size:13px; }

    /* Code block containers with syntax highlighting */
    .code-block { position:relative; background:#0a0f1a; border:1px solid #1a2540; border-radius:6px; margin:6px 0; overflow:hidden; }
    .code-block pre { margin:0; padding:10px 12px; border:none; background:transparent; font-family:'Cascadia Code','Fira Code','JetBrains Mono',monospace; font-size:12px; line-height:1.5; white-space:pre-wrap; word-break:break-word; color:#c9d1d9; }
    .code-block-lang { display:inline-block; background:#1a2540; color:#7bb0ff; font-size:10px; font-weight:700; padding:2px 8px; border-radius:0 0 4px 0; text-transform:uppercase; letter-spacing:.5px; }
    .diff-block { border-color:#1a2540; }
    .diff-header { color:#79c0ff; font-weight:700; }
    .diff-hunk { color:#39d353; background:rgba(57,211,83,.08); display:inline-block; width:100%; }
    .diff-add { color:#3fb950; background:rgba(63,185,80,.08); display:inline-block; width:100%; }
    .diff-remove { color:#f85149; background:rgba(248,81,73,.08); display:inline-block; width:100%; }
    .shell-block { border-color:#1a2540; }
    .shell-cmd { color:#f0c674; font-weight:600; }
    .copy-btn { position:absolute; top:4px; right:4px; background:#1a2540; color:#7bb0ff; border:1px solid #2a3550; border-radius:4px; padding:2px 8px; font-size:11px; cursor:pointer; opacity:0; transition:opacity .2s; }
    .code-block:hover .copy-btn { opacity:1; }
    .copy-btn:hover { background:#2a3550; }

    /* Info table for platform & version — single row */
    .info-table { width:100%; border-collapse:collapse; margin:4px 0; font-size:13px; }
    .info-table th { text-align:left; color:var(--muted); font-weight:600; font-size:11px; padding:5px 8px; background:var(--panel2); border:1px solid var(--border); white-space:nowrap; }
    .info-table td { padding:5px 10px; border:1px solid var(--border); white-space:nowrap; }
    .info-table td strong { color:#93c5fd; }

    .test-info { padding:6px 0; font-size:13px; color:#93c5fd; border-bottom:1px solid var(--border); margin-bottom:8px; }
    .ref-item { display:flex; align-items:flex-start; gap:8px; padding:4px 0; font-size:13px; }
    .ref-item a { color:#7bb0ff; text-decoration:none; font-weight:600; white-space:nowrap; flex-shrink:0; }
    .ref-item a:hover { text-decoration:underline; }
    .ref-title { color:var(--text); font-size:13px; line-height:1.4; }
    .ref-badge { font-size:11px; padding:2px 6px; border-radius:999px; font-weight:600; flex-shrink:0; }
    .ref-badge.high { background:#5c1d1d; color:#fca5a5; }
    .ref-badge.medium { background:#5c3a0a; color:#fde68a; }
    .ref-badge.low { background:#2a2a2a; color:#9ca3af; }

    /* Run list */
    .run-item { padding:10px 12px; border-bottom:1px solid var(--border); cursor:pointer; }
    .run-item:hover { background:#1b2230; }
    .run-item.active { background:#1f2d45; }
    .run-item-title { font-size:13px; font-weight:700; margin-bottom:4px; }
    .run-item-meta { font-size:12px; color:var(--muted); }

    /* Pagination */
    .pagination { display:flex; justify-content:center; gap:8px; margin:16px 0; }
    .pagination button { min-width:32px; }

    /* Stats */
    .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:20px; }
    .stat-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px; text-align:center; }
    .stat-card .value { font-size:28px; font-weight:700; color:var(--primary); }
    .stat-card .label { font-size:12px; color:var(--muted); margin-top:4px; }
    .stat-card.warn .value { color:var(--warn); }
    .stat-card.bad .value { color:var(--bad); }
    .stat-card.ok .value { color:var(--ok); }
    .clickable-stat { cursor:pointer; transition:transform .15s,box-shadow .15s; }
    .clickable-stat:hover { transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,.3); }
    .stats-section { margin-bottom:18px; }
    .stats-section h2 { font-size:15px; margin:0 0 10px; }
    .trend-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(380px, 1fr)); gap:12px; margin-bottom:18px; }
    .trend-panel { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px; min-height:180px; display:flex; flex-direction:column; }
    .trend-panel h3 { margin:0 0 10px; font-size:14px; flex-shrink:0; }
    .trend-body { overflow-y:auto; max-height:400px; flex:1; }
    .trend-body::-webkit-scrollbar { width:6px; }
    .trend-body::-webkit-scrollbar-track { background:var(--panel2); border-radius:3px; }
    .trend-body::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
    .trend-body::-webkit-scrollbar-thumb:hover { background:var(--muted); }
    .bar-row { display:flex; align-items:center; gap:6px; margin:4px 0; font-size:12px; padding-right:8px; }
    .bar-label { flex:0 0 auto; min-width:0; max-width:86px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding-right:8px; }
    .bar-track { flex:1; height:8px; background:#202838; border-radius:999px; overflow:hidden; min-width:40px; }
    .bar-fill { height:100%; min-width:3px; background:var(--primary); border-radius:999px; }
    .bar-count { flex-shrink:0; width:28px; text-align:right; color:var(--muted); }
    .issue-mini-list { display:grid; gap:8px; }
    .issue-mini { display:grid; grid-template-columns:92px minmax(0, 1fr) 128px; gap:10px; align-items:start; padding:10px 12px; background:#101620; border:1px solid var(--border); border-radius:8px; font-size:13px; }
    .issue-mini a { color:#7bb0ff; text-decoration:none; font-weight:700; }
    .issue-mini a:hover { text-decoration:underline; }
    .issue-mini-title { min-width:0; }
    .issue-mini-title strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text); font-weight:600; }
    .issue-mini-title span { display:block; color:var(--muted); font-size:12px; line-height:1.5; margin-top:2px; }
    .issue-mini-meta { color:var(--muted); font-size:12px; text-align:right; line-height:1.5; }

    /* Layout */
    .two-col { display:grid; grid-template-columns:340px minmax(0,1fr); gap:12px; min-height:calc(100vh - 160px); }
    .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
    .panel h2 { margin:0; padding:10px 12px; font-size:14px; background:var(--panel2); border-bottom:1px solid var(--border); }
    .panel .scroll { overflow:auto; max-height:calc(100vh - 220px); }

    details { margin-top:6px; }
    details summary { cursor:pointer; font-size:13px; color:var(--muted); font-weight:600; }
    pre { white-space:pre-wrap; word-break:break-word; background:#090c11; border:1px solid var(--border); border-radius:6px; padding:10px; color:#d7e1f5; font-size:12px; }

    @media (max-width:900px) { header { flex-wrap:wrap; } .tabs { margin-left:0; width:100%; } .filter-bar input { width:140px; } .two-col { grid-template-columns:1fr; } .issue-mini { grid-template-columns:1fr; } .issue-mini-meta { text-align:left; } }
  </style>
</head>
<body>
<header>
  <div class="tabs">
    <div class="tab active" data-tab="stats" onclick="switchTab('stats')">📈 统计</div>
    <div class="tab" data-tab="issues" onclick="switchTab('issues')">📋 工单列表</div>
    <div class="tab" data-tab="runs" onclick="switchTab('runs')">📊 扫描记录</div>
  </div>
  <div style="flex:1"></div>
  <div class="filter-bar" style="margin:0">
    <input type="text" id="searchInput" placeholder="搜索工单 / 输入 #工单号查询Redmine..." onkeydown="if(event.key==='Enter')smartSearch()" style="width:260px">
    <select id="statusFilter" onchange="loadIssues()">
      <option value="">全部状态</option>
      <option value="新建">新建</option>
      <option value="进行中">进行中</option>
      <option value="已解决">已解决</option>
      <option value="已关闭">已关闭</option>
      <option value="反馈">反馈</option>
    </select>
    <select id="priorityFilter" onchange="loadIssues()">
      <option value="">全部优先级</option>
      <option value="紧急">紧急</option>
      <option value="高">高</option>
      <option value="正常">正常</option>
      <option value="低">低</option>
    </select>
  </div>
  <div class="btn-group">
    <button id="scanBtn" onclick="startScan()">🔍 扫描</button>
    <button class="secondary" onclick="triggerSync()">🔄 同步</button>
    <button class="secondary" onclick="refreshCurrentTab()">刷新</button>
  </div>
</header>

<div id="tab-issues" class="tab-content">
  <div id="issuesList"></div>
  <div id="issuesPagination" class="pagination"></div>
</div>

<div id="tab-runs" class="tab-content">
  <div class="two-col">
    <section class="panel">
      <h2>扫描记录</h2>
      <div id="runsList" class="scroll"></div>
    </section>
    <section class="panel">
      <h2 id="runDetailTitle">日报详情</h2>
      <div id="runDetail" class="scroll" style="padding:14px;"><div class="muted">选择一次扫描记录查看结果。</div></div>
    </section>
  </div>
</div>

<div id="tab-stats" class="tab-content active">
  <div id="statsContent"><div class="muted">加载中...</div></div>
</div>

<script>
function scrollToSection(id) {
  var el = document.getElementById(id);
  if (el) el.scrollIntoView({behavior:'smooth', block:'start'});
}
let currentTab = 'stats';
let currentPage = 1;
const pageSize = 15;
let currentRunId = '';

// ---- API helper ----
async function api(url, options) {
  const r = await fetch(url, options || {});
  const data = await r.json();
  if (!data.success) throw new Error(data.error || '请求失败');
  return data.data || data;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function trunc(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n) + '...' : s;
}
function truncSafe(s, n) {
  s = trunc(s, n);
  var opens = (s.match(new RegExp(_F3, 'g')) || []).length;
  if (opens % 2 !== 0) s += _NL + _F3;
  return s;
}

// ---- Formatted content rendering ----
var _BT = String.fromCharCode(96);
var _F3 = _BT+_BT+_BT;
var _NL = String.fromCharCode(10);
var _MD_RE = new RegExp(_F3 + '(\\\\w*)' + _NL + '([\\\\s\\\\S]*?)' + _F3, 'g');
var _HTML_RE = /<pre><code(?:\s+class="(\w*)")?\s*>([\s\S]*?)<\/code><\/pre>/g;

function _nl2br(s) { return s.replace(new RegExp(_NL, 'g'), '<br>'); }

function renderFormattedContent(text, defaultClass) {
  if (!text) return '';
  var cls = defaultClass || 'field-content';
  var result = '';
  var parts = []; // {type:'text'|'code', content, lang}
  var lastIdx = 0;

  // 1. Extract HTML <pre><code class="lang">...</code></pre> blocks
  _HTML_RE.lastIndex = 0;
  var m;
  while ((m = _HTML_RE.exec(text)) !== null) {
    if (m.index > lastIdx) parts.push({type:'text', content:text.slice(lastIdx, m.index), lang:''});
    parts.push({type:'code', content:m[2]||'', lang:(m[1]||'').toLowerCase()});
    lastIdx = _HTML_RE.lastIndex;
  }
  if (lastIdx < text.length) parts.push({type:'text', content:text.slice(lastIdx), lang:''});

  // If no HTML blocks found, try markdown ```lang``` blocks
  if (parts.length <= 1 && parts[0] && parts[0].type === 'text') {
    parts = [];
    lastIdx = 0;
    var mdRe = new RegExp(_F3 + '(\\\\w*)' + _NL + '([\\\\s\\\\S]*?)' + _F3, 'g');
    var mm;
    while ((mm = mdRe.exec(text)) !== null) {
      if (mm.index > lastIdx) parts.push({type:'text', content:text.slice(lastIdx, mm.index), lang:''});
      parts.push({type:'code', content:mm[2]||'', lang:(mm[1]||'').toLowerCase()});
      lastIdx = mdRe.lastIndex;
    }
    if (lastIdx < text.length) parts.push({type:'text', content:text.slice(lastIdx), lang:''});
  }

  // If still no blocks, return escaped text
  if (!parts.length) return _nl2br(esc(text));

  // 2. Render each part
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p.type === 'text') {
      if (p.content.trim()) result += '<div class="' + cls + '">' + _nl2br(esc(p.content)) + '</div>';
    } else {
      if (p.lang === 'diff') result += renderDiffBlock(p.content);
      else if (p.lang === 'shell' || p.lang === 'bash' || p.lang === 'sh') result += renderShellBlock(p.content);
      else result += renderGenericCodeBlock(p.content, p.lang);
    }
  }
  return result || _nl2br(esc(text));
}
function renderDiffBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (line.startsWith('---') || line.startsWith('+++')) return '<span class="diff-header">' + e + '</span>';
    if (line.startsWith('@@')) return '<span class="diff-hunk">' + e + '</span>';
    if (line.startsWith('+')) return '<span class="diff-add">' + e + '</span>';
    if (line.startsWith('-')) return '<span class="diff-remove">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block diff-block"><div class="code-block-lang">diff</div><pre><code>' + lines + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function renderShellBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (/^\\$\\s/.test(line)) return '<span class="shell-cmd">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block shell-block"><div class="code-block-lang">shell</div><pre><code>' + lines + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function renderGenericCodeBlock(code, lang) {
  var langLabel = lang || 'code';
  return '<div class="code-block"><div class="code-block-lang">' + esc(langLabel) + '</div><pre><code>' + esc(code) + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function copyCode(btn) {
  var code = btn.previousElementSibling.querySelector('code');
  navigator.clipboard.writeText(code.textContent).then(function() {
    btn.textContent = '已复制';
    setTimeout(function() { btn.textContent = '复制'; }, 1500);
  });
}

// ---- Tab switching ----
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + tab));
  if (tab === 'issues') loadIssues();
  else if (tab === 'runs') loadRuns();
  else if (tab === 'stats') loadStatistics();
}

function refreshCurrentTab() { switchTab(currentTab); }

// ---- Smart search: detect issue ID and fetch from Redmine ----
async function smartSearch() {
  var q = document.getElementById('searchInput').value.trim();
  if (!q) { loadIssues(); return; }
  // Detect issue ID pattern: #634227, 634227, or pure number
  var idMatch = q.match(/^#?(\d{4,})$/);
  if (idMatch) {
    var issueId = parseInt(idMatch[1]);
    // Check local DB first
    try {
      var local = await api('/api/redmine-agent/issues/' + issueId);
      if (local && local.issue_id) {
        loadIssues();
        return;
      }
    } catch (_) {
      // Not found locally — fetch from Redmine
    }
    // Fetch from Redmine
    await fetchIssueFromRedmine(issueId);
  } else {
    loadIssues();
  }
}

async function fetchIssueFromRedmine(issueId) {
  var btn = document.getElementById('scanBtn');
  var origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ 拉取 #' + issueId + '...';
  try {
    var result = await api('/api/redmine-agent/issues/' + issueId + '/fetch', {method: 'POST'});
    if (result.action === 'exists') {
      document.getElementById('searchInput').value = '';
      loadIssues();
      return;
    }
    // Wait for analysis to complete
    btn.textContent = '⏳ 分析 #' + issueId + '...';
    await waitForRun(result.run_id);
    document.getElementById('searchInput').value = '';
    loadIssues();
  } catch (e) {
    alert('拉取工单失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ---- Issues list ----
async function loadIssues(page) {
  if (page) currentPage = page;
  const search = document.getElementById('searchInput').value.trim();
  const status = document.getElementById('statusFilter').value;
  const priority = document.getElementById('priorityFilter').value;
  const offset = (currentPage - 1) * pageSize;
  let url = `/api/redmine-agent/issues?limit=${pageSize}&offset=${offset}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;
  if (priority) url += `&priority=${encodeURIComponent(priority)}`;
  try {
    const data = await api(url);
    renderIssuesList(data.items || []);
    renderPagination(data.total || 0, data.limit, data.offset);
  } catch (e) {
    document.getElementById('issuesList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderIssuesList(issues) {
  const box = document.getElementById('issuesList');
  if (!issues.length) {
    box.innerHTML = '<div class="muted" style="padding:20px">暂无工单数据。点击"全量同步"按钮拉取所有指派给你的 Redmine 工单。</div>';
    return;
  }
  box.innerHTML = issues.map(renderIssueCard).join('');
}

function renderIssueCard(item) {
  const refs = item.references_json || [];
  const failures = item.failures_json || [];
  const ai = item.ai_json || {};

  // Extract seven fields
  const title = esc(item.subject || ai.title || '-');
  const problemDesc = esc(item.problem_description || item.description || '-');
  const errorInfoRaw = item.error_info || _extractErrorHtml(failures) || '-';
  const errorAnalysis = esc(item.error_analysis || ai.root_cause_guess || '-');
  const solutionRaw = item.solution || ai.solution || '-';
  const patchRaw = item.patch_direction || ai.patch_direction || '-';

  const statusClass = ['已关闭','Closed','已解决','Resolved'].includes(item.status_name) ? 'ok' :
                      ['紧急','Urgent'].includes(item.priority_name) ? 'high' :
                      ['高','High'].includes(item.priority_name) ? 'medium' : '';

  // Build combined error info: test module/case + error stack trace in one code block
  var errorInfoCombined = errorInfoRaw;
  if (failures && failures.length) {
    var f0 = failures[0];
    var header = '';
    if (f0.module) header += '测试模块: ' + f0.module + _NL;
    if (f0.name) header += '测试用例: ' + f0.name + _NL;
    if (header) header += _NL;
    // Prepend test info before the error code block
    // If errorInfoRaw starts with ```, insert after the opening fence
    if (errorInfoRaw.startsWith(_F3) || errorInfoRaw.startsWith(_BT+_BT+_BT)) {
      // Find the first newline after ```
      var nlIdx = errorInfoRaw.indexOf(_NL);
      if (nlIdx > 0) {
        errorInfoCombined = errorInfoRaw.substring(0, nlIdx + 1) + header + errorInfoRaw.substring(nlIdx + 1);
      } else {
        errorInfoCombined = header + errorInfoRaw;
      }
    } else {
      errorInfoCombined = header + errorInfoRaw;
    }
  }

  // Build references HTML — full display, no truncation
  let refsHtml = '';
  if (refs.length) {
    refsHtml = refs.map(r => {
      const level = r.similarity_level || 'low';
      const score = (r.score || 0).toFixed(0);
      const levelText = level === 'high' ? '高' : level === 'medium' ? '中' : '低';
      return `<div class="ref-item">
        <a href="https://redmine.rock-chips.com/issues/${r.issue_id}" target="_blank">#${r.issue_id}</a>
        <span class="ref-badge ${level}">${levelText} ${score}</span>
        <span class="ref-title">${esc(r.subject || '')}</span>
      </div>`;
    }).join('');
  } else {
    refsHtml = '<div class="muted">暂无参考单</div>';
  }

  // Detect issue type: GMS certification or SDK platform
  var issueType = 'SDK';
  var comp = (item.component || '').toUpperCase();
  var cat = (item.category || '').toUpperCase();
  var fv = (item.fixed_version || '').toUpperCase();
  if (comp.includes('GMS') || cat.includes('GMS') || fv.includes('GMS')) issueType = 'GMS';

  // Detect status display
  var statusName = item.status_name || '-';
  var statusIcon = '';
  if (['已关闭','Closed'].includes(statusName)) statusIcon = '✅ ';
  else if (['已解决','Resolved'].includes(statusName)) statusIcon = '✓ ';
  else if (['新建','New'].includes(statusName)) statusIcon = '🆕 ';
  var isClosed = ['已关闭','Closed','已解决','Resolved'].includes(statusName);

  return `<div class="issue-card">
    <h3>
      <a href="https://redmine.rock-chips.com/issues/${item.issue_id}" target="_blank">#${item.issue_id}</a>
      <span>${title}</span>
      <span style="margin-left:auto;font-size:12px;color:var(--muted)">${esc(item.priority_name || '-')}</span>
    </h3>

    <div class="field-label">📋 基本信息</div>
    <table class="info-table">
      <tr>
        <th>SoC</th><td><strong>${esc(item.soc_platform || '-')}</strong></td>
        <th>Android</th><td><strong>${esc(item.android_version || '-')}</strong></td>
        <th>类型</th><td>${esc(issueType)}</td>
        <th>分类</th><td>${esc(item.category || '-')}</td>
        <th>状态</th><td>${statusIcon}${esc(statusName)}</td>
        <th>指派</th><td>${esc(item.assigned_to_name || '-')}</td>
        <th>创建</th><td>${esc((item.created_on || '-').slice(0, 10))}</td>
      </tr>
    </table>

    <div class="field">
      <div class="field-label">📝 问题描述</div>
      <div class="field-content">${trunc(problemDesc, 500)}</div>
    </div>

    <div class="field">
      <div class="field-label">🔴 报错信息</div>
      ${renderFormattedContent(truncSafe(errorInfoCombined, 2000), 'field-content error-section')}
    </div>

    <div class="field">
      <div class="field-label">🔍 报错分析</div>
      <div class="field-content">${trunc(errorAnalysis, 800)}</div>
    </div>

    <div class="field">
      <div class="field-label">✅ 解决方案</div>
      <div class="field-content solution-section">${renderFormattedContent(truncSafe(solutionRaw, 1500), 'field-content solution-section')}</div>
    </div>

    ${patchRaw && patchRaw !== '-' && patchRaw !== '需要进一步分析具体日志和源码' ? `<div class="field">
      <div class="field-label">🔧 解决补丁</div>
      ${renderFormattedContent(patchRaw, 'field-content')}
    </div>` : ''}

    ${refs.length ? `<div class="field">
      <div class="field-label">📎 参考Redmine</div>
      ${refsHtml}
    </div>` : ''}

    <details><summary>📄 完整文档</summary><div class="formatted-doc">${renderFormattedContent(item.doc_content || '', 'field-content')}</div></details>
  </div>`;
}

function _extractErrorHtml(failures) {
  if (!failures || !failures.length) return '';
  return failures.slice(0, 3).map(f => `[${f.module || '-'}] ${f.name || '-'}: ${trunc(f.reason || '', 200)}`).join('\\n');
}

function renderPagination(total, limit, offset) {
  const box = document.getElementById('issuesPagination');
  const pages = Math.ceil(total / limit);
  const current = Math.floor(offset / limit) + 1;
  if (pages <= 1) { box.innerHTML = `<div class="muted">共 ${total} 条</div>`; return; }
  let html = '';
  if (current > 1) html += `<button onclick="loadIssues(${current-1})">上一页</button>`;
  html += `<span class="muted" style="line-height:32px">第 ${current}/${pages} 页 (共${total}条)</span>`;
  if (current < pages) html += `<button onclick="loadIssues(${current+1})">下一页</button>`;
  box.innerHTML = html;
}

// ---- Runs ----
async function loadRuns() {
  try {
    const data = await api('/api/redmine-agent/runs?limit=30');
    const items = data.items || [];
    const box = document.getElementById('runsList');
    box.innerHTML = items.map(run => `
      <div class="run-item ${run.run_id === currentRunId ? 'active' : ''}" onclick="loadRun('${esc(run.run_id)}')">
        <div class="run-item-title">${esc(run.started_at || run.run_id)}</div>
        <div class="run-item-meta">${esc(run.status)} | mode=${esc(run.mode)} | issues ${run.issue_count || 0} | done ${run.processed_count || 0}</div>
      </div>`).join('') || '<div class="muted" style="padding:12px">暂无扫描记录</div>';
    if (!currentRunId && items.length) loadRun(items[0].run_id);
  } catch (e) {
    document.getElementById('runsList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function loadRun(runId) {
  currentRunId = runId;
  try {
    const data = await api('/api/redmine-agent/runs/' + encodeURIComponent(runId));
    document.getElementById('runDetailTitle').textContent = '日报详情 ' + runId;
    const issues = data.issues || [];
    document.getElementById('runDetail').innerHTML = `
      <div class="muted">状态: ${esc(data.run.status)} | 报告: ${esc(data.run.report_path || '-')}</div>
      <div style="height:10px"></div>
      ${issues.map(renderIssueCard).join('') || '<div class="muted">没有扫描到问题。</div>'}`;
    loadRuns();
  } catch (e) {
    document.getElementById('runDetail').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

// ---- Statistics ----
function renderTrend(title, items, keyName) {
  const reversed = items.slice().reverse();
  const max = Math.max(1, ...reversed.map(item => Number(item.count || 0)));
  const rows = reversed.map(item => {
    const label = item[keyName] || '-';
    const count = Number(item.count || 0);
    const pct = Math.max(5, Math.round((count / max) * 100));
    return `<div class="bar-row">
      <div class="bar-label" title="${esc(label)}">${esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="bar-count">${count}</div>
    </div>`;
  }).join('');
  return `<section class="trend-panel"><h3>${esc(title)}</h3><div class="trend-body">${rows || '<div class="muted">暂无已解决数据</div>'}</div></section>`;
}

function renderMiniIssueList(title, items, emptyText, sectionId) {
  const rows = (items || []).map(item => {
    const issueId = item.issue_id || '';
    const reply = item.last_external_reply_by ? `最后回复: ${item.last_external_reply_by}` : `附件: ${item.attachment_count || 0}`;
    const time = item.last_external_reply_at || item.updated_on || item.created_on || '-';
    return `<div class="issue-mini">
      <div><a href="https://redmine.rock-chips.com/issues/${issueId}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
      <div class="issue-mini-title">
        <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
        <span>${esc(reply)}${item.last_external_reply ? ' | ' + esc(trunc(item.last_external_reply, 120)) : ''}</span>
      </div>
      <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(time).slice(0, 16))}</div>
    </div>`;
  }).join('');
  return `<section class="stats-section" id="${sectionId || ''}"><h2>${esc(title)}</h2><div class="issue-mini-list">${rows || `<div class="muted">${esc(emptyText || '暂无数据')}</div>`}</div></section>`;
}

function renderGroupCards(title, data) {
  const cards = Object.entries(data || {}).map(([k,v]) => `<div class="stat-card"><div class="value">${v}</div><div class="label">${esc(k)}</div></div>`).join('');
  return `<section class="stats-section"><h2>${esc(title)}</h2><div class="stats-grid">${cards || '<div class="muted">无数据</div>'}</div></section>`;
}

async function loadStatistics() {
  try {
    const [basic, workload] = await Promise.all([
      api('/api/redmine-agent/statistics'),
      api('/api/redmine-agent/statistics/workload?stale_days=3&list_limit=30')
    ]);
    const lists = workload.lists || {};
    const meta = workload.meta || {};

    document.getElementById('statsContent').innerHTML = `
      <section class="stats-section">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0">
          <h2 style="margin:0">我的 Redmine 概览</h2>
          <div class="muted" style="margin:0;font-size:12px">统计身份: ${(meta.owner_names || []).map(esc).join(' / ') || '未识别'} | 更新时间: ${esc((meta.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, ''))}</div>
        </div>
        <div class="stats-grid" style="margin-top:12px">
          <div class="stat-card warn"><div class="value">${workload.open_count || 0}</div><div class="label">当前未 Close</div></div>
          <div class="stat-card bad clickable-stat" onclick="scrollToSection('sec-waiting-reply')"><div class="value">${workload.waiting_my_reply || 0}</div><div class="label">待我回复 ⬇</div></div>
          <div class="stat-card bad clickable-stat" onclick="scrollToSection('sec-no-reply-3d')"><div class="value">${workload.no_reply_3_days || 0}</div><div class="label">3 天未回复 ⬇</div></div>
          <div class="stat-card warn clickable-stat" onclick="scrollToSection('sec-missing-report')"><div class="value">${workload.missing_test_report || 0}</div><div class="label">缺失测试报告 ⬇</div></div>
          <div class="stat-card"><div class="value">${workload.total_owned || 0}</div><div class="label">我名下历史数量</div></div>
          <div class="stat-card ok"><div class="value">${workload.closed_count || 0}</div><div class="label">已解决 / 已关闭</div></div>
        </div>
      </section>

      <div class="trend-grid">
        ${renderTrend('每天解决的 Redmine 问题', workload.resolved_daily || [], 'date')}
        ${renderTrend('每周解决的 Redmine 问题', workload.resolved_weekly || [], 'week')}
        ${renderTrend('每月解决的 Redmine 问题', workload.resolved_monthly || [], 'month')}
        ${renderTrend('每年解决的 Redmine 问题', workload.resolved_yearly || [], 'year')}
      </div>

      ${renderMiniIssueList('待我回复的问题 (' + (lists.waiting_my_reply || []).length + ')', lists.waiting_my_reply || [], '暂无待回复问题', 'sec-waiting-reply')}
      ${renderMiniIssueList('3天我未回复的问题 (' + (lists.no_reply_3_days || []).length + ')', lists.no_reply_3_days || [], '暂无超过3天未回复问题', 'sec-no-reply-3d')}
      ${renderMiniIssueList('缺失测试报告的问题 (' + (lists.missing_test_report || []).length + ')', lists.missing_test_report || [], '暂无缺失测试报告问题', 'sec-missing-report')}
    `;
  } catch (e) {
    document.getElementById('statsContent').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

// ---- Actions ----
function _sendParentNotification(title, message, level) {
  try {
    window.parent.postMessage({type:'redmine-agent-notification', title, message, level}, '*');
  } catch(_) {}
}

async function startScan() {
  const btn = document.getElementById('scanBtn');
  btn.disabled = true; btn.textContent = '⏳ 扫描中...';
  try {
    const started = await api('/api/redmine-agent/runs?hours=48&max_issues=50', {method:'POST'});
    const rid = started.run_id || '';
    btn.textContent = '⏳ 等待结果...';
    await waitForRun(rid, '扫描');
  } catch (e) { alert('扫描失败: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '🔍 扫描'; }
}

async function triggerSync() {
  if (!confirm('确认全量同步所有指派给你的 Redmine 工单？这可能需要几分钟。')) return;
  const btn = document.getElementById('scanBtn');
  try {
    const started = await api('/api/redmine-agent/sync?max_analyze=30', {method:'POST'});
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 同步中...'; }
    await waitForRun(started.run_id, '同步');
  } catch (e) { alert('同步失败: ' + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '🔍 扫描'; } }
}

async function waitForRun(runId, label) {
  for (let i = 0; i < 240; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const status = await api('/api/redmine-agent/status');
      if (!status.running) {
        refreshCurrentTab();
        _sendParentNotification('RedmineAgent ' + label + '完成', '任务 ' + runId + ' 已完成', 'success');
        return;
      }
    } catch (_) {}
  }
  refreshCurrentTab();
  _sendParentNotification('RedmineAgent ' + label + '超时', '任务 ' + runId + ' 等待超时，请检查状态', 'warning');
}

// ---- Init ----
loadStatistics();

// Check if a task is already running on page load — reset button state
(async function() {
  try {
    const status = await api('/api/redmine-agent/status');
    if (!status.running) {
      var btn = document.getElementById('scanBtn');
      if (btn) { btn.disabled = false; btn.textContent = '🔍 扫描'; }
    }
  } catch(_) {}
})();

// Auto-refresh status
setInterval(async () => {
  try {
    const status = await api('/api/redmine-agent/status');
    var btn = document.getElementById('scanBtn');
    if (status.running) {
      document.title = '⏳ RedmineAgent (运行中...)';
    } else {
      document.title = '🔧 RedmineAgent';
      if (btn && btn.disabled) { btn.disabled = false; btn.textContent = '🔍 扫描'; }
    }
  } catch (_) {}
}, 10000);
</script>
</body>
</html>
"""
    )
