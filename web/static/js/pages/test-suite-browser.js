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
let suiteBrowserInitSignal = null;
let suiteBrowserDirectoryRequestGeneration = 0;

async function initTestSuiteBrowserPage(signal = window.GmsPageLifecycle?.signalFor('test-suites')) {
    if (suiteBrowserInitPromise && !suiteBrowserInitSignal?.aborted) return suiteBrowserInitPromise;
    suiteBrowserInitSignal = signal;
    const pending = initTestSuiteBrowserPageOnce(signal);
    suiteBrowserInitPromise = pending;
    try {
        return await pending;
    } finally {
        if (suiteBrowserInitPromise === pending) suiteBrowserInitPromise = null;
    }
}

async function initTestSuiteBrowserPageOnce(signal) {
    const listEl = $('suite-browser-list');
    if (listEl && !suiteBrowserInitialized) {
        listEl.innerHTML = '<div class="suite-empty">正在加载...</div>';
    }

    await loadSuiteWorkerSelector(signal);
    signal?.throwIfAborted();
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

    await loadSuitesForBrowserWorker(false, signal);
    signal?.throwIfAborted();
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
let _suiteWorkerSelectorSignal = null;
async function loadSuiteWorkerSelector(signal = window.GmsPageLifecycle?.signalFor('test-suites')) {
    const select = $('suite-worker-select');
    if (!select || select.dataset.loaded === '1') return;
    if (_suiteWorkerSelectorPromise && !_suiteWorkerSelectorSignal?.aborted) return _suiteWorkerSelectorPromise;
    _suiteWorkerSelectorSignal = signal;
    select.disabled = true;
    _suiteWorkerSelectorPromise = (async () => { try {
        const response = await fetch('/api/cluster/workers', {cache: 'no-store', signal});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        await (window.GmsWorkspace?.ready || Promise.resolve());
        signal?.throwIfAborted();
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
        if (isApiRequestCancelled(error, signal)) throw error;
        debugLog('[Suites] Worker selector unavailable:', error);
        select.innerHTML = `<option value="${escapeHtml(workspaceLocalWorkerId())}">${escapeHtml(workspaceLocalWorkerId())}</option>`;
        select.disabled = false;
    } })();
    const pending = _suiteWorkerSelectorPromise;
    try {
        await pending;
    } finally {
        if (_suiteWorkerSelectorPromise === pending) _suiteWorkerSelectorPromise = null;
    }
}

// Suite Browser keeps its OWN cache, keyed by worker: browsing Worker
// B's suites must not replace the execution page's shared cached list
// (and the reverse).
let _browserSuitesCache = [];
let _browserSuitesWorkerId = '';

// Request generation guard for remote suite listing. A slow response
// for worker A must not overwrite the browser cache after the user has
// already switched to worker B.
let _browserSuitesRequestGeneration = 0;

async function loadSuitesForBrowserWorker(force = false, signal = window.GmsPageLifecycle?.signalFor('test-suites')) {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    // Suite Browser 是浏览上下文：只维护 page-local 的选择器，
    // 不改全局 Test Workspace（worker_id），否则浏览 Worker B
    // 的套件会顺手把测试执行上下文切到 B。
    syncSuiteWorkerSelectOnly(workerId);
    if (!force && _browserSuitesWorkerId === workerId && _browserSuitesCache.length > 0) {
        return _browserSuitesCache;
    }
    // The LOCAL branch fetches the local suite list directly — never via
    // loadTestSuites(), which loads by the GLOBAL EXECUTION worker (that
    // would cache B's suites under the local label).
    if (isLocalWorkspaceWorker(workerId)) {
        const generation = ++_browserSuitesRequestGeneration;
        const url = force ? '/api/test/suites?force_refresh=1' : '/api/test/suites';
        const response = await apiCall(url, 'GET', null, {signal});
        signal?.throwIfAborted();
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
                'POST', null, {signal});
        } catch (refreshError) {
            if (isApiRequestCancelled(refreshError, signal)) throw refreshError;
            debugLog(`[suite-browser] worker refresh failed: ${refreshError.message}`);
        }
    }
    const generation = ++_browserSuitesRequestGeneration;
    const response = await fetch(`/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`, {cache: 'no-store', signal});
    if (!response.ok) throw new Error('加载 Worker 套件失败');
    const payload = await response.json();
    signal?.throwIfAborted();
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

let suiteBrowserSearchRequestGeneration = 0;

// UI event handlers own their promises; awaited initialization still propagates
// cancellation to GmsPageLifecycle without running later UI updates.
async function runSuiteBrowserAction(action) {
    const signal = window.GmsPageLifecycle?.signalFor('test-suites');
    try {
        signal?.throwIfAborted();
        await action();
    } catch (error) {
        if (isApiRequestCancelled(error, signal)) return;
        console.error('[Suites] Browser action failed:', error);
        showToast(`套件浏览操作失败: ${error.message}`, 'error');
    }
}

function clearSuiteBrowserSelection(message) {
    state.suiteBrowser.selectedSuitePath = '';
    state.suiteBrowser.currentPath = '';
    state.suiteBrowser.highlightPath = '';
    state.suiteBrowser.suiteRoot = '';

    const titleEl = $('suite-browser-title');
    const pathEl = $('suite-browser-path');
    const breadcrumb = $('suite-browser-breadcrumb');
    if (titleEl) titleEl.textContent = '未选择测试套件';
    if (pathEl) { pathEl.textContent = ''; pathEl.title = ''; }
    const typeEl = $('suite-browser-type');
    if (typeEl) { typeEl.textContent = ''; delete typeEl.dataset.type; typeEl.hidden = true; }
    if (breadcrumb) {
        breadcrumb.replaceChildren();
        breadcrumb.title = '';
    }
    $('suite-browser-run-actions')?.replaceChildren();
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
    const suites = _browserSuitesCache.filter(suite => {
        const haystack = [
            suite.test_type,
            suite.version,
            suite.tools_path,
            suite.binary
        ].join(' ').toLowerCase();
        return !filterText || haystack.includes(filterText);
    });

    if (countEl) {
        countEl.textContent = String(_browserSuitesCache.length);
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
        badge.dataset.type = suiteTypeColorKey(displayType);

        const main = document.createElement('div');
        main.className = 'suite-suite-main';
        main.innerHTML = `
            <div class="suite-suite-name">${escapeHtml(getSuiteDisplayName(suite))}</div>
            <div class="suite-suite-path">${escapeHtml(getSuiteReleasePath(suite))}</div>
        `;

        row.append(badge, main);
        row.title = getSuiteReleasePath(suite);
        row.tabIndex = 0;
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', String(suite.tools_path === state.suiteBrowser.selectedSuitePath));
        row.addEventListener('click', () => runSuiteBrowserAction(() => selectTestSuiteForBrowser(suite.tools_path)));
        row.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            runSuiteBrowserAction(() => selectTestSuiteForBrowser(suite.tools_path));
        });
        listEl.appendChild(row);
    });
}

async function selectTestSuiteForBrowser(suitePath, path = '', options = {}) {
    const suite = _browserSuitesCache.find(s => s.tools_path === suitePath);
    if (!suite) {
        renderSuiteFileEmpty('测试套件不存在');
        return;
    }

    if (state.suiteBrowser.selectedSuitePath !== suite.tools_path) {
        state.suiteBrowser.suiteRoot = '';
    }
    state.suiteBrowser.selectedSuitePath = suite.tools_path;
    state.suiteBrowser.currentPath = path || '';
    // Suite Browser 是纯浏览上下文：这里选择套件不得写入全局测试执行
    // 上下文（suite_key/suite_path 与测试页 select）——否则浏览 Worker B
    // 后执行页会带着 B 的套件路径显示 Worker A。浏览状态只留在
    // state.suiteBrowser；「用于测试」流程才是唯一构建执行上下文的入口。
    if (!options.preserveHighlight) {
        state.suiteBrowser.highlightPath = '';
    }
    if (!options.preserveSearchResults) {
        clearSuiteSearchResults();
    }

    const titleEl = $('suite-browser-title');
    const pathEl = $('suite-browser-path');
    let displayType = suite.test_type || '';
    // 将 cts-verifier 显示为 CTS-V
    if (displayType === 'cts-verifier') displayType = 'cts-v';
    if (titleEl) titleEl.textContent = getSuiteDisplayName(suite);
    const fullPath = getSuiteRootFromToolsPath(suite.tools_path);
    if (pathEl) { pathEl.textContent = fullPath; pathEl.title = fullPath; }
    const typeEl = $('suite-browser-type');
    if (typeEl) { typeEl.textContent = displayType.toUpperCase(); typeEl.dataset.type = suiteTypeColorKey(displayType); typeEl.hidden = false; }

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
    ++suiteBrowserSearchRequestGeneration;
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
        resultsEl.innerHTML = `<div class="suite-empty ui-empty-state ui-empty-state--compact">未找到: ${escapeHtml(query)}</div>`;
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
        row.addEventListener('click', () => runSuiteBrowserAction(() => locateSuiteSearchResult(item)));
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
    const generation = ++suiteBrowserSearchRequestGeneration;
    const input = $('suite-file-search');
    const query = (input?.value || '').trim();
    if (!query) {
        showToast('请输入搜索关键词', 'warning');
        return;
    }
    if (!_browserSuitesCache.length) {
        // 浏览页空缓存时走自己的加载器，避免把执行页的缓存
        // 隐式拉进浏览上下文。
        await loadSuitesForBrowserWorker(false);
        if (generation !== suiteBrowserSearchRequestGeneration) return;
    }
    if (!_browserSuitesCache.length) {
        showToast('未找到可搜索的测试套件', 'warning');
        return;
    }

    const resultsEl = $('suite-search-results');
    if (resultsEl) {
        resultsEl.innerHTML = '<div class="suite-empty ui-empty-state ui-empty-state--compact">搜索中...</div>';
        resultsEl.style.display = 'block';
    }

    try {
        const selectedSuite = _browserSuitesCache.find(suite => suite.tools_path === state.suiteBrowser.selectedSuitePath);
        const orderedSuites = [
            ...(selectedSuite ? [selectedSuite] : []),
            ..._browserSuitesCache.filter(suite => !selectedSuite || suite.tools_path !== selectedSuite.tools_path)
        ];
        let items = [];
        for (const suite of orderedSuites) {
            items = await searchSuiteFilesInSuite(suite, query, 30);
            if (generation !== suiteBrowserSearchRequestGeneration) return;
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
        if (generation !== suiteBrowserSearchRequestGeneration) return;
        renderSuiteSearchResults([], query);
        showToast('搜索失败: ' + error.message, 'error');
    }
}

async function loadSuiteBrowserDirectory(path = '') {
    const signal = window.GmsPageLifecycle?.signalFor('test-suites');
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
        const suite = _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
        if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
        const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? '/api/cluster/suites/files' : '/api/test/suites/files';
        const result = await apiCall(`${endpoint}?${params.toString()}`, 'GET', null, {signal});
        signal?.throwIfAborted();
        if (requestGeneration !== suiteBrowserDirectoryRequestGeneration
                || state.suiteBrowser.selectedSuitePath !== requestedSuitePath) return;
        const data = result.data || {};
        state.suiteBrowser.currentPath = data.path || '';
        // 保留解析后的套件根绝对路径，供"报告分析"等需要绝对路径的操作使用。
        state.suiteBrowser.suiteRoot = data.suite_root || '';
        renderSuiteBreadcrumb(state.suiteBrowser.currentPath);
        renderSuiteFiles(data.items || []);
    } catch (error) {
        if (isApiRequestCancelled(error, signal)) throw error;
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
