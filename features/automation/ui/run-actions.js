// Automation page run-actions chunk; loaded by page.html in dependency order.

async function pollGerrit() {
    try {
        const data = await api('/api/automation/gerrit/poll', {method: 'POST'});
        const rejected = data.rejected_count || 0;
        const suffix = rejected ? `，预检拒绝 ${rejected} 条：${data.rejected?.[0]?.error || '资源未就绪'}` : '';
        toast(`Gerrit poll 创建 ${data.created_count || 0} 条，已存在 ${data.existing_count || 0} 条${suffix}`);
        await loadRuns();
    } catch (err) { toast(err.message); }
}

async function tickWorker(executor) {
    try {
        const run = await api(`/api/automation/worker/tick?executor=${encodeURIComponent(executor)}`, {method: 'POST'});
        toast(run
            ? `推进到 ${statusLabel(run.status)}：${run.id}`
            : '没有可推进的运行');
        await loadRuns();
        if (run && run.id) await loadEvents(run.id);
    } catch (err) { toast(err.message); }
}

async function retryRun(runId) {
    if (atsRuns.find(run => run.id === runId)?.permissions?.can_manage !== true) {
        toast('仅运行创建者或管理员可操作');
        return;
    }
    try {
        const message = '将使用原配置重新执行整条流水线，包含已配置的构建、烧写和测试。新运行会保留来源记录。是否继续？';
        const confirmed = typeof window.parent.showConfirmDialog === 'function'
            ? await window.parent.showConfirmDialog('重跑流水线', message)
            : window.confirm(message);
        if (!confirmed) return;
        const run = await api(`/api/automation/runs/${encodeURIComponent(runId)}/retry`, {method: 'POST'});
        resetRunHistoryPage('runs');
        setStatusFilter('', {load: false});
        switchWorkflowPane('runs', {load: false});
        toast(`已创建流水线重跑 ${run.id}`);
        await loadRuns();
        await loadEvents(run.id);
    } catch (err) { toast(err.message); }
}

async function cancelRun(runId) {
    if (atsRuns.find(run => run.id === runId)?.permissions?.can_manage !== true) {
        toast('仅运行创建者或管理员可操作');
        return;
    }
    try {
        const run = await api(`/api/automation/runs/${encodeURIComponent(runId)}/cancel`, {method: 'POST'});
        toast(`已取消 ${run.id}`);
        await loadRuns();
        await loadEvents(run.id);
    } catch (err) { toast(err.message); }
}

async function releaseRunReservation(runId) {
    const run = atsRuns.find(item => item.id === runId);
    if (run?.permissions?.can_manage !== true || run.status !== 'completed') {
        toast('仅运行创建者或管理员可释放已完成运行的预约');
        return;
    }
    try {
        await api(`/api/automation/runs/${encodeURIComponent(runId)}/release-reservation`, {method: 'POST'});
        toast('设备预约已释放');
        await loadRuns();
        if (selectedRunId === runId) await loadEvents(runId);
    } catch (error) { toast(error.message); }
}
