// Tradefed 测试结果。
let _reportCopyWorkers = [];
const _reportCopySuites = new Map();
let _reportCopyPreferredReport = '';
let _reportCopyRunning = false;

function setReportCopyStatus(message, kind = 'info') {
    const status = $('report-copy-status');
    if (!status) return;
    status.style.display = message ? 'block' : 'none';
    status.style.color = kind === 'error'
        ? 'var(--danger-color, #e53935)'
        : kind === 'success'
            ? 'var(--success-color, #43a047)'
            : 'var(--text-secondary)';
    status.textContent = message || '';
}

function currentSuiteReportName() {
    const candidates = [state.suiteBrowser.highlightPath, state.suiteBrowser.currentPath];
    for (const candidate of candidates) {
        const parts = String(candidate || '').split('/').filter(Boolean);
        if (parts[0]?.toLowerCase() === 'results' && parts[1]) {
            const name = tradefedResultFolderName(parts[1]);
            if (name) return name;
        }
    }
    return '';
}

function reportCopyWorkerLabel(worker) {
    const address = worker.address || worker.hostname || '';
    return address && !String(worker.id).includes(address)
        ? `${worker.id} (${address})`
        : worker.id;
}

function fillReportCopyWorkerSelect(select, selectedId = '') {
    if (!select) return;
    select.innerHTML = '';
    _reportCopyWorkers.forEach(worker => {
        const option = new Option(reportCopyWorkerLabel(worker), worker.id);
        select.add(option);
    });
    if (_reportCopyWorkers.some(worker => worker.id === selectedId)) {
        select.value = selectedId;
    }
}

function reportCopySuiteLabel(suite) {
    const pathParts = String(suite?.tools_path || '')
        .replace(/\\/g, '/')
        .split('/')
        .filter(Boolean);
    const releaseDirectory = pathParts.find(part =>
        /^android-(?:cts|gts|vts|sts)(?:[-_].+)$/i.test(part)
    );
    return releaseDirectory
        || suite?.version
        || suite?.suite_version
        || suite?.suite_key
        || suite?.tools_path
        || '-';
}

async function loadReportCopySuites(workerId, selectId, preferredPath = '') {
    const select = $(selectId);
    if (!select) return [];
    select.disabled = true;
    select.innerHTML = '<option value="">正在加载套件...</option>';
    try {
        let suites = _reportCopySuites.get(workerId);
        if (!suites) {
            const payload = await apiCall(`/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`);
            suites = (payload.suites || []).filter(suite => suite.available);
            _reportCopySuites.set(workerId, suites);
        }
        select.innerHTML = '';
        suites.forEach(suite => {
            const label = reportCopySuiteLabel(suite);
            const option = new Option(label, suite.tools_path);
            option.title = suite.tools_path;
            select.add(option);
        });
        if (!suites.length) {
            select.add(new Option('此主机暂无可用套件', ''));
        } else if (suites.some(suite => suite.tools_path === preferredPath)) {
            select.value = preferredPath;
        }
        select.disabled = false;
        return suites;
    } catch (error) {
        select.innerHTML = '<option value="">套件加载失败</option>';
        select.disabled = false;
        throw error;
    }
}

function selectedReportCopySuite(role) {
    const workerId = $(`report-copy-${role}-worker`)?.value || '';
    const suitePath = $(`report-copy-${role}-suite`)?.value || '';
    return (_reportCopySuites.get(workerId) || []).find(
        suite => suite.tools_path === suitePath
    ) || null;
}

function matchingTargetSuitePath(targetSuites) {
    const source = selectedReportCopySuite('source');
    if (!source) return '';
    const sourceLabel = reportCopySuiteLabel(source).toLocaleLowerCase();
    const releaseMatch = targetSuites.find(
        suite => reportCopySuiteLabel(suite).toLocaleLowerCase() === sourceLabel
    );
    if (releaseMatch) return releaseMatch.tools_path || '';
    const match = targetSuites.find(suite =>
        (source.suite_key && suite.suite_key === source.suite_key)
        || (
            String(suite.test_type || '').toLowerCase() === String(source.test_type || '').toLowerCase()
            && String(suite.suite_version || suite.version || '')
                === String(source.suite_version || source.version || '')
        )
    );
    return match?.tools_path || '';
}

async function loadReportCopySourceReports() {
    const workerId = $('report-copy-source-worker')?.value || '';
    const suitePath = $('report-copy-source-suite')?.value || '';
    const select = $('report-copy-source-report');
    if (!select) return;
    select.disabled = true;
    select.innerHTML = '<option value="">正在加载报告...</option>';
    if (!workerId || !suitePath) {
        select.innerHTML = '<option value="">请先选择来源套件</option>';
        updateReportCopySubmitState();
        return;
    }
    try {
        const params = new URLSearchParams({worker_id: workerId, suite_path: suitePath, path: 'results'});
        const payload = await apiCall(`/api/cluster/suites/files?${params.toString()}`);
        const reports = (payload.data?.items || [])
            .filter(item => item.type === 'directory' && tradefedResultFolderName(item.name))
            .sort((left, right) => left.name.localeCompare(right.name));
        select.innerHTML = '';
        reports.forEach(report => select.add(new Option(report.name, report.name)));
        if (!reports.length) {
            select.add(new Option('此套件暂无 results 报告', ''));
        } else if (reports.some(report => report.name === _reportCopyPreferredReport)) {
            select.value = _reportCopyPreferredReport;
        }
        _reportCopyPreferredReport = '';
        select.disabled = false;
    } catch (error) {
        select.innerHTML = '<option value="">results 目录不可用</option>';
        select.disabled = false;
        setReportCopyStatus(`来源报告加载失败：${error.message}`, 'error');
    }
    updateReportCopySubmitState();
}

function updateReportCopySubmitState() {
    const submit = $('report-copy-submit');
    if (!submit) return;
    const sourceWorker = $('report-copy-source-worker')?.value || '';
    const targetWorker = $('report-copy-target-worker')?.value || '';
    submit.disabled = _reportCopyRunning
        || !sourceWorker
        || !targetWorker
        || sourceWorker === targetWorker
        || !$('report-copy-source-suite')?.value
        || !$('report-copy-source-report')?.value
        || !$('report-copy-target-suite')?.value;
}

window.updateReportCopyTargetPath = function updateReportCopyTargetPath() {
    const suitePath = $('report-copy-target-suite')?.value || '';
    const path = $('report-copy-target-path');
    if (path) path.value = suitePath ? `${getSuiteRootFromToolsPath(suitePath)}/results` : '';
    updateReportCopySubmitState();
};

window.onReportCopySourceWorkerChange = async function onReportCopySourceWorkerChange() {
    const workerId = $('report-copy-source-worker')?.value || '';
    const currentWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    const preferred = workerId === currentWorkerId ? state.suiteBrowser.selectedSuitePath : '';
    try {
        await loadReportCopySuites(workerId, 'report-copy-source-suite', preferred);
        await window.onReportCopySourceSuiteChange();
    } catch (error) {
        setReportCopyStatus(`来源套件加载失败：${error.message}`, 'error');
        updateReportCopySubmitState();
    }
};

window.onReportCopySourceSuiteChange = async function onReportCopySourceSuiteChange() {
    await loadReportCopySourceReports();
    await window.onReportCopyTargetWorkerChange();
};

window.onReportCopyTargetWorkerChange = async function onReportCopyTargetWorkerChange() {
    const workerId = $('report-copy-target-worker')?.value || '';
    if (!workerId) {
        window.updateReportCopyTargetPath();
        return;
    }
    try {
        const suites = await loadReportCopySuites(workerId, 'report-copy-target-suite');
        const preferred = matchingTargetSuitePath(suites);
        const select = $('report-copy-target-suite');
        if (select && preferred) select.value = preferred;
        window.updateReportCopyTargetPath();
    } catch (error) {
        setReportCopyStatus(`目标套件加载失败：${error.message}`, 'error');
        updateReportCopySubmitState();
    }
};

window.openReportCopyModal = async function openReportCopyModal() {
    ModalManager.open('report-copy-modal');
    setReportCopyStatus('正在加载主机和套件...', 'info');
    _reportCopySuites.clear();
    _reportCopyPreferredReport = currentSuiteReportName();
    const submit = $('report-copy-submit');
    if (submit) {
        submit.textContent = '开始拷贝';
        submit.disabled = true;
    }
    try {
        const payload = await apiCall('/api/cluster/workers');
        _reportCopyWorkers = (payload.workers || []).filter(
            worker => ['online', 'busy'].includes(worker.status)
        );
        if (_reportCopyWorkers.length < 2) {
            throw new Error('至少需要两台在线 Worker 才能跨主机拷贝');
        }
        const currentWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const sourceWorkerId = _reportCopyWorkers.some(worker => worker.id === currentWorkerId)
            ? currentWorkerId
            : _reportCopyWorkers[0].id;
        const targetWorkerId = _reportCopyWorkers.find(worker => worker.id !== sourceWorkerId)?.id || '';
        fillReportCopyWorkerSelect($('report-copy-source-worker'), sourceWorkerId);
        fillReportCopyWorkerSelect($('report-copy-target-worker'), targetWorkerId);
        await window.onReportCopySourceWorkerChange();
        if (
            $('report-copy-source-report')?.value
            && $('report-copy-target-suite')?.value
        ) {
            setReportCopyStatus('', 'info');
        }
    } catch (error) {
        setReportCopyStatus(error.message, 'error');
        updateReportCopySubmitState();
    }
};

window.closeReportCopyModal = function closeReportCopyModal() {
    ModalManager.close('report-copy-modal');
};

async function pollReportCopyTransfer(transferId) {
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const payload = await apiCall(`/api/cluster/transfers/${encodeURIComponent(transferId)}`);
        const transfer = payload.transfer || {};
        if (transfer.status === 'completed') return transfer;
        if (['failed', 'cancelled'].includes(transfer.status)) {
            throw new Error(transfer.error || '来源报告导出失败');
        }
        setReportCopyStatus(
            transfer.status === 'uploading'
                ? '正在通过 Controller 传输来源报告...'
                : '正在来源主机打包报告...',
            'info'
        );
    }
}

async function pollReportCopyCommand(commandId) {
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const payload = await apiCall(`/api/cluster/commands/${encodeURIComponent(commandId)}`);
        const command = payload.command || {};
        if (command.status === 'completed') return command.result || {};
        if (['failed', 'cancelled'].includes(command.status)) {
            throw new Error(command.error || '目标主机导入报告失败');
        }
        setReportCopyStatus('目标主机正在校验并导入报告...', 'info');
    }
}

window.submitReportCopy = async function submitReportCopy() {
    if (_reportCopyRunning) return;
    const sourceWorkerId = $('report-copy-source-worker')?.value || '';
    const sourceSuitePath = $('report-copy-source-suite')?.value || '';
    const reportName = $('report-copy-source-report')?.value || '';
    const targetWorkerId = $('report-copy-target-worker')?.value || '';
    const targetSuitePath = $('report-copy-target-suite')?.value || '';
    if (sourceWorkerId === targetWorkerId) {
        setReportCopyStatus('来源主机和目标主机不能相同', 'error');
        return;
    }
    if (!sourceSuitePath || !reportName || !targetSuitePath) {
        setReportCopyStatus('请选择完整的报告来源和拷贝目标', 'error');
        return;
    }

    _reportCopyRunning = true;
    const submit = $('report-copy-submit');
    if (submit) submit.textContent = '拷贝中...';
    updateReportCopySubmitState();
    try {
        setReportCopyStatus('正在来源主机打包报告...', 'info');
        const created = await apiCall('/api/cluster/suites/report-copies', 'POST', {
            source_worker_id: sourceWorkerId,
            source_suite_path: sourceSuitePath,
            report_name: reportName,
            target_worker_id: targetWorkerId,
            target_suite_path: targetSuitePath
        });
        const transferId = created.copy_id;
        let transfer = created.transfer || {};
        if (transfer.status !== 'completed') {
            transfer = await pollReportCopyTransfer(transferId);
        }
        setReportCopyStatus(
            `来源报告已传输（${formatBytes(transfer.size_bytes || 0, true)}），正在写入目标套件...`,
            'info'
        );
        const imported = await apiCall(
            `/api/cluster/suites/report-copies/${encodeURIComponent(transferId)}/import`,
            'POST'
        );
        const result = imported.command_id
            ? await pollReportCopyCommand(imported.command_id)
            : (imported.result || {});
        const destination = result.destination
            || `${getSuiteRootFromToolsPath(targetSuitePath)}/results/${reportName}`;
        setReportCopyStatus(`拷贝完成：${destination}`, 'success');
        showToast('跨主机测试报告拷贝完成', 'success');
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult('测试报告拷贝完成', destination, 'success', 'report-copy', {
                worker_id: targetWorkerId,
                suite_path: targetSuitePath,
                artifact_id: transferId
            });
        }
        window.GmsWorkspace?.update({artifact_id: transferId}, {source: 'report-copy'});
        if (submit) submit.textContent = '已完成';
    } catch (error) {
        setReportCopyStatus(`拷贝失败：${error.message}`, 'error');
        if (typeof notifyOperationResult === 'function') {
            notifyOperationResult('测试报告拷贝失败', error.message, 'error', 'report-copy');
        }
        if (submit) submit.textContent = '重新拷贝';
    } finally {
        _reportCopyRunning = false;
        updateReportCopySubmitState();
    }
};

