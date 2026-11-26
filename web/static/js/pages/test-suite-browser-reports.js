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
    updateReportCopySubmitState();
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
    const sameWorker = $('report-copy-source-worker')?.value === $('report-copy-target-worker')?.value;
    if (sameWorker) {
        targetSuites = targetSuites.filter(suite =>
            reportCopySuiteRoot(suite.tools_path) !== reportCopySuiteRoot(source.tools_path)
        );
    }
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
    return match?.tools_path || (sameWorker ? targetSuites[0]?.tools_path : '') || '';
}

function reportCopySuiteRoot(suitePath) {
    return getSuiteRootFromToolsPath(String(suitePath || '').replace(/\/+$/, ''));
}

function reportCopyTargetsSourceSuite() {
    return $('report-copy-source-worker')?.value === $('report-copy-target-worker')?.value
        && reportCopySuiteRoot($('report-copy-source-suite')?.value)
            === reportCopySuiteRoot($('report-copy-target-suite')?.value);
}

async function loadReportCopySourceReports() {
    const workerId = $('report-copy-source-worker')?.value || '';
    const suitePath = $('report-copy-source-suite')?.value || '';
    const select = $('report-copy-source-report');
    if (!select) return;
    select.disabled = true;
    select.innerHTML = '<option value="">正在加载报告...</option>';
    updateReportCopySubmitState();
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
        || reportCopyTargetsSourceSuite()
        || $('report-copy-source-suite')?.disabled
        || $('report-copy-source-report')?.disabled
        || $('report-copy-target-suite')?.disabled
        || !$('report-copy-source-suite')?.value
        || !$('report-copy-source-report')?.value
        || !$('report-copy-target-suite')?.value;
    submit.title = reportCopyTargetsSourceSuite()
        ? '同一主机内请选择不同的来源套件和目标套件'
        : '';
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
        if (!_reportCopyWorkers.length) {
            throw new Error('暂无在线 Worker 可供拷贝报告');
        }
        const currentWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const sourceWorkerId = _reportCopyWorkers.some(worker => worker.id === currentWorkerId)
            ? currentWorkerId
            : _reportCopyWorkers[0].id;
        const targetWorkerId = _reportCopyWorkers.find(worker => worker.id !== sourceWorkerId)?.id || sourceWorkerId;
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
    if (!sourceWorkerId || !targetWorkerId || !sourceSuitePath || !reportName || !targetSuitePath) {
        setReportCopyStatus('请选择完整的报告来源和拷贝目标', 'error');
        return;
    }
    if (reportCopyTargetsSourceSuite()) {
        setReportCopyStatus('同一主机内请选择不同的来源套件和目标套件', 'error');
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
        showToast('测试报告拷贝完成', 'success');
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

// 客户端缓存：Worker + suitePath → { results, columns }，避免不同主机的同路径串数据。
const _testResultsCache = new Map();

function testResultsCacheKey(suitePath, suite = null) {
    const workerId = suite?.worker_id
        || _browserSuitesWorkerId
        || $('suite-worker-select')?.value
        || workspaceLocalWorkerId();
    return `${workerId}\u0000${suitePath || ''}`;
}

window.openTestResultsModal = function openTestResultsModal() {
    if (!state.suiteBrowser.selectedSuitePath) {
        showToast('请先选择一个测试套件', 'warning');
        return;
    }
    ModalManager.open('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
    // 若已有缓存则立即渲染，再后台静默刷新（后端缓存命中时几乎无延迟）。
    const suitePath = state.suiteBrowser.selectedSuitePath;
    const suite = _browserSuitesCache.find(item => item.tools_path === suitePath);
    const cached = _testResultsCache.get(testResultsCacheKey(suitePath, suite));
    if (cached) {
        renderTestResults(cached.results, cached.columns);
        const statusEl = $('test-results-modal-status');
        if (statusEl) statusEl.textContent = `共 ${cached.results.length} 条结果 · 点击行跳转目录 · 缓存`;
        loadTestResults(false, false);
    } else {
        loadTestResults(false);
    }
};

window.closeTestResultsModal = function closeTestResultsModal() {
    ModalManager.close('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
};

window.minimizeTestResultsModal = function minimizeTestResultsModal() {
    ModalManager.close('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    const title = document.getElementById('test-results-minimized-title');
    if (title) {
        const suite = document.getElementById('test-results-modal-suite');
        title.textContent = suite ? suite.textContent.trim() : '';
    }
    if (minimized) minimized.style.display = 'flex';
};

window.restoreTestResultsModal = function restoreTestResultsModal() {
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
    ModalManager.open('test-results-modal');
};

async function loadTestResults(force = false, showSpinner = true) {
    const suitePath = state.suiteBrowser.selectedSuitePath;
    const suite = _browserSuitesCache.find(s => s.tools_path === suitePath);
    const cacheKey = testResultsCacheKey(suitePath, suite);
    const requestWorkerId = cacheKey.split('\u0000', 1)[0];
    const listEl = $('test-results-list');
    const statusEl = $('test-results-modal-status');
    const suiteLabelEl = $('test-results-modal-suite');

    if (suiteLabelEl && suite) {
        let displayType = suite.test_type || '';
        if (displayType === 'cts-verifier') displayType = 'cts-v';
        suiteLabelEl.textContent = `· ${displayType.toUpperCase()} ${getSuiteDisplayName(suite)}`;
    }

    if (!suitePath) {
        if (listEl) listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">请先选择测试套件</div>';
        return;
    }

    if (showSpinner) {
        if (listEl) listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">查询 tradefed list results 中...</div>';
        if (statusEl) statusEl.textContent = '正在执行 tradefed list results，可能需要数秒...';
    }

    try {
        // 不传 tradefed_bin：让后端 find_tradefed_binary 解析绝对路径。
        // suite.binary 只是裸文件名（如 vts-tradefed），cd 到 tools 后不在
        // PATH 中无法直接执行，会触发系统 "command not found" 建议而失败。
        const forceParam = force ? '?force_refresh=true' : '';
        const payload = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? await apiCall(`/api/cluster/suites/results?${new URLSearchParams({
                worker_id: suite.worker_id, suite_path: suitePath
            })}`, 'POST')
            : await apiCall(`/api/test/suites/result${forceParam}`, 'POST', {suite_path: suitePath});
        if (!payload || !payload.success) {
            const msg = (payload && (payload.error || payload.message)) || '查询失败';
            if (listEl) listEl.innerHTML = `<div style="padding: 20px; color: var(--danger-color, #e53935); text-align: center;">查询失败: ${escapeHtml(msg)}</div>`;
            if (statusEl) statusEl.textContent = '查询失败';
            return;
        }
        const currentSuite = _browserSuitesCache.find(item => item.tools_path === suitePath);
        if (state.suiteBrowser.selectedSuitePath !== suitePath
                || testResultsCacheKey(suitePath, currentSuite).split('\u0000', 1)[0] !== requestWorkerId) {
            return;
        }
        renderTestResults(payload.results || [], payload.columns || []);
        _testResultsCache.set(cacheKey, { results: payload.results || [], columns: payload.columns || [] });
        const cacheTag = payload.cached ? ' · 缓存' : '';
        if (statusEl) statusEl.textContent = `共 ${payload.count || 0} 条结果 · 点击行跳转目录${cacheTag}`;
    } catch (error) {
        if (listEl) listEl.innerHTML = `<div style="padding: 20px; color: var(--danger-color, #e53935); text-align: center;">加载失败: ${escapeHtml(error.message || String(error))}</div>`;
        if (statusEl) statusEl.textContent = '加载失败';
    }
}

// 原始列名 → 字段渲染。不同套件列不同（CTS/GTS 多 Warning 列），按后端
// 返回的原始表头 columns 动态渲染，列名与 tradefed 输出完全一致。
const RESULT_COLUMN_RENDERERS = {
    'session': r => ({ text: escapeHtml(String(r.session ?? '-')) }),
    'pass': r => ({ text: escapeHtml(String(r.pass ?? '-')), style: 'text-align: right; color: var(--success-color, #43a047);' }),
    'fail': r => {
        const failNum = Number(r.fail) || 0;
        return { text: escapeHtml(String(r.fail ?? '-')), style: `text-align: right;${failNum > 0 ? ' color: var(--danger-color, #e53935); font-weight: var(--ui-type-weight-semibold);' : ''}` };
    },
    'warning': r => ({ text: escapeHtml(String(r.warning ?? '-')), style: 'text-align: right;' }),
    'modules complete': r => ({
        text: (r.modules || r.modules_total)
            ? `${escapeHtml(String(r.modules ?? '-'))}${r.modules_total ? ` of ${escapeHtml(String(r.modules_total))}` : ''}`
            : '<span style="color: var(--text-secondary);">-</span>',
    }),
    'result directory': r => ({
        text: r.result_directory ? `📁 ${escapeHtml(String(r.result_directory))}` : '<span style="color: var(--text-secondary);">-</span>',
    }),
    'test plan': r => ({ text: escapeHtml(String(r.test_plan ?? '-')) }),
    'device serial(s)': r => {
        const v = String(r.device_serial ?? '-');
        return {
            text: `<span title="${escapeHtml(v)}" style="display: inline-block; max-width: 120px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom;">${escapeHtml(v)}</span>`,
        };
    },
    'build id': r => {
        const v = String(r.build_id ?? '-');
        return {
            text: `<span title="${escapeHtml(v)}" style="display: inline-block; max-width: 120px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom;">${escapeHtml(v)}</span>`,
        };
    },
    'product': r => ({ text: escapeHtml(String(r.product ?? '-')) }),
    'project': r => {
        const project = String(r.project ?? '');
        if (!project) return { text: '<span style="color: var(--text-secondary);">-</span>' };
        const palette = ['#1e88e5', '#43a047', '#e53935', '#8e24aa', '#00897b', '#f4511e'];
        const hash = Array.from(project).reduce(
            (value, char) => ((value * 31) + char.charCodeAt(0)) >>> 0,
            0,
        );
        const color = palette[hash % palette.length];
        return {
            text: `<span style="background: ${color}22; color: ${color}; padding: 2px 8px; border-radius: 4px; font-weight: var(--ui-type-weight-semibold); font-size: var(--ui-type-size-compact);">${escapeHtml(project)}</span>`,
        };
    },
};

function renderTestResults(results, columns) {
    const listEl = $('test-results-list');
    if (!listEl) return;

    if (!results.length) {
        listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">暂无测试结果</div>';
        return;
    }

    // 若后端未返回表头，回退到默认列集（不含 Warning）。
    // 始终在 Product 后插入"项目"列以区分不同芯片平台。
    const _DEFAULT_COLS = ['Session', 'Pass', 'Fail', 'Modules Complete', 'Result Directory', 'Test Plan', 'Device serial(s)', 'Build ID', 'Product', 'Project'];
    let cols = (columns && columns.length)
        ? [...columns]
        : _DEFAULT_COLS;
    // 若后端列已有 Product 但没有 Project，在 Product 后插入。
    if (!cols.map(c => c.toLowerCase()).includes('project')) {
        const productIdx = cols.findIndex(c => c.toLowerCase() === 'product');
        if (productIdx >= 0) {
            cols.splice(productIdx + 1, 0, 'Project');
        } else {
            cols.push('Project');
        }
    }

    // 数值列（Pass/Fail/Warning）表头右对齐，与数据 text-align:right 保持一致，
    // 否则宽列里表头左对齐、数字右对齐会错位。
    const numericCols = new Set(['pass', 'fail', 'warning']);
    const headerCells = cols.map(name => {
        const align = numericCols.has(name.toLowerCase()) ? 'right' : 'left';
        return `<th style="text-align: ${align}; white-space: nowrap;">${escapeHtml(name)}</th>`;
    }).join('');

    listEl.innerHTML = `
        <table style="width: 100%; border-collapse: collapse;">
            <thead style="position: sticky; top: 0; z-index: 1;">
                <tr style="background: var(--darker-bg); border-bottom: 1px solid var(--border-color); ">${headerCells}</tr>
            </thead>
            <tbody id="test-results-tbody"></tbody>
        </table>
    `;

    const tbody = $('test-results-tbody');
    results.forEach(r => {
        const tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom: 1px solid var(--border-color); cursor: pointer; ';
        tr.onmouseenter = () => { tr.style.background = 'var(--hover-bg, rgba(0,0,0,0.04))'; };
        tr.onmouseleave = () => { tr.style.background = ''; };
        tr.title = r.result_directory ? `跳转到目录 results/${r.result_directory}` : '无结果目录';

        const cells = cols.map(name => {
            const renderer = RESULT_COLUMN_RENDERERS[name.toLowerCase()];
            const cell = renderer ? renderer(r) : { text: '' };
            const titleAttr = cell.title ? ` title="${escapeHtml(cell.title)}"` : '';
            // nowrap：每列单行显示，避免内容换行造成视觉错位。
            return `<td style="white-space: nowrap; ${cell.style || ''}"${titleAttr}>${cell.text}</td>`;
        }).join('');
        tr.innerHTML = cells;

        tr.addEventListener('click', () => runSuiteBrowserAction(() => jumpToResultDirectory(r)));
        tbody.appendChild(tr);
    });
}

async function jumpToResultDirectory(result) {
    const dir = result && result.result_directory;
    if (!dir) {
        showToast('该结果没有结果目录信息', 'warning');
        return;
    }
    // 结果目录位于套件根下的 results/<timestamp>，文件浏览器以套件根为相对根。
    const relPath = `results/${dir}`;
    closeTestResultsModal();
    // 先确保停留在当前选中套件，再跳转到结果目录并高亮。
    state.suiteBrowser.highlightPath = relPath;
    await loadSuiteBrowserDirectory(relPath);
    setSuiteBrowserHighlightedPath(relPath);
    showToast(`已跳转到 ${relPath}`, 'success');
}
