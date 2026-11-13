// Automation page entry chunk; loaded by page.html in dependency order.
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && qs('ats-trace-drawer')?.classList.contains('open')) {
        event.preventDefault();
        event.stopPropagation();
        closeTrace();
    }
});

// WAI-ARIA tabs pattern：tablist 内左右方向键移动焦点并激活页签，
// Home/End 跳转首/尾；roving tabindex（非活跃 tab tabIndex=-1）由
// switchWorkflowPane 维护，这里只按可见 tab 顺序循环。
document.querySelector('.workflow-tabs')?.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'Home' && event.key !== 'End') return;
    const tabs = Array.from(document.querySelectorAll('.workflow-tab'))
        .filter(tab => tab.offsetParent !== null);
    if (!tabs.length) return;
    const activeIndex = tabs.indexOf(document.querySelector('.workflow-tab.active'));
    let nextIndex;
    if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else if (activeIndex === -1) nextIndex = 0;
    else nextIndex = (activeIndex + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    tabs[nextIndex].focus();
    tabs[nextIndex].scrollIntoView({ block: 'nearest', inline: 'nearest' });
    switchWorkflowPane(tabs[nextIndex].dataset.workflow);
});

// 滚轮落在 tab 栏时转发给激活 workflow 面板的滚动容器：
// 页面本体 overflow:hidden，“页面滚动”由面板内层承载，与
// Gerrit/Redmine 的滚动语义保持一致；tab 栏自身永远不动。
document.querySelector('.workflow-tabs')?.addEventListener('wheel', event => {
    if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
    const pane = document.querySelector('.workflow-pane.active');
    if (!pane) return;
    const delta = event.deltaMode === 1 ? event.deltaY * 40 : event.deltaY;
    const layers = [
        pane,
        ...pane.querySelectorAll(':scope > *'),
        ...pane.querySelectorAll(':scope > * > *'),
        ...pane.querySelectorAll(':scope > * > * > *'),
    ];
    const scroller = layers.find(node =>
        node.scrollHeight > node.clientHeight + 1
        && ['auto', 'scroll'].includes(getComputedStyle(node).overflowY));
    if (scroller) scroller.scrollTop += delta;
}, { passive: true });

async function dryRunProfile() {
    try {
        const profileId = qs('automation-profile').value;
        if (!profileId) throw new Error('请先选择运行配置');
        const payload = {
            project: qs('dryrun-project').value.trim(),
            branch: qs('dryrun-branch').value.trim(),
            change_id: qs('dryrun-change-id').value.trim(),
            patchset: qs('dryrun-patchset').value.trim(),
        };
        const data = await api(`/api/automation/profiles/${encodeURIComponent(profileId)}/dry-run`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const result = qs('dryrun-result');
        const req = data.run_request || {};
        result.innerHTML = `<span class="badge ${data.matched ? 'completed' : 'failed'}">${data.matched ? '匹配' : '未匹配'}</span>`
            + (data.matched ? `<div class="muted" style="margin-top:6px">将创建运行：${esc(req.profile_id || profileId)} / ${esc(req.test_plan?.test_type || '-')} / 设备 ${(req.devices || []).length}</div>` : '');
        toast(data.matched ? '试运行：匹配，会创建运行' : '试运行：未匹配');
    } catch (err) { toast(err.message); }
}

function setStatusFilter(status, {persist = true, load = true} = {}) {
    const target = AUTOMATION_STATUS_FILTERS.has(status) ? status : '';
    atsStatus = target;
    resetRunHistoryPage('runs');
    document.querySelectorAll('.subtabs .tab').forEach(btn => btn.classList.toggle('active', btn.dataset.status === target));
    if (persist) {
        try { window.sessionStorage.setItem(AUTOMATION_STATUS_STORAGE_KEY, target); } catch (_error) {}
        const url = new URL(window.location.href);
        if (target) url.searchParams.set('status', target);
        else url.searchParams.delete('status');
        window.history.replaceState({}, '', url.toString());
    }
    if (load) loadRuns().catch(err => toast(err.message));
}

// page.html 先绘制默认骨架；脚本加载后、首批 API 请求前恢复当前标签页状态，
// 避免刷新时先触发错误面板的额外请求或覆盖已保存筛选。
switchWorkflowPane(activeWorkflowPane, {persist: false, load: false});
setStatusFilter(atsStatus, {persist: false, load: false});
restoreRunHistoryControls();

async function loadWorkerStatus() {
    try {
        const data = await api('/api/automation/worker/status');
        renderWorkerStatus(data);
    } catch { /* worker status is informational only */ }
}

function renderWorkerStatus(data) {
    const el = qs('worker-indicator');
    if (!el) return;
    if (!data) { el.className = 'worker-dot down'; el.title = 'Worker 未知'; return; }
    const ago = data.last_tick_seconds_ago;
    const alive = data.running && (ago === null || ago < data.interval_seconds * 4);
    el.className = `worker-dot ${alive ? 'up' : 'down'}`;
    el.title = alive
        ? `Worker 运行中（${data.executor}，${data.interval_seconds}s，上次 tick ${ago === null ? '-' : ago + 's'} 前）`
        : `Worker 未运行或停滞${ago !== null ? '（上次 tick ' + ago + 's 前）' : ''}`;
}

async function loadAll(silent = false) {
    try {
        await loadDashboard();
    } catch (_) { /* 概览失败不阻断主流程 */ }
    try {
        await Promise.all([loadBuildConfig(), loadClusterWorkers()]);
        await loadTestSuitesForAutomation();
        await Promise.all([loadDevices(false), loadProfiles()]);
        await Promise.all([loadRuns(), loadBuildJobs(), loadWorkerStatus(), loadOpsStatus()]);
        if (activeWorkflowPane === 'plans') await loadTestPlans();
        if (activeWorkflowPane === 'resources') await loadResourceCatalog();
        if (activeWorkflowPane === 'ops') await Promise.all([loadOpsEvents(), loadOpsDisk()]);
        await applyAutomationWorkspaceContext(atsWorkspaceContext);
        if (!silent) toast('已刷新');
    } catch (err) { toast(err.message); }
}

function userIsEditing() {
    const el = document.activeElement;
    if (!el) return false;
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
}

// 统一定时刷新：用户正在编辑输入时跳过本轮，避免打断。
// 页面隐藏时暂停请求，重新进入后立即追平一次状态。
let automationRefreshInterval = null;
let automationRefreshStarted = false;
let automationRefreshPromise = null;
async function refreshAutomationActivity() {
    if (automationRefreshPromise) return automationRefreshPromise;
    automationRefreshPromise = refreshAutomationActivityOnce().finally(() => {
        automationRefreshPromise = null;
    });
    return automationRefreshPromise;
}

async function refreshAutomationActivityOnce() {
    if (userIsEditing()) return;
    try {
        await loadWorkerStatus();
        if (activeWorkflowPane === 'overview') await loadDashboard();
        if (activeWorkflowPane === 'plans') await loadTestPlans();
        if (activeWorkflowPane === 'resources') {
            await loadResourceCatalog();
            await refreshResourceCommands();
        }
        if (activeWorkflowPane === 'runs' || activeWorkflowPane === 'reports') await loadRuns();
        if (activeWorkflowPane === 'ops') {
            await loadOpsStatus();
            await loadOpsEvents();
            await loadOpsDisk();
        }
        if (activeWorkflowPane === 'build') {
            await loadBuildJobs();
            if (selectedBuildJobId) await loadBuildLog(selectedBuildJobId, {silent: true});
        }
        if (activeWorkflowPane === 'events' && selectedRunId) await refreshSelectedEvents();
    } catch (_) { /* 后台刷新静默失败 */ }
}

function syncAutomationAutoRefresh(event) {
    const visible = event?.detail?.visible
        ?? window.GmsEmbeddedWorkspace?.isVisible?.()
        ?? true;
    if (automationRefreshInterval) {
        clearInterval(automationRefreshInterval);
        automationRefreshInterval = null;
    }
    if (!visible) return;
    if (automationRefreshStarted) refreshAutomationActivity();
    automationRefreshStarted = true;
    automationRefreshInterval = setInterval(refreshAutomationActivity, 8000);
}
window.addEventListener('gms:embedded-workspace', event => {
    applyAutomationWorkspaceContext(
        event.detail?.context || {},
        event.detail?.type === 'workspace-context-navigate'
    ).catch(error => toast(error.message));
});
window.addEventListener('gms:embedded-visibility', syncAutomationAutoRefresh);
syncAutomationAutoRefresh();

document.addEventListener('DOMContentLoaded', () => {
    // E2E 以 automationReady 为就绪信号：必须在首轮 loadAll 完成（成功或
    // 失败）后置位，否则测试会在首轮 API 重绘表单前操作，丢失草稿状态
    // 。
    // 首屏区域占位：整页级容器渲染超新星动画，数据到达后被各自 load 覆盖。
    for (const [id, label] of [
        ['dashboard-stats', '正在加载自动化概览...'],
        ['automation-runs', '正在加载运行记录...'],
        ['ops-events', '正在加载运维事件...'],
    ]) {
        const box = document.getElementById(id);
        setLoadingOrbit(box, label);
    }
    loadAll(true)
        .catch(() => {})
        .finally(() => {
            document.body.dataset.automationReady = 'true';
            window.GmsEmbeddedWorkspace?.markReady();
            focusRunFromDeepLink();
        });
});

// 深链直达：shell 侧把 #automation?run=<id> 解析为 sessionStorage 暂存，
// iframe 首帧加载后消费并打开该 run 的全链路时间线。60 秒新鲜度防止
// 旧暂存在无 hash 的后续访问中劫持页面。
function focusRunFromDeepLink() {
    let stashed = null;
    try {
        const raw = window.sessionStorage.getItem('gms_automation_focus_run');
        if (raw) {
            window.sessionStorage.removeItem('gms_automation_focus_run');
            stashed = JSON.parse(raw);
        }
    } catch (_error) { return; }
    const runId = String(stashed?.id || '').trim();
    const age = Date.now() - Number(stashed?.ts || 0);
    if (!runId || age < 0 || age > 60000) return;
    loadEvents(runId).catch(() => {});
}


// act-bridge 委托目标（替代历史 inline handler）。
function onLunchTargetChange(){invalidateRunPreflight();updateStepIndicators();}
function refreshLunchOptionsForced(){refreshLunchOptions({forceRefresh:true});}
function onAutomationWorkerChange(){invalidateRunPreflight();syncAutomationWorkspaceSelection();loadDevices(true);loadTestSuitesForAutomation();updateStepIndicators();}
function onTestTypeChange(){renderSuiteOptions();syncAutomationWorkspaceSelection();updateStepIndicators();}
function onTestSuiteChange(){invalidateRunPreflight();syncAutomationWorkspaceSelection();updateStepIndicators();}
function loadTraceSelected(){if(selectedRunId)loadTrace(selectedRunId);}
function closeTraceOpenReport(event,runId){closeTrace();openRunReport(event,runId);}
function closeTraceOpenAnalysis(event,runId){closeTrace();openRunAnalysis(event,runId);}
