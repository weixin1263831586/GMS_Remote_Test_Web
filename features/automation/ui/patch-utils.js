// Automation page patch-utils chunk; loaded by page.html in dependency order.

function syncAutomationWorkspaceSelection(extra = {}) {
    if (applyingWorkspaceContext) return;
    const workerId = selectedWorkerId();
    const checked = Array.from(document.querySelectorAll(
        '#automation-device-list input[type="checkbox"]:checked'
    )).map(input => input.value);
    const suitePath = qs('automation-test-suite')?.value || '';
    const suite = testSuites.find(item => (item.tools_path || item.full_path) === suitePath);
    window.GmsEmbeddedWorkspace?.update({
        worker_id: workerId,
        device_ids: [...new Set(checked)].map(value =>
            isLocalAutomationWorker(workerId) || value.startsWith(`${workerId}:`)
                ? value : `${workerId}:${value}`),
        suite_key: suite?.suite_key || suitePath,
        suite_path: suitePath,
        origin_page: 'automation',
        ...extra,
    });
}

async function applyAutomationWorkspaceContext(next, navigate = false) {
    atsWorkspaceContext = {...atsWorkspaceContext, ...(next || {})};
    applyingWorkspaceContext = true;
    try {
        const worker = atsWorkspaceContext.scope_mode === 'cluster'
            ? (atsWorkspaceContext.worker_id || atsLocalWorkerId) : atsLocalWorkerId;
        const workerSelect = qs('automation-worker');
        const workerOption = workerSelect
            ? Array.from(workerSelect.options).find(option => option.value === worker && !option.disabled)
            : null;
        if (workerSelect && workerOption) {
            const changed = workerSelect.value !== worker;
            workerSelect.value = worker;
            if (changed) {
                await loadTestSuitesForAutomation();
                await loadDevices(false);
            }
        }
        if (atsWorkspaceContext.suite_path) {
            setSelectValue('automation-test-suite', atsWorkspaceContext.suite_path);
        }
        const selected = new Set((atsWorkspaceContext.device_ids || []).map(workspaceDeviceSerial));
        document.querySelectorAll('#automation-device-list input[type="checkbox"]').forEach(input => {
            input.checked = !input.disabled && selected.has(workspaceDeviceSerial(input.value));
        });
        if (atsWorkspaceContext.gerrit_change_id) {
            if (qs('dryrun-change-id')) qs('dryrun-change-id').value = atsWorkspaceContext.gerrit_change_id;
            if (qs('dryrun-patchset')) qs('dryrun-patchset').value = atsWorkspaceContext.gerrit_patchset || '';
        }
        if (navigate && atsWorkspaceContext.automation_run_id) {
            await loadRuns();
            if (atsRuns.some(run => run.id === atsWorkspaceContext.automation_run_id)) {
                await loadEvents(atsWorkspaceContext.automation_run_id);
            }
        }
        updateStepIndicators();
    } finally {
        applyingWorkspaceContext = false;
    }
}

function selectedWorkerId() {
    return qs('automation-worker')?.value || atsLocalWorkerId;
}

async function loadClusterWorkers() {
    const select = qs('automation-worker');
    if (!select) return;
    try {
        const statusResponse = await fetch('/api/cluster/status', {cache: 'no-store'});
        const status = await statusResponse.json();
        atsLocalWorkerId = String(status.local_worker_id || atsLocalWorkerId);
        const contextWorker = atsWorkspaceContext.scope_mode === 'cluster'
            ? atsWorkspaceContext.worker_id : atsLocalWorkerId;
        const previous = contextWorker || select.value || atsLocalWorkerId;
        if (!statusResponse.ok || !status.enabled) {
            select.innerHTML = `<option value="${esc(atsLocalWorkerId)}">Controller / Local Worker（需启用集群 Agent）</option>`;
            select.disabled = true;
            select.title = 'GMS ATS 使用持久化任务队列，请先启用集群能力和 Worker Agent';
            return;
        }
        const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
        const payload = await response.json();
        const workers = payload.workers || [];
        const workerAvailability = worker => {
            if (worker.status === 'draining') return '停止派发';
            if (!['online', 'busy'].includes(worker.status)) return '离线';
            if (
                isLocalAutomationWorker(worker.id)
                && String(worker.agent_version || '').startsWith('controller-')
            ) return '未安装 ATS Agent';
            if (!isLocalAutomationWorker(worker.id) && !status.remote_dispatch_enabled) {
                return '未启用远程派发';
            }
            return '';
        };
        const labelForWorker = worker => {
            const name = String(worker.name || worker.id);
            const host = String(worker.address || worker.hostname || '');
            return host && !name.includes(host) ? `${name} / ${host}` : name;
        };
        const eligible = workers.filter(worker => !workerAvailability(worker));
        select.innerHTML = workers.map(worker => {
            const reason = workerAvailability(worker);
            const label = `${labelForWorker(worker)}${reason ? `（${reason}）` : ''}`;
            return `<option value="${esc(worker.id)}"${reason ? ' disabled' : ''}>${esc(label)}</option>`;
        }).join('');
        select.disabled = !eligible.length;
        const unavailable = workers.filter(workerAvailability).map(labelForWorker);
        select.title = eligible.length
            ? (unavailable.length ? `${unavailable.join('、')} 当前不可用` : '')
            : '没有安装持久化 Agent 的在线 Worker';
        const selected = eligible.find(worker => worker.id === previous) || eligible[0];
        if (selected) select.value = selected.id;
    } catch (_) {
        select.innerHTML = `<option value="${esc(atsLocalWorkerId)}">Controller / Local Worker</option>`;
        select.disabled = true;
    }
}

