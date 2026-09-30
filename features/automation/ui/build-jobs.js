// Automation page build-jobs chunk; loaded by page.html in dependency order.

async function loadBuildJobs() {
    const data = await api('/api/build/jobs?limit=20');
    buildJobs = data.items || [];
    qs('build-jobs').innerHTML = buildJobs.length
        ? buildJobs.map(job => {
            const terminal = TERMINAL_STATUSES.has(job.status);
            return `<div class="build-job ${job.id === selectedBuildJobId ? 'active' : ''}" data-click="loadBuildLog" data-a0="${esc(job.id)}">
                <div class="build-job-head"><span class="badge ${esc(job.status)}" title="${esc(job.status)}">${esc(statusLabel(job.status))}</span><strong>${esc(job.template_id)}</strong>
                ${terminal ? `<button type="button" class="build-job-delete" title="删除历史任务" data-click="deleteBuildJob" data-a0="${esc(job.id)}" data-stop>删除</button>` : ''}</div>
                <div class="muted">${esc(job.id)} / ${esc(job.remote_workspace || '')}</div>
                <div class="build-job-source">${job.automation_run_id ? `ATS ${esc(job.automation_run_id)}` : '独立调试构建'}</div>
                <div>${esc((job.artifacts || [])[0]?.path || job.error || '')}</div></div>`;
        }).join('')
        : '<div class="muted">暂无构建任务。</div>';
}

async function deleteBuildJob(jobId) {
    const confirmed = typeof window.parent?.showConfirmDialog === 'function'
        ? await window.parent.showConfirmDialog(
            '删除构建任务',
            `确定删除历史构建任务 ${jobId}？\n此操作只删除平台记录，不删除远端源码和构建产物。`
        )
        : window.confirm(`确定删除历史构建任务 ${jobId}？`);
    if (!confirmed) return;
    try {
        await api(`/api/build/jobs/${encodeURIComponent(jobId)}`, {method: 'DELETE'});
        if (selectedBuildJobId === jobId) {
            selectedBuildJobId = '';
            buildLogRaw = '';
            qs('build-log-title').textContent = '未选择任务';
            qs('build-log').textContent = '选择构建任务查看日志。';
        }
        toast(`已删除历史构建任务 ${jobId}`);
        await loadBuildJobs();
    } catch (err) { toast(err.message); }
}

async function compileAndJump() {
    try {
        const build = collectBuildPlan({forceBuild: true});
        if (!build) throw new Error('请填写编译服务器、模板、源码目录和 lunch target');
        const serverPassword = await getBuildPassword(build.server_id);
        const job = await api('/api/build/jobs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                server_id: build.server_id,
                template_id: build.template_id,
                server_password: serverPassword,
                parameters: build.parameters,
                source_type: 'manual-ui',
            }),
        });
        toast(`已创建独立构建任务 ${job.id}；该任务不会自动进入烧写和测试`);
        switchWorkflowPane('build');
        await loadBuildJobs();
        await loadBuildLog(job.id);
    } catch (err) { toast(err.message); }
}

async function loadBuildLog(jobId, {silent = false} = {}) {
    try {
        selectedBuildJobId = jobId;
        let job = await api(`/api/build/jobs/${encodeURIComponent(jobId)}`);
        if (job.server_id && !buildPasswordCache[job.server_id] && ['queued', 'running'].includes(job.status)) {
            const password = await getBuildPassword(job.server_id);
            if (password) {
                await api(`/api/build/jobs/${encodeURIComponent(jobId)}/password`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({server_password: password}),
                });
            }
        }
        job = await api(`/api/build/jobs/${encodeURIComponent(jobId)}?poll=true`);
        const log = await api(`/api/build/jobs/${encodeURIComponent(jobId)}/log?lines=5000`);
        buildLogRaw = log.text || '';
        renderBuildLog();
        qs('build-log-title').textContent = `${job.id} / ${statusLabel(job.status)}`;
        if (!silent) toast(`构建任务 ${job.id}：${statusLabel(job.status)}`);
        await loadBuildJobs();
    } catch (err) { toast(err.message); }
}

function filteredBuildLog() {
    const query = String(qs('build-log-search')?.value || '').trim().toLowerCase();
    if (!query) return buildLogRaw;
    return buildLogRaw.split('\n').filter(line => line.toLowerCase().includes(query)).join('\n');
}

function renderBuildLog() {
    const logEl = qs('build-log');
    if (!logEl) return;
    const autoFollow = qs('build-log-auto-follow')?.checked !== false;
    const wasNearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 48;
    const previousTop = logEl.scrollTop;
    logEl.textContent = filteredBuildLog() || (buildLogRaw ? '没有匹配的日志。' : '暂无日志。');
    if (autoFollow && (wasNearBottom || !logEl.dataset.loaded)) {
        requestAnimationFrame(() => { logEl.scrollTop = logEl.scrollHeight; });
    } else {
        logEl.scrollTop = previousTop;
    }
    logEl.dataset.loaded = '1';
}

function copyBuildLog() {
    copyText(filteredBuildLog(), '构建日志已复制').catch(error => toast(error.message));
}

function downloadBuildLog() {
    const content = filteredBuildLog();
    if (!content) { toast('当前没有可下载的日志'); return; }
    downloadText(`${selectedBuildJobId || 'build'}.log`, content);
}

function refreshSelectedBuildLog() {
    if (!selectedBuildJobId) {
        toast('请先选择一个构建任务');
        return;
    }
    loadBuildLog(selectedBuildJobId).catch(err => toast(err.message));
}
