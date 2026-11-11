// Automation page runs chunk; loaded by page.html in dependency order.

async function loadRuns() {
    const view = activeWorkflowPane === 'reports' ? 'reports' : 'runs';
    const state = atsHistoryViews[view];
    const query = new URLSearchParams({limit: state.limit, cursor: state.cursors[state.index] || ''});
    for (const key of ['query', 'profile_id', 'worker_id', 'outcome']) {
        if (state[key]) query.set(key, state[key]);
    }
    if (view === 'reports') query.set('reports_only', 'true');
    else if (atsStatus) query.set('status', atsStatus);
    const request = ++atsHistoryRequest;
    const data = await api('/api/automation/runs?' + query);
    if (request !== atsHistoryRequest) return;
    atsRuns = data.items || [];
    state.hasMore = Boolean(data.has_more && data.next_cursor);
    state.nextCursor = data.next_cursor || '';
    renderRunHistoryPager(view);
    renderBlockedRunsBanner();
    const list = qs('automation-runs');
    const reportList = qs('automation-runs-report');
    if (!atsRuns.length) {
        list.innerHTML = '<div class="empty-state">没有匹配的运行记录，可调整筛选条件。</div>';
        if (reportList) reportList.innerHTML = '<div class="empty-state">没有匹配的测试报告，可调整筛选条件。</div>';
        return;
    }
    const html = atsRuns.map(run => `
        <article class="run-card${run.id === selectedRunId ? ' active' : ''}" data-click="loadEvents" data-a0="${esc(run.id)}">
            <div class="run-main">
                <div class="run-title">
                    <span class="badge ${esc(run.status)}" title="${esc(run.status)}">${esc(statusLabel(run.status))}</span>
                    <strong>${esc(run.profile_id || 'manual')}</strong>
                    <span class="muted">${esc(run.source_type || 'manual')}</span>
                    ${run.source_type === 'assistant_chat' ? `<button type="button" class="btn-xs" data-click="jumpToAgentConversation" data-a0="${esc(run.id)}" data-stop title="回到发起这条运行的对话 Agent 会话">来自对话 Agent ↗</button>` : ''}
                    ${renderTestOutcome(run)}
                </div>
                ${runMeta(run) ? `<div class="muted">${esc(runMeta(run))}</div>` : ''}
                <div class="muted run-identifiers">运行 ${esc(run.id)} · Worker ${esc(run.worker_id || '-')} · 创建者 ${esc(run.created_by_username || run.created_by || '-')}</div>
                ${run.plan_batch_id ? `<div class="muted">计划批次 ${esc(run.plan_batch_id)}</div>` : ''}
                ${run.source_type === 'auto_retry' ? `<div class="muted">失败用例自动重试 ${esc(run.retry_index)} / ${esc(run.retry_limit)}${run.not_before ? ` · 最早执行 ${esc(compactTime(run.not_before))} UTC` : ''}</div>` : ''}
                ${run.retry_of_run_id ? `<div class="run-retry-source">重跑来源：<button type="button" data-click="loadEvents" data-a0="${esc(run.retry_of_run_id)}" data-stop>${esc(run.retry_of_run_id)}</button></div>` : ''}
                <div class="run-detail-grid">
                    <div>
                        <div class="field-label">测试配置</div>
                        <div class="run-value">${esc([run.test_type, run.test_summary?.suite_version, run.test_module].filter(Boolean).join(' · ') || '-')}</div>
                        <div class="muted run-value">${esc(run.test_suite || '')}</div>
                    </div>
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
                ${run.status === 'artifact_fetching' ? renderArtifactDownload(run.artifact_download) : ''}
            </div>
            <div class="run-actions">
                <button type="button" data-click="loadEvents" data-a0="${esc(run.id)}" data-stop>日志</button>
                <button type="button" data-click="loadTrace" data-a0="${esc(run.id)}" data-stop>链路</button>
                ${run.permissions?.can_manage !== true
                    ? '<span class="muted" title="仅运行创建者或管理员可操作">只读</span>'
                    : TERMINAL_STATUSES.has(run.status)
                    ? `<button type="button" data-click="retryRun" data-a0="${esc(run.id)}" data-stop title="使用原配置重新执行整条流水线，包含已配置的构建和烧写">重跑流水线</button>
                       ${run.status === 'completed' && run.device_reservation_id && run.resource_cleanup_state !== 'released'
                           ? `<button type="button" data-click="releaseRunReservation" data-a0="${esc(run.id)}" data-stop title="核对预约归属与设备命令结束状态后，释放这笔已完成运行的遗留预约">释放预约</button>` : ''}`
                    : ['flashing', 'aoa_recovering'].includes(run.status)
                    ? '<button type="button" class="danger" disabled title="设备操作完成前不能安全中止">设备操作中不可取消</button>'
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
                            ${renderTestOutcome(run)}
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

function renderArtifactDownload(download) {
    const total = Number(download?.total_bytes);
    const done = Number(download?.downloaded_bytes);
    const speed = Number(download?.bytes_per_second);
    if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(done) || done < 0 || done > total) return '';
    const percent = (done * 100 / total).toFixed(1);
    const rate = Number.isFinite(speed) && speed > 0 ? ` · ${resourceBytes(speed)}/s` : '';
    return `<div class="artifact-download">固件下载 ${percent}% · ${resourceBytes(done)} / ${resourceBytes(total)}${rate}<br><progress max="100" value="${percent}" aria-label="固件下载进度"></progress></div>`;
}

function renderTestOutcome(run) {
    const summary = run.test_summary || {};
    const labels = {passed: '用例通过', failed: '用例失败', incomplete: '统计不完整'};
    const outcome = Object.hasOwn(labels, summary.outcome) ? summary.outcome : 'unknown';
    const label = labels[outcome] || (TERMINAL_STATUSES.has(run.status) || run.report_timestamp ? '结果待确认' : '等待结果');
    const counts = Number.isInteger(summary.failed) && Number.isInteger(summary.total)
        ? ` · 失败 ${summary.failed} / ${summary.total}` : '';
    return `<span class="test-outcome ${outcome}" title="执行状态与用例结果分别统计；计数来自归档报告">${esc(label + counts)}</span>`;
}

function resetRunHistoryPage(view) {
    Object.assign(atsHistoryViews[view], {cursors: [''], index: 0, nextCursor: '', hasMore: false});
}

function restoreRunHistoryControls() {
    for (const view of ['runs', 'reports']) {
        const state = atsHistoryViews[view];
        for (const key of ['query', 'profile_id', 'worker_id', 'outcome', 'limit']) {
            const input = qs(`history-${key}-${view}`);
            if (input) input.value = state[key];
        }
        renderRunHistoryPager(view);
    }
}

async function applyRunHistoryFilters(view) {
    const state = atsHistoryViews[view];
    for (const key of ['query', 'profile_id', 'worker_id', 'outcome']) {
        state[key] = String(qs(`history-${key}-${view}`)?.value || '').trim();
    }
    state.limit = Number(qs(`history-limit-${view}`).value);
    resetRunHistoryPage(view);
    try { window.sessionStorage.setItem(`gms_automation_history_${view}`, JSON.stringify(state)); } catch (_) {}
    try { await loadRuns(); } catch (error) { toast(error.message); }
}

async function clearRunHistoryFilters(view) {
    const state = atsHistoryViews[view];
    Object.assign(state, {query: '', profile_id: '', worker_id: '', outcome: ''});
    resetRunHistoryPage(view);
    restoreRunHistoryControls();
    await applyRunHistoryFilters(view);
}

function renderRunHistoryPager(view) {
    const state = atsHistoryViews[view];
    const previous = qs(`history-previous-${view}`);
    const next = qs(`history-next-${view}`);
    const first = qs(`history-first-${view}`);
    if (first) first.disabled = state.index === 0;
    if (previous) previous.disabled = state.index === 0;
    if (next) next.disabled = !state.hasMore;
    const label = qs(`history-page-${view}`);
    if (label) label.textContent = `第 ${state.index + 1} 页 · 每页 ${state.limit} 条`;
}

async function changeRunHistoryPage(view, direction) {
    const state = atsHistoryViews[view];
    if ((direction > 0 && !state.hasMore) || (direction < 0 && !state.index)) return;
    const previousIndex = state.index;
    if (direction > 0) state.cursors[state.index + 1] = state.nextCursor;
    state.index = direction === 0 ? 0 : state.index + direction;
    qs(`history-first-${view}`).disabled = true;
    qs(`history-next-${view}`).disabled = true;
    qs(`history-previous-${view}`).disabled = true;
    try { await loadRuns(); } catch (error) { state.index = previousIndex; toast(error.message); }
    renderRunHistoryPager(view);
}

// 反向链：从 ATS 运行卡片跳回发起该运行的对话 Agent 会话页。卡片点击
// 不经过 loadEvents，先写入目标 run 的上下文再导航，避免 Agent 页续接
// 上一次查看的 run（act-bridge 只把 data-a0 的 run id 传进来）。
function jumpToAgentConversation(runId) {
    const target = String(runId || '').trim();
    if (!target) return;
    const run = atsRuns.find(item => item.id === target);
    window.GmsEmbeddedWorkspace?.update({
        automation_run_id: target,
        worker_id: run?.worker_id || selectedWorkerId(),
        cluster_job_id: run?.cluster_job_id || '',
        attempt_id: run?.attempt_id || '',
        report_id: run?.report_id || '',
        report_timestamp: run?.report_timestamp || '',
        gerrit_change_id: run?.gerrit_change_id || '',
        gerrit_patchset: run?.gerrit_patchset || '',
    });
    window.GmsEmbeddedWorkspace?.navigate('agent', {origin_page: 'automation'});
}
