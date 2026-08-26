// ==================== Security Audit ====================

function recordSecurityPageView(pageName) {
    if (!pageName || !state.authReady) return;
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
    recordsCache: [],
    loaded: false,
    requestGeneration: 0
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

function getSecurityAuditFilterKey() {
    const params = getSecurityAuditFilterParams();
    params.delete('offset');
    params.delete('limit');
    return params.toString();
}

async function loadSecurityAudit(reset = false) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (securityAuditState.loading && !reset) return;
    const requestGeneration = reset
        ? ++securityAuditState.requestGeneration
        : securityAuditState.requestGeneration;
    const hadRenderedRecords = securityAuditState.loaded;
    const previousOffset = securityAuditState.offset;
    securityAuditState.loading = true;
    tbody.setAttribute('aria-busy', 'true');

    if (reset) {
        securityAuditState.offset = 0;
        if (!hadRenderedRecords) tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    加载中...
                </td>
            </tr>
        `;
    }

    try {
        const params = getSecurityAuditFilterParams();
        const requestedFilterKey = getSecurityAuditFilterKey();
        const result = await apiCall(`/api/security-audit/logs?${params.toString()}`);
        if (requestGeneration !== securityAuditState.requestGeneration) return false;
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
        securityAuditState.loaded = true;
        securityAuditState.currentFilterParams = requestedFilterKey;
        updateSecurityAuditStats(payload.stats || {});
        return true;
    } catch (error) {
        if (requestGeneration !== securityAuditState.requestGeneration) return false;
        if (reset && hadRenderedRecords) securityAuditState.offset = previousOffset;
        const needElevation = error.status === 403 && !state.elevated;
        if (reset && (needElevation || !hadRenderedRecords)) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                        ${needElevation
                            ? '🔒 此页面需要管理员权限，请点击右上角提权后查看。'
                            : `加载失败: ${escapeHtml(error.message)}`}
                    </td>
                </tr>
            `;
        } else if (!needElevation) {
            showToast(
                reset ? '审计记录刷新失败: ' + error.message
                    : '加载更多审计记录失败: ' + error.message,
                'error'
            );
        }
        return false;
    } finally {
        if (requestGeneration === securityAuditState.requestGeneration) {
            securityAuditState.loading = false;
            tbody.setAttribute('aria-busy', 'false');
            updateSecurityAuditLoadMoreButton();
        }
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
    if (securityAuditState.currentFilterParams !== getSecurityAuditFilterKey()) {
        await loadSecurityAudit(true);
        return;
    }
    const previousOffset = securityAuditState.offset;
    securityAuditState.offset += securityAuditState.limit;
    const requestedOffset = securityAuditState.offset;
    const loaded = await loadSecurityAudit(false);
    if (!loaded && securityAuditState.offset === requestedOffset) {
        securityAuditState.offset = previousOffset;
    }
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
