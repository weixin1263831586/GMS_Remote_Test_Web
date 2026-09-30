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
        countEl.textContent = `${_browserSuitesCache.length} 个套件`;
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
    const suite = _browserSuitesCache.find(s => s.tools_path === suitePath);
    if (!suite) {
        renderSuiteFileEmpty('测试套件不存在');
        return;
    }

    state.suiteBrowser.selectedSuitePath = suite.tools_path;
    state.suiteBrowser.currentPath = path || '';
    // Suite Browser is a browsing context — selecting a suite here
    // must NOT touch the global test-execution context at all.  Writing
    // suite_key/suite_path (and the test page's select) made the
    // execution page show Worker A with Worker B's suite path after a
    // browse.  Browsing state stays in state.suiteBrowser only; the
    // "use for test" flow remains the single way to build an execution
    // context.
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
    if (!_browserSuitesCache.length) {
        // 浏览页空缓存时走自己的加载器，避免把执行页的缓存
        // 隐式拉进浏览上下文。
        await loadSuitesForBrowserWorker(false);
    }
    if (!_browserSuitesCache.length) {
        showToast('未找到可搜索的测试套件', 'warning');
        return;
    }

    const resultsEl = $('suite-search-results');
    if (resultsEl) {
        resultsEl.innerHTML = '<div class="suite-empty" style="padding: 10px;">搜索中...</div>';
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
        const suite = _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
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

