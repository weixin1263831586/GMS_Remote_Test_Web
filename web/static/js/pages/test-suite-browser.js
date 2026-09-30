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
    if (!normalizedType || !Array.isArray(_browserSuitesCache) || _browserSuitesCache.length === 0) {
        return '';
    }

    const exact = _browserSuitesCache.find(suite => normalizeReportTestType(suite.test_type) === normalizedType);
    if (exact) return exact.tools_path || '';

    const pathMatch = _browserSuitesCache.find(suite => {
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
    // 缺省值归属本机 Worker；远端 Worker 分享链接始终包含 worker_id。
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
    const suite = _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    const workerId = suite?.worker_id
        || _browserSuitesWorkerId
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
        const selectedSuite = _browserSuitesCache.find(
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
        const selectedSuite = _browserSuitesCache.find(s => s.tools_path === state.suiteBrowser.selectedSuitePath);
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

// Suite Browser keeps its OWN cache.  It previously wrote the
// execution page's shared test-suites cache, so browsing
// Worker B's suites replaced the execution page's cached list (and the
// reverse).  A per-browser cache keyed by worker keeps both views intact.
let _browserSuitesCache = [];
let _browserSuitesWorkerId = '';

// Request generation guard for remote suite listing. A slow response
// for worker A must not overwrite the browser cache after the user has
// already switched to worker B.
let _browserSuitesRequestGeneration = 0;

async function loadSuitesForBrowserWorker(force = false) {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    // Suite Browser 是浏览上下文：只维护 page-local 的选择器，
    // 不改全局 Test Workspace（worker_id），否则浏览 Worker B
    // 的套件会顺手把测试执行上下文切到 B。
    syncSuiteWorkerSelectOnly(workerId);
    if (!force && _browserSuitesWorkerId === workerId && _browserSuitesCache.length > 0) {
        return _browserSuitesCache;
    }
    // The LOCAL branch used to call loadTestSuites(), which loads by
    // the GLOBAL EXECUTION worker — browsing "local" while the execution
    // target was B cached B's suites under the local label.  Fetch the
    // local suite list directly instead, independent of execution state.
    if (isLocalWorkspaceWorker(workerId)) {
        const generation = ++_browserSuitesRequestGeneration;
        const url = force ? '/api/test/suites?force_refresh=1' : '/api/test/suites';
        const response = await apiCall(url);
        if (generation !== _browserSuitesRequestGeneration) {
            // A newer request superseded this one; drop the stale result.
            return _browserSuitesCache;
        }
        _browserSuitesCache = (response?.suites || []).map(item => ({
            tools_path: item.tools_path,
            test_type: String(item.test_type || '').toLowerCase(),
            version: item.version,
            suite_key: item.suite_key || item.tools_path,
            worker_id: workerId
        }));
        _browserSuitesWorkerId = workerId;
        return _browserSuitesCache;
    }
    // force 时触发真正的 Worker 端套件扫描，再读回写后的清单。
    if (force) {
        try {
            await apiCall(
                `/api/cluster/workers/${encodeURIComponent(workerId)}/refresh?inventory=suites`,
                'POST');
        } catch (refreshError) {
            debugLog(`[suite-browser] worker refresh failed: ${refreshError.message}`);
        }
    }
    const generation = ++_browserSuitesRequestGeneration;
    const response = await fetch(`/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('加载 Worker 套件失败');
    const payload = await response.json();
    if (generation !== _browserSuitesRequestGeneration) {
        // Stale response for a previous worker target.
        return _browserSuitesCache;
    }
    _browserSuitesCache = (payload.suites || []).filter(item => item.available).map(item => ({
        tools_path: item.tools_path,
        test_type: String(item.test_type || '').toLowerCase(),
        version: item.version,
        suite_key: item.suite_key || item.tools_path,
        worker_id: workerId
    }));
    _browserSuitesWorkerId = workerId;
    return _browserSuitesCache;
}

async function switchSuiteWorker() {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    // Suite Browser 的 Worker 切换只影响浏览目标，不修改全局
    // Test Workspace / 测试运行状态——浏览 Worker B 套件不应把
    // 测试执行上下文切到 B。
    syncSuiteWorkerSelectOnly(workerId);
    clearSuiteBrowserSelection('正在加载 Worker 套件...');
    try {
        _browserSuitesCache = [];
        await loadSuitesForBrowserWorker(true);
        renderTestSuiteBrowserList();
        clearSuiteBrowserSelection(_browserSuitesCache.length ? '请选择左侧测试套件' : '此 Worker 暂无套件');
    } catch (error) {
        clearSuiteBrowserSelection(`加载失败: ${error.message}`);
    }
}

// 只同步 Suite Browser 自己的下拉框显示，不触碰全局 workspace/其他页面选择器。
function syncSuiteWorkerSelectOnly(workerId) {
    const select = document.getElementById('suite-worker-select');
    if (select && Array.from(select.options).some(option => option.value === workerId)) {
        select.value = workerId;
    }
}

window.switchSuiteWorker = switchSuiteWorker;

async function refreshTestSuiteBrowser(preferredSuiteRoot = '') {
    await loadSuitesForBrowserWorker(true);
    renderTestSuiteBrowserList();
    const normalizedPreferredRoot = (preferredSuiteRoot || '').replace(/\/+$/, '');
    if (normalizedPreferredRoot) {
        const preferredSuite = _browserSuitesCache.find(suite => {
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

    const selectedSuite = _browserSuitesCache.find(s => s.tools_path === suitePath);
    if (selectedSuite) {
        await selectTestSuiteForBrowser(suitePath, state.suiteBrowser.currentPath || '');
    } else {
        clearSuiteBrowserSelection('已选择的测试套件不存在');
    }
}

function filterTestSuiteBrowserList() {
    renderTestSuiteBrowserList();
}

