// ==================== Security Audit ====================

function recordSecurityPageView(pageName) {
    if (!pageName || !state.authReady) return;
    fetch('/api/security-audit/page-view', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
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

// 提权入口：此前的空状态提示让用户「点击右上角提权」，但 shell 顶部并
// 没有该按钮，用户被卡死。改为在空状态内直接提供可点击的提权按钮
// （act-bridge 事件委托，动态插入即可用），提权成功后原地重新加载数据。
function securityAuditGateHtml() {
    return `
        <tr>
            <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                <div style="margin-bottom: 12px;">🔒 此页面需要管理员权限，请先提权后查看。</div>
                <button class="btn-xs btn-primary" type="button" data-click="elevateSecurityAuditView">🔓 立即提权</button>
            </td>
        </tr>
    `;
}

async function elevateSecurityAuditView() {
    const granted = await requestElevatedAccess('查看安全审计');
    if (!granted) return;
    await loadSecurityAudit(true);
}

async function loadSecurityAudit(reset = false) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    // Audit records are intentionally restricted to a temporarily elevated
    // administrator. Do not issue a request that is known to be forbidden
    // for an ordinary Web session: besides avoiding a noisy 403 in DevTools,
    // this makes the access requirement visible before any data request.
    if (state.authRequired && !state.elevated) {
        if (reset || !securityAuditState.loaded) {
            tbody.innerHTML = securityAuditGateHtml();
        }
        return false;
    }

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
            tbody.innerHTML = needElevation
                ? securityAuditGateHtml()
                : `
                    <tr>
                        <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                            加载失败: ${escapeHtml(error.message)}
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
            <button class="btn-xs" id="audit-load-more-btn" type="button">
                加载更多
            </button>
        `;
        wrapper
            .querySelector('#audit-load-more-btn')
            .addEventListener('click', loadMoreSecurityAudit);
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
                <button type="button" class="modal-close" aria-label="关闭" data-action="close-security-audit-detail">&times;</button>
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
    modal
        .querySelector('[data-action="close-security-audit-detail"]')
        .addEventListener('click', closeSecurityAuditDetailModal);
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
window.elevateSecurityAuditView = elevateSecurityAuditView;

// ==================== 历史邮件（email outbox ledger） ====================
// 与 users 页「用户列表 / Agent 接入」同款视图页签：owner 视图为默认，
// 管理员可勾选查看全部用户。审计页本身是管理员页面，但历史邮件的
// owner 视图让普通运维在提权会话内也能查到自己触发的邮件记录；
// 「查看全部用户」需要 admin 角色（服务端二次校验，前端仅控制可见性）。

const auditOutboxState = {
    offset: 0,
    limit: 50,
    loading: false,
    hasMore: false,
    loaded: false,
};

const AUDIT_OUTBOX_CHANNEL_LABELS = {
    manual: '手动',
    automation: '自动化',
    ops: '运维告警',
    redmine_reminder: 'Redmine',
};

function auditOutboxPanelIsOpen() {
    const panel = document.getElementById('audit-outbox-panel');
    return !!panel && panel.style.display !== 'none';
}

async function auditOutboxToggle(show) {
    const panel = document.getElementById('audit-outbox-panel');
    const mainView = document.getElementById('audit-main-view');
    const tabs = document.getElementById('audit-view-tabs');
    if (!panel || !mainView) return;
    const visible = show !== undefined ? Boolean(show) : !auditOutboxPanelIsOpen();

    panel.style.display = visible ? 'flex' : 'none';
    mainView.style.display = visible ? 'none' : '';
    try { sessionStorage.setItem('gms_audit_view', visible ? 'outbox' : 'logs'); } catch (_error) {}
    // 左上角页面标题保持「🛡️ 安全审计」不变：视图切换由页签 active 态表达。

    const logsTab = document.getElementById('audit-logs-tab');
    const outboxTab = document.getElementById('audit-outbox-tab');
    logsTab?.classList.toggle('active', !visible);
    outboxTab?.classList.toggle('active', visible);
    logsTab?.setAttribute('aria-selected', String(!visible));
    outboxTab?.setAttribute('aria-selected', String(visible));

    // 「查看全部用户」仅 admin 可见（服务端 admin_view 仍会二次校验）。
    const allUsersLabel = document.getElementById('email-outbox-all-users-label');
    if (allUsersLabel) {
        allUsersLabel.style.display =
            (state.currentUser?.role === 'admin') ? 'inline-flex' : 'none';
    }

    if (visible && !auditOutboxState.loaded) await loadAuditOutbox(true);
}

function getAuditOutboxFilterParams(reset = false) {
    const params = new URLSearchParams();
    if (reset) auditOutboxState.offset = 0;
    params.set('limit', String(auditOutboxState.limit));
    params.set('offset', String(auditOutboxState.offset));
    const channel = $('email-outbox-channel-filter')?.value || '';
    const sent = $('email-outbox-sent-filter')?.value || '';
    const query = $('email-outbox-search-input')?.value?.trim() || '';
    if (channel) params.set('channel', channel);
    if (sent) params.set('sent', sent);
    if (query) params.set('q', query);
    if ($('email-outbox-all-users')?.checked) params.set('admin_view', 'true');
    return params;
}

function formatOutboxTime(iso) {
    // 2026-11-09T07:36:14Z -> 2026-11-09 07:36:14（UTC 字面量，不换时区：
    // 台账统一存 UTC，转本地时区会让同一行数据与 journalctl 时间对不上）。
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(String(iso || ''));
    return match ? `${match[1]} ${match[2]}` : String(iso || '');
}

function renderAuditOutboxRows(items, { append = false } = {}) {
    const tbody = $('email-outbox-table-body');
    if (!tbody) return;
    if (!items.length && !append) {
        tbody.innerHTML = `
            <tr><td colspan="5" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                暂无历史邮件（当前筛选条件下无匹配）
            </td></tr>`;
        return;
    }
    const html = items.map((item) => {
        const state = item.sent
            ? '<span style="color: var(--success-color);">✓ 成功</span>'
            : `<span style="color: var(--danger-color);" title="${escapeJsAttr(item.error || '')}">✗ 失败</span>`;
        const channelLabel = AUDIT_OUTBOX_CHANNEL_LABELS[item.channel]
            || escapeJsAttr(item.channel || '未知');
        const ref = item.ref_type && item.ref_id
            ? `<span title="${escapeJsAttr(item.ref_type + ':' + item.ref_id)}" style="color: var(--text-secondary); margin-left: 4px;">↳${escapeJsAttr(String(item.ref_id).slice(0, 12))}</span>`
            : '';
        // 一行一封邮件：主行 = 收件人 + 主题；次信息（发送者/回链/预览）
        // 全部进悬停提示，行内不再堆叠两行文本。
        const toText = (item.to_addrs || []).join(', ');
        const senderText = item.username || '系统';
        const previewText = item.body_preview ? `｜${item.body_preview.slice(0, 100)}` : '';
        const hoverTitle = [
            `发件人: ${senderText}`,
            `主题: ${item.subject || ''}`,
            `正文预览: ${(item.body_preview || '').slice(0, 200)}`,
            item.error ? `错误: ${item.error}` : '',
            item.ref_type ? `关联: ${item.ref_type}:${item.ref_id}` : '',
        ].filter(Boolean).join('\n');
        return `<tr data-outbox-id="${escapeHtml(item.id || '')}" style="border-bottom: 1px solid var(--border-color); cursor: pointer;">
            <td style="padding: 6px 8px; text-align: center; font-size: 12px; white-space: nowrap;" title="${escapeJsAttr(formatOutboxTime(item.created_at))} (UTC)">${escapeHtml(formatOutboxTime(item.created_at))}</td>
            <td style="padding: 6px 8px; text-align: center; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeJsAttr(((item.username || '系统')) + (item.ref_type ? ` ↳${item.ref_type}:${item.ref_id}` : ''))}">${channelLabel}${ref}</td>
            <td style="padding: 6px 8px; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeJsAttr(toText)}">${escapeHtml(toText)}</td>
            <td style="padding: 6px 8px; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeJsAttr(hoverTitle)}">${escapeHtml(String(item.subject || ''))}</td>
            <td style="padding: 6px 8px; text-align: center; font-size: 12px; white-space: nowrap;">${state}</td>
        </tr>`;
    }).join('');
    if (append) {
        tbody.insertAdjacentHTML('beforeend', html);
        tbody.querySelectorAll('tr[data-outbox-id]:not([data-outbox-bound])').forEach((row) => {
            row.setAttribute('data-outbox-bound', '1');
            row.addEventListener('click', () => showOutboxDetail(row.dataset.outboxId));
        });
    } else {
        tbody.innerHTML = html;
        tbody.querySelectorAll('tr[data-outbox-id]').forEach((row) => {
            row.addEventListener('click', () => showOutboxDetail(row.dataset.outboxId));
        });
    }
}

// ==================== 历史邮件详情弹框（同审计日志详情模板） ====================

function ensureOutboxDetailModal() {
    let modal = $('outbox-detail-modal');
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = 'outbox-detail-modal';
    modal.className = 'modal';
    modal.innerHTML = `
        <div class="modal-content" style="width: min(760px, 92vw); max-width: min(760px, 92vw); max-height: 88vh; overflow: hidden; display: flex; flex-direction: column;">
            <div class="modal-header">
                <span class="modal-title">历史邮件详情</span>
                <button type="button" class="modal-close" aria-label="关闭" data-action="close-outbox-detail">&times;</button>
            </div>
            <div class="modal-body" id="outbox-detail-body" style="overflow: auto; padding-right: 4px;">
                加载中...
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (event) => {
        if (event.target === modal) closeOutboxDetailModal();
    });
    modal.querySelector('[data-action="close-outbox-detail"]')
        .addEventListener('click', closeOutboxDetailModal);
    return modal;
}

function closeOutboxDetailModal() {
    ModalManager.close('outbox-detail-modal');
}

function renderOutboxDetailField(label, value) {
    return `
        <div style="display: grid; grid-template-columns: 96px 1fr; gap: 6px; padding: 5px 0; border-bottom: 1px dashed var(--border-color); font-size: 12px;">
            <div style="color: var(--text-secondary);">${escapeHtml(label)}</div>
            <div style="color: var(--text-primary); word-break: break-all;">${value}</div>
        </div>
    `;
}

async function showOutboxDetail(emailId) {
    if (!emailId) return;
    const modal = ensureOutboxDetailModal();
    const body = $('outbox-detail-body');
    ModalManager.open('outbox-detail-modal');
    body.innerHTML = '加载中...';
    try {
        const result = await apiCall(`/api/email/outbox/${encodeURIComponent(emailId)}`);
        const entry = result.data || {};
        const sentBadge = entry.sent
            ? '<span style="color: var(--success-color);">✓ 成功</span>'
            : `<span style="color: var(--danger-color);">✗ 失败</span>`;
        const backlink = entry.ref_type && entry.ref_id
            ? `${escapeHtml(entry.ref_type)}:<span style="user-select: all;">${escapeHtml(entry.ref_id)}</span>`
            : '-';
        const errorBlock = entry.error
            ? renderAuditDetailBlock('失败原因', entry.error, { json: false })
            : '';
        body.innerHTML = `
            <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px; margin-bottom: 10px;">
                ${renderOutboxDetailField('记录 ID', `<span style="user-select: all;">${escapeHtml(entry.id || '')}</span>`)}
                ${renderOutboxDetailField('时间 (UTC)', escapeHtml(formatOutboxTime(entry.created_at)))}
                ${renderOutboxDetailField('通道', escapeHtml(AUDIT_OUTBOX_CHANNEL_LABELS[entry.channel] || entry.channel || '-'))}
                ${renderOutboxDetailField('发送者', escapeHtml(entry.username || '系统'))}
                ${renderOutboxDetailField('发件人地址', escapeHtml(entry.from_addr || '-'))}
                ${renderOutboxDetailField('收件人', escapeHtml((entry.to_addrs || []).join(', ') || '-'))}
                ${renderOutboxDetailField('抄送', escapeHtml((entry.cc_addrs || []).join(', ') || '-'))}
                ${renderOutboxDetailField('主题', escapeHtml(entry.subject || '-'))}
                ${renderOutboxDetailField('附件', escapeHtml((entry.attachments || []).join(', ') || '无'))}
                ${renderOutboxDetailField('结果', sentBadge)}
                ${renderOutboxDetailField('发送模式', escapeHtml(entry.mode || '-'))}
                ${renderOutboxDetailField('业务回链', backlink)}
            </div>
            ${renderAuditDetailBlock('正文预览（台账仅存前 500 字符）', entry.body_preview || '（空）', { json: false })}
            ${errorBlock}
        `;
    } catch (error) {
        body.innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(error.message)}</div>`;
    }
}

window.showOutboxDetail = showOutboxDetail;
window.closeOutboxDetailModal = closeOutboxDetailModal;

async function loadAuditOutbox(reset = false, append = false) {
    const tbody = $('email-outbox-table-body');
    if (!tbody) return;
    if (auditOutboxState.loading) return;
    auditOutboxState.loading = true;
    if (reset && !append) {
        tbody.innerHTML = `
            <tr><td colspan="5" style="padding: 40px; text-align: center; color: var(--text-secondary);">加载中...</td></tr>`;
    }
    try {
        const params = getAuditOutboxFilterParams(reset);
        const result = await apiCall(`/api/email/outbox?${params.toString()}`);
        const data = result?.data || {};
        const items = Array.isArray(data.items) ? data.items : [];
        auditOutboxState.hasMore = (auditOutboxState.offset + auditOutboxState.limit) < (data.total || 0);
        auditOutboxState.offset += items.length;
        auditOutboxState.loaded = true;
        renderAuditOutboxRows(items, { append });
        const wrapper = $('email-outbox-load-more-wrapper');
        if (wrapper) {
            wrapper.innerHTML = auditOutboxState.hasMore
                ? `<button class="btn-xs" data-click="loadAuditOutboxMore">加载更多（共 ${data.total} 条）</button>`
                : (items.length ? `<span style="color: var(--text-secondary); font-size: 12px;">共 ${data.total} 条</span>` : '');
        }
    } catch (error) {
        if (!append) {
            tbody.innerHTML = `
                <tr><td colspan="5" style="padding: 40px; text-align: center; color: var(--danger-color);">
                    加载历史邮件失败: ${escapeHtml(error.message)}
                </td></tr>`;
        }
    } finally {
        auditOutboxState.loading = false;
    }
}

async function loadAuditOutboxMore() {
    await loadAuditOutbox(false, true);
}

// 页签容器随登录就绪显示（模板与 users-view-tabs 一致）。
function auditViewTabsEnsureVisible() {
    if (!state.authReady) return;
    const tabs = document.getElementById('audit-view-tabs');
    if (tabs) tabs.style.display = 'flex';
}

runAfterAuthReady(auditViewTabsEnsureVisible);
window.addEventListener('gms:auth-ready', auditViewTabsEnsureVisible);

window.auditOutboxToggle = auditOutboxToggle;
window.loadAuditOutbox = loadAuditOutbox;
window.loadAuditOutboxMore = loadAuditOutboxMore;
