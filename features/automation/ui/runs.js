// Automation page runs chunk; loaded by page.html in dependency order.

async function loadRuns() {
    const effectiveStatus = activeWorkflowPane === 'reports' ? '' : atsStatus;
    const query = effectiveStatus ? `?status=${encodeURIComponent(effectiveStatus)}&limit=100` : '?limit=100';
    const data = await api('/api/automation/runs' + query);
    atsRuns = data.items || [];
    const list = qs('automation-runs');
    const reportList = qs('automation-runs-report');
    if (!atsRuns.length) {
        list.innerHTML = '<div class="muted">暂无运行记录。</div>';
        if (reportList) reportList.innerHTML = '<div class="empty-state">暂无已生成的测试报告。</div>';
        return;
    }
    const html = atsRuns.map(run => `
        <article class="run-card${run.id === selectedRunId ? ' active' : ''}" data-click="loadEvents" data-a0="${esc(run.id)}">
            <div class="run-main">
                <div class="run-title">
                    <span class="badge ${esc(run.status)}" title="${esc(run.status)}">${esc(statusLabel(run.status))}</span>
                    <strong>${esc(run.profile_id || 'manual')}</strong>
                    <span class="muted">${esc(run.source_type || 'manual')}</span>
                </div>
                ${runMeta(run) ? `<div class="muted">${esc(runMeta(run))}</div>` : ''}
                <div class="run-detail-grid">
                    <div>
                        <div class="field-label">固件</div>
                        <div class="run-value">${esc(run.artifact_path || run.artifact_url || '-')}</div>
                    </div>
                    <div>
                        <div class="field-label">设备</div>
                        <div class="run-value">${esc(formatDevices(run.devices_json))}</div>
                    </div>
                    <div>
                        <div class="field-label">更新时间</div>
                        <div class="run-value nowrap">${esc(compactTime(run.updated_at || run.created_at))}</div>
                    </div>
                    <div>
                        <div class="field-label">报告</div>
                        <div class="run-value">${run.report_timestamp ? `<button type="button" data-click="openRunAnalysis" data-r0="event" data-a1="${esc(run.id)}">分析报告</button>` : '<span class="muted">-</span>'}</div>
                    </div>
                    ${(FAILURE_STATUSES.has(run.status) && run.error) ? `
                    <div style="grid-column: 1 / -1">
                        <div class="field-label">错误原因</div>
                        <div class="run-value error">${esc(run.error)}</div>
                    </div>` : ''}
                </div>
                ${renderStageBar(run.status, run.current_stage)}
            </div>
            <div class="run-actions">
                <button type="button" data-click="loadEvents" data-a0="${esc(run.id)}" data-stop>日志</button>
                <button type="button" data-click="loadTrace" data-a0="${esc(run.id)}" data-stop>链路</button>
                ${TERMINAL_STATUSES.has(run.status)
                    ? `<button type="button" data-click="retryRun" data-a0="${esc(run.id)}" data-stop>重试</button>`
                    : run.status === 'flashing'
                    ? '<button type="button" class="danger" disabled title="刷机过程中断电或终止可能损坏设备">刷机中不可取消</button>'
                    : `<button type="button" class="danger" data-click="cancelRun" data-a0="${esc(run.id)}" data-stop>取消</button>`}
            </div>
        </article>
    `).join('');
    list.innerHTML = html;
    if (reportList) {
        const reportRuns = atsRuns.filter(run => run.report_timestamp);
        reportList.innerHTML = reportRuns.length
            ? reportRuns.map(run => `
                <article class="report-card">
                    <div class="report-card-main">
                        <div class="run-title">
                            <span class="badge ${esc(run.status)}" title="${esc(run.status)}">${esc(statusLabel(run.status))}</span>
                            <strong>${esc(run.profile_id || 'manual')}</strong>
                            <span class="muted">${esc(run.source_type || 'manual')}</span>
                        </div>
                        ${runMeta(run) ? `<div class="muted">${esc(runMeta(run))}</div>` : ''}
                        <div class="report-card-meta">
                            <span>报告：${esc(compactTime(run.report_timestamp))}</span>
                            <span>设备：${esc(formatDevices(run.devices_json))}</span>
                            <span>运行：${esc(run.id)}</span>
                        </div>
                    </div>
                    <div class="report-card-actions">
                        <button type="button" data-click="openRunReport" data-r0="event" data-a1="${esc(run.id)}">报告详情</button>
                        <button type="button" class="primary" data-click="openRunAnalysis" data-r0="event" data-a1="${esc(run.id)}">分析报告</button>
                    </div>
                </article>
            `).join('')
            : '<div class="empty-state">暂无已生成的测试报告。</div>';
    }
}
