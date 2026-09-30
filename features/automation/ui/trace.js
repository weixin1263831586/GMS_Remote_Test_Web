// Automation page trace chunk; loaded by page.html in dependency order.

function traceField(label, value) {
    if (value === undefined || value === null || value === '') return '';
    return `<div class="trace-key">${esc(label)}</div><div class="trace-val">${esc(value)}</div>`;
}

async function loadDashboard() {
    const data = await api('/api/automation/dashboard');
    const stats = qs('dashboard-stats');
    if (!stats) return;
    const byStatus = data.run_by_status || {};
    let inProgress = 0, failedTotal = 0;
    for (const [s, c] of Object.entries(byStatus)) {
        if (!TERMINAL_STATUSES.has(s)) inProgress += c;
        if (FAILURE_STATUSES.has(s)) failedTotal += c;
    }
    const card = (value, label, cls = '') => `<div class="stat-card ${cls}"><div class="stat-value">${esc(value)}</div><div class="stat-label">${esc(label)}</div></div>`;
    stats.innerHTML = [
        card(data.run_total || 0, '运行总数'),
        card(inProgress, '进行中', 'primary'),
        card(data.completed_total || 0, '已完成', 'ok'),
        card(failedTotal, '失败', failedTotal ? 'danger' : ''),
        card(data.build_total || 0, '构建任务'),
    ].join('');

    const statusEl = qs('dashboard-status-breakdown');
    if (statusEl) {
        const entries = Object.entries(byStatus).sort((a, b) => b[1] - a[1]);
        statusEl.innerHTML = entries.length
            ? entries.map(([s, c]) => `<div class="breakdown-row"><span class="badge ${esc(s)}" title="${esc(s)}">${esc(statusLabel(s))}</span><span class="breakdown-count">${esc(c)}</span></div>`).join('')
            : '<div class="muted">暂无运行。</div>';
    }

    const profileEl = qs('dashboard-profile-breakdown');
    if (profileEl) {
        const profiles = data.run_by_profile || {};
        const rows = Object.entries(profiles).map(([pid, counts]) => {
            const total = Object.values(counts).reduce((a, b) => a + b, 0);
            const done = (counts.completed || 0);
            const failed = Object.entries(counts).filter(([s]) => FAILURE_STATUSES.has(s)).reduce((s, [, c]) => s + c, 0);
            return `<div class="breakdown-row"><span><strong>${esc(pid)}</strong></span><span class="muted">${esc(done)}✓ / ${esc(failed)}✗ / ${esc(total)}</span></div>`;
        });
        profileEl.innerHTML = rows.length ? rows.join('') : '<div class="muted">暂无数据。</div>';
    }
}

async function loadTrace(runId) {
    try {
        const data = await api(`/api/automation/runs/${encodeURIComponent(runId)}/trace`);
        qs('trace-title').textContent = `运行链路 / ${data.run_id}`;
        const commit = data.commit || {};
        qs('trace-commit').innerHTML = [
            traceField('Change-Id', commit.gerrit_change_id),
            traceField('Patchset', commit.gerrit_patchset ? `PS${commit.gerrit_patchset}` : ''),
            traceField('分支', commit.branch),
            traceField('项目', data.profile_id),
            traceField('主题', commit.gerrit_subject),
        ].join('') || '<div class="muted">无 Gerrit 提交信息（手动运行）。</div>';

        const build = data.build_job;
        qs('trace-build').innerHTML = build ? [
            `<div class="trace-key">状态</div><div class="trace-val"><span class="badge ${esc(build.status)}" title="${esc(build.status)}">${esc(statusLabel(build.status))}</span></div>`,
            traceField('模板', build.template_id),
            traceField('工作目录', build.remote_workspace),
            traceField('产物', (build.artifacts || [])[0]?.path || ''),
            data.build_job_id ? `<div class="trace-key">任务</div><div class="trace-val"><a href="#" data-click="jumpToBuildLog" data-a0="${esc(data.build_job_id)}" data-prevent>${esc(data.build_job_id)}</a></div>` : '',
        ].join('') : '<div class="muted">无关联构建任务。</div>';

        qs('trace-artifact').innerHTML = [
            traceField('产物路径', data.artifact_path),
            traceField('产物 ID', data.build_artifact_id),
            traceField('Worker', data.worker_id),
            traceField('设备预约', data.device_reservation_id),
            traceField('烧写暂存', data.flash_stage_id),
            traceField('烧写命令', data.flash_command_id),
        ].join('') || '<div class="muted">（无固件产物）</div>';

        qs('trace-test').innerHTML = [
            traceField('Trace ID', data.trace_id),
            traceField('状态版本', data.state_version),
            traceField('恢复次数', data.recovery_count),
            traceField('Cluster Job', data.cluster_job_id),
            traceField('Attempt', data.attempt_id),
            traceField('任务状态', data.cluster_job?.status),
            traceField('Worker', data.cluster_job?.assigned_worker_id),
        ].join('') || '<div class="muted">尚未创建集群测试任务。</div>';

        const summary = data.result_summary || {};
        const reportRows = [
            traceField('报告时间', compactTime(data.report_timestamp)),
            traceField('报告 ID', data.report_id),
            ...Object.entries(summary).slice(0, 8).map(([k, v]) => traceField(k, typeof v === 'object' ? JSON.stringify(v) : v)),
        ].join('');
        qs('trace-report').innerHTML = reportRows
            + (data.report_timestamp ? `<div class="trace-link-row"><button type="button" data-click="closeTraceOpenReport" data-r0="event" data-a1="${esc(runId)}">报告详情</button> <button type="button" class="primary" data-click="closeTraceOpenAnalysis" data-r0="event" data-a1="${esc(runId)}">分析报告</button></div>` : '')
            || '<div class="muted">尚未生成报告。</div>';

        openTrace();
    } catch (err) { toast(err.message); }
}

function jumpToBuildLog(jobId) {
    closeTrace();
    switchWorkflowPane('build');
    loadBuildLog(jobId).catch(err => toast(err.message));
}

function openTrace() {
    window.atsTraceFocusOrigin = document.activeElement;
    const drawer = qs('ats-trace-drawer');
    drawer.classList.add('open');
    qs('ats-trace-backdrop').classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    syncAutomationOverlayState();
    drawer.focus({preventScroll: true});
}
function closeTrace() {
    qs('ats-trace-drawer').classList.remove('open');
    qs('ats-trace-backdrop').classList.remove('open');
    qs('ats-trace-drawer').setAttribute('aria-hidden', 'true');
    syncAutomationOverlayState();
    if (window.atsTraceFocusOrigin && window.atsTraceFocusOrigin.isConnected) {
        window.atsTraceFocusOrigin.focus({preventScroll: true});
    }
    window.atsTraceFocusOrigin = null;
}
