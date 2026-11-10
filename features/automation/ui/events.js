// Automation page events chunk; loaded by page.html in dependency order.

async function loadEvents(runId) {
    selectedRunId = runId;
    const run = atsRuns.find(r => r.id === runId)
        || await api(`/api/automation/runs/${encodeURIComponent(runId)}`);
    if (selectedRunId !== runId) return;
    window.GmsEmbeddedWorkspace?.update({
        automation_run_id: runId,
        worker_id: run ? (run.worker_id || selectedWorkerId()) : selectedWorkerId(),
        cluster_job_id: run?.cluster_job_id || '',
        attempt_id: run?.attempt_id || '',
        report_id: run?.report_id || '',
        report_timestamp: run?.report_timestamp || '',
        gerrit_change_id: run?.gerrit_change_id || '',
        gerrit_patchset: run?.gerrit_patchset || '',
        origin_page: 'automation'
    });
    qs('events-title').textContent = run ? `全链路时间线 / ${run.profile_id || run.id}` : '全链路时间线';
    renderEventSourceStrip(run);
    renderRelatedOpsAlerts(run);
    const traceButton = qs('events-trace-button');
    if (traceButton) traceButton.disabled = false;
    qs('automation-runs').querySelectorAll('.run-card').forEach(el => el.classList.remove('active'));
    const card = qs('automation-runs').querySelector(`.run-card[data-click="loadEvents"][data-a0="${runId}"]`);
    if (card) card.classList.add('active');
    const [data, trace] = await Promise.all([
        api(`/api/automation/runs/${encodeURIComponent(runId)}/timeline`),
        api(`/api/automation/runs/${encodeURIComponent(runId)}/trace`).catch(() => null),
    ]);
    if (selectedRunId !== runId) return;
    atsTimelineEvents = data.items || [];
    selectedRunTrace = trace;
    await renderRunLogLinks(trace);
    try {
        const progress = await api(`/api/automation/runs/${encodeURIComponent(runId)}/progress`);
        if (selectedRunId === runId) renderLiveProgress(progress);
    } catch (_) { qs('automation-live-progress').textContent = '模块进度暂不可用，可查看原始日志。'; }
    renderEventLog();
    switchWorkflowPane('events');
}

function eventLevelClass(event) {
    const level = String(event.level || 'info').toLowerCase();
    if (level.includes('error') || level.includes('fail')) return 'error';
    if (level.includes('warn')) return 'warning';
    return 'info';
}

function renderLiveProgress(progress) {
    const preparationNames = {stay_awake: '充电时常亮', animations_off: '关闭动画', wifi_on: '启用 Wi-Fi'};
    const preparations = (progress.preparations || []).map(item => `<div>${esc(preparationNames[item.action] || item.action)} · ${esc(item.device)} · ${esc(statusLabel(item.state))}</div>`).join('');
    const modules = (progress.modules || []).map(item => `<tr><td>${esc(item.module)}</td><td>${esc(item.abi || '-')}</td><td>${esc(statusLabel(item.state))}</td><td>${esc(item.passed ?? '-')}</td><td>${esc(item.failed ?? '-')}</td><td>${esc(item.observed_seconds ?? '-')} 秒</td><td>${esc(compactTime(item.last_reported_at))}</td></tr>`).join('');
    const download = renderArtifactDownload(progress.artifact_download);
    qs('automation-live-progress').innerHTML = download + preparations + (modules ? `<table><thead><tr><th>模块</th><th>ABI</th><th>状态</th><th>通过</th><th>失败</th><th>观测耗时</th><th>最近上报</th></tr></thead><tbody>${modules}</tbody></table>` : (download ? '' : '<div class="muted">尚未观测到模块事件；不据此判断测试通过或失败。</div>'));
}

function eventLogLine(event) {
    const stage = event.stage || event.to_state || event.event_type || '-';
    const detail = [
        event.from_state && event.to_state ? `${event.from_state} -> ${event.to_state}` : '',
        event.operation_id ? `op=${event.operation_id}` : '',
    ].filter(Boolean).join(' ');
    return `${compactTime(event.created_at)} ${eventLevelClass(event).toUpperCase().padEnd(5)} ${String(event.domain || 'automation').padEnd(10)} ${String(stage).padEnd(20)} ${event.message || ''}${detail ? ` | ${detail}` : ''}`;
}

function filteredTimelineEvents() {
    const query = String(qs('events-search')?.value || '').trim().toLowerCase();
    const level = qs('events-level')?.value || '';
    return atsTimelineEvents.filter(event => {
        if (level && eventLevelClass(event) !== level) return false;
        return !query || eventLogLine(event).toLowerCase().includes(query);
    });
}

function renderEventLog() {
    const target = qs('automation-events');
    if (!target) return;
    const events = filteredTimelineEvents();
    const autoFollow = qs('events-auto-follow')?.checked !== false;
    const wasNearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 48;
    const previousTop = target.scrollTop;
    target.classList.remove('muted');
    target.innerHTML = events.length ? events.map(event => {
        const level = eventLevelClass(event);
        const stage = event.stage || event.to_state || event.event_type || '-';
        const detail = [
            event.from_state && event.to_state ? `${event.from_state} → ${event.to_state}` : '',
            event.operation_id ? `op=${event.operation_id}` : '',
        ].filter(Boolean).join(' · ');
        return `<div class="event-line">
            <span class="event-line-time">${esc(compactTime(event.created_at))}</span>
            <span class="event-line-level ${level}">${esc(level === 'warning' ? 'WARN' : level.toUpperCase())}</span>
            <span class="event-line-domain">${esc(event.domain || 'automation')}</span>
            <span class="event-line-stage">${esc(stage)}</span>
            <span class="event-line-message">${esc(event.message || '-')}${detail ? ` <span class="event-line-detail">| ${esc(detail)}</span>` : ''}</span>
        </div>`;
    }).join('') : '<div class="event-log-empty">没有匹配当前筛选条件的日志。</div>';
    if (autoFollow && (wasNearBottom || !target.dataset.loaded)) {
        requestAnimationFrame(() => { target.scrollTop = target.scrollHeight; });
    } else {
        target.scrollTop = previousTop;
    }
    target.dataset.loaded = '1';
}

async function renderRunLogLinks(trace) {
    const target = qs('run-log-links');
    if (!target) return;
    const links = [];
    if (trace?.build_job_id) {
        links.push(`<button type="button" data-click="jumpToBuildLog" data-a0="${esc(trace.build_job_id)}">构建原始日志</button>`);
    }
    if (trace?.cluster_job_id) {
        try {
            const response = await fetch(`/api/cluster/jobs/${encodeURIComponent(trace.cluster_job_id)}/artifacts`, {cache: 'no-store'});
            const payload = await response.json();
            if (response.ok && payload.success !== false) {
                (payload.artifacts || []).filter(item => ['stdout.log', 'stderr.log'].includes(item.filename)).forEach(item => {
                    const url = `/api/cluster/jobs/${encodeURIComponent(trace.cluster_job_id)}/artifacts/${encodeURIComponent(item.id)}/download`;
                    links.push(`<a class="log-artifact-link" href="${url}">${esc(item.filename)}</a>`);
                });
            }
        } catch (_) { /* 日志产物入口是增强信息，不阻断时间线 */ }
    }
    target.innerHTML = links.length ? links.join('') : '时间 · 级别 · 来源 · 阶段 · 消息';
}

function copyEventLog() {
    copyText(filteredTimelineEvents().map(eventLogLine).join('\n'), '运行日志已复制')
        .catch(error => toast(error.message));
}

function downloadEventLog() {
    const content = filteredTimelineEvents().map(eventLogLine).join('\n');
    if (!content) { toast('当前没有可下载的日志'); return; }
    downloadText(`${selectedRunId || 'ats-run'}.log`, content);
}

async function refreshSelectedEvents() {
    if (!selectedRunId) { toast('请先选择一条运行。'); return; }
    await loadEvents(selectedRunId);
}

// ==================== Run 详情增强：来源链 + 关联运维告警 ====================
// 目标：同一个任务的对话来源、执行时间线与平台级受阻根因在同一视图闭环，
// 不再要求用户在「运行监控 / 事件诊断 / 运维」三个页签间来回拼信息。

function renderEventSourceStrip(run) {
    const strip = qs('events-source-strip');
    if (!strip) return;
    if (!run) {
        strip.hidden = true;
        strip.innerHTML = '';
        return;
    }
    const chips = [];
    if (run.source_type === 'assistant_chat') {
        chips.push('<span class="test-outcome unknown">来自对话 Agent</span>');
    } else if (run.source_type === 'auto_retry') {
        chips.push('<span class="test-outcome unknown">失败用例自动重试</span>');
    }
    const buttons = [];
    if (run.source_type === 'assistant_chat') {
        buttons.push('<button type="button" class="btn-xs" data-click="jumpToAgentFromEvents">回到对话 Agent ↗</button>');
    }
    if (run.report_timestamp) {
        buttons.push(`<button type="button" class="btn-xs" data-click="openRunAnalysis" data-r0="event" data-a1="${esc(run.id)}">分析报告</button>`);
    }
    if (!chips.length && !buttons.length) {
        strip.hidden = true;
        return;
    }
    strip.hidden = false;
    strip.innerHTML = `
        <div class="events-heading">
            <span>来源${chips.length ? ' ' + chips.join(' ') : ''}</span>
            <span class="log-actions log-toolbar-actions">${buttons.join('')}</span>
        </div>`;
}

// 平台受阻提示只针对非终态：终态（含 failed）是运行自身的结果，与
// Worker 停转 / kill-switch 无关，不查 ops status。
const RELATED_ALERT_SKIP_STATUSES = Object.freeze(['completed', 'cancelled', 'failed']);

async function renderRelatedOpsAlerts(run) {
    const host = qs('events-related-alerts');
    if (!host) return;
    host.hidden = true;
    host.innerHTML = '';
    const runId = String(run?.id || '');
    const status = String(run?.status || '');
    if (!runId || RELATED_ALERT_SKIP_STATUSES.includes(status)) return;
    let data = null;
    try {
        data = await api('/api/ops/status');
    } catch (_) { return; }
    // ops status 是异步查询：期间用户可能已切换到另一条 run，过期结果不落视图。
    if (selectedRunId !== runId) return;
    const paused = Boolean(data?.advancement_paused);
    const workerDown = Boolean(data?.worker && !data.worker.running);
    if (!paused && !workerDown) return;
    if (!['queued', 'waiting_device'].includes(String(run.current_stage || status))) return;
    const alertCount = Number(data?.open_alerts || 0);
    host.hidden = false;
    host.innerHTML = `
        <div class="events-heading">
            <span>⚠ 该运行正在排队/等待设备，但 ${esc(workerDown ? '自动化 Worker 未运行' : '推进已被暂停')}——队列不会推进</span>
            <span class="log-actions log-toolbar-actions">
                <button type="button" class="btn-xs" data-click="switchWorkflowPane" data-a0="ops">前往运维${alertCount ? `（${alertCount} 条告警）` : ''}</button>
            </span>
        </div>`;
}

function jumpToAgentFromEvents() {
    // 经父 shell 导航回对话 Agent 页；workspace 上下文已由 loadEvents 写入
    // automation_run_id，Agent 页可继续跟踪同一任务的会话。
    window.GmsEmbeddedWorkspace?.navigate('agent', {origin_page: 'automation'});
}
