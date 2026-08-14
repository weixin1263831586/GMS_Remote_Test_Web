// ==================== Test Reports ====================
let reportsRefreshInterval = null;
let currentUserFilter = false;  // 当前是否只显示本用户报告
let reportsWorkersLoaded = false;
let reportsWorkersPromise = null;
let reportsNextCursor = '';
let reportsLoadedItems = [];
let reportsLoadedPages = 1;
let reportsRequestGeneration = 0;
let reportsHasLoaded = false;
let reportsLastLoadedAt = 0;
let reportsLastQueryKey = '';
let reportsRefreshInFlight = false;
const REPORTS_REENTRY_CACHE_MS = 10000;
const reportsRequests = new Map();

async function loadReportWorkers() {
    if (reportsWorkersLoaded) return;
    if (reportsWorkersPromise) return reportsWorkersPromise;
    reportsWorkersPromise = loadReportWorkersOnce().finally(() => {
        reportsWorkersPromise = null;
    });
    return reportsWorkersPromise;
}

async function loadReportWorkersOnce() {
    const select = document.getElementById('reports-worker-filter');
    if (!select) return;
    try {
        const snapshot = window.clusterWorkersSnapshot;
        let workers = snapshot
            && Date.now() - Number(snapshot.loadedAt || 0) < REPORTS_REENTRY_CACHE_MS
            ? snapshot.workers
            : null;
        if (!Array.isArray(workers)) {
            const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
            const payload = await response.json();
            workers = Array.isArray(payload.workers) ? payload.workers : [];
            window.clusterWorkersSnapshot = {workers, loadedAt: Date.now()};
        }
        await (window.GmsWorkspace?.ready || Promise.resolve());
        const workspace = window.GmsWorkspace?.get?.() || {};
        const previous = workspace.worker_id || '';
        const localWorkerId = workspaceLocalWorkerId();
        workers = [...workers].sort((left, right) =>
            Number(right.id === localWorkerId) - Number(left.id === localWorkerId)
        );
        select.innerHTML = workers.map(worker =>
            `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.id)}</option>`
        ).join('') + '<option value="">全部 Worker</option>';
        if (Array.from(select.options).some(option => option.value === previous)) select.value = previous;
        select.dataset.workersLoaded = 'true';
        reportsWorkersLoaded = true;
    } catch (error) {
        debugLog('[Reports] Worker filter unavailable:', error);
    }
}

function requestReportsData(url) {
    if (reportsRequests.has(url)) return reportsRequests.get(url);
    const request = fetch(url, {cache: 'no-store'}).then(async response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
    }).finally(() => reportsRequests.delete(url));
    reportsRequests.set(url, request);
    return request;
}

async function preloadTestReports(userOnly = false) {
    if (reportsHasLoaded) return;
    await (window.GmsWorkspace?.ready || Promise.resolve());
    const workersPromise = loadReportWorkers();
    const url = reportsListUrl(userOnly);
    const [data] = await Promise.all([
        requestReportsData(url),
        workersPromise,
    ]);
    if (reportsHasLoaded) return;
    reportsLoadedItems = Array.isArray(data.reports) ? data.reports : [];
    reportsNextCursor = data.next_cursor || '';
    reportsLoadedPages = 1;
    reportsHasLoaded = true;
    reportsLastLoadedAt = Date.now();
    reportsLastQueryKey = url;
}
window.preloadTestReports = preloadTestReports;

async function switchReportsWorker() {
    const workerId = document.getElementById('reports-worker-filter')?.value || '';
    if (workerId) {
        window.GmsWorkspace?.update({
            scope_mode: isLocalWorkspaceWorker(workerId) ? window.GmsWorkspace.get().scope_mode : 'cluster',
            worker_id: workerId,
            origin_page: 'reports'
        }, {source: 'reports'});
        syncWorkspaceWorkerSelectors(workerId);
    }
    await loadTestReports(currentUserFilter, false, true);
}

window.switchReportsWorker = switchReportsWorker;

function reportsListUrl(userOnly, cursor = '') {
    const params = new URLSearchParams();
    if (userOnly) params.set('user_only', 'true');
    const workerSelect = document.getElementById('reports-worker-filter');
    const workerId = workerSelect?.dataset.workersLoaded === 'true'
        ? workerSelect.value
        : (window.GmsWorkspace?.get?.().worker_id || '');
    if (workerId) params.set('worker_id', workerId);
    if (cursor) params.set('cursor', cursor);
    const query = params.toString();
    return `/api/reports/list${query ? `?${query}` : ''}`;
}

// 离开页面时清理报告轮询定时器。
function cleanupReportsPolling() {
    if (reportsRefreshInterval) {
        clearInterval(reportsRefreshInterval);
        reportsRefreshInterval = null;
    }
}

function renderReportsPagination() {
    const wrapper = document.getElementById('reports-load-more-wrapper');
    if (!wrapper) return;
    wrapper.innerHTML = '';
    if (reportsNextCursor) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-xs';
        button.textContent = `加载更多（已显示 ${reportsLoadedItems.length} 条）`;
        button.addEventListener('click', () => loadTestReports(currentUserFilter, true));
        wrapper.appendChild(button);
    } else if (reportsLoadedItems.length) {
        wrapper.textContent = `已显示全部 ${reportsLoadedItems.length} 条报告`;
        wrapper.style.color = 'var(--text-secondary)';
        wrapper.style.fontSize = '12px';
    }
}

async function loadTestReports(userOnly = false, append = false, force = false) {
    const requestGeneration = ++reportsRequestGeneration;
    const tbody = document.getElementById('reports-table-body');
    const hadRenderedReports = Boolean(reportsHasLoaded && tbody?.children.length);
    if (tbody) tbody.setAttribute('aria-busy', 'true');
    try {
        await (window.GmsWorkspace?.ready || Promise.resolve());
        const workersPromise = loadReportWorkers();
        const queryKey = reportsListUrl(userOnly);
        // 筛选或 Worker 已改变时，旧游标不再属于当前查询。刷新失败后
        // 仍可保留旧表格，但下一次“加载更多”必须退化为新查询的第一页。
        if (append && reportsLastQueryKey !== queryKey) {
            append = false;
            force = true;
        }
        const url = reportsListUrl(userOnly, append ? reportsNextCursor : '');
        const hasCachedQuery = !append && !force && reportsHasLoaded
            && reportsLastQueryKey === queryKey;
        if (hasCachedQuery) {
            await workersPromise;
            displayTestReports(reportsLoadedItems);
            renderReportsPagination();
            if (Date.now() - reportsLastLoadedAt < REPORTS_REENTRY_CACHE_MS) return;
        }
        if (!append) {
            if (tbody && !hasCachedQuery && !hadRenderedReports) {
                tbody.innerHTML = '<tr><td colspan="10" style="padding:40px;text-align:center;color:var(--text-secondary);">正在加载报告...</td></tr>';
            }
        }
        const [data] = await Promise.all([
            requestReportsData(url),
            workersPromise,
        ]);
        if (requestGeneration !== reportsRequestGeneration) return;

        const page = Array.isArray(data.reports) ? data.reports : [];
        if (append) {
            const seen = new Set(reportsLoadedItems.map(report => report.report_id || report.timestamp));
            reportsLoadedItems = reportsLoadedItems.concat(
                page.filter(report => !seen.has(report.report_id || report.timestamp)),
            );
            reportsLoadedPages += 1;
        } else {
            reportsLoadedItems = page;
            reportsLoadedPages = 1;
        }
        reportsNextCursor = data.next_cursor || '';
        reportsHasLoaded = true;
        reportsLastLoadedAt = Date.now();
        reportsLastQueryKey = queryKey;
        displayTestReports(reportsLoadedItems);
        if (tbody) tbody.setAttribute('aria-busy', 'false');
        renderReportsPagination();

        // 启动自动刷新（每15秒）带变更检测
        if (currentPage === 'reports' && !reportsRefreshInterval) {
            let lastReportsHash = null;

            reportsRefreshInterval = setInterval(async () => {
                if (currentPage === 'reports' && !document.hidden && !reportsRefreshInFlight) {
                    reportsRefreshInFlight = true;
                    try {
                        if (reportsLoadedPages > 1) return;
                        const url = reportsListUrl(currentUserFilter);
                        const data = await requestReportsData(url);

                        // 计算报告列表的哈希值以检测变更
                        const reportsHash = JSON.stringify(data.reports);

                        // 只有在报告列表发生变化时才更新DOM
                        if (reportsHash !== lastReportsHash) {
                            lastReportsHash = reportsHash;
                            reportsLoadedItems = Array.isArray(data.reports) ? data.reports : [];
                            reportsNextCursor = data.next_cursor || '';
                            reportsHasLoaded = true;
                            reportsLastLoadedAt = Date.now();
                            reportsLastQueryKey = url;
                            displayTestReports(reportsLoadedItems);
                            renderReportsPagination();
                        }
                    } catch (error) {
                        console.error('[Reports] Error refreshing reports:', error);
                    } finally {
                        reportsRefreshInFlight = false;
                    }
                }
            }, REPORTS_REFRESH_INTERVAL);
        }
    } catch (e) {
        if (requestGeneration !== reportsRequestGeneration) return;
        console.error('[Reports] Error loading reports:', e);
        if (hadRenderedReports) {
            showToast('报告列表刷新失败: ' + e.message, 'error');
        } else if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="10" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                        加载失败
                    </td>
                </tr>
            `;
        }
    } finally {
        if (requestGeneration === reportsRequestGeneration && tbody) {
            tbody.setAttribute('aria-busy', 'false');
        }
    }
}

window.loadMoreTestReports = () => loadTestReports(currentUserFilter, true);

function toggleUserReports() {
    const checkbox = document.getElementById('filter-user-checkbox');
    currentUserFilter = checkbox.checked;

    // 重新加载报告列表
    loadTestReports(currentUserFilter, false, true);
}

function displayTestReports(reports) {
    const tbody = document.getElementById('reports-table-body');
    if (!tbody) return;

    if (reports.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="10" style="padding: 60px 40px; text-align: center; color: var(--text-secondary);">
                    暂无测试报告
                </td>
            </tr>
        `;
        return;
    }

    // 使用 DocumentFragment 提高渲染性能
    const fragment = document.createDocumentFragment();

    // 测试类型颜色映射（定义在循环外，避免重复创建）
    const typeColors = {
        'CTS': '#3B82F6',
        'GTS': '#10B981',
        'STS': '#F59E0B',
        'VTS': '#8B5CF6',
        'XTS': '#EC4899',
    };

    reports.forEach(report => {
        const testType = String(report.test_type || '-');
        const displayClient = report.display_client_id || report.client_name || report.user || report.client_id || '-';
        const passCount = report.pass !== undefined ? String(report.pass) : '-';
        const failCount = report.fail !== undefined ? String(report.fail) : '-';
        const totalCount = report.total !== undefined ? String(report.total) : '-';
        const passRate = report.total > 0 ? ((report.pass / report.total) * 100).toFixed(1) + '%' : '-';
        const suiteName = getReportSuiteDisplayName(report);
        const workerId = report.worker_id || workspaceLocalWorkerId() || '-';

        const passRateStyle = report.total > 0 ? (report.pass / report.total >= 0.9 ? 'color: var(--success-color);' : 'color: var(--warning-color);') : '';

        const typeColor = typeColors[testType] || 'var(--text-secondary)';

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-color)';
        tr.dataset.timestamp = report.timestamp;
        tr.dataset.testType = report.test_type || '';
        tr.dataset.suitePath = report.suite_path || '';
        tr.dataset.workerId = report.worker_id || workspaceLocalWorkerId();
        tr.dataset.clusterJobId = report.cluster_job_id || '';
        tr.dataset.attemptId = report.attempt_id || '';
        tr.dataset.automationRunId = report.automation_run_id || '';
        tr.dataset.reportId = report.report_id || report.timestamp || '';
        tr.dataset.artifactId = report.artifact_id || '';
        tr.dataset.sourceTimestamp = report.source_timestamp || '';
        tr.dataset.reportName = report.report_name || '';

        tr.innerHTML = `
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">${escapeHtml(displayClient)}</td>
            <td style="padding: 4px 6px; text-align: center; font-weight: 700; font-size: 13px; color: ${typeColor};">${escapeHtml(testType)}</td>
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px; color: var(--text-primary);">${escapeHtml(suiteName)}</td>
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">${escapeHtml(workerId)}</td>
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;" title="${escapeIconAttr(report.timestamp || '')}">${escapeHtml(report.report_name || report.timestamp || '-')}</td>
            <td style="padding: 4px 6px; text-align: center; color: var(--success-color); font-weight: 600; font-size: 13px;">${escapeHtml(passCount)}</td>
            <td style="padding: 4px 6px; text-align: center; color: var(--danger-color); font-weight: 600; font-size: 13px;">${escapeHtml(failCount)}</td>
            <td style="padding: 4px 6px; text-align: center; font-weight: 600; font-size: 13px;">${escapeHtml(totalCount)}</td>
            <td style="padding: 4px 6px; text-align: center; font-weight: 600; font-size: 13px; ${passRateStyle}">${escapeHtml(passRate)}</td>
            <td style="padding: 4px 6px; text-align: center;">
                <button class="btn-xxs" data-action="analyze" style="margin: 2px; font-size: 12px;">📈 分析</button>
                <button class="btn-xxs" data-action="retry" style="background: var(--primary-color); margin: 2px; font-size: 12px;">🔄 retry</button>
                <button class="btn-xxs" data-action="results" style="background: var(--info-color); margin: 2px; font-size: 12px;">results</button>
                <button class="btn-xxs" data-action="logs" style="background: var(--warning-color); margin: 2px; font-size: 12px;">logs</button>
                <button class="btn-xxs" data-action="download" style="background: var(--success-color); margin: 2px; font-size: 12px;">⬇️ 下载</button>
                <button class="btn-xxs" data-action="delete" style="background: var(--danger-color); margin: 2px; font-size: 12px;">🗑️ 删除</button>
            </td>
        `;

        fragment.appendChild(tr);
    });

    tbody.innerHTML = '';
    tbody.appendChild(fragment);

    // 使用事件委托处理按钮点击（提高性能）
    tbody.removeEventListener('click', handleReportAction);
    tbody.addEventListener('click', handleReportAction);
}

// 事件委托处理函数
function handleReportAction(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;

    const action = button.dataset.action;
    const tr = button.closest('tr');
    if (!tr) return;

    const timestamp = tr.dataset.timestamp;
    const testType = tr.dataset.testType;
    const suitePath = tr.dataset.suitePath;
    const reportContext = {
        worker_id: tr.dataset.workerId || workspaceLocalWorkerId(),
        cluster_job_id: tr.dataset.clusterJobId || '',
        attempt_id: tr.dataset.attemptId || '',
        automation_run_id: tr.dataset.automationRunId || '',
        report_id: tr.dataset.reportId || timestamp,
        report_timestamp: timestamp,
        artifact_id: tr.dataset.artifactId || '',
        source_timestamp: tr.dataset.sourceTimestamp || '',
        report_name: tr.dataset.reportName || '',
        suite_path: suitePath || '',
        origin_page: 'reports'
    };
    window.GmsWorkspace?.update(reportContext, {source: 'reports'});

    event.stopPropagation();

    switch (action) {
        case 'analyze':
            analyzeReport(timestamp, reportContext.report_id);
            break;
        case 'retry':
            retryReportWithSuite(reportContext.report_name || timestamp, testType, suitePath, reportContext);
            break;
        case 'download':
            downloadReport(timestamp, reportContext.report_id, reportContext.report_name);
            break;
        case 'results':
            openReportSuiteDirectory(timestamp, suitePath, testType, 'results', reportContext);
            break;
        case 'logs':
            openReportSuiteDirectory(timestamp, suitePath, testType, 'logs', reportContext);
            break;
        case 'delete':
            deleteReport(timestamp, reportContext.report_id, reportContext.report_name);
            break;
    }
}

async function openReportSuiteDirectory(timestamp, suitePath, testType, kind, reportContext = {}, targetFile = '') {
    if (!timestamp || !['results', 'logs'].includes(kind)) {
        showToast('报告目录参数无效', 'error');
        return;
    }

    const workerId = reportContext.worker_id || workspaceLocalWorkerId();
    window.GmsWorkspace?.update({...reportContext, worker_id: workerId}, {source: 'reports'});
    const suiteWorker = document.getElementById('suite-worker-select');
    if (suiteWorker) {
        await loadSuiteWorkerSelector();
        if (Array.from(suiteWorker.options).some(option => option.value === workerId)) suiteWorker.value = workerId;
        await loadSuitesForBrowserWorker(false);
    } else if (!testSuitesCache.length || testSuitesWorkerId !== workerId) {
        await loadTestSuites();
    }

    const resolvedSuitePath = findSuitePathForReport(testType, suitePath);
    if (!resolvedSuitePath) {
        showToast('未找到该报告对应的测试套件路径', 'warning');
        return;
    }

    // 旧集群报告可能把 start_display（"Fri Jul 31 ..."）误存到
    // source_timestamp。只接受 Tradefed 目录格式，并优先使用报告名恢复。
    const folderName = tradefedResultFolderName(reportContext.report_name)
        || tradefedResultFolderName(reportContext.source_timestamp)
        || tradefedResultFolderName(timestamp);
    if (!folderName) {
        showToast('报告缺少有效的 Tradefed 结果目录，请刷新报告列表后重试', 'error');
        return;
    }
    const targetPath = `${kind}/${folderName}`;
    switchPage('test-suites', null);
    const filePath = targetFile ? `${targetPath}/${targetFile}` : '';
    if (filePath) state.suiteBrowser.highlightPath = filePath;
    await selectTestSuiteForBrowser(resolvedSuitePath, targetPath, {
        preserveHighlight: Boolean(filePath)
    });
    if (filePath) {
        setSuiteBrowserHighlightedPath(filePath);
        showToast(`已定位到 ${filePath}`, 'success');
    }
}

async function deleteReport(timestamp, reportId = '', reportName = '') {
    const displayName = reportName || timestamp;
    const confirmed = await showConfirmDialog(
        '删除报告',
        `确定要删除报告 ${displayName} 吗？此操作不可恢复。`
    );

    if (!confirmed) return;

    try {
        const identity = reportId
            ? `report_id=${encodeURIComponent(reportId)}`
            : `timestamp=${encodeURIComponent(timestamp)}`;
        const response = await fetch(`/api/reports/delete?${identity}`, {
            method: 'DELETE'
        });

        const result = await response.json();

        if (result.success) {
            showToast('报告已删除', 'success');
            // 刷新报告列表
            await loadTestReports(currentUserFilter, false, true);
        } else {
            showToast('删除失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (error) {
        console.error('Delete report error:', error);
        showToast('删除失败: ' + error.message, 'error');
    }
}


async function retryReport(timestamp, testType) {
    try {
        // 先切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            debugLog(`[Retry] 开始填充数据, timestamp=${timestamp}, testType=${testType}`);

            // 填入测试报告名称（字段ID是 retry-result）
            const reportNameInput = document.getElementById('retry-result');
            if (reportNameInput) {
                reportNameInput.value = timestamp;
                debugLog(`[Retry] 已填入报告名称: ${timestamp}`);
            } else {
                console.error('[Retry] 未找到 retry-result 元素');
            }

            // 互斥：填入报告时清空模块和用例
            enforceFieldExclusion('retry');

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect) {
                if (testType) {
                    testTypeSelect.value = testType;
                    debugLog(`[Retry] 已设置测试类型: ${testType}, 当前值: ${testTypeSelect.value}`);
                } else {
                    console.warn('[Retry] testType 为空');
                }
            } else {
                console.error('[Retry] 未找到 test-type 元素');
            }

            // 根据测试类型填入测试套件路径
            const suitePathInput = document.getElementById('test-suite');
            if (suitePathInput) {
                // 根据测试类型设置默认路径
                const suitePaths = {
                    'CTS': 'android-cts',
                    'GSI': 'android-gsi',
                    'GTS': 'android-gts',
                    'STS': 'android-sts',
                    'VTS': 'android-vts',
                    'APTS': 'android-apts'
                };

                // 如果有匹配的测试类型，使用对应的路径
                if (testType && suitePaths[testType]) {
                    suitePathInput.value = suitePaths[testType];
                    debugLog(`[Retry] 已设置测试套件路径: ${suitePaths[testType]}, 当前值: ${suitePathInput.value}`);
                } else {
                    console.warn(`[Retry] testType=${testType} 没有对应的套件路径`);
                }
            } else {
                console.error('[Retry] 未找到 test-suite 元素');
            }

            // 打印所有相关元素的值以便调试
            debugLog('[Retry] 当前字段值:', {
                reportName: document.getElementById('retry-result')?.value,
                testType: document.getElementById('test-type')?.value,
                suitePath: document.getElementById('test-suite')?.value
            });
        }, 200);

        showToast(`已填入报告名称: ${timestamp}${testType ? ' (类型: ' + testType + ')' : ''}`, 'success');

        // 可选：自动开始测试（如果需要的话，取消下面的注释）
        // setTimeout(() => {
        //     startTest();
        // }, 500);
    } catch (error) {
        console.error('Retry report error:', error);
        showToast('操作失败: ' + error.message, 'error');
    }
}

async function retryReportWithSuite(timestamp, testType, suitePath, reportContext = {}) {
    try {
        const workerId = reportContext.worker_id || workspaceLocalWorkerId();
        const workerSelect = document.getElementById('cluster-worker');
        if (workerSelect) {
            await loadClusterWorkers();
            if (Array.from(workerSelect.options).some(option => option.value === workerId)) {
                workerSelect.value = workerId;
                await switchTestWorker();
            }
        }
        // 先切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            debugLog(`[Retry] 开始填充数据, timestamp=${timestamp}, testType=${testType}, suitePath=${suitePath}`);

            // 填入测试报告名称（字段ID是 retry-result）
            const reportNameInput = document.getElementById('retry-result');
            if (reportNameInput) {
                reportNameInput.value = timestamp;
                debugLog(`[Retry] 已填入报告名称: ${timestamp}`);
            } else {
                console.error('[Retry] 未找到 retry-result 元素');
            }

            // 互斥：填入报告时清空模块和用例
            enforceFieldExclusion('retry');

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect) {
                if (testType) {
                    testTypeSelect.value = testType;
                    debugLog(`[Retry] 已设置测试类型: ${testType}, 当前值: ${testTypeSelect.value}`);
                } else {
                    console.warn('[Retry] testType 为空');
                }
            } else {
                console.error('[Retry] 未找到 test-type 元素');
            }

            // 填入测试套件路径（优先使用原始路径，否则使用默认路径）
            const suitePathInput = document.getElementById('test-suite');
            if (suitePathInput) {
                if (suitePath && suitePath !== 'null' && suitePath !== '') {
                    // 使用报告中的原始测试套件路径
                    suitePathInput.value = suitePath;
                    debugLog(`[Retry] 已设置测试套件路径(原始): ${suitePath}, 当前值: ${suitePathInput.value}`);
                } else {
                    // 根据测试类型设置默认路径
                    const suitePaths = {
                        'CTS': 'android-cts',
                        'GSI': 'android-gsi',
                        'GTS': 'android-gts',
                        'STS': 'android-sts',
                        'VTS': 'android-vts',
                        'APTS': 'android-apts'
                    };

                    if (testType && suitePaths[testType]) {
                        suitePathInput.value = suitePaths[testType];
                        debugLog(`[Retry] 已设置测试套件路径(默认): ${suitePaths[testType]}, 当前值: ${suitePathInput.value}`);
                    } else {
                        console.warn(`[Retry] testType=${testType} 没有对应的套件路径`);
                    }
                }
            } else {
                console.error('[Retry] 未找到 test-suite 元素');
            }

            // 打印所有相关元素的值以便调试
            debugLog('[Retry] 当前字段值:', {
                reportName: document.getElementById('retry-result')?.value,
                testType: document.getElementById('test-type')?.value,
                suitePath: document.getElementById('test-suite')?.value
            });
        }, 200);

        showToast(`已填入报告名称: ${timestamp}${testType ? ' (类型: ' + testType + ')' : ''}`, 'success');

        // 可选：自动开始测试（如果需要的话，取消下面的注释）
        // setTimeout(() => {
        //     startTest();
        // }, 500);
    } catch (error) {
        console.error('Retry report error:', error);
        showToast('操作失败: ' + error.message, 'error');
    }
}

async function downloadReport(timestamp, reportId = '', reportName = '') {
    try {
        debugLog('[downloadReport] Starting download for timestamp:', timestamp);
        await downloadReportAsZip(timestamp, reportId, reportName);
    } catch (error) {
        console.error('Download report error:', error);
        notifyOperationResult('报告下载失败', error.message, 'error', 'report-download', { timestamp });
    }
}

// 回退方案：下载为 ZIP
async function downloadReportAsZip(timestamp, reportId = '', reportName = '') {
    try {
        const identity = reportId
            ? `report_id=${encodeURIComponent(reportId)}`
            : `report_timestamp=${encodeURIComponent(timestamp)}`;
        const response = await fetch(`/api/reports/download?${identity}&download=true`);

        if (!response.ok) {
            let errorMsg = `HTTP ${response.status}`;
            try {
                const errorData = await response.json();
                errorMsg = errorData.error || errorMsg;
            } catch (e) {
                // 如果无法解析 JSON，使用默认错误消息
            }
            console.error('Download failed:', response.status, errorMsg);
            notifyOperationResult('报告下载失败', errorMsg, 'error', 'report-download', { timestamp });
            return;
        }

        // 检查 Content-Type
        const contentType = response.headers.get('Content-Type');
        debugLog('Response Content-Type:', contentType);

        if (contentType && contentType.includes('application/json')) {
            // 如果返回的是 JSON 而不是文件，说明有错误
            const errorData = await response.json();
            console.error('Server returned error:', errorData);
            notifyOperationResult('报告下载失败', errorData.error || '服务器错误', 'error', 'report-download', { timestamp });
            return;
        }

        // 获取文件名
        const contentDisposition = response.headers.get('Content-Disposition');
        let filename = `${reportName || timestamp}.zip`;

        if (contentDisposition) {
            const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
            if (filenameMatch && filenameMatch[1] && typeof filenameMatch[1] === 'string') {
                filename = filenameMatch[1].replace(/['"]/g, '');
            }
        }
        debugLog('Downloading file as:', filename);

        // 下载文件
        const blob = await response.blob();
        debugLog('Blob size:', blob.size, 'bytes');

        if (blob.size === 0) {
            notifyOperationResult('报告下载失败', '文件为空', 'error', 'report-download', { timestamp });
            return;
        }

        const url = window.URL.createObjectURL(blob);
        triggerDownload(url, filename, true);

        notifyOperationResult('报告下载完成', `ZIP 下载成功：${filename}`, 'success', 'report-download', { timestamp, filename });
    } catch (error) {
        console.error('Download report as ZIP error:', error);
        notifyOperationResult('报告下载失败', error.message, 'error', 'report-download', { timestamp });
    }
}

function openReportAnalysis(timestamp) {
    // 切换到报告分析页面
    const sidebarItem = document.querySelector('[data-page="report-analysis"]');
    if (sidebarItem) {
        sidebarItem.click();
    }

    // 等待页面切换完成后，自动加载并分析报告
    setTimeout(() => {
        analyzeReport(timestamp);
    }, 300);
}

async function analyzeReport(timestamp, reportId = '') {
    try {
        // 从报告列表行中提前回写 Worker 上下文，确保分析结果跳转和后续操作
        // 能正确继承来源 Worker / Cluster Job 信息。
        const reportRow = document.querySelector(`tr[data-timestamp="${timestamp}"]`);
        if (reportRow) {
            const reportContext = {
                worker_id: reportRow.dataset.workerId || workspaceWorkerId(),
                cluster_job_id: reportRow.dataset.clusterJobId || '',
                attempt_id: reportRow.dataset.attemptId || '',
                automation_run_id: reportRow.dataset.automationRunId || '',
                report_id: reportRow.dataset.reportId || timestamp,
                report_timestamp: timestamp,
                artifact_id: reportRow.dataset.artifactId || '',
                suite_path: reportRow.dataset.suitePath || '',
                origin_page: 'report-analysis'
            };
            window.GmsWorkspace?.update(reportContext, {source: 'report-analysis'});
        } else {
            window.GmsWorkspace?.update({report_timestamp: timestamp, origin_page: 'report-analysis'},
                {source: 'report-analysis'});
        }

        // 切换到报告分析页面
        const sidebarItem = document.querySelector('[data-page="report-analysis"]');
        if (sidebarItem) {
            sidebarItem.click();
        }

        // 等待页面切换完成后，自动加载并分析报告
        setTimeout(async () => {
            showToast('正在分析报告...', 'info');

            const formData = createFormData(AnalysisMode.SAVED, {
                report_timestamp: timestamp,
                report_id: reportId || reportRow?.dataset.reportId || ''
            });
            const resp = await fetch('/api/reports/analyze', {
                method: 'POST',
                body: formData
            });
            const data = await resp.json();

            if (!data.success) {
                notifyOperationResult('报告分析失败', data.error || '未知错误', 'error', 'report-analysis', { timestamp });
                return;
            }

            // 使用与手动上传相同的显示函数，保持布局一致
            displayReportAnalysis(data.data);
            notifyOperationResult(
                '报告分析完成',
                data.data?.report_name || data.data?.test_result?.test_name || '报告分析完成',
                'success',
                'report-analysis',
                { timestamp }
            );
        }, 300);
    } catch (e) {
        console.error('[Reports] Error analyzing report:', e);
        notifyOperationResult('报告分析失败', e.message, 'error', 'report-analysis', { timestamp });
    }
}


// ==================== 安装指南弹窗 ====================

function showInstallGuide(title, guide) {
    ModalManager.open('install-guide-modal');
}

function closeInstallGuide() {
    const modal = document.getElementById('install-guide-modal');
    if (modal) {
        // 隐藏进度条
        const progressDiv = document.getElementById('install-progress');
        if (progressDiv) {
            progressDiv.style.display = 'none';
        }
    }
    ModalManager.close('install-guide-modal');
}

async function autoInstallUsbipd() {
    const progressDiv = document.getElementById('install-progress');
    const progressBar = document.getElementById('install-progress-bar');
    const statusText = document.getElementById('install-status');

    // 显示进度条
    progressDiv.style.display = 'block';

    try {
        // 更新状态：准备安装
        progressBar.style.width = '10%';
        statusText.textContent = '📡 正在连接 Windows 主机...';

        // 调用后端安装 API
        const result = await apiCall('/api/usbip/install', 'POST', {});

        // 更新状态：安装中
        progressBar.style.width = '50%';
        statusText.textContent = '⏳ 正在安装 usbipd，请稍候...';

        if (result.success) {
            // 安装成功
            progressBar.style.width = '100%';
            progressBar.style.background = 'var(--success-color, #28a745)';
            statusText.innerHTML = '✅ 安装成功！usbipd 已就绪';
            statusText.style.color = 'var(--success-color, #28a745)';

            addLogEntry('usbipd 自动安装成功', 'success');

            // 3秒后关闭弹窗并刷新设备
            setTimeout(() => {
                closeInstallGuide();
                // 直接调用 refreshDevices 而不是 debouncedRefreshDevices，避免防抖延迟
                refreshDevices();
            }, 3000);
        } else {
            // 安装失败
            progressBar.style.width = '100%';
            progressBar.style.background = 'var(--danger-color, #dc3545)';
            statusText.textContent = '❌ 安装失败: ' + (result.error || '未知错误');
            statusText.style.color = 'var(--danger-color, #dc3545)';

            if (result.install_guide) {
                showInstallGuide('usbipd 安装指南', result.install_guide);
            }
            addLogEntry('usbipd 自动安装失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (error) {
        // 异常处理
        progressBar.style.width = '100%';
        progressBar.style.background = 'var(--danger-color, #dc3545)';
        statusText.textContent = '❌ 安装失败: ' + error.message;
        statusText.style.color = 'var(--danger-color, #dc3545)';

        if (error.installGuide) {
            showInstallGuide('usbipd 安装指南', error.installGuide);
        }
        addLogEntry('usbipd 自动安装失败: ' + error.message, 'error');
    }
}

// ==================== SSHD 安装指南弹窗 ====================
function showSshdInstallGuide(guide) {
    if (!guide) {
        addLogEntry('SSHD 安装指南为空，未打开弹框', 'warning');
        return;
    }
    const modal = document.getElementById('sshd-install-guide-modal');
    if (modal) {
        // 设置指南内容
        const guideContent = document.getElementById('sshd-guide-content');
        if (guideContent) {
            guideContent.textContent = guide;
        }
        ModalManager.open('sshd-install-guide-modal');
    }
}

function closeSshdInstallGuide() {
    ModalManager.close('sshd-install-guide-modal');
}

async function autoInstallSshd() {
    // SSHD 需要手动安装，直接显示提示
    addLogEntry('⚠️ SSHD 需要在 Windows 客户端上手动安装，请按照安装指南操作', 'warning');
}
