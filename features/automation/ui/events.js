// Automation page events chunk; loaded by page.html in dependency order.

async function loadEvents(runId) {
    selectedRunId = runId;
    const run = atsRuns.find(r => r.id === runId);
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
    const traceButton = qs('events-trace-button');
    if (traceButton) traceButton.disabled = false;
    qs('automation-runs').querySelectorAll('.run-card').forEach(el => el.classList.remove('active'));
    const card = qs('automation-runs').querySelector(`.run-card[data-click="loadEvents"][data-a0="${runId}"]`);
    if (card) card.classList.add('active');
    const [data, trace] = await Promise.all([
        api(`/api/automation/runs/${encodeURIComponent(runId)}/timeline`),
        api(`/api/automation/runs/${encodeURIComponent(runId)}/trace`).catch(() => null),
    ]);
    atsTimelineEvents = data.items || [];
    selectedRunTrace = trace;
    await renderRunLogLinks(trace);
    renderEventLog();
    switchWorkflowPane('events');
}

function eventLevelClass(event) {
    const level = String(event.level || 'info').toLowerCase();
    if (level.includes('error') || level.includes('fail')) return 'error';
    if (level.includes('warn')) return 'warning';
    return 'info';
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
