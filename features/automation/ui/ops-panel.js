// Ops panel chunk: kill-switch strip, incident list and ack actions.
// Loaded by page.html before page.js; uses the shared api()/qs()/toast()/esc()
// helpers and the data-click delegation contract. Status is a read-only poll
// (like loadWorkerStatus); POSTs happen only on real user actions.

let opsStatusCache = null;
let opsEventsCache = [];

function opsConfirm(title, message) {
    return typeof window.parent?.showConfirmDialog === 'function'
        ? window.parent.showConfirmDialog(title, message) : window.confirm(message);
}

// 受阻横幅：排队/等设备/烧写前的运行依赖平台健康度。自动化 Worker 停止
// 或推进被暂停时，不在这里的运行会一直"卡住"，用户通常去运行列表猜——
// 把根因直接顶到运行监控页首，并可一键跳运维处理。
function renderBlockedRunsBanner() {
    const host = qs('runs-blocker-banner');
    if (!host) return;
    const data = opsStatusCache;
    if (!data || (!data.advancement_paused && data.worker?.running)) {
        host.innerHTML = '';
        return;
    }
    const reasons = [];
    if (data.worker && !data.worker.running) {
        reasons.push('自动化 Worker 未运行，队列不会推进');
    }
    if (data.advancement_paused) {
        reasons.push('自动化推进已被暂停（kill-switch）');
    }
    const alertCount = Number(data.open_alerts || 0);
    host.innerHTML = `
        <div class="run-value" style="margin: 10px 16px 0; padding: 10px 12px; border: 1px solid var(--warn); border-radius: 8px; background: rgba(255, 152, 0, 0.08);">
            <strong>⚠ 新运行受阻：${esc(reasons.join('；'))}</strong>
            <span class="muted">排队 / 等待设备 / 烧写前的运行将保持等待，直到恢复推进。</span>
            <button type="button" class="primary" data-click="switchWorkflowPane" data-a0="ops">前往运维${alertCount > 0 ? `（${alertCount} 条未确认告警）` : ''}</button>
        </div>`;
}

async function loadOpsStatus() {
    try {
        const data = await api('/api/ops/status');
        opsStatusCache = data;
        renderOpsStatus(data);
        // 平台健康变化即时反映到运行监控的受阻横幅。
        if (typeof renderBlockedRunsBanner === 'function') renderBlockedRunsBanner();
    } catch { /* ops status is informational; next tick retries */ }
}

async function loadOpsDisk() {
    const wrap = qs('ops-disk');
    if (!wrap) return;
    try {
        const data = await api('/api/ops/disk');
        const disk = data || {};
        const candidates = (disk.candidates || []);
        wrap.innerHTML = `
            <strong>磁盘</strong><span class="muted">剩余 ${disk.disk_free_gb ?? '-'} GB</span>
            <span class="muted">可清理暂存固件 ${disk.candidate_size_gb ?? 0} GB（${disk.candidate_count ?? 0} 个）</span>
            <button type="button" data-click="runDiskCleanup" data-a0="false">清理临时文件</button>
            <button type="button" data-click="runDiskCleanup" data-a0="true">清理含暂存固件（管理员）</button>
            ${(disk.largest_dirs || []).slice(0, 3).map(item => `<span class="muted">data/${esc(item.path)}: ${item.size_gb} GB</span>`).join('')}
            ${candidates.length ? `<div class="history-help">最旧候选：${esc(candidates.slice(0, 3).map(item => item.path).join('，'))}</div>` : ''}`;
    } catch { /* disk preview is informational */ }
}

async function runDiskCleanup(includeStage) {
    const message = includeStage === 'true' || includeStage === true
        ? '将删除超过 7 天的暂存固件与所有过期临时文件（先预览确认无排队回放引用）。继续？'
        : '仅清理下载残片（.part）与过期上传临时目录，不碰暂存固件。继续？';
    if (!await opsConfirm('磁盘清理', message)) return;
    try {
        const data = await api('/api/ops/disk/cleanup', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                dry_run: false,
                include_stage: includeStage === 'true' || includeStage === true,
            }),
        });
        toast(`已清理 ${data.removed_files ?? 0} 个文件，释放 ${((data.freed_bytes || 0) / 1024 ** 3).toFixed(2)} GB`);
        await loadOpsDisk();
        await loadOpsStatus();
    } catch (error) { toast(error.message); }
}

function renderOpsStatus(data) {
    const dot = qs('ops-indicator');
    if (dot) {
        const paused = Boolean(data.advancement_paused);
        const alerts = Number(data.open_alerts || 0);
        dot.className = `worker-dot ${paused || alerts > 0 ? 'down' : 'up'}`;
        dot.textContent = paused ? '已暂停' : `运维${alerts > 0 ? `(${alerts})` : ''}`;
        dot.title = paused
            ? `自动化推进已暂停（${data.flag?.updated_by || '-'}，${data.flag?.updated_at || '-'}）`
            : `运维正常：Worker ${data.worker?.running ? '运行中' : '未运行'}，磁盘剩余 ${data.disk?.available_gb ?? '-'} GB，未确认告警 ${alerts} 条`;
    }
    const pane = qs('ops-status-strip');
    if (pane) {
        const paused = Boolean(data.advancement_paused);
        const worker = data.worker || {};
        const alive = worker.running
            && (worker.last_tick_seconds_ago === null
                || worker.last_tick_seconds_ago < (worker.interval_seconds || 15) * 4);
        pane.innerHTML = `
            <span class="badge ${paused ? 'failed' : 'completed'}">${paused ? '推进已暂停' : '推进正常'}</span>
            <span class="muted">Worker：${alive ? '运行中' : '未运行/停滞'}</span>
            <span class="muted">磁盘剩余：${data.disk?.available_gb ?? '-'} GB</span>
            <span class="muted">未确认告警：${Number(data.open_alerts || 0)} 条</span>
            ${paused && data.flag?.updated_at ? `<span class="muted">暂停于 ${esc(data.flag.updated_at)}（${esc(data.flag.updated_by || '-')}）</span>` : ''}`;
    }
    const button = qs('ops-kill-switch');
    if (button) {
        const paused = Boolean(data.advancement_paused);
        button.textContent = paused ? '恢复推进' : '暂停推进';
        button.classList.toggle('primary', !paused);
    }
}

async function toggleKillSwitch() {
    const current = Boolean(opsStatusCache?.advancement_paused);
    const target = !current;
    const message = target
        ? '暂停后：不再认领新的自动化运行，正在执行的步骤完成后停止；超时清理与告警照常运行。确定暂停推进？'
        : '恢复后：自动化 Worker 继续推进队列中的运行（含暂停期间排队的计划）。确定恢复推进？';
    if (!await opsConfirm('自动化停机开关', message)) return;
    try {
        await api('/api/ops/kill-switch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paused: target, reason: 'automation page kill-switch'}),
        });
        toast(target ? '已暂停自动化推进' : '已恢复自动化推进');
        await loadOpsStatus();
        if (activeWorkflowPane === 'ops') await loadOpsEvents();
    } catch (error) { toast(error.message); }
}

async function loadOpsEvents() {
    try {
        const data = await api('/api/ops/events?limit=100');
        opsEventsCache = data.items || [];
        renderOpsEvents();
    } catch { /* events refresh is background-only */ }
}

function renderOpsEvents() {
    const list = qs('ops-events');
    if (!list) return;
    list.innerHTML = opsEventsCache.length ? opsEventsCache.map(event => `
        <article class="run-card"><div class="run-main">
        <div class="run-title"><span class="badge ${event.severity === 'critical' ? 'failed' : (event.severity === 'warning' ? 'cancelled' : 'completed')}">${esc(event.severity)}</span><strong>${esc(event.kind)}</strong>${event.subject ? `<span class="test-outcome unknown">${esc(event.subject)}</span>` : ''}</div>
        <div class="run-value">${esc(event.message)}</div>
        <div class="muted">${esc(event.created_at)} · ${event.status === 'open' ? '未确认' : `已确认 ${esc(event.acknowledged_by || '')}`}</div>
        </div>
        <div class="run-actions">${event.status === 'open' ? `<button type="button" data-click="ackOpsEvent" data-a0="${esc(event.id)}">确认</button>` : ''}</div>
        </article>`).join('') : '<div class="empty-state">暂无运维事件</div>';
}

async function ackOpsEvent(eventId) {
    try {
        await api(`/api/ops/events/${encodeURIComponent(eventId)}/ack`, {method: 'POST'});
        await loadOpsEvents();
        await loadOpsStatus();
    } catch (error) { toast(error.message); }
}
