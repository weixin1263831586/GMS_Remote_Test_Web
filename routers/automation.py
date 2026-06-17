"""Automation APIs for GMS ATS-style runs."""

from __future__ import annotations

import uuid
import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from core.api_response import error_response
from core.automation.executors import HttpAutomationExecutor, StubAutomationExecutor
from core.automation.gerrit_trigger import match_profiles, normalize_gerrit_event, profile_matches_event
from core.automation.models import AutomationRunCreateRequest
from core.automation.orchestrator import AutomationOrchestrator
from core.automation.profiles import load_profiles, upsert_profile
from core.automation.store import AutomationStore
from core.settings import PROJECT_ROOT


router = APIRouter(prefix="/api/automation")
page_router = APIRouter()
automation_store = AutomationStore(Path(PROJECT_ROOT) / "data" / "automation_runs.sqlite3")


def _new_run_id() -> str:
    return f"ats_{uuid.uuid4().hex[:12]}"


def _orchestrator(executor_name: str = "stub") -> AutomationOrchestrator:
    if executor_name == "http":
        return AutomationOrchestrator(automation_store, HttpAutomationExecutor())
    return AutomationOrchestrator(automation_store, StubAutomationExecutor())


def _profiles_path() -> Path:
    profiles_path = Path(PROJECT_ROOT) / "configs" / "automation_profiles.json"
    if profiles_path.exists():
        return profiles_path
    return Path(PROJECT_ROOT) / "configs" / "automation_profiles.example.json"


def _load_profiles_for_api(enabled_only: bool = False):
    return load_profiles(_profiles_path(), enabled_only=enabled_only)


@page_router.get("/automation", response_class=HTMLResponse)
async def automation_page():
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GMS ATS</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #111827;
            --panel: #172033;
            --panel-2: #0f172a;
            --line: #334155;
            --text: #e5e7eb;
            --muted: #94a3b8;
            --primary: #2dd4bf;
            --warn: #f59e0b;
            --danger: #ef4444;
            --ok: #22c55e;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            font-size: 13px;
        }
        .ats-shell { min-height: 100vh; padding: 14px; display: grid; gap: 12px; }
        .ats-toolbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 10px 12px;
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 6px;
        }
        .ats-title { margin: 0; font-size: 18px; font-weight: 700; }
        .ats-subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
        .ats-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
        button {
            border: 1px solid var(--line);
            background: #1e293b;
            color: var(--text);
            border-radius: 5px;
            padding: 7px 10px;
            cursor: pointer;
            font-size: 12px;
        }
        button:hover { border-color: var(--primary); }
        button.primary { background: #0f766e; border-color: #14b8a6; }
        button.danger { background: #7f1d1d; border-color: #b91c1c; }
        .ats-grid { display: grid; grid-template-columns: minmax(320px, 380px) 1fr; gap: 12px; align-items: start; }
        .panel {
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 6px;
            min-width: 0;
        }
        .panel-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 10px 12px;
            border-bottom: 1px solid var(--line);
            font-weight: 700;
        }
        .panel-body { padding: 12px; }
        .stack { display: grid; gap: 12px; }
        label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
        input, select, textarea {
            width: 100%;
            border: 1px solid var(--line);
            background: var(--panel-2);
            color: var(--text);
            border-radius: 5px;
            padding: 8px;
            font-size: 12px;
        }
        textarea { min-height: 76px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
        .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .tabs { display: flex; gap: 6px; }
        .tab.active { background: #0f766e; border-color: #14b8a6; }
        .table-wrap { overflow: auto; max-height: calc(100vh - 220px); }
        table { width: 100%; border-collapse: collapse; min-width: 860px; }
        th, td { border-bottom: 1px solid #253449; padding: 8px; text-align: left; vertical-align: top; }
        th { color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--panel); z-index: 1; }
        tr.run-row { cursor: pointer; }
        tr.run-row:hover { background: #1e293b; }
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 2px 7px;
            border-radius: 999px;
            border: 1px solid var(--line);
            color: var(--text);
            white-space: nowrap;
            font-size: 12px;
        }
        .badge.completed { color: var(--ok); border-color: var(--ok); }
        .badge.failed, .badge.jenkins_failed, .badge.flash_failed, .badge.test_failed { color: var(--danger); border-color: var(--danger); }
        .badge.testing, .badge.flashing, .badge.jenkins_building { color: var(--warn); border-color: var(--warn); }
        .events { max-height: 300px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
        .event { border-bottom: 1px solid #253449; padding: 8px 0; }
        .event-meta { color: var(--muted); margin-bottom: 3px; }
        .muted { color: var(--muted); }
        .nowrap { white-space: nowrap; }
        .toast { min-height: 18px; color: var(--muted); }
        @media (max-width: 980px) {
            .ats-grid { grid-template-columns: 1fr; }
            .ats-toolbar { align-items: flex-start; flex-direction: column; }
            .ats-actions { justify-content: flex-start; }
            .form-row { grid-template-columns: 1fr; }
            .table-wrap { max-height: none; }
        }
    </style>
</head>
<body>
    <main class="ats-shell">
        <section class="ats-toolbar">
            <div>
                <h1 class="ats-title">GMS ATS</h1>
                <div class="ats-subtitle">Gerrit -> Jenkins -> 刷机 -> GMS 测试 -> 报告分析</div>
            </div>
            <div class="ats-actions">
                <button type="button" onclick="loadAll()">刷新</button>
                <button type="button" onclick="pollGerrit()">Gerrit Poll</button>
                <button type="button" onclick="tickWorker('stub')">Worker Tick</button>
                <button type="button" class="primary" onclick="tickWorker('http')">真实 API Tick</button>
            </div>
        </section>

        <section class="ats-grid">
            <div class="stack">
                <section class="panel">
                    <div class="panel-header">手动创建运行</div>
                    <div class="panel-body stack">
                        <label>Profile
                            <select id="automation-profile"></select>
                        </label>
                        <label>固件路径或 URL
                            <input id="automation-artifact" placeholder="/path/to/update.img 或 http://...">
                        </label>
                        <label>设备序列号
                            <input id="automation-devices" placeholder="ABC123,DEF456">
                        </label>
                        <div class="form-row">
                            <label>测试类型
                                <input id="automation-test-type" value="CTS">
                            </label>
                            <label>测试套件
                                <input id="automation-test-suite" placeholder="android-cts">
                            </label>
                        </div>
                        <label>测试模块
                            <input id="automation-test-module" placeholder="CtsAppSecurityHostTestCases">
                        </label>
                        <label>附加 test_plan JSON
                            <textarea id="automation-test-plan" placeholder='{"retry": false}'></textarea>
                        </label>
                        <button type="button" id="automation-create-run" class="primary" onclick="createRun()">创建运行</button>
                        <div id="automation-toast" class="toast"></div>
                    </div>
                </section>

                <section class="panel">
                    <div class="panel-header">Profiles</div>
                    <div class="panel-body">
                        <div id="automation-profiles" class="stack"></div>
                    </div>
                </section>

                <section class="panel">
                    <div class="panel-header">事件</div>
                    <div class="panel-body">
                        <div id="automation-events" class="events muted">选择一条运行查看事件。</div>
                    </div>
                </section>
            </div>

            <section class="panel">
                <div class="panel-header">
                    <span>Runs</span>
                    <div class="tabs">
                        <button type="button" class="tab active" data-status="" onclick="setStatusFilter('')">全部</button>
                        <button type="button" class="tab" data-status="queued" onclick="setStatusFilter('queued')">Queued</button>
                        <button type="button" class="tab" data-status="testing" onclick="setStatusFilter('testing')">Testing</button>
                        <button type="button" class="tab" data-status="completed" onclick="setStatusFilter('completed')">Completed</button>
                    </div>
                </div>
                <div class="table-wrap">
                    <table id="automation-runs">
                        <thead>
                            <tr>
                                <th>状态</th>
                                <th>Profile</th>
                                <th>来源</th>
                                <th>Gerrit</th>
                                <th>固件</th>
                                <th>设备</th>
                                <th>报告</th>
                                <th>更新时间</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody><tr><td colspan="9" class="muted">加载中...</td></tr></tbody>
                    </table>
                </div>
            </section>
        </section>
    </main>

    <script>
        let atsProfiles = [];
        let atsRuns = [];
        let atsStatus = '';

        function qs(id) { return document.getElementById(id); }
        function esc(value) {
            return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
        }
        function toast(message) { qs('automation-toast').textContent = message; }
        async function api(path, options) {
            const resp = await fetch(path, options);
            const data = await resp.json();
            if (!data.success) throw new Error(data.error || data.message || '请求失败');
            return data.data;
        }

        async function loadProfiles() {
            const data = await api('/api/automation/profiles');
            atsProfiles = data.items || [];
            const select = qs('automation-profile');
            select.innerHTML = atsProfiles.map(p => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('');
            qs('automation-profiles').innerHTML = atsProfiles.length
                ? atsProfiles.map(p => `<div><span class="badge">${esc(p.enabled ? 'enabled' : 'disabled')}</span> <strong>${esc(p.name || p.id)}</strong><div class="muted">${esc(p.id)} / ${esc((p.jenkins || {}).job || 'manual')}</div></div>`).join('')
                : '<div class="muted">未配置 automation_profiles.json，当前使用 example 配置。</div>';
        }

        async function loadRuns() {
            const query = atsStatus ? `?status=${encodeURIComponent(atsStatus)}&limit=100` : '?limit=100';
            const data = await api('/api/automation/runs' + query);
            atsRuns = data.items || [];
            const body = qs('automation-runs').querySelector('tbody');
            if (!atsRuns.length) {
                body.innerHTML = '<tr><td colspan="9" class="muted">暂无运行记录。</td></tr>';
                return;
            }
            body.innerHTML = atsRuns.map(run => `
                <tr class="run-row" onclick="loadEvents('${esc(run.id)}')">
                    <td><span class="badge ${esc(run.status)}">${esc(run.status)}</span></td>
                    <td>${esc(run.profile_id)}</td>
                    <td>${esc(run.source_type || 'manual')}<div class="muted">${esc(run.owner || '')}</div></td>
                    <td>${esc(run.project || '')}<div class="muted">${esc(run.gerrit_change_id || '')}${run.gerrit_patchset ? ' / PS' + esc(run.gerrit_patchset) : ''}</div></td>
                    <td>${esc(run.artifact_path || run.artifact_url || '')}</td>
                    <td>${esc(run.devices_json || '')}</td>
                    <td>${run.report_url ? `<a href="${esc(run.report_url)}" target="_blank">报告</a>` : '<span class="muted">-</span>'}</td>
                    <td class="nowrap">${esc(run.updated_at || run.created_at || '')}</td>
                    <td class="nowrap">
                        <button type="button" onclick="event.stopPropagation(); retryRun('${esc(run.id)}')">重试</button>
                        <button type="button" class="danger" onclick="event.stopPropagation(); cancelRun('${esc(run.id)}')">取消</button>
                    </td>
                </tr>
            `).join('');
        }

        async function loadEvents(runId) {
            const data = await api(`/api/automation/runs/${encodeURIComponent(runId)}/events`);
            const items = data.items || [];
            qs('automation-events').innerHTML = items.length
                ? items.map(ev => `<div class="event"><div class="event-meta">${esc(ev.created_at)} / ${esc(ev.stage)} / ${esc(ev.level)}</div><div>${esc(ev.message)}</div></div>`).join('')
                : '<div class="muted">暂无事件。</div>';
        }

        function collectTestPlan() {
            let extra = {};
            const raw = qs('automation-test-plan').value.trim();
            if (raw) extra = JSON.parse(raw);
            return {
                ...extra,
                test_type: qs('automation-test-type').value.trim(),
                test_suite: qs('automation-test-suite').value.trim(),
                test_module: qs('automation-test-module').value.trim(),
            };
        }

        async function createRun() {
            try {
                const artifact = qs('automation-artifact').value.trim();
                const devices = qs('automation-devices').value.split(',').map(v => v.trim()).filter(Boolean);
                const payload = {
                    profile_id: qs('automation-profile').value,
                    source_type: 'manual',
                    artifact_path: artifact.startsWith('http') ? '' : artifact,
                    artifact_url: artifact.startsWith('http') ? artifact : '',
                    devices,
                    test_plan: collectTestPlan(),
                };
                const run = await api('/api/automation/runs', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                toast(`已创建 ${run.id}`);
                await loadRuns();
                await loadEvents(run.id);
            } catch (err) {
                toast(err.message);
            }
        }

        async function pollGerrit() {
            try {
                const data = await api('/api/automation/gerrit/poll', {method: 'POST'});
                toast(`Gerrit poll 创建 ${data.created_count || 0} 条，已存在 ${data.existing_count || 0} 条`);
                await loadRuns();
            } catch (err) { toast(err.message); }
        }

        async function tickWorker(executor) {
            try {
                const run = await api(`/api/automation/worker/tick?executor=${encodeURIComponent(executor)}`, {method: 'POST'});
                toast(run ? `推进到 ${run.status}: ${run.id}` : '没有可推进的运行');
                await loadRuns();
                if (run && run.id) await loadEvents(run.id);
            } catch (err) { toast(err.message); }
        }

        async function retryRun(runId) {
            try {
                const run = await api(`/api/automation/runs/${encodeURIComponent(runId)}/retry`, {method: 'POST'});
                toast(`已创建重试 ${run.id}`);
                await loadRuns();
            } catch (err) { toast(err.message); }
        }

        async function cancelRun(runId) {
            try {
                const run = await api(`/api/automation/runs/${encodeURIComponent(runId)}/cancel`, {method: 'POST'});
                toast(`已取消 ${run.id}`);
                await loadRuns();
                await loadEvents(run.id);
            } catch (err) { toast(err.message); }
        }

        function setStatusFilter(status) {
            atsStatus = status;
            document.querySelectorAll('.tab').forEach(btn => btn.classList.toggle('active', btn.dataset.status === status));
            loadRuns().catch(err => toast(err.message));
        }

        async function loadAll() {
            try {
                await loadProfiles();
                await loadRuns();
                toast('已刷新');
            } catch (err) { toast(err.message); }
        }

        document.addEventListener('DOMContentLoaded', loadAll);
    </script>
</body>
</html>
        """
    )


def _format_template_map(raw: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    formatted = {}
    for key, value in (raw or {}).items():
        if isinstance(value, str):
            try:
                formatted[key] = value.format(
                    gerrit_change_id=event.get("change_id", ""),
                    gerrit_patchset=event.get("patchset", ""),
                    project=event.get("project", ""),
                    branch=event.get("branch", ""),
                    revision=event.get("revision", ""),
                )
            except Exception:
                formatted[key] = value
        else:
            formatted[key] = value
    return formatted


def _run_request_from_gerrit_event(event: Dict[str, Any], profile: Dict[str, Any]) -> AutomationRunCreateRequest:
    jenkins = profile.get("jenkins") if isinstance(profile.get("jenkins"), dict) else {}
    test_plan = profile.get("test_plan") if isinstance(profile.get("test_plan"), dict) else {}
    flash = profile.get("flash") if isinstance(profile.get("flash"), dict) else {}
    device_selector = profile.get("device_selector") if isinstance(profile.get("device_selector"), dict) else {}
    reporting = profile.get("reporting") if isinstance(profile.get("reporting"), dict) else {}
    jenkins_plan = {
        **jenkins,
        "parameters": _format_template_map(jenkins.get("parameters") or {}, event),
        "artifact_pattern": jenkins.get("artifact_pattern", ""),
    }
    merged_test_plan = {
        **test_plan,
        "flash": flash,
        "device_selector": device_selector,
        "reporting": reporting,
        "jenkins": jenkins_plan,
    }
    return AutomationRunCreateRequest(
        profile_id=profile.get("id", ""),
        source_type=event.get("source_type", "gerrit_webhook"),
        source_key=f"{event.get('source_key', '')}:{profile.get('id', '')}",
        project=event.get("project", ""),
        branch=event.get("branch", ""),
        gerrit_change_id=event.get("change_id", ""),
        gerrit_patchset=event.get("patchset", ""),
        gerrit_subject=event.get("subject", ""),
        owner=event.get("owner", ""),
        test_plan=merged_test_plan,
    )


def _gerrit_change_to_event(change: Dict[str, Any]) -> Dict[str, Any]:
    revision = str(change.get("current_revision") or change.get("revision") or "")
    revisions = change.get("revisions") if isinstance(change.get("revisions"), dict) else {}
    revision_info = revisions.get(revision) if revision else {}
    patchset = str(
        change.get("patchset")
        or change.get("patch_set")
        or (revision_info or {}).get("_number")
        or (revision_info or {}).get("number")
        or ""
    )
    return normalize_gerrit_event({
        "type": "poll",
        "change": {
            "project": change.get("project", ""),
            "branch": change.get("branch", ""),
            "number": change.get("number") or change.get("_number") or change.get("id") or "",
            "subject": change.get("subject", ""),
            "owner": change.get("owner") or {},
        },
        "patchSet": {"number": patchset, "revision": revision},
    })


def _create_runs_for_event(event: Dict[str, Any], profiles: list[Dict[str, Any]]) -> Dict[str, Any]:
    matches = match_profiles(event, profiles)
    created = []
    existing = []
    for profile in matches:
        create_req = _run_request_from_gerrit_event(event, profile)
        old_run = automation_store.get_run_by_source_key(create_req.source_key)
        if old_run:
            existing.append(old_run)
            continue
        run_data = create_req.to_run_dict(_new_run_id())
        jenkins = profile.get("jenkins") if isinstance(profile.get("jenkins"), dict) else {}
        run_data["jenkins_job"] = str(jenkins.get("job") or "")
        run_data["test_plan_json"] = json.dumps(create_req.test_plan or {}, ensure_ascii=False, separators=(",", ":"))
        run = automation_store.create_run(run_data)
        automation_store.append_event(run["id"], run["status"], "info", "Gerrit event matched automation profile", {
            "event": event,
            "profile_id": profile.get("id", ""),
        })
        created.append(run)
    return {"matched_profiles": [p.get("id", "") for p in matches], "created": created, "existing": existing}


async def query_gerrit_changes_for_automation(query: str, limit: int = 100) -> list[Dict[str, Any]]:
    from core.config import config_manager
    from routers.gerrit_dashboard import _query_gerrit_dual_mode

    cfg = config_manager.get_gerrit_dashboard_config()
    effective_query = query.strip() or "status:open"
    if "limit:" not in effective_query:
        effective_query = f"{effective_query} limit:{limit}"
    result = await _query_gerrit_dual_mode(cfg, effective_query, max_changes=limit)
    return result.get("items") or result.get("changes") or []


@router.get("/profiles")
async def list_automation_profiles(enabled_only: bool = Query(False)):
    return {"success": True, "data": {"items": _load_profiles_for_api(enabled_only=enabled_only)}}


@router.post("/profiles")
async def save_automation_profile(req: Dict[str, Any]):
    try:
        profile = upsert_profile(_profiles_path(), req or {})
    except ValueError as exc:
        return error_response(str(exc), 400)
    return {"success": True, "data": {"profile": profile, "items": _load_profiles_for_api(enabled_only=False)}}


@router.put("/profiles/{profile_id}")
async def update_automation_profile(profile_id: str, req: Dict[str, Any]):
    body = dict(req or {})
    body["id"] = profile_id
    return await save_automation_profile(body)


@router.post("/profiles/{profile_id}/dry-run")
async def dry_run_automation_profile(profile_id: str, req: Dict[str, Any]):
    profiles = _load_profiles_for_api(enabled_only=False)
    profile = next((item for item in profiles if item.get("id") == profile_id), None)
    if not profile:
        return error_response("Automation profile not found", 404)
    event = normalize_gerrit_event({
        "type": "dry-run",
        "change": {
            "project": req.get("project", ""),
            "branch": req.get("branch", ""),
            "number": req.get("change_id") or req.get("number") or "",
            "subject": req.get("subject", ""),
            "owner": {"email": req.get("owner", "")},
        },
        "patchSet": {"number": req.get("patchset", ""), "revision": req.get("revision", "")},
    })
    matched = profile_matches_event(profile, event)
    run_request = _run_request_from_gerrit_event(event, profile).model_dump() if matched else {}
    return {"success": True, "data": {"matched": matched, "event": event, "profile": profile, "run_request": run_request}}


@router.post("/runs")
async def create_automation_run(req: Dict[str, Any]):
    create_req = AutomationRunCreateRequest(**(req or {}))
    run = automation_store.create_run(create_req.to_run_dict(_new_run_id()))
    automation_store.append_event(run["id"], run["status"], "info", "Automation run queued", {"profile_id": run["profile_id"]})
    return {"success": True, "data": run}


@router.get("/runs")
async def list_automation_runs(status: str = Query(""), limit: int = Query(50, ge=1, le=500)):
    return {"success": True, "data": {"items": automation_store.list_runs(status=status, limit=limit)}}


@router.get("/runs/{run_id}")
async def get_automation_run(run_id: str):
    run = automation_store.get_run(run_id)
    if not run:
        return error_response("Automation run not found", 404)
    return {"success": True, "data": run}


@router.get("/runs/{run_id}/events")
async def get_automation_run_events(run_id: str):
    if not automation_store.get_run(run_id):
        return error_response("Automation run not found", 404)
    return {"success": True, "data": {"items": automation_store.list_events(run_id)}}


@router.post("/runs/{run_id}/cancel")
async def cancel_automation_run(run_id: str):
    try:
        run = _orchestrator().cancel_run(run_id)
    except ValueError:
        return error_response("Automation run not found", 404)
    return {"success": True, "data": run}


@router.post("/runs/{run_id}/retry")
async def retry_automation_run(run_id: str):
    old = automation_store.get_run(run_id)
    if not old:
        return error_response("Automation run not found", 404)
    create_req = AutomationRunCreateRequest(
        profile_id=old["profile_id"],
        source_type=old["source_type"],
        project=old["project"],
        branch=old["branch"],
        gerrit_change_id=old["gerrit_change_id"],
        gerrit_patchset=old["gerrit_patchset"],
        gerrit_subject=old["gerrit_subject"],
        owner=old["owner"],
        artifact_url=old["artifact_url"],
        artifact_path=old["artifact_path"],
        devices=[],
        test_plan={},
    )
    run_data = create_req.to_run_dict(_new_run_id())
    run_data["devices_json"] = old["devices_json"]
    run_data["test_plan_json"] = old["test_plan_json"]
    run = automation_store.create_run(run_data)
    automation_store.append_event(run["id"], run["status"], "info", f"Retry created from {run_id}", {"source_run_id": run_id})
    return {"success": True, "data": run}


@router.post("/worker/tick")
async def automation_worker_tick(executor: str = Query("stub")):
    run = _orchestrator(executor_name=executor).advance_next()
    return {"success": True, "data": run}


@router.post("/gerrit/webhook")
async def handle_gerrit_webhook(payload: Dict[str, Any]):
    event = normalize_gerrit_event(payload or {})
    profiles = _load_profiles_for_api(enabled_only=True)
    result = _create_runs_for_event(event, profiles)
    return {"success": True, "data": {"event": event, **result}}


@router.post("/gerrit/poll")
async def poll_gerrit_changes(limit: int = Query(100, ge=1, le=500)):
    profiles = _load_profiles_for_api(enabled_only=True)
    created = []
    existing = []
    events = []
    for profile in profiles:
        gerrit = profile.get("gerrit") if isinstance(profile.get("gerrit"), dict) else {}
        query = str(gerrit.get("query") or "").strip()
        if not query:
            continue
        for change in await query_gerrit_changes_for_automation(query, limit=limit):
            event = _gerrit_change_to_event(change)
            events.append(event)
            result = _create_runs_for_event(event, [profile])
            created.extend(result["created"])
            existing.extend(result["existing"])
    return {"success": True, "data": {"events": events, "created": created, "existing": existing, "created_count": len(created), "existing_count": len(existing)}}
