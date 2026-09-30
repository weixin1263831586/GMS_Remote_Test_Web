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
