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
            const selectedSuite = _browserSuitesCache.find(item => item.tools_path === suitePath);
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
            const suite = _browserSuitesCache.find(item => item.tools_path === suitePath);
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
    const suite = _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/download' : '/api/test/suites/download';
    const preview = window.open(
        `${endpoint}?${buildReadablePathQuery(params)}`,
        '_blank',
        'noopener,noreferrer'
    );
    if (preview) preview.opener = null;
}

async function startRemoteSuiteExport(path, directory = false) {
    const suite = _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
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
    suite = suite || _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
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
    const suite = _browserSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
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
        const suite = _browserSuitesCache.find(item =>
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

// 用户列表由用户管理页的 loadUsersList()（shell.html）按需加载；
// /api/users/list 需要管理员提权，启动预取会在每个新标签页触发 403
// 和提权弹框，故此处不再提供 loadUsers 预取逻辑。


