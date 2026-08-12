// ==================== Test Suite Browser ====================
function getSuiteDisplayName(suite) {
    if (!suite) return '-';
    return suite.version || suite.binary || (suite.tools_path || '').split('/').filter(Boolean).slice(-2).join('/') || suite.tools_path || '-';
}

function getSuiteRootFromToolsPath(toolsPath) {
    if (!toolsPath) return '';
    return toolsPath.endsWith('/tools') ? toolsPath.slice(0, -'/tools'.length) : toolsPath;
}

function normalizeReportTestType(testType) {
    return String(testType || '').trim().toLowerCase().replace(/_/g, '-');
}

function tradefedResultFolderName(value) {
    const normalized = String(value || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    const name = normalized.split('/').filter(Boolean).pop() || '';
    return /^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?$/.test(name)
        ? name
        : '';
}

function findSuitePathForReport(testType, suitePath = '') {
    const normalizedSuitePath = String(suitePath || '').trim();
    if (normalizedSuitePath) {
        return normalizedSuitePath;
    }

    const normalizedType = normalizeReportTestType(testType);
    if (!normalizedType || !Array.isArray(testSuitesCache) || testSuitesCache.length === 0) {
        return '';
    }

    const exact = testSuitesCache.find(suite => normalizeReportTestType(suite.test_type) === normalizedType);
    if (exact) return exact.tools_path || '';

    const pathMatch = testSuitesCache.find(suite => {
        const path = String(suite.tools_path || '').toLowerCase();
        return path.includes(`/android-${normalizedType}-`) || path.includes(`/android-${normalizedType}/`);
    });
    return pathMatch?.tools_path || '';
}

function getReportSuiteVersion(report) {
    if (report?.suite_version) {
        return report.suite_version;
    }
    const suitePath = String(report?.suite_path || '');
    const match = suitePath.match(/android-[^/]*?-(\d+(?:\.\d+)?_r\d+)(?:\/|$)/i);
    if (match) {
        return match[1];
    }
    const versionMatch = suitePath.match(/(\d+(?:\.\d+)?_r\d+)/i);
    return versionMatch ? versionMatch[1] : '-';
}

function getReportSuiteDisplayName(report) {
    const suitePath = String(report?.suite_path || '').replace(/\\/g, '/');
    const pathName = suitePath
        .split('/')
        .find(part => /^android-(?:cts|gts|vts|sts|xts)-/i.test(part));
    if (pathName) return pathName;

    const version = getReportSuiteVersion(report);
    const type = normalizeReportTestType(report?.test_type);
    if (type && version && version !== '-') {
        return `android-${type}-${version}`;
    }
    return report?.suite_key || version || '-';
}

function getSuiteReleasePath(suite) {
    const toolsPath = suite?.tools_path || '';
    const version = suite?.version || '';

    if (toolsPath && version) {
        const marker = `/${version}`;
        const markerIndex = toolsPath.indexOf(marker);
        if (markerIndex !== -1) {
            return toolsPath.slice(0, markerIndex + marker.length);
        }
    }

    const rootPath = getSuiteRootFromToolsPath(toolsPath);
    const parts = rootPath.split('/').filter(Boolean);
    if (parts.length >= 1 && /^android-[^/]+$/.test(parts[parts.length - 1])) {
        parts.pop();
        return `/${parts.join('/')}`;
    }
    return rootPath || toolsPath;
}

function getSuiteBrowserRouteParams() {
    const rawHash = window.location.hash.substring(1);
    const [page, query = ''] = rawHash.split('?');
    if (page !== 'test-suites' || !query) {
        return null;
    }

    const params = new URLSearchParams(query);
    const suitePath = params.get('suite_path') || params.get('suite') || '';
    const filePath = params.get('file') || '';
    const directoryPath = params.get('path') || (filePath ? getParentSuitePath(filePath) : '');
    // 旧实现只有 Controller 本机分享链接会省略 Worker ID，因此缺省值
    // 可以明确归属本机；远端 Worker 分享链接始终包含 worker_id。
    const workerId = params.get('worker_id') || params.get('host') || workspaceLocalWorkerId();

    if (!suitePath) {
        return null;
    }

    return {
        suitePath,
        directoryPath,
        filePath,
        workerId
    };
}

function buildSuiteBrowserLink(path = '', type = 'file') {
    const params = new URLSearchParams();
    params.set('suite_path', state.suiteBrowser.selectedSuitePath);
    if (type === 'directory') {
        params.set('path', path || '');
    } else {
        params.set('file', path || '');
    }
    // 分享链接始终携带明确 Worker ID；本机链接也必须能把其他浏览器
    // 从上次保存的远端 Worker 切回 Controller。
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    const workerId = suite?.worker_id
        || testSuitesWorkerId
        || $('suite-worker-select')?.value
        || workspaceLocalWorkerId();
    params.set('worker_id', workerId);

    // Hash 内的分享参数不发送给服务器。仅恢复路径分隔符以提升可读性，
    // 其余可能改变查询参数边界的字符继续保持 URL 编码。
    const readableQuery = buildReadablePathQuery(params);
    return `${window.location.origin}${window.location.pathname}${window.location.search}#test-suites?${readableQuery}`;
}

function buildReadablePathQuery(params) {
    // Query values still encode characters that could alter parameter
    // boundaries, but path separators remain readable in copied/opened URLs.
    return params.toString().replace(/%2F/gi, '/');
}

let suiteBrowserInitialized = false;
let suiteBrowserInitPromise = null;
let suiteBrowserDirectoryRequestGeneration = 0;

async function initTestSuiteBrowserPage() {
    if (suiteBrowserInitPromise) return suiteBrowserInitPromise;
    const pending = initTestSuiteBrowserPageOnce();
    suiteBrowserInitPromise = pending;
    try {
        return await pending;
    } finally {
        if (suiteBrowserInitPromise === pending) suiteBrowserInitPromise = null;
    }
}

async function initTestSuiteBrowserPageOnce() {
    const listEl = $('suite-browser-list');
    if (listEl && !suiteBrowserInitialized) {
        listEl.innerHTML = '<div class="suite-empty">正在加载...</div>';
    }

    await loadSuiteWorkerSelector();
    const routeParams = getSuiteBrowserRouteParams();
    // 在首次加载套件前先应用链接指定的 Worker，避免先按浏览器保存的
    // ats-worker-* 加载并短暂显示“测试套件不存在”。
    if (routeParams?.workerId) {
        const workerSelect = $('suite-worker-select');
        const supported = workerSelect
            && Array.from(workerSelect.options).some(opt => opt.value === routeParams.workerId);
        if (workerSelect && supported) {
            workerSelect.value = routeParams.workerId;
        } else {
            debugLog('[Suites] Shared link targets unknown worker:', routeParams.workerId);
        }
    }

    await loadSuitesForBrowserWorker(false);
    renderTestSuiteBrowserList();

    // 普通页面回访保留已绘制的目录和滚动位置。套件列表仍会
    // 在上方同步，但不用“正在加载”临时页覆盖已经可用的内容。
    if (suiteBrowserInitialized && !routeParams) {
        const selectedSuite = testSuitesCache.find(
            suite => suite.tools_path === state.suiteBrowser.selectedSuitePath
        );
        if (!state.suiteBrowser.selectedSuitePath || selectedSuite) return;
        clearSuiteBrowserSelection('已选择的测试套件不存在');
        return;
    }

    if (routeParams) {
        state.suiteBrowser.highlightPath = routeParams.filePath || '';
        await selectTestSuiteForBrowser(
            routeParams.suitePath,
            routeParams.directoryPath || '',
            { preserveHighlight: true }
        );
        suiteBrowserInitialized = true;
        return;
    }

    if (state.suiteBrowser.selectedSuitePath) {
        const selectedSuite = testSuitesCache.find(s => s.tools_path === state.suiteBrowser.selectedSuitePath);
        if (selectedSuite) {
            await selectTestSuiteForBrowser(selectedSuite.tools_path, state.suiteBrowser.currentPath || '');
            suiteBrowserInitialized = true;
            return;
        }
    }

    clearSuiteBrowserSelection('请选择左侧测试套件');
    resumeSuiteDownloadIfNeeded();
    suiteBrowserInitialized = true;
}

let _suiteWorkerSelectorPromise = null;
async function loadSuiteWorkerSelector() {
    const select = $('suite-worker-select');
    if (!select || select.dataset.loaded === '1') return;
    if (_suiteWorkerSelectorPromise) return _suiteWorkerSelectorPromise;
    select.disabled = true;
    _suiteWorkerSelectorPromise = (async () => { try {
        const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        await (window.GmsWorkspace?.ready || Promise.resolve());
        const workspace = window.GmsWorkspace?.get?.() || {};
        const localWorkerId = workspaceLocalWorkerId();
        const saved = workspace.worker_id || localWorkerId;
        const workers = (payload.workers || []).filter(worker => worker.status !== 'offline');
        select.innerHTML = workers.map(worker =>
            `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.id)}</option>`
        ).join('');
        if (!workers.some(worker => worker.id === localWorkerId)) {
            select.insertAdjacentHTML('afterbegin', `<option value="${escapeHtml(localWorkerId)}">${escapeHtml(localWorkerId)}</option>`);
        }
        if (Array.from(select.options).some(option => option.value === saved)) select.value = saved;
        select.dataset.loaded = '1';
        select.disabled = false;
    } catch (error) {
        debugLog('[Suites] Worker selector unavailable:', error);
        select.innerHTML = `<option value="${escapeHtml(workspaceLocalWorkerId())}">${escapeHtml(workspaceLocalWorkerId())}</option>`;
        select.disabled = false;
    } })();
    try {
        await _suiteWorkerSelectorPromise;
    } finally {
        _suiteWorkerSelectorPromise = null;
    }
}

async function loadSuitesForBrowserWorker(force = false) {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    window.GmsWorkspace?.update({
        worker_id: workerId
    }, {source: 'suites'});
    syncWorkspaceWorkerSelectors(workerId);
    if (isLocalWorkspaceWorker(workerId)) {
        testSuitesWorkerId = '';
        return loadTestSuites(force);
    }
    const response = await fetch(`/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('加载 Worker 套件失败');
    const payload = await response.json();
    testSuitesCache = (payload.suites || []).filter(item => item.available).map(item => ({
        tools_path: item.tools_path,
        test_type: String(item.test_type || '').toLowerCase(),
        version: item.version,
        suite_key: item.suite_key || item.tools_path,
        worker_id: workerId
    }));
    testSuitesWorkerId = workerId;
    return testSuitesCache;
}

async function switchSuiteWorker() {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    window.GmsWorkspace?.update({
        scope_mode: isLocalWorkspaceWorker(workerId) ? window.GmsWorkspace.get().scope_mode : 'cluster',
        worker_id: workerId, suite_key: '', suite_path: ''
    }, {source: 'suites'});
    syncWorkspaceWorkerSelectors(workerId);
    clearSuiteBrowserSelection('正在加载 Worker 套件...');
    // 立即清除旧主机的测试状态，再异步查询新主机状态。
    state.clusterJobId = '';
    state.clusterEventSequence = -1;
    state.testing = false;
    state.testStopping = false;
    updateTestToggleButton(false);
    refreshTestStatusForWorker(workerId);
    try {
        testSuitesCache = [];
        await loadSuitesForBrowserWorker(true);
        renderTestSuiteBrowserList();
        clearSuiteBrowserSelection(testSuitesCache.length ? '请选择左侧测试套件' : '此 Worker 暂无套件');
    } catch (error) {
        clearSuiteBrowserSelection(`加载失败: ${error.message}`);
    }
}

window.switchSuiteWorker = switchSuiteWorker;

async function refreshTestSuiteBrowser(preferredSuiteRoot = '') {
    await loadSuitesForBrowserWorker(true);
    renderTestSuiteBrowserList();
    const normalizedPreferredRoot = (preferredSuiteRoot || '').replace(/\/+$/, '');
    if (normalizedPreferredRoot) {
        const preferredSuite = testSuitesCache.find(suite => {
            const toolsPath = (suite.tools_path || '').replace(/\/+$/, '');
            const releasePath = (getSuiteReleasePath(suite) || '').replace(/\/+$/, '');
            return toolsPath === normalizedPreferredRoot
                || releasePath === normalizedPreferredRoot
                || toolsPath.startsWith(`${normalizedPreferredRoot}/`);
        });
        if (preferredSuite) {
            await selectTestSuiteForBrowser(preferredSuite.tools_path, '');
            return;
        }
    }

    const suitePath = state.suiteBrowser.selectedSuitePath || '';
    if (!suitePath) {
        clearSuiteBrowserSelection('请选择左侧测试套件');
        return;
    }

    const selectedSuite = testSuitesCache.find(s => s.tools_path === suitePath);
    if (selectedSuite) {
        await selectTestSuiteForBrowser(suitePath, state.suiteBrowser.currentPath || '');
    } else {
        clearSuiteBrowserSelection('已选择的测试套件不存在');
    }
}

function filterTestSuiteBrowserList() {
    renderTestSuiteBrowserList();
}

// 暴露到全局作用域
window.downloadTestSuite = async function downloadTestSuite() {
    const urlInput = $('suite-download-url');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');
    const logDiv = $('suite-download-log');

    debugLog('[downloadTestSuite] urlInput:', urlInput);
    debugLog('[downloadTestSuite] downloadBtn:', downloadBtn);

    if (!urlInput || !urlInput.value) {
        showToast('请输入下载地址', 'error');
        return;
    }

    const url = urlInput.value.trim();

    debugLog('[downloadTestSuite] URL:', url);

    if (downloadBtn) {
        downloadBtn.disabled = true;
        downloadBtn.textContent = '⬇️ 下载中...';
    }
    if (extractBtn) extractBtn.disabled = true;
    if (progressDiv) progressDiv.style.display = 'block';
    if (logDiv) {
        logDiv.style.display = 'block';
        logDiv.innerHTML = '';
    }

    let pollingStarted = false;

    const log = (msg) => {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ${msg}\n`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        debugLog('[downloadTestSuite] ' + msg);
    };

    debugLog('[downloadTestSuite] 开始下载：', url);

    try {
        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        if (!isLocalWorkspaceWorker(suiteWorkerId)) {
            const accepted = await apiCall('/api/cluster/suites/download', 'POST', {
                worker_id: suiteWorkerId, url
            });
            if (progressStatus) progressStatus.textContent = `正在由 ${suiteWorkerId} 下载...`;
            if (progressBar) progressBar.style.width = '10%';
            if (progressPercent) progressPercent.textContent = '10%';
            let command;
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 1500));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(accepted.command_id)}`);
                command = status.command;
                if (['completed', 'failed', 'cancelled'].includes(command.status)) break;
            }
            if (command.status !== 'completed') throw new Error(command.error || 'Worker 下载失败');
            const downloaded = command.result || {};
            urlInput.dataset.lastArchivePath = downloaded.archive_path || '';
            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';
            log(`✅ ${suiteWorkerId} 下载完成：${downloaded.archive_path}`);
            log(`📦 文件大小：${((downloaded.file_size || 0) / 1024 / 1024).toFixed(2)} MB`);
            notifyOperationResult('测试套件下载完成', downloaded.message || '下载完成',
                'success', 'suite-download', {worker_id: suiteWorkerId, archive_path: downloaded.archive_path});
            return;
        }
        const result = await apiCall('/api/test/suites/download-url', 'POST', {
            url: url,
            save_dir: getDefaultSuitesPath()
        });
        debugLog('[downloadTestSuite] 响应结果:', result);

        if (result.success && result.task_id) {
            pollingStarted = true;
            sessionStorage.setItem('active_suite_download', JSON.stringify({
                task_id: result.task_id,
                archive_path: result.archive_path || ''
            }));
            await pollDownloadProgress(result.task_id);
        } else if (result.success) {
            log(`✅ 下载完成：${result.archive_path}`);
            log(`📦 文件大小：${(result.file_size / 1024 / 1024).toFixed(2)} MB`);

            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';

            notifyOperationResult(
                '测试套件下载完成',
                result.message || '下载完成',
                'success',
                'suite-download',
                { archive_path: result.archive_path }
            );

            await refreshTestSuiteBrowser();
        } else {
            log(`❌ 下载失败：${result.error}`);
            if (progressStatus) progressStatus.textContent = '❌ 下载失败';
            notifyOperationResult('测试套件下载失败', result.error, 'error', 'suite-download');
        }
    } catch (error) {
        console.error('[downloadTestSuite] 异常:', error);
        log(`❌ 错误：${error.message}`);
        if (progressStatus) progressStatus.textContent = '❌ 错误';
        notifyOperationResult('测试套件下载失败', error.message, 'error', 'suite-download');
    } finally {
        if (!pollingStarted) {
            if (downloadBtn) {
                downloadBtn.disabled = false;
                downloadBtn.textContent = '⬇️ 下载套件';
            }
            if (extractBtn) extractBtn.disabled = false;
        }
    }
};

async function pollTaskProgress({ statusUrl, progressBar, progressPercent, progressStatus, completedLabel, activeLabel }) {
    let lastPercent = -1;
    let lastStatus = '';
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const resp = await fetch(statusUrl);
        const result = await resp.json();
        if (!result.success) {
            throw new Error(result.error || '任务状态查询失败');
        }
        const task = result.task;
        const percent = Math.max(0, Math.min(100, Number(task.progress || 0)));
        if (progressBar && percent !== lastPercent) progressBar.style.width = `${percent}%`;
        if (progressPercent && percent !== lastPercent) progressPercent.textContent = `${percent.toFixed(1)}%`;
        const statusText = task.status === 'completed' ? completedLabel : activeLabel;
        if (progressStatus && statusText !== lastStatus) {
            progressStatus.textContent = statusText;
            lastStatus = statusText;
        }
        lastPercent = percent;
        if (task.status === 'completed') return task;
        if (task.status === 'error') throw new Error(task.error || '任务失败');
    }
}

async function pollDownloadProgress(taskId) {
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');
    const logDiv = $('suite-download-log');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const urlInput = $('suite-download-url');

    if (downloadBtn) { downloadBtn.disabled = true; downloadBtn.textContent = '⬇️ 下载中...'; }
    if (extractBtn) extractBtn.disabled = true;
    if (progressDiv) progressDiv.style.display = 'block';

    try {
        const statusUrl = `/api/test/suites/download-status/${encodeURIComponent(taskId)}`;
        const completedTask = await pollTaskProgress({
            statusUrl,
            progressBar, progressPercent, progressStatus,
            completedLabel: '✅ 下载完成',
            activeLabel: '下载中...'
        });

        const sizeMb = ((completedTask.downloaded_size || 0) / 1024 / 1024).toFixed(2);
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ✅ 下载完成：${completedTask.archive_path}\n`;
            logDiv.innerHTML += `[${time}] 📦 文件大小：${sizeMb} MB\n`;
        }
        notifyOperationResult(
            '测试套件下载完成',
            completedTask.message || '下载完成',
            'success',
            'suite-download',
            { task_id: taskId, archive_path: completedTask.archive_path }
        );
        if (urlInput) urlInput.dataset.lastArchivePath = completedTask.archive_path || '';
        await refreshTestSuiteBrowser();
    } catch (error) {
        notifyOperationResult(
            '测试套件下载失败',
            error.message,
            'error',
            'suite-download',
            { task_id: taskId }
        );
        if (progressStatus) progressStatus.textContent = `❌ ${error.message}`;
    } finally {
        sessionStorage.removeItem('active_suite_download');
        if (downloadBtn) { downloadBtn.disabled = false; downloadBtn.textContent = '⬇️ 下载套件'; }
        if (extractBtn) extractBtn.disabled = false;
    }
}

async function resumeSuiteDownloadIfNeeded() {
    const saved = sessionStorage.getItem('active_suite_download');
    if (!saved) return;
    try {
        const { task_id } = JSON.parse(saved);
        if (!task_id) return;
        const resp = await fetch(`/api/test/suites/download-status/${encodeURIComponent(task_id)}`);
        const result = await resp.json();
        if (!result.success || !result.task) {
            sessionStorage.removeItem('active_suite_download');
            return;
        }
        const task = result.task;
        if (task.status === 'completed' || task.status === 'error') {
            sessionStorage.removeItem('active_suite_download');
            return;
        }
        // Active download found — resume polling
        await pollDownloadProgress(task_id);
    } catch (e) {
        sessionStorage.removeItem('active_suite_download');
    }
}

// 显示添加本地测试套件路径弹框
window.showAddLocalSuiteDialog = function showAddLocalSuiteDialog() {
    const modal = $('add-local-suite-modal');
    if (modal) {
        ModalManager.open('add-local-suite-modal');
        const input = $('local-suite-path-input');
        if (input) {
            input.value = '';
            input.focus();
        }
    }
};

// 关闭弹框
window.closeAddLocalSuiteModal = function closeAddLocalSuiteModal() {
    ModalManager.close('add-local-suite-modal');
};

// 浏览服务器目录，选择本地测试套件目录后回填到输入框
window.browseLocalSuitePath = async function browseLocalSuitePath() {
    state.fileBrowser.mode = 'local-suite';
    state.fileBrowser.targetInputId = 'local-suite-path-input';
    state.fileBrowser.selectedFile = null;
    document.getElementById('file-browser-title').textContent = '选择测试套件目录';
    ModalManager.open('file-browser-modal');

    await loadFileDirectory(getDefaultSuitesPath());
};

// 处理 Esc 键关闭弹框
window.handleAddLocalSuiteKeydown = function handleAddLocalSuiteKeydown(event) {
    if (event.key === 'Escape') {
        closeAddLocalSuiteModal();
    }
    // 回车键提交
    if (event.key === 'Enter') {
        submitAddLocalSuite();
    }
};

// 提交添加本地测试套件
window.submitAddLocalSuite = async function submitAddLocalSuite() {
    const pathInput = $('local-suite-path-input');
    if (!pathInput || !pathInput.value) {
        showToast('请输入本地路径', 'error');
        return;
    }

    const localPath = pathInput.value.trim();
    debugLog('[submitAddLocalSuite] 本地路径:', localPath);

    try {
        const result = await apiCall('/api/test/suites/add-local', 'POST', { path: localPath });
        debugLog('[submitAddLocalSuite] 响应结果:', result);

        if (result.success) {
            showToast(`添加成功：${result.message}`, 'success');
            closeAddLocalSuiteModal();
            await refreshTestSuiteBrowser();
        } else {
            showToast(`添加失败：${result.error}`, 'error');
        }
    } catch (error) {
        console.error('[submitAddLocalSuite] 异常:', error);
        showToast(`添加失败：${error.message}`, 'error');
    }
};

function deriveSuiteFolderNameFromArchivePath(archivePath) {
    const filename = (archivePath || '').split('/').pop() || '';
    const extensions = ['.tar.bz2', '.tar.gz', '.tgz', '.zip', '.tar'];
    for (const ext of extensions) {
        if (filename.endsWith(ext)) return filename.slice(0, -ext.length);
    }
    return filename.replace(/\.[^.]+$/, '') || 'test-suite';
}

window.extractTestSuite = async function extractTestSuite() {
    await showExtractSuiteModal();
};

window.showExtractSuiteModal = async function showExtractSuiteModal() {
    const urlInput = $('suite-download-url');
    const modal = $('extract-suite-modal');
    const select = $('extract-suite-archive-select');
    const pathInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    if (!modal || !select || !pathInput || !folderInput) return;

    modal.style.display = '';
    ModalManager.open('extract-suite-modal');
    select.innerHTML = '<option value="">正在加载压缩包...</option>';

    try {
        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const result = await apiCall(
            isLocalWorkspaceWorker(suiteWorkerId)
                ? '/api/test/suites/archives'
                : `/api/cluster/suites/archives?worker_id=${encodeURIComponent(suiteWorkerId)}`,
            'GET'
        );
        const archives = result.success ? (result.archives || []) : [];
        select.innerHTML = '<option value="">手动输入压缩包路径</option>' + archives.map(archive => {
            const sizeMb = ((archive.size || 0) / 1024 / 1024).toFixed(1);
            return `<option value="${escapeHtml(archive.path)}" data-folder="${escapeHtml(archive.default_dir_name || '')}">${escapeHtml(archive.name)} (${sizeMb} MB)</option>`;
        }).join('');

        const lastArchivePath = urlInput?.dataset?.lastArchivePath || '';
        const defaultPath = lastArchivePath || (archives[0]?.path || '');
        if (defaultPath) {
            pathInput.value = defaultPath;
            const option = Array.from(select.options).find(opt => opt.value === defaultPath);
            if (option) select.value = defaultPath;
        } else if (urlInput && urlInput.value) {
            pathInput.value = `${getDefaultSuitesPath()}/${urlInput.value.split('/').pop()}`;
        } else {
            pathInput.value = '';
        }
        folderInput.value = deriveSuiteFolderNameFromArchivePath(pathInput.value);
        folderInput.focus();
        folderInput.select();
    } catch (error) {
        select.innerHTML = '<option value="">手动输入压缩包路径</option>';
        showToast(`加载压缩包列表失败：${error.message}`, 'warning');
    }
};

window.closeExtractSuiteModal = function closeExtractSuiteModal() {
    ModalManager.close('extract-suite-modal');
    const modal = $('extract-suite-modal');
    if (modal) modal.style.display = 'none';
};

window.handleExtractSuiteKeydown = function handleExtractSuiteKeydown(event) {
    if (event.key === 'Escape') closeExtractSuiteModal();
    if (event.key === 'Enter') submitExtractSuite();
};

window.handleExtractArchiveSelectChange = function handleExtractArchiveSelectChange() {
    const select = $('extract-suite-archive-select');
    const pathInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    if (!select || !pathInput || !folderInput || !select.value) return;
    pathInput.value = select.value;
    folderInput.value = select.selectedOptions[0]?.dataset?.folder || deriveSuiteFolderNameFromArchivePath(select.value);
};

window.submitExtractSuite = async function submitExtractSuite() {
    const archiveInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const submitBtn = $('btn-submit-extract-suite');
    const logDiv = $('suite-download-log');
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');

    try {
        const archivePath = (archiveInput?.value || '').trim();
        const folderName = (folderInput?.value || '').trim();

        if (!archivePath) {
            showToast('请选择或输入压缩包路径', 'error');
            return;
        }
        if (!folderName) {
            showToast('请输入解压后的文件夹名称', 'error');
            return;
        }

        if (extractBtn) {
            extractBtn.disabled = true;
            extractBtn.textContent = '📦 解压中...';
        }
        if (downloadBtn) downloadBtn.disabled = true;
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = '解压中...';
        }

        closeExtractSuiteModal();
        if (progressDiv) progressDiv.style.display = 'block';
        if (progressBar) progressBar.style.width = '0%';
        if (progressPercent) progressPercent.textContent = '0%';
        if (progressStatus) progressStatus.textContent = '正在解压...';

        if (logDiv) {
            logDiv.style.display = 'block';
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] 开始解压：${archivePath}\n`;
        }

        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const result2 = await apiCall(
            isLocalWorkspaceWorker(suiteWorkerId)
                ? '/api/test/suites/extract-start'
                : '/api/cluster/suites/extract',
            'POST',
            isLocalWorkspaceWorker(suiteWorkerId) ? {
                archive_path: archivePath,
                extract_dir: getDefaultSuitesPath(),
                target_dir_name: folderName
            } : {
                worker_id: suiteWorkerId,
                archive_path: archivePath,
                target_dir_name: folderName
            }
        );

        if (result2.success && (result2.task_id || result2.command_id)) {
            let completedTask;
            if (result2.command_id) {
                while (true) {
                    await new Promise(resolve => setTimeout(resolve, 1000));
                    const state = await apiCall(`/api/cluster/commands/${encodeURIComponent(result2.command_id)}`);
                    if (['completed', 'failed', 'cancelled'].includes(state.command.status)) {
                        if (state.command.status !== 'completed') throw new Error(state.command.error || 'Worker 解压失败');
                        completedTask = state.command.result || {};
                        break;
                    }
                }
                if (progressBar) progressBar.style.width = '100%';
                if (progressPercent) progressPercent.textContent = '100%';
                if (progressStatus) progressStatus.textContent = '✅ 解压完成';
            } else {
                const statusUrl = `/api/test/suites/extract-status/${encodeURIComponent(result2.task_id)}`;
                completedTask = await pollTaskProgress({statusUrl, progressBar, progressPercent, progressStatus,
                    completedLabel: '✅ 解压完成', activeLabel: '正在解压...'});
            }
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ✅ 解压完成：${completedTask.extracted_path}\n`;
            }
            notifyOperationResult(
                '测试套件解压完成',
                completedTask.message || '解压完成',
                'success',
                'suite-extract',
                { task_id: result2.task_id || result2.command_id, extracted_path: completedTask.extracted_path }
            );

            debugLog('[submitExtractSuite] refreshing suite browser, extracted_path:', completedTask.extracted_path);
            await refreshTestSuiteBrowser(completedTask.extracted_path || '');
        } else {
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ❌ 解压失败：${result2.error}\n`;
            }
            notifyOperationResult(
                '测试套件解压失败',
                result2.error,
                'error',
                'suite-extract',
                { archive_path: archivePath }
            );
        }
    } catch (error) {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ❌ 错误：${error.message}\n`;
        }
        notifyOperationResult(
            '测试套件解压失败',
            error.message,
            'error',
            'suite-extract',
            { archive_path: archivePath }
        );
    } finally {
        if (extractBtn) {
            extractBtn.disabled = false;
            extractBtn.textContent = '📦 解压套件';
        }
        if (downloadBtn) downloadBtn.disabled = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '开始解压';
        }
    }
};

function clearSuiteBrowserSelection(message) {
    state.suiteBrowser.selectedSuitePath = '';
    state.suiteBrowser.currentPath = '';
    state.suiteBrowser.highlightPath = '';

    const titleEl = $('suite-browser-title');
    const pathEl = $('suite-browser-path');
    const breadcrumb = $('suite-browser-breadcrumb');
    if (titleEl) titleEl.textContent = '未选择测试套件';
    if (pathEl) pathEl.textContent = '';
    if (breadcrumb) breadcrumb.innerHTML = '';
    clearSuiteSearchResults();

    renderTestSuiteBrowserList();
    renderSuiteFileEmpty(message || '请选择左侧测试套件');
}

function setSuiteBrowserHighlightedPath(path) {
    state.suiteBrowser.highlightPath = path || '';
    const rows = document.querySelectorAll('#suite-file-list .suite-file-row');
    rows.forEach(row => {
        const isTarget = row.dataset.path === path;
        row.classList.toggle('active', isTarget);
    });
}

function renderTestSuiteBrowserList() {
    const listEl = $('suite-browser-list');
    const countEl = $('suite-browser-count');
    if (!listEl) return;

    const filterText = ($('suite-browser-filter')?.value || '').trim().toLowerCase();
    const suites = testSuitesCache.filter(suite => {
        const haystack = [
            suite.test_type,
            suite.version,
            suite.tools_path,
            suite.binary
        ].join(' ').toLowerCase();
        return !filterText || haystack.includes(filterText);
    });

    if (countEl) {
        countEl.textContent = `${testSuitesCache.length} 个套件`;
    }

    if (suites.length === 0) {
        listEl.innerHTML = '<div class="suite-empty">没有匹配的测试套件</div>';
        return;
    }

    listEl.innerHTML = '';
    suites.forEach(suite => {
        const row = document.createElement('div');
        row.className = `suite-suite-item ${suite.tools_path === state.suiteBrowser.selectedSuitePath ? 'active' : ''}`;
        row.dataset.suitePath = suite.tools_path;

        const badge = document.createElement('span');
        badge.className = 'suite-type-badge';
        let displayType = suite.test_type || '-';
        // 将 cts-verifier 显示为 CTS-V
        if (displayType === 'cts-verifier') displayType = 'cts-v';
        badge.textContent = displayType.toUpperCase();

        const main = document.createElement('div');
        main.className = 'suite-suite-main';
        main.innerHTML = `
            <div class="suite-suite-name">${escapeHtml(getSuiteDisplayName(suite))}</div>
            <div class="suite-suite-path">${escapeHtml(getSuiteReleasePath(suite))}</div>
        `;

        row.append(badge, main);
        row.addEventListener('click', () => selectTestSuiteForBrowser(suite.tools_path));
        listEl.appendChild(row);
    });
}

async function selectTestSuiteForBrowser(suitePath, path = '', options = {}) {
    const suite = testSuitesCache.find(s => s.tools_path === suitePath);
    if (!suite) {
        renderSuiteFileEmpty('测试套件不存在');
        return;
    }

    state.suiteBrowser.selectedSuitePath = suite.tools_path;
    state.suiteBrowser.currentPath = path || '';
    window.GmsWorkspace?.update({
        worker_id: $('suite-worker-select')?.value || workspaceWorkerId(),
        suite_key: suite.suite_key || suite.tools_path,
        suite_path: suite.tools_path,
        origin_page: 'test-suites'
    }, {source: 'suites'});
    if (!options.preserveHighlight) {
        state.suiteBrowser.highlightPath = '';
    }
    if (!options.preserveSearchResults) {
        clearSuiteSearchResults();
    }

    const suiteSelect = document.getElementById('test-suite');
    if (suiteSelect && suiteSelect.value !== suite.tools_path) {
        suiteSelect.value = suite.tools_path;
    }

    const titleEl = $('suite-browser-title');
    const pathEl = $('suite-browser-path');
    let displayType = suite.test_type || '';
    // 将 cts-verifier 显示为 CTS-V
    if (displayType === 'cts-verifier') displayType = 'cts-v';
    if (titleEl) titleEl.textContent = `${displayType.toUpperCase()} ${getSuiteDisplayName(suite)}`;
    if (pathEl) pathEl.textContent = getSuiteRootFromToolsPath(suite.tools_path);

    renderTestSuiteBrowserList();
    await loadSuiteBrowserDirectory(path || '');
}

function handleSuiteFileSearchKeydown(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        searchSuiteFiles();
    }
    if (event.key === 'Escape') {
        clearSuiteFileSearch();
    }
}

function clearSuiteSearchResults() {
    const resultsEl = $('suite-search-results');
    if (resultsEl) {
        resultsEl.innerHTML = '';
        resultsEl.style.display = 'none';
    }
}

function clearSuiteFileSearch() {
    const input = $('suite-file-search');
    if (input) input.value = '';
    clearSuiteSearchResults();
    state.suiteBrowser.highlightPath = '';
    setSuiteBrowserHighlightedPath('');
}

function renderSuiteSearchResults(items, query) {
    const resultsEl = $('suite-search-results');
    if (!resultsEl) return;

    if (!items.length) {
        resultsEl.innerHTML = `<div class="suite-empty" style="padding: 10px;">未找到: ${escapeHtml(query)}</div>`;
        resultsEl.style.display = 'block';
        return;
    }

    resultsEl.innerHTML = '';
    items.slice(0, 30).forEach(item => {
        const row = document.createElement('div');
        row.className = 'suite-search-result';
        row.title = item.path || item.name || '';
        row.innerHTML = `
            <span>${item.type === 'directory' ? '📁' : (item.is_apk ? '📦' : (item.is_jar ? '🫙' : '📄'))}</span>
            <div class="suite-search-result-main">
                <div class="suite-search-result-name">${escapeHtml(item.name || '-')}</div>
                <div class="suite-search-result-path">${escapeHtml([item.suite_label || '', item.path || ''].filter(Boolean).join(' · '))}</div>
            </div>
        `;
        row.addEventListener('click', () => locateSuiteSearchResult(item));
        resultsEl.appendChild(row);
    });
    resultsEl.style.display = 'block';
}

async function locateSuiteSearchResult(item) {
    if (!item || !item.path) return;
    const targetPath = item.path || '';
    const parentPath = item.type === 'directory' ? getParentSuitePath(targetPath) : getParentSuitePath(targetPath);
    state.suiteBrowser.highlightPath = targetPath;
    await selectTestSuiteForBrowser(
        item.suite_path || state.suiteBrowser.selectedSuitePath,
        parentPath,
        { preserveHighlight: true, preserveSearchResults: true }
    );
}

async function searchSuiteFilesInSuite(suite, query, limit = 30) {
    const params = new URLSearchParams({
        suite_path: suite.tools_path,
        query,
        limit: String(limit)
    });
    if (suite.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/search' : '/api/test/suites/search';
    const result = await apiCall(`${endpoint}?${params.toString()}`);
    const payload = result.data || {};
    const suiteLabel = `${String(suite.test_type || '').toUpperCase()} ${getSuiteDisplayName(suite)}`.trim();
    return (payload.items || []).map(item => ({
        ...item,
        suite_path: suite.tools_path,
        suite_label: suiteLabel
    }));
}

async function searchSuiteFiles() {
    const input = $('suite-file-search');
    const query = (input?.value || '').trim();
    if (!query) {
        showToast('请输入搜索关键词', 'warning');
        return;
    }
    if (!testSuitesCache.length) {
        await loadTestSuites();
    }
    if (!testSuitesCache.length) {
        showToast('未找到可搜索的测试套件', 'warning');
        return;
    }

    const resultsEl = $('suite-search-results');
    if (resultsEl) {
        resultsEl.innerHTML = '<div class="suite-empty" style="padding: 10px;">搜索中...</div>';
        resultsEl.style.display = 'block';
    }

    try {
        const selectedSuite = testSuitesCache.find(suite => suite.tools_path === state.suiteBrowser.selectedSuitePath);
        const orderedSuites = [
            ...(selectedSuite ? [selectedSuite] : []),
            ...testSuitesCache.filter(suite => !selectedSuite || suite.tools_path !== selectedSuite.tools_path)
        ];
        let items = [];
        for (const suite of orderedSuites) {
            items = await searchSuiteFilesInSuite(suite, query, 30);
            if (items.length) break;
        }
        renderSuiteSearchResults(items, query);
        if (items.length) {
            await locateSuiteSearchResult(items[0]);
            showToast(`找到 ${items.length} 个匹配项`, 'success');
        } else {
            showToast('未找到匹配项', 'warning');
        }
    } catch (error) {
        renderSuiteSearchResults([], query);
        showToast('搜索失败: ' + error.message, 'error');
    }
}

async function loadSuiteBrowserDirectory(path = '') {
    if (!state.suiteBrowser.selectedSuitePath) {
        renderSuiteFileEmpty('请先选择测试套件');
        return;
    }

    const fileList = $('suite-file-list');
    const requestGeneration = ++suiteBrowserDirectoryRequestGeneration;
    const requestedSuitePath = state.suiteBrowser.selectedSuitePath;
    const hadRenderedDirectory = Boolean(
        state.suiteBrowser.suiteRoot || fileList?.querySelector('.suite-file-row')
    );
    if (fileList) fileList.setAttribute('aria-busy', 'true');
    if (fileList && !hadRenderedDirectory) {
        fileList.innerHTML = '<div class="suite-empty">正在加载目录...</div>';
    }

    try {
        const params = new URLSearchParams({
            suite_path: state.suiteBrowser.selectedSuitePath,
            path: path || ''
        });
        const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
        if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
        const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? '/api/cluster/suites/files' : '/api/test/suites/files';
        const result = await apiCall(`${endpoint}?${params.toString()}`);
        if (requestGeneration !== suiteBrowserDirectoryRequestGeneration
                || state.suiteBrowser.selectedSuitePath !== requestedSuitePath) return;
        const data = result.data || {};
        state.suiteBrowser.currentPath = data.path || '';
        // 保留解析后的套件根绝对路径，供"报告分析"等需要绝对路径的操作使用。
        state.suiteBrowser.suiteRoot = data.suite_root || '';
        renderSuiteBreadcrumb(state.suiteBrowser.currentPath);
        renderSuiteFiles(data.items || []);
    } catch (error) {
        if (requestGeneration !== suiteBrowserDirectoryRequestGeneration) return;
        if (hadRenderedDirectory) {
            showToast(`目录刷新失败: ${error.message}`, 'error');
        } else {
            renderSuiteFileEmpty(`加载失败: ${error.message}`);
        }
    } finally {
        if (requestGeneration === suiteBrowserDirectoryRequestGeneration && fileList) {
            fileList.setAttribute('aria-busy', 'false');
        }
    }
}

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

// 客户端缓存：Worker + suitePath → { results, columns }，避免不同主机的同路径串数据。
const _testResultsCache = new Map();

function testResultsCacheKey(suitePath, suite = null) {
    const workerId = suite?.worker_id
        || testSuitesWorkerId
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
    const suite = testSuitesCache.find(item => item.tools_path === suitePath);
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
    const suite = testSuitesCache.find(s => s.tools_path === suitePath);
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
        const currentSuite = testSuitesCache.find(item => item.tools_path === suitePath);
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
        return { text: escapeHtml(String(r.fail ?? '-')), style: `text-align: right;${failNum > 0 ? ' color: var(--danger-color, #e53935); font-weight: 600;' : ''}` };
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
            style: 'padding: 4px 6px;',
        };
    },
    'build id': r => {
        const v = String(r.build_id ?? '-');
        return {
            text: `<span title="${escapeHtml(v)}" style="display: inline-block; max-width: 120px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom;">${escapeHtml(v)}</span>`,
            style: 'padding: 4px 6px;',
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
            text: `<span style="background: ${color}22; color: ${color}; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;">${escapeHtml(project)}</span>`,
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
        return `<th style="padding: 8px; text-align: ${align}; white-space: nowrap;">${escapeHtml(name)}</th>`;
    }).join('');

    listEl.innerHTML = `
        <table style="width: 100%; border-collapse: collapse;">
            <thead style="position: sticky; top: 0; z-index: 1;">
                <tr style="background: var(--darker-bg); border-bottom: 1px solid var(--border-color); font-size: 12px;">${headerCells}</tr>
            </thead>
            <tbody id="test-results-tbody"></tbody>
        </table>
    `;

    const tbody = $('test-results-tbody');
    results.forEach(r => {
        const tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom: 1px solid var(--border-color); cursor: pointer; font-size: 12px;';
        tr.onmouseenter = () => { tr.style.background = 'var(--hover-bg, rgba(0,0,0,0.04))'; };
        tr.onmouseleave = () => { tr.style.background = ''; };
        tr.title = r.result_directory ? `跳转到目录 results/${r.result_directory}` : '无结果目录';

        const cells = cols.map(name => {
            const renderer = RESULT_COLUMN_RENDERERS[name.toLowerCase()];
            const cell = renderer ? renderer(r) : { text: '' };
            const titleAttr = cell.title ? ` title="${escapeHtml(cell.title)}"` : '';
            // nowrap：每列单行显示，避免内容换行造成视觉错位。
            return `<td style="padding: 8px; white-space: nowrap; ${cell.style || ''}"${titleAttr}>${cell.text}</td>`;
        }).join('');
        tr.innerHTML = cells;

        tr.addEventListener('click', () => jumpToResultDirectory(r));
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

function renderSuiteBreadcrumb(path) {
    const breadcrumb = $('suite-browser-breadcrumb');
    if (!breadcrumb) return;

    const parts = (path || '').split('/').filter(Boolean);
    breadcrumb.innerHTML = '';

    const rootBtn = document.createElement('button');
    rootBtn.className = 'btn-xs';
    rootBtn.textContent = '根目录';
    rootBtn.addEventListener('click', () => loadSuiteBrowserDirectory(''));
    breadcrumb.appendChild(rootBtn);

    // 当前位于运行文件夹 results/<ts> 或 logs/<ts> 时，在面包屑右侧显示互跳按钮：
    // results 显示「跳到 logs」，logs 显示「跳到 results」。无论在目录内浏览多深，
    // 只要路径前缀是 results/<ts> 或 logs/<ts> 即可互跳（保留 <ts>）。
    const runKind = (parts.length >= 2 && (parts[0].toLowerCase() === 'results' || parts[0].toLowerCase() === 'logs'))
        ? parts[0].toLowerCase()
        : '';
    if (runKind) {
        const sibling = runKind === 'results' ? 'logs' : 'results';
        const sibBtn = document.createElement('button');
        sibBtn.className = 'btn-xs';
        sibBtn.textContent = `跳到 ${sibling}`;
        sibBtn.title = `跳转到 ${sibling}/${parts[1]}`;
        // 面包屑为普通块级布局，float 右靠使按钮固定在右侧。
        sibBtn.style.cssFloat = 'right';
        sibBtn.addEventListener('click', () => {
            const target = `${sibling}/${parts[1]}`;
            state.suiteBrowser.highlightPath = target;
            loadSuiteBrowserDirectory(target).then(() => {
                setSuiteBrowserHighlightedPath(target);
                showToast(`已跳转到 ${target}`, 'success');
            });
        });
        breadcrumb.appendChild(sibBtn);

        // 「retry」：跳到测试页并预填该运行的时间戳/测试类型/套件路径，
        // 与报告管理页 retry 按钮逻辑一致。与互跳按钮同处面包屑右侧。
        const retryBtn = document.createElement('button');
        retryBtn.className = 'btn-xs';
        retryBtn.style.background = 'var(--primary-color)';
        retryBtn.style.cssFloat = 'right';
        retryBtn.textContent = 'retry报告';
        retryBtn.title = '跳到测试页并预填该运行信息';
        retryBtn.addEventListener('click', () => {
            const ts = parts[1] || '';
            // 从套件路径（如 android-gts-14-R1-...）解析测试类型，归一化到
            // #test-type 下拉框的合法 value（CTS/GSI/GTS/...）。
            // 直接用 test_type 字段常因 GTS-root 等变体不匹配而填不进下拉框。
            const suitePath = state.suiteBrowser.selectedSuitePath || '';
            const m = String(suitePath).toLowerCase().match(/android-([a-z]+)/);
            const typeMap = { cts: 'CTS', gsi: 'GSI', gts: 'GTS', sts: 'STS', vts: 'VTS', apts: 'APTS' };
            const testType = (m && typeMap[m[1]]) || '';
            const selectedSuite = testSuitesCache.find(item => item.tools_path === suitePath);
            retryReportWithSuite(ts, testType, suitePath, {
                worker_id: selectedSuite?.worker_id || workspaceLocalWorkerId(),
                source_timestamp: ts
            });
        });
        breadcrumb.appendChild(retryBtn);
    }

    if (parts.length === 0) return;

    let current = '';
    parts.forEach(part => {
        current = current ? `${current}/${part}` : part;
        const separator = document.createTextNode(' / ');
        const btn = document.createElement('button');
        btn.className = 'btn-xs';
        btn.textContent = part;
        const targetPath = current;
        btn.addEventListener('click', () => loadSuiteBrowserDirectory(targetPath));
        breadcrumb.append(separator, btn);
    });
}

function renderSuiteFiles(items) {
    const fileList = $('suite-file-list');
    if (!fileList) return;

    fileList.innerHTML = '';

    if (state.suiteBrowser.currentPath) {
        const parentRow = createSuiteFileRow({
            name: '..',
            path: getParentSuitePath(state.suiteBrowser.currentPath),
            type: 'directory',
            size: 0,
            isParent: true
        });
        fileList.appendChild(parentRow);
    }

    if (!items.length) {
        if (!state.suiteBrowser.currentPath) {
            renderSuiteFileEmpty('目录为空');
        }
        return;
    }

    items.forEach(item => {
        fileList.appendChild(createSuiteFileRow(item));
    });

    const activeRow = fileList.querySelector('.suite-file-row.active');
    if (activeRow) {
        activeRow.scrollIntoView({ block: 'center' });
    }
}

function isSuiteResultsFolderPath(currentPath) {
    // 当前浏览路径位于某个 .../results 目录内（例如 "android-vts/results" 或
    // "android-vts/results/2026.06.25_10.57.05"）。
    const segs = (currentPath || '').split('/').filter(Boolean);
    return segs.some(seg => seg.toLowerCase() === 'results');
}

// item 是否为一个测试运行文件夹 results/<ts> 或 logs/<ts>——恰好两段、首段为
// results/logs。用 item 自身 path 判断（而非 currentPath），避免在
// logs/2026.06.25_10.57.05 内部对 inv_* 子文件夹也误判为运行文件夹而错误显示
// 下载/互跳按钮，导致跳转到不存在的 logs/.../results/inv_*。
function getSuiteRunFolderKind(itemPath) {
    const segs = (itemPath || '').split('/').filter(Boolean);
    if (segs.length !== 2) return '';
    const head = segs[0].toLowerCase();
    return (head === 'results' || head === 'logs') ? head : '';
}

function isSuiteLogsFolderPath(currentPath) {
    // 当前浏览路径位于某个 .../logs 目录内（例如 "android-vts/logs" 或
    // "android-vts/logs/2026.06.25_10.57.05"）。用路径段判断，避免误匹配
    // 名字里含 "logs" 的目录（如 "catalogs"）。
    const segs = (currentPath || '').split('/').filter(Boolean);
    return segs.some(seg => seg.toLowerCase() === 'logs');
}

async function analyzeSuiteLogDir(relPath) {
    // 复用现有的报告分析页与展示逻辑：切到 report-analysis 页，调用专门的
    // 日志目录分析端点，结果交给 displayReportAnalysis 渲染。
    const suitePath = state.suiteBrowser.selectedSuitePath;
    if (!suitePath) {
        showToast('请先选择测试套件', 'warning');
        return;
    }
    const folderName = (relPath || '').split('/').filter(Boolean).pop() || '日志目录';

    const sidebarItem = document.querySelector('[data-page="report-analysis"]');
    if (sidebarItem) sidebarItem.click();

    setTimeout(async () => {
        showToast(`正在分析 ${folderName} ...`, 'info');
        try {
            const suite = testSuitesCache.find(item => item.tools_path === suitePath);
            let data;
            if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) {
                const transferId = await createRemoteSuiteTransfer(relPath, true, suite);
                data = await apiCall(
                    `/api/cluster/transfers/${encodeURIComponent(transferId)}/report-analysis`,
                    'POST'
                );
            } else {
                const formData = new FormData();
                formData.append('suite_path', suitePath);
                formData.append('path', relPath || '');
                const resp = await fetch('/api/reports/analyze-log-dir', {
                    method: 'POST',
                    body: formData
                });
                data = await resp.json().catch(() => ({ success: false }));
            }
            if (!data.success) {
                notifyOperationResult('报告分析失败', data.message || data.error || '未知错误', 'error', 'report-analysis', { path: relPath });
                return;
            }
            displayReportAnalysis(data.data);
            notifyOperationResult(
                '报告分析完成',
                data.data?.report_name || folderName,
                'success',
                'report-analysis',
                { path: relPath }
            );
        } catch (e) {
            console.error('[Reports] analyzeSuiteLogDir error:', e);
            notifyOperationResult('报告分析失败', e.message, 'error', 'report-analysis', { path: relPath });
        }
    }, 300);
}

function createSuiteFileRow(item) {
    const row = document.createElement('div');
    row.className = 'suite-file-row';
    row.dataset.path = item.path || '';
    if (item.path && item.path === state.suiteBrowser.highlightPath) {
        row.classList.add('active');
    }
    row.addEventListener('click', () => {
        if (!item.isParent) {
            setSuiteBrowserHighlightedPath(item.path || '');
        }
    });

    const icon = document.createElement('span');
    icon.textContent = item.type === 'directory' ? '📁' : (item.is_apk ? '📦' : (item.is_jar ? '🫙' : '📄'));

    const main = document.createElement('div');
    main.className = 'suite-file-main';

    const name = document.createElement('div');
    name.className = 'suite-file-name';
    name.textContent = item.name;

    main.appendChild(name);

    if (item.type !== 'directory') {
        const meta = document.createElement('div');
        meta.className = 'suite-file-meta';
        meta.textContent = `${formatBytes(item.size || 0, true)}${item.is_apk ? ' · APK' : (item.is_jar ? ' · JAR' : '')}`;
        main.appendChild(meta);
    }

    const actions = document.createElement('div');
    actions.className = 'suite-file-actions';

    if (item.type === 'directory') {
        // 下载 + 互跳 只对真正的运行文件夹 results/<ts>、logs/<ts> 显示（按 item 自身
        // path 精确判断），避免在 logs/<ts>/inv_* 这类深层子目录误显示导致跳转到
        // 不存在的 logs/.../results/inv_*。
        const runKind = !item.isParent ? getSuiteRunFolderKind(item.path || '') : '';
        const isRunnableFolder = Boolean(runKind);
        const inResults = runKind === 'results';
        const inLogs = runKind === 'logs';
        // 报告分析适用于 logs 目录树，包括 inv_* 子目录。
        const inLogsTree = !item.isParent && isSuiteLogsFolderPath(state.suiteBrowser.currentPath);

        const openBtn = document.createElement('button');
        openBtn.className = 'btn-xs';
        // results/logs 目录内的时间戳运行文件夹：首按钮为「下载」(打包整个文件夹)。
        // 其余目录（含 .. 返回行）保持「打开」。
        openBtn.textContent = isRunnableFolder ? '下载' : (item.isParent ? '返回' : '打开');
        if (isRunnableFolder) {
            openBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                downloadSuiteDir(item.path || '', item.name);
            });
        } else {
            openBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                if (!item.isParent) {
                    setSuiteBrowserHighlightedPath(item.path || '');
                }
                loadSuiteBrowserDirectory(item.path || '');
            });
        }
        actions.appendChild(openBtn);

        if (!item.isParent) {
            //   - results/<ts>: + 「logs」互跳
            //   - logs/<ts>:   + 「results」互跳 + 保留「报告分析」
            // 行体点击/双击仍可进入子目录，导航能力不丢。
            if (isRunnableFolder) {
                const sibling = inResults ? 'logs' : 'results';
                const sibBtn = document.createElement('button');
                sibBtn.className = 'btn-xs';
                sibBtn.textContent = sibling;
                sibBtn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    jumpSuiteSiblingFolder(item.path || '', sibling);
                });
                actions.appendChild(sibBtn);
            }

            if (inLogsTree) {
                const analyzeLogBtn = document.createElement('button');
                analyzeLogBtn.className = 'btn-xs';
                analyzeLogBtn.textContent = '报告分析';
                analyzeLogBtn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    setSuiteBrowserHighlightedPath(item.path || '');
                    analyzeSuiteLogDir(item.path || '');
                });
                actions.appendChild(analyzeLogBtn);
            }

            const copyBtn = document.createElement('button');
            copyBtn.className = 'btn-xs';
            copyBtn.textContent = '分享链接';
            copyBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                setSuiteBrowserHighlightedPath(item.path || '');
                copySuiteBrowserLink(item.path || '', 'directory');
            });
            actions.appendChild(copyBtn);
        }

        row.addEventListener('dblclick', () => loadSuiteBrowserDirectory(item.path || ''));
    } else {
        if (item.is_apk || item.is_jar) {
            const analyzeBtn = document.createElement('button');
            analyzeBtn.className = 'btn-xs';
            analyzeBtn.textContent = '反编译';
            analyzeBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                analyzeSuiteApk(item.path);
            });
            actions.appendChild(analyzeBtn);
        }

        const downloadBtn = document.createElement('button');
        downloadBtn.className = 'btn-xs';
        downloadBtn.textContent = '下载';
        downloadBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            downloadSuiteFile(item.path, item.name);
        });
        actions.appendChild(downloadBtn);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn-xs';
        copyBtn.textContent = '分享链接';
        copyBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            setSuiteBrowserHighlightedPath(item.path || '');
            copySuiteBrowserLink(item.path || '', 'file');
        });
        actions.appendChild(copyBtn);

        row.addEventListener('dblclick', () => {
            // HTML 报告双击在浏览器新标签页内联预览；其余文件仍下载。
            if (isSuiteHtmlFile(item.name)) {
                openSuiteFileInline(item.path);
            } else {
                downloadSuiteFile(item.path, item.name);
            }
        });
    }

    row.append(icon, main, actions);
    return row;
}

function copySuiteBrowserLink(path, type = 'file') {
    if (!state.suiteBrowser.selectedSuitePath) return;
    copyText(buildSuiteBrowserLink(path, type), { successMsg: '链接已复制' });
}

if (!window.__suiteBrowserHashListenerInstalled) {
    window.__suiteBrowserHashListenerInstalled = true;
    window.addEventListener('hashchange', () => {
        if (!getSuiteBrowserRouteParams()) {
            return;
        }

        if (typeof window.switchPage === 'function') {
            window.switchPage('test-suites', null);
        } else {
            initTestSuiteBrowserPage();
        }
    });
}

function getParentSuitePath(path) {
    const parts = (path || '').split('/').filter(Boolean);
    parts.pop();
    return parts.join('/');
}

function renderSuiteFileEmpty(message) {
    const fileList = $('suite-file-list');
    if (fileList) {
        fileList.innerHTML = `<div class="suite-empty">${escapeHtml(message)}</div>`;
    }
}

function isSuiteHtmlFile(name) {
    // 是否为可在浏览器内联预览的 HTML 文件（test_result.html 等报告）。
    return /\.(html?|htm)$/i.test(name || '');
}

function openSuiteFileInline(path) {
    // 用 inline=true 让后端返回 Content-Disposition: inline，浏览器新标签页内联渲染。
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    const params = new URLSearchParams({
        suite_path: state.suiteBrowser.selectedSuitePath,
        path,
        inline: 'true'
    });
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/download' : '/api/test/suites/download';
    window.open(`${endpoint}?${buildReadablePathQuery(params)}`, '_blank');
}

async function startRemoteSuiteExport(path, directory = false) {
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (!suite?.worker_id || isLocalWorkspaceWorker(suite.worker_id)) return false;
    const transferId = await createRemoteSuiteTransfer(path, directory, suite);
    const frame = document.getElementById('suite-download-frame') || Object.assign(document.createElement('iframe'), {
        id: 'suite-download-frame', name: 'suite-download-frame'
    });
    frame.style.display = 'none';
    if (!frame.parentNode) document.body.appendChild(frame);
    window.open(`/api/cluster/transfers/${encodeURIComponent(transferId)}/download`, frame.name);
    return true;
}

async function createRemoteSuiteTransfer(path, directory = false, suite = null) {
    suite = suite || testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (!suite?.worker_id || isLocalWorkspaceWorker(suite.worker_id)) {
        throw new Error('未选择远端 Worker 套件');
    }
    showToast(`正在从 ${suite.worker_id} 准备下载...`, 'info');
    const params = new URLSearchParams({worker_id: suite.worker_id,
        suite_path: suite.tools_path, path, directory: String(directory)});
    const created = await apiCall(`/api/cluster/suites/export?${params.toString()}`, 'POST');
    const transferId = created.transfer.id;
    window.GmsWorkspace?.update({
        worker_id: suite.worker_id,
        suite_key: suite.suite_key || suite.tools_path,
        suite_path: suite.tools_path,
        artifact_id: transferId
    }, {source: 'suite-export'});
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const status = await apiCall(`/api/cluster/transfers/${encodeURIComponent(transferId)}`);
        if (status.transfer.status === 'completed') break;
        if (status.transfer.status === 'failed') throw new Error(status.transfer.error || '远端导出失败');
    }
    return transferId;
}

async function downloadSuiteFile(path, filename = '') {
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    try {
        if (await startRemoteSuiteExport(path, false)) return;
    } catch (error) {
        showToast(`远端文件下载失败: ${error.message}`, 'error');
        return;
    }
    const params = new URLSearchParams({
        suite_path: state.suiteBrowser.selectedSuitePath,
        path
    });
    let frame = document.getElementById('suite-download-frame');
    if (!frame) {
        frame = document.createElement('iframe');
        frame.id = 'suite-download-frame';
        frame.name = 'suite-download-frame';
        frame.style.display = 'none';
        document.body.appendChild(frame);
    }

    const link = document.createElement('a');
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/download' : '/api/test/suites/download';
    link.href = `${endpoint}?${buildReadablePathQuery(params)}`;
    link.download = filename || path.split('/').pop() || 'download';
    link.target = frame.name;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
}

async function downloadSuiteDir(path, name = '') {
    // 后端把整个文件夹打包成 zip 流式回传（保持目录树）。复用 downloadSuiteFile
    // 的隐藏 iframe 模式，避免浏览器把流响应当作页面跳转。
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    try {
        if (await startRemoteSuiteExport(path, true)) return;
    } catch (error) {
        showToast(`远端目录下载失败: ${error.message}`, 'error');
        return;
    }
    const params = new URLSearchParams({
        suite_path: state.suiteBrowser.selectedSuitePath,
        path
    });
    let frame = document.getElementById('suite-download-frame');
    if (!frame) {
        frame = document.createElement('iframe');
        frame.id = 'suite-download-frame';
        frame.name = 'suite-download-frame';
        frame.style.display = 'none';
        document.body.appendChild(frame);
    }
    const link = document.createElement('a');
    link.href = `/api/test/suites/download-dir?${buildReadablePathQuery(params)}`;
    const dirSuffix = getSuiteRunFolderKind(path) ? `-${getSuiteRunFolderKind(path)}` : '';
    link.download = `${name || path.split('/').pop() || 'download'}${dirSuffix}.zip`;
    link.target = frame.name;
    link.style.display = 'none';
    document.body.appendChild(link);
    showToast(`正在打包下载 ${name || path} ...`, 'info');
    link.click();
    link.remove();
}

function jumpSuiteSiblingFolder(itemPath, sibling) {
    // 替换完整相对路径的首段，在 results 和 logs 同名目录间跳转。
    const parts = (itemPath || '').split('/').filter(Boolean);
    if (parts.length < 2) {
        showToast('无法定位同级目录', 'warning');
        return;
    }
    parts[0] = sibling;
    const target = parts.join('/');
    closeTestResultsModal();
    state.suiteBrowser.highlightPath = target;
    loadSuiteBrowserDirectory(target).then(() => {
        setSuiteBrowserHighlightedPath(target);
        showToast(`已跳转到 ${target}`, 'success');
    });
}

async function analyzeSuiteApk(path, options = {}) {
    if (!state.suiteBrowser.selectedSuitePath || !path) return;

    try {
        showToast('正在准备反编译任务...', 'info');
        const suite = testSuitesCache.find(item =>
            item.tools_path === state.suiteBrowser.selectedSuitePath);
        const result = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? await (async () => {
                const transferId = await createRemoteSuiteTransfer(path, false, suite);
                return apiCall(
                    `/api/cluster/transfers/${encodeURIComponent(transferId)}/apk-analysis`,
                    'POST'
                );
            })()
            : await apiCall('/api/test/suites/apk/analyze', 'POST', {
                suite_path: state.suiteBrowser.selectedSuitePath,
                path
            });
        const task = result.data || {};
        if (!task.task_id) {
            showToast('创建反编译任务失败', 'error');
            return;
        }

        switchPage('apk-analysis', null);
        initApkAnalysisPage();
        stopApkPolling();
        window.apkNotifiedTaskId = null;

        window.apkCurrentTaskId = task.task_id;
        window.GmsWorkspace?.update({
            worker_id: suite?.worker_id || workspaceLocalWorkerId(),
            suite_key: suite?.suite_key || suite?.tools_path || '',
            suite_path: suite?.tools_path || '',
            artifact_id: task.transfer_id || '',
            origin_page: 'apk-analysis'
        }, {source: 'suite-apk-analysis'});
        setApkUploadEmpty(false);
        const pendingOpenPaths = Array.from(new Set([
            options.openSourcePath,
            options.openFallbackSourcePath
        ].filter(Boolean)));
        window.apkPendingOpenTarget = pendingOpenPaths.length ? {
            filePath: pendingOpenPaths[0],
            fallbackPaths: pendingOpenPaths.slice(1),
            line: Number(options.openSourceLine || 0) || null
        } : null;

        const fileSizeMB = task.size ? (task.size / (1024 * 1024)).toFixed(1) : '-';
        $('apk-analysis-status').style.display = 'block';
        $('apk-file-name').textContent = `${task.filename || path} (${fileSizeMB}MB)`;
        $('apk-analysis-state').textContent = '已从测试套件导入，正在启动反编译';
        $('apk-btn-download').style.display = 'none';
        $('apk-analysis-result').style.display = 'none';
        $('apk-analysis-progress-container').style.display = 'none';
        $('apk-analysis-progress-bar').style.width = '0%';

        const sourceTree = $('apk-source-tree');
        if (sourceTree) {
            sourceTree.dataset.loaded = '';
            sourceTree.innerHTML = '';
        }
        const permList = $('apk-permissions-list');
        if (permList) {
            permList.dataset.loaded = '';
            permList.innerHTML = '';
        }
        const manifestInfo = $('apk-manifest-info');
        if (manifestInfo) manifestInfo.innerHTML = '';
        const rawXml = $('apk-raw-xml');
        if (rawXml) rawXml.textContent = '';
        closeApkFileViewer();
        switchApkTab('manifest');

        await startApkAnalysis();
    } catch (error) {
        showToast(`准备反编译失败: ${error.message}`, 'error');
    }
}

// 用户列表管理
async function loadUsers(forceRefresh = false) {
    if (state.isRefreshingUsers) {
        return;
    }

    state.isRefreshingUsers = true;

    try {
        const url = forceRefresh ? '/api/users/list?force_refresh=1' : '/api/users/list';
        const response = await apiCall(url);

        debugLog('[loadUsers] API response:', response);

        // 处理不同的响应格式
        let users = [];
        if (Array.isArray(response)) {
            users = response;
            debugLog('[loadUsers] Response is array, length:', users.length);
        } else if (response && response.users && Array.isArray(response.users)) {
            users = response.users;
            debugLog('[loadUsers] Response has users array, length:', users.length);
        } else if (response && response.data && Array.isArray(response.data)) {
            users = response.data;
            debugLog('[loadUsers] Response has data array, length:', users.length);
        } else {
            console.warn('[loadUsers] Unexpected user list format:', response);
        }

        state.users = users;
        debugLog('[loadUsers] state.users set to:', state.users);
        // renderUsers() 已移除，使用 HTML 中的 displayUsersList() 避免重复渲染
    } catch (error) {
        console.error('加载用户列表失败:', error);
    } finally {
        state.isRefreshingUsers = false;
    }
}


// 防抖版本的刷新函数
const debouncedRefreshDevices = debounce(() => loadDevices(false), 500);
const debouncedRefreshUsers = debounce(() => loadUsers(false), 500);

function renderDevices() {
    debugLog('[renderDevices] Called, state.devices:', state.devices);
    const leftContainer = document.getElementById('device-list-left');
    const rightContainer = document.getElementById('device-list-right');
    const deviceCanvas = document.getElementById('device-canvas');

    debugLog('[renderDevices] Containers:', { leftContainer: !!leftContainer, rightContainer: !!rightContainer, deviceCanvas: !!deviceCanvas });

    // Early return if containers not ready
    if (!leftContainer || !rightContainer || !deviceCanvas) {
        console.warn('[renderDevices] Early return: containers not ready');
        return;
    }

    if (state.devices.length === 0) {
        // 先加居中 class 再渲染消息，避免分两步布局导致空态提示先出现在
        // 左栏顶部、再被 class 拉到正中间的视觉跳变。
        rightContainer.innerHTML = '';
        deviceCanvas.classList.add('device-canvas-empty');
        leftContainer.innerHTML = '<div class="empty-message">点击刷新按钮获取设备列表...</div>';
        syncLocalUsbActionButtons();
        return;
    }

    deviceCanvas.classList.remove('device-canvas-empty');

    // 设备统一放入响应式网格，由可用宽度自动决定一至三列。
    // ADB 区按"关注"筛选：开启且有关注分组时，只显示属于任一关注分组的设备
    const followedIds = new Set(
        (state.deviceGroups || []).filter(g => g.followed).flatMap(g => g.device_ids || [])
    );
    const visibleDevices = (state.followFilter && followedIds.size > 0)
        ? state.devices.filter(d => {
            const id = typeof d === 'string' ? d : d.device_id;
            return followedIds.has(id);
        })
        : state.devices;

    const deviceInfos = [];
    visibleDevices.forEach(device => {
        // Handle both string device IDs and device objects
        const deviceId = typeof device === 'string' ? device : device.device_id;
        const isLocked = typeof device === 'object' && device.locked;
        const lockedBy = typeof device === 'object' ? device.locked_by : '';
        const status = typeof device === 'object'
            ? (device.status || device.state || 'online')
            : 'online';
        const selectable = isSelectableWorkspaceDevice(device);
        const transport = typeof device === 'object'
            ? (device.transport || 'local_usb')
            : 'local_usb';
        const adbProxySourceWorkerId = typeof device === 'object'
            ? (device.adb_proxy_source_worker_id || '')
            : '';
        const adbProxyTargetWorkerId = typeof device === 'object'
            ? (device.cluster_worker_id || device.worker_id || '')
            : '';
        const isUsbip = typeof device === 'object'
            && (device.is_usbip === true || transport === 'usbip');
        const usbipSourceHost = typeof device === 'object'
            ? (device.usbip_source_host || device.source || '')
            : '';
        const displaySerial = typeof device === 'object'
            ? (device.adb_proxy_source_serial || device.serial || deviceId)
            : deviceId;

        deviceInfos.push({
            deviceId, isLocked, lockedBy, status, selectable,
            transport, adbProxySourceWorkerId, adbProxyTargetWorkerId,
            isUsbip, usbipSourceHost, displaySerial
        });
    });

    // 使用DocumentFragment优化DOM操作
    // 容器统一使用事件委托。
    const renderDeviceItem = (info) => buildDeviceItemEl(info);

    // 旧的左右栏 ID 保持不变以兼容现有页面选择器；主栏承载响应式网格。
    const deviceFragment = document.createDocumentFragment();
    deviceInfos.forEach(deviceInfo => {
        deviceFragment.appendChild(renderDeviceItem(deviceInfo));
    });
    leftContainer.innerHTML = '';
    leftContainer.appendChild(deviceFragment);
    rightContainer.innerHTML = '';

    // 按 data 属性初始化一次事件委托。
    const setupDeviceDelegation = (container) => {
        if (container._delegated) return;
        container._delegated = true;
        container.addEventListener('click', (e) => {
            if (e.target.classList.contains('device-checkbox') && !e.target.disabled) {
                e.stopPropagation();
            }
            const item = e.target.closest('.device-item');
            if (!item || item.dataset.locked === 'true') return;
            const deviceId = item.dataset.deviceId;
            if (deviceId) toggleDevice(deviceId);
        });
    };
    setupDeviceDelegation(leftContainer);
    setupDeviceDelegation(rightContainer);
    syncLocalUsbActionButtons();
}

// 构建单个设备项 DOM（renderDevices 奇偶分栏与分组视图共用）
function buildDeviceItemEl({
    deviceId,
    isLocked,
    lockedBy,
    status = 'online',
    selectable = true,
    transport = 'local_usb',
    adbProxySourceWorkerId = '',
    adbProxyTargetWorkerId = '',
    isUsbip = false,
    usbipSourceHost = '',
    displaySerial = ''
}) {
    const div = document.createElement('div');
    const isSelected = state.selectedDevices.has(deviceId);
    div.className = `device-item ${isSelected ? 'selected' : ''} ${isLocked ? 'locked' : ''}`;
    div.dataset.deviceId = deviceId;
    if (!selectable) div.dataset.locked = 'true';
    const adbProxyTargetHint = adbProxyTargetWorkerId
        ? `；接入：${adbProxyTargetWorkerId}`
        : '';
    const usbipSource = String(usbipSourceHost || '').split('@').pop() || '来源未知';
    const lockHint = isLocked ? `；占用：${lockedBy}` : '';
    div.title = transport === 'adb_proxy'
        ? `ADB Proxy远程设备，来源：${adbProxySourceWorkerId || '未知'}${adbProxyTargetHint}；可执行ADB/测试，不能执行Fastboot、锁定或烧写${lockHint}`
        : isUsbip
        ? `USB/IP远程设备，来源：${usbipSource}${lockHint}`
        : isLocked
        ? `已被 ${lockedBy} 占用`
        : status === 'fastboot'
        ? 'Fastboot/Fastbootd 设备仅可用于 GSI 烧写'
        : selectable ? '点击选择设备' : `设备当前处于 ${status} 状态`;

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'device-checkbox';
    checkbox.checked = isSelected;
    if (!selectable) checkbox.disabled = true;

    const info = document.createElement('div');
    info.className = 'device-info';
    const idDiv = document.createElement('div');
    idDiv.className = 'device-id';
    idDiv.textContent = displaySerial || deviceId;
    info.appendChild(idDiv);
    if (transport === 'adb_proxy') {
        const sourceStatus = document.createElement('div');
        sourceStatus.className = 'device-source';
        const source = adbProxySourceWorkerId || '来源未知';
        sourceStatus.textContent = `ADB · ${source}`;
        info.appendChild(sourceStatus);
    } else if (isUsbip) {
        const sourceStatus = document.createElement('div');
        sourceStatus.className = 'device-source';
        sourceStatus.textContent = `USB/IP · ${usbipSource}`;
        info.appendChild(sourceStatus);
    }
    const statusEl = document.createElement('span');
    statusEl.className = 'device-status';
    const displayStatus = String(status || '').toLowerCase() === 'fastboot'
        ? 'Fastboot'
        : String(status || '').toLowerCase() === 'unauthorized'
        ? '未授权'
        : isLocked ? '已分配' : selectable ? '可用' : status;
    statusEl.textContent = isLocked && displayStatus !== '已分配'
        ? `${displayStatus} · 已占用`
        : displayStatus;

    div.appendChild(checkbox);
    div.appendChild(info);
    div.appendChild(statusEl);
    return div;
}

// 加载分组定义（GET /api/device-groups）
async function loadDeviceGroups() {
    try {
        const res = await apiCall('/api/device-groups', 'GET');
        state.deviceGroups = res?.data?.groups || [];
    } catch (e) {
        debugLog('[loadDeviceGroups] error:', e);
        state.deviceGroups = [];
    }
    syncFollowFilterBtn();
}

// 主页 ADB 区"只看关注"开关
function toggleFollowFilter() {
    state.followFilter = !state.followFilter;
    localStorage.setItem('gms_follow_filter', state.followFilter ? '1' : '0');
    syncFollowFilterBtn();
    renderDevices();
}

function syncFollowFilterBtn() {
    const btn = $('btn-follow-filter');
    if (!btn) return;
    const hasFollowed = (state.deviceGroups || []).some(g => g.followed);
    btn.classList.toggle('active', state.followFilter && hasFollowed);
    btn.disabled = !hasFollowed;
    btn.title = hasFollowed
        ? (state.followFilter ? '当前只显示关注分组的设备，点击显示全部' : '点击只显示关注分组的设备')
        : '请先在设备管理页"关注"一个分组';
}
window.toggleFollowFilter = toggleFollowFilter;

// 设备分组的交互逻辑（视图切换/筛选/弹框/自动分组）由设备管理页面提供，
// 以下函数仅供设备管理页的 allDevices 表格使用。

function toggleDevice(deviceId) {
    const device = state.devices.find(item => {
        const id = typeof item === 'string' ? item : item.device_id;
        return id === deviceId;
    });
    if (device && !isSelectableWorkspaceDevice(device)) {
        showToast(`设备 ${deviceId} 当前不可选择`, 'warning');
        return;
    }
    if (state.selectedDevices.has(deviceId)) {
        state.selectedDevices.delete(deviceId);
    } else {
        state.selectedDevices.add(deviceId);
    }
    window.GmsWorkspace?.update({device_ids: Array.from(state.selectedDevices)}, {source: 'test'});
    renderDevices();
}

async function refreshDevices() {
    const button = document.getElementById('refresh-devices-btn');
    if (button?.disabled) return;
    if (button) {
        button.disabled = true;
        button.textContent = '刷新中…';
        button.setAttribute('aria-busy', 'true');
    }
    showToast('正在刷新设备列表...', 'info');
    try {
        // 手动刷新时强制绕过缓存，并标记来源为手动。
        await loadDevices(true, {source: 'manual'});
        showToast('设备列表已刷新', 'success');
    } catch (error) {
        showToast(`刷新设备失败: ${error.message}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = '↻ 刷新设备';
            button.removeAttribute('aria-busy');
        }
    }
}

function selectAllDevices() {
    const selectableDevices = state.devices.filter(isSelectableTestDevice);
    const selectableIds = selectableDevices.map(
        device => typeof device === 'string' ? device : device.device_id
    );
    if (
        selectableIds.length > 0
        && selectableIds.every(deviceId => state.selectedDevices.has(deviceId))
    ) {
        // Deselect all
        state.selectedDevices.clear();
    } else {
        // Select all - skip locked devices and non-ADB protocol states.
        let selectedCount = 0;
        let skippedUnavailable = 0;

        state.devices.forEach(device => {
            // Extract device_id from object or use string directly
            const deviceId = typeof device === 'string' ? device : device.device_id;
            const deviceObj = typeof device === 'string' ?
                state.devices.find(d => d.device_id === deviceId) : device;

            // 锁定设备以及 Fastboot 等非 ADB 可用状态均不可选。
            if (deviceObj && !isSelectableTestDevice(deviceObj)) {
                skippedUnavailable++;
                debugLog(`[SelectAll] Skipping unavailable device: ${deviceId} (${deviceObj.status || deviceObj.state || deviceObj.locked_by})`);
            } else {
                state.selectedDevices.add(deviceId);
                selectedCount++;
            }
        });

        if (skippedUnavailable > 0) {
            showToast(`跳过 ${skippedUnavailable} 台锁定或非 ADB 可用设备`, 'warning');
            addLogEntry(`全选设备：已选择 ${selectedCount} 台，跳过 ${skippedUnavailable} 台不可用设备`, 'warning');
        }
    }
    window.GmsWorkspace?.update({device_ids: Array.from(state.selectedDevices)}, {source: 'test'});
    renderDevices();
    addLogEntry(`已选择 ${state.selectedDevices.size} 台设备`, 'info');
}

async function rebootDevices() {
    if (!validateDeviceSelection()) return;

    // 获取选中设备的序列号
    const selectedDeviceSerials = Array.from(state.selectedDevices).map(deviceId => {
        const device = state.devices.find(d =>
            (d.device_id && d.device_id === deviceId) ||
            (d.serial && d.serial === deviceId) ||
            d === deviceId
        );
        return device ? (device.device_id || device.serial || deviceId) : deviceId;
    });

    const confirmed = await showConfirmDialog(
        '重启设备',
        `确定要重启以下 ${state.selectedDevices.size} 台设备吗？\n\n${selectedDeviceSerials.join('\n')}`
    );

    if (!confirmed) return;

    try {
        const workerId = selectedClusterWorker();
        await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/reboot', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'reboot'}
                     : {devices: Array.from(state.selectedDevices)});
        addLogEntry(`正在重启 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('设备正在重启', 'success');
        if (state.usbipConnected) {
            scheduleUsbipReconnect('USB/IP 设备正在重启');
        }
    } catch (error) {
        addLogEntry('重启设备失败: ' + error.message, 'error');
    }
}

async function remountDevices() {
    const button = document.getElementById('btn-remount-devices');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        addLogEntry('正在执行 remount...', 'info');
        const workerId = selectedClusterWorker();
        if (workerId) {
            await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'remount'
            });
        } else {
            await callDeviceApi('/api/devices/remount');
        }
    } catch (error) {
        addLogEntry('Remount失败: ' + error.message, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

async function connectWifi() {
    if (!validateDeviceSelection()) return;
    // 预填 config.wifi 的默认 SSID/密码（管理员在 /api/config/read 中拿到明文密码）
    const wifi = state.config?.wifi || {};
    const ssidInput = document.getElementById('wifi-ssid');
    const pwdInput = document.getElementById('wifi-password');
    if (ssidInput) ssidInput.value = wifi.ssid || '';
    if (pwdInput) {
        pwdInput.value = wifi.password || '';
        pwdInput.placeholder = '请输入 Wi-Fi 密码';
        pwdInput.onfocus = null;
        delete pwdInput.dataset.savedPassword;
    }
    ModalManager.open('wifi-modal');
}

function closeWifiModal() {
    ModalManager.close('wifi-modal');
}

async function submitWifiConfig() {
    const ssid = document.getElementById('wifi-ssid').value.trim();
    const password = document.getElementById('wifi-password').value.trim();

    if (!ssid) {
        showToast('SSID 不能为空', 'error');
        return;
    }
    if (!password) {
        showToast('密码不能为空', 'error');
        return;
    }

    try {
        // 立即关闭模态框
        closeWifiModal();

        addLogEntry(`正在连接 Wi-Fi (${ssid})...`, 'info');
        showToast('正在连接 Wi-Fi...', 'info');

        const workerId = selectedClusterWorker();
        await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/wifi', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices),
                        action: 'wifi', ssid, password}
                     : {devices: Array.from(state.selectedDevices), ssid, password});

        addLogEntry(`Wi-Fi 连接命令已发送 (${ssid})`, 'success');
    } catch (error) {
        addLogEntry('连接 WiFi 失败: ' + error.message, 'error');
    }
}

async function lockSelectedDevices(action) {
    if (!validateBootloaderDeviceSelection()) return;

    const buttonId = action === 'lock' ? 'btn-lock-device' : 'btn-unlock-device';
    const button = document.getElementById(buttonId);
    const actionText = action === 'lock' ? '锁定' : '解锁';

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        const granted = await requestElevatedAccess(`${actionText}设备 Bootloader`);
        if (!granted) return;
        addLogEntry(`正在${actionText}设备...`, 'info');
        const workerId = selectedClusterWorker();
        let result;
        if (workerId) {
            result = await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices: Array.from(state.selectedDevices),
                action: action === 'lock' ? 'bootloader_lock' : 'bootloader_unlock'
            });
        } else {
            result = await apiCall(`/api/devices/bootloader-${action}`, 'POST', {
                devices: Array.from(state.selectedDevices)
            });
        }
        const operationResults = result?.data?.results || result?.results || [];
        const failedResults = operationResults.filter(item => !item.success);
        if (result?.success === false || failedResults.length > 0) {
            const detail = failedResults.map(
                item => `${item.device}: ${item.error || item.output || '未知错误'}`
            ).join('; ');
            throw new Error(result?.error || detail || `设备${actionText}失败`);
        }
        addLogEntry(`设备${actionText}完成`, 'info');
        // 解锁/锁定后设备会重启并经历 fastboot→正常启动的状态转换，
        // 轮询刷新直到设备重新上线，避免界面停留在旧状态。
        loadDevices(true).catch(() => {});
        startBurnDeviceProtocolRefresh(Array.from(state.selectedDevices));
    } catch (error) {
        addLogEntry(`设备${actionText}失败: ${error.message}`, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

async function checkDeviceLockStatus() {
    if (!validateDeviceSelection()) return;

    const button = document.getElementById('btn-check-lock-status');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        const workerId = selectedClusterWorker();
        const result = await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/bootloader-status', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'bootloader_status'}
                     : {devices: Array.from(state.selectedDevices)});
        addLogEntry('设备锁定状态: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('获取锁定状态失败: ' + error.message, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

async function collectDeviceInfo() {
    if (!validateDeviceSelection()) return;

    const button = document.getElementById('btn-device-info');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        const workerId = selectedClusterWorker();
        const result = await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/info', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'get_properties'}
                     : {devices: Array.from(state.selectedDevices)});
        addLogEntry('设备信息: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('获取设备信息失败: ' + error.message, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}
