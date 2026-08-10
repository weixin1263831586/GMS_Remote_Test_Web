// ==================== Security Audit ====================

function recordSecurityPageView(pageName) {
    if (!pageName) return;
    fetch('/api/security-audit/page-view', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getClientIdentityHeaders()
        },
        body: JSON.stringify({
            page: pageName,
            title: document.title || '',
            hash: window.location.hash || ''
        })
    }).catch(error => debugLog('[SecurityAudit] page view record failed:', error));
}

let securityAuditState = {
    offset: 0,
    limit: 100,
    loading: false,
    hasMore: false,
    currentFilterParams: null,
    recordsCache: []
};

function getSecurityAuditFilterParams() {
    const params = new URLSearchParams();
    params.set('limit', String(securityAuditState.limit));
    params.set('offset', String(securityAuditState.offset));

    const source = $('audit-source-filter')?.value || '';
    const actionType = $('audit-type-filter')?.value || '';
    const query = $('audit-search-input')?.value?.trim() || '';

    if (source) params.set('source', source);
    if (actionType) params.set('action_type', actionType);
    if (query) params.set('q', query);
    return params;
}

async function loadSecurityAudit(reset = false) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (securityAuditState.loading) return;
    securityAuditState.loading = true;

    if (reset) {
        securityAuditState.offset = 0;
        securityAuditState.recordsCache = [];
        securityAuditState.hasMore = false;
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    加载中...
                </td>
            </tr>
        `;
    }

    try {
        const params = getSecurityAuditFilterParams();
        securityAuditState.currentFilterParams = params.toString();
        const result = await apiCall(`/api/security-audit/logs?${params.toString()}`);
        const payload = result.data || {};
        const fetchedRecords = payload.records || [];
        securityAuditState.hasMore = payload.has_more || false;

        if (reset) {
            securityAuditState.recordsCache = fetchedRecords;
            renderSecurityAuditRows(securityAuditState.recordsCache);
        } else {
            securityAuditState.recordsCache.push(...fetchedRecords);
            appendSecurityAuditRows(fetchedRecords);
        }
        updateSecurityAuditStats(payload.stats || {});
    } catch (error) {
        const needElevation = error.status === 403 && !state.elevated;
        if (reset) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                        ${needElevation
                            ? '🔒 此页面需要管理员权限，请点击右上角提权后查看。'
                            : `加载失败: ${escapeHtml(error.message)}`}
                    </td>
                </tr>
            `;
        } else {
            if (!needElevation) showToast('加载更多审计记录失败: ' + error.message, 'error');
        }
    } finally {
        securityAuditState.loading = false;
        updateSecurityAuditLoadMoreButton();
    }
}

function updateSecurityAuditLoadMoreButton() {
    const wrapper = $('audit-load-more-wrapper');
    if (!wrapper) return;
    if (securityAuditState.hasMore) {
        wrapper.innerHTML = `
            <button class="btn-xs" id="audit-load-more-btn" onclick="loadMoreSecurityAudit()">
                加载更多
            </button>
        `;
    } else {
        wrapper.innerHTML = '';
    }
}

async function loadMoreSecurityAudit() {
    if (!securityAuditState.hasMore || securityAuditState.loading) return;
    securityAuditState.offset += securityAuditState.limit;
    await loadSecurityAudit(false);
}

function appendSecurityAuditRows(records) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (!records.length) return;

    const html = buildSecurityAuditRowsHtml(records);
    const temp = document.createElement('tbody');
    temp.innerHTML = html;
    while (temp.firstChild) {
        tbody.appendChild(temp.firstChild);
    }

    tbody.querySelectorAll('[data-audit-id]').forEach(row => {
        row.addEventListener('click', () => showSecurityAuditDetail(row.dataset.auditId));
    });
}

function updateSecurityAuditStats(stats) {
    const setText = (id, value) => {
        const el = $(id);
        if (el) el.textContent = value ?? 0;
    };
    setText('audit-total-count', stats.total);
    setText('audit-web-count', stats.web);
    setText('audit-cli-count', stats.cli);
    setText('audit-error-count', stats.errors);
}

function getAuditSourceLabel(source) {
    if (source === 'cli') {
        return '<span style="color: var(--warning-color); font-weight: 600;">CLI</span>';
    }
    if (source === 'web') {
        return '<span style="color: var(--success-color); font-weight: 600;">Web</span>';
    }
    return `<span style="color: var(--text-secondary);">${escapeHtml(source || '-')}</span>`;
}

function getAuditStatusLabel(statusCode) {
    const code = Number(statusCode || 0);
    const color = code >= 500 ? 'var(--danger-color)' : code >= 400 ? 'var(--warning-color)' : 'var(--success-color)';
    return `<span style="color: ${color}; font-weight: 600;">${code || '-'}</span>`;
}

function formatAuditTime(timestamp) {
    if (!timestamp) return '-';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return date.toLocaleString('zh-CN', { hour12: false });
}

function renderSecurityAuditRows(records) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (!records.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    暂无审计记录
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = buildSecurityAuditRowsHtml(records);

    tbody.querySelectorAll('[data-audit-id]').forEach(row => {
        row.addEventListener('click', () => showSecurityAuditDetail(row.dataset.auditId));
    });
}

function buildSecurityAuditRowsHtml(records) {
    return records.map(record => {
        const userIpText = `${record.username || 'unknown'} / ${record.client_ip || '-'}`;
        const path = record.page ? `#${record.page}` : (record.path || '');
        const detail = [
            record.method ? `${record.method}` : '',
            path,
            record.query && Object.keys(record.query).length ? JSON.stringify(record.query) : ''
        ].filter(Boolean).join(' ');
        const operation = record.operation || detail || '-';
        const operationLine = [operation, detail && detail !== operation ? detail : ''].filter(Boolean).join('  |  ');
        const rowTitle = [
            '点击查看审计详情',
            `时间: ${formatAuditTime(record.timestamp)}`,
            `用户/IP: ${userIpText}`,
            `操作: ${operationLine}`,
        ].join('\n');

        return `
            <tr data-audit-id="${escapeHtml(record.id || '')}" style="border-bottom: 1px solid var(--border-color); cursor: pointer; height: 34px;" title="${escapeHtml(rowTitle)}">
                <td style="padding: 7px 8px; font-size: 12px; color: var(--text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(formatAuditTime(record.timestamp))}</td>
                <td style="padding: 7px 8px; font-size: 12px; text-align: center; white-space: nowrap;">${getAuditSourceLabel(record.source)}</td>
                <td style="padding: 7px 8px; font-size: 12px; text-align: center; white-space: nowrap;">${getAuditStatusLabel(record.status_code)}</td>
                <td style="padding: 7px 8px; font-size: 12px; color: var(--text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(userIpText)}</td>
                <td style="padding: 7px 8px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                    <span style="color: var(--text-primary); font-weight: 600;">${escapeHtml(operationLine)}</span>
                </td>
                <td style="padding: 7px 8px; font-size: 12px; color: var(--text-secondary); text-align: center; white-space: nowrap;">${escapeHtml(String(record.duration_ms ?? 0))} ms</td>
            </tr>
        `;
    }).join('');
}

function formatAuditJson(value) {
    if (value === undefined || value === null || value === '') return '-';
    try {
        return JSON.stringify(value, null, 2);
    } catch (error) {
        return String(value);
    }
}

function renderAuditDetailBlock(title, content, options = {}) {
    const isJson = options.json !== false;
    const text = isJson ? formatAuditJson(content) : String(content || '-');
    return `
        <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px; margin-bottom: 10px;">
            <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text-primary);">${escapeHtml(title)}</div>
            <pre style="margin: 0; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: 11px; line-height: 1.45; color: var(--text-secondary);">${escapeHtml(text)}</pre>
        </div>
    `;
}

function ensureSecurityAuditDetailModal() {
    let modal = $('security-audit-detail-modal');
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = 'security-audit-detail-modal';
    modal.className = 'modal';
    modal.innerHTML = `
        <div class="modal-content" style="width: min(980px, 92vw); max-width: min(980px, 92vw); max-height: 88vh; overflow: hidden; display: flex; flex-direction: column;">
            <div class="modal-header">
                <span class="modal-title">安全审计详情</span>
                <span class="modal-close" onclick="closeSecurityAuditDetailModal()">&times;</span>
            </div>
            <div class="modal-body" id="security-audit-detail-body" style="overflow: auto; padding-right: 4px;">
                加载中...
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (event) => {
        if (event.target === modal) closeSecurityAuditDetailModal();
    });
    return modal;
}

function closeSecurityAuditDetailModal() {
    ModalManager.close('security-audit-detail-modal');
}

function renderRelatedAuditLogs(relatedLogs) {
    const recentLogs = relatedLogs?.recent_client_logs || [];
    const savedTail = relatedLogs?.saved_log_tail || [];
    const blocks = [];

    if (recentLogs.length) {
        blocks.push(renderAuditDetailBlock('最近页面操作日志', recentLogs));
    }

    if (relatedLogs?.saved_log_file) {
        blocks.push(renderAuditDetailBlock('已保存日志文件', relatedLogs.saved_log_file, { json: false }));
    }

    if (savedTail.length) {
        blocks.push(renderAuditDetailBlock('已保存日志尾部', savedTail.join(''), { json: false }));
    }

    return blocks.join('') || renderAuditDetailBlock('关联日志', '暂无关联日志', { json: false });
}

async function showSecurityAuditDetail(auditId) {
    if (!auditId) return;
    const modal = ensureSecurityAuditDetailModal();
    const body = $('security-audit-detail-body');
    ModalManager.open('security-audit-detail-modal');
    body.innerHTML = '加载中...';

    try {
        const result = await apiCall(`/api/security-audit/detail/${encodeURIComponent(auditId)}`);
        const payload = result.data || {};
        const record = payload.record || {};
        const relatedLogs = payload.related_logs || {};
        const metadata = {
            id: record.id,
            timestamp: record.timestamp,
            source: record.source,
            action_type: record.action_type,
            operation: record.operation,
            method: record.method,
            path: record.path,
            page: record.page,
            status_code: record.status_code,
            duration_ms: record.duration_ms,
            username: record.username,
            client_ip: record.client_ip,
            client_id: record.client_id,
            user_agent: record.user_agent,
            error: record.error || ''
        };

        body.innerHTML = `
            ${renderAuditDetailBlock('基本信息', metadata)}
            ${renderAuditDetailBlock('请求参数摘要', record.request_summary || record.query || {})}
            ${renderAuditDetailBlock('执行结果摘要', record.response_summary || {})}
            ${renderRelatedAuditLogs(relatedLogs)}
        `;
    } catch (error) {
        body.innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(error.message)}</div>`;
    }
}

async function exportSecurityAudit() {
    const granted = await requestElevatedAccess('导出安全审计日志');
    if (!granted) return;
    try {
        await apiCall('/api/security-audit/verify');
    } catch (_error) {
        return;
    }
    window.open('/api/security-audit/export', '_blank');
}

window.recordSecurityPageView = recordSecurityPageView;
window.loadSecurityAudit = loadSecurityAudit;
window.showSecurityAuditDetail = showSecurityAuditDetail;
window.closeSecurityAuditDetailModal = closeSecurityAuditDetailModal;
window.exportSecurityAudit = exportSecurityAudit;

// ==================== APK 文件搜索功能 ====================

let apkSearchDebounceTimer = null;

async function filterApkFiles() {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');

    if (!query || query.length < 2) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    let matches = [];
    if (window.apkCurrentTaskId) {
        try {
            const data = await apiCall(`/api/apk/search/${window.apkCurrentTaskId}?q=${encodeURIComponent(query)}&limit=20`);
            matches = data.success ? (data.data.items || []) : [];
        } catch (e) {
            debugLog('[APK Search] backend search failed:', e.message);
        }
    }
    window.apkLastSearchMatches = matches;

    if (!resultsEl || matches.length === 0) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    // 显示搜索结果
    resultsEl.innerHTML = '';
    for (const file of matches) {
        const item = document.createElement('div');
        item.className = 'apk-search-result-item';
        item.onclick = () => jumpToApkFile(file.path);
        item.innerHTML = `<span class="apk-search-result-name">${escapeHtml(file.name)}</span><span class="apk-search-result-path">${escapeHtml(file.path)}</span>`;
        resultsEl.appendChild(item);
    }
    resultsEl.style.display = 'block';

    // 定位搜索结果到搜索框下方，宽度与输入框一致
    const searchEl = $('apk-file-search');
    if (searchEl && resultsEl) {
        const rect = searchEl.getBoundingClientRect();
        resultsEl.style.position = 'absolute';
        resultsEl.style.top = (rect.bottom + window.scrollY) + 'px';
        resultsEl.style.left = (rect.left + window.scrollX) + 'px';
        resultsEl.style.width = rect.width + 'px';
    }
}

// Use generic debounce utility for APK search
const debounceFilterApkFiles = debounce(filterApkFiles, 300);

function jumpToApkFile(selectedPath) {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');

    // 如果没有指定路径，从搜索结果或缓存中查找
    let path = selectedPath;
    if (!path && query) {
        const matches = window.apkLastSearchMatches || [];
        if (matches.length > 0) {
            path = matches[0].path;
        }
    }

    if (!path) {
        showToast('未找到匹配的文件', 'warning');
        return;
    }

    // 关闭搜索结果
    if (resultsEl) resultsEl.style.display = 'none';

    // 打开文件
    viewApkFile(path);

    // 展开文件树到该文件
    expandApkTreeToPath(path);
}

function clearApkSearch() {
    const searchEl = $('apk-file-search');
    const resultsEl = $('apk-search-results');
    if (searchEl) searchEl.value = '';
    if (resultsEl) resultsEl.style.display = 'none';
}

function expandApkTreeToPath(filePath) {
    const parts = filePath.split('/');
    let currentPath = '';

    for (let i = 0; i < parts.length - 1; i++) {
        currentPath = (currentPath ? currentPath + '/' : '') + parts[i];
        const container = document.querySelector(`[data-apk-path="${CSS.escape(currentPath)}"]`);
        if (container) {
            const childContainer = container.querySelector('.apk-tree-children');
            if (childContainer && childContainer.classList.contains('apk-tree-children')) {
                childContainer.classList.add('expanded');
            }
        }
    }
}

// 点击搜索结果外部时关闭
document.addEventListener('click', (e) => {
    const resultsEl = $('apk-search-results');
    const searchEl = $('apk-file-search');
    if (resultsEl && searchEl && !resultsEl.contains(e.target) && e.target !== searchEl) {
        resultsEl.style.display = 'none';
    }
});

// Export APK search functions to window
window.filterApkFiles = filterApkFiles;
window.jumpToApkFile = jumpToApkFile;
window.clearApkSearch = clearApkSearch;
window.expandApkTreeToPath = expandApkTreeToPath;
window.debounceFilterApkFiles = debounceFilterApkFiles;
window.handleApkFile = handleApkFile;
window.initApkAnalysisPage = initApkAnalysisPage;
