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

// Use the same elevation prompt as the log view for the read-only disk tab.
function diskArtifactAuditGateHtml() {
    return securityAuditGateHtml().replace(
        'data-click="elevateSecurityAuditView"',
        'data-click="elevateDiskArtifactAudit"'
    );
}

async function elevateDiskArtifactAudit() {
    const granted = await requestElevatedAccess('查看磁盘审计');
    if (!granted) return;
    await loadDiskArtifactAudit(true);
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
        updateAuditVisualSummary(payload.stats || {});
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
    bindSecurityAuditRowClicks(tbody);
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

function updateAuditVisualSummary(stats) {
    const root = document.getElementById('page-security-audit');
    if (!root) return;
    const asCount = value => Math.max(0, Number.isFinite(Number(value)) ? Number(value) : 0);
    const total = asCount(stats.total);
    const web = Math.min(total, asCount(stats.web));
    const cli = Math.min(Math.max(0, total - web), asCount(stats.cli));
    const other = Math.max(0, total - web - cli);
    const formatCount = count => Math.round(count).toLocaleString('zh-CN');
    const legend = (count) => `${formatCount(count)} (${total ? (100 * count / total).toFixed(1) : '0.0'}%)`;
    const donut = root.querySelector('.audit-source-donut');
    if (!donut) return;
    donut.style.setProperty('--audit-web-end', `${total ? 360 * web / total : 0}deg`);
    donut.style.setProperty('--audit-cli-end', `${total ? 360 * (web + cli) / total : 0}deg`);
    donut.dataset.empty = String(total === 0);
    donut.setAttribute('aria-label', total ? `审计记录 ${formatCount(total)} 条，Web ${formatCount(web)} 条，CLI ${formatCount(cli)} 条，其他 ${formatCount(other)} 条` : '暂无审计记录');
    root.querySelector('.audit-donut-total').textContent = formatCount(total);
    root.querySelector('.audit-web-legend').textContent = legend(web);
    root.querySelector('.audit-cli-legend').textContent = legend(cli);
    root.querySelector('.audit-other-legend').textContent = legend(other);
    root.querySelector('.audit-other-row').hidden = other === 0;
}

async function verifySecurityAuditIntegrity() {
    const root = document.getElementById('page-security-audit');
    const button = root?.querySelector('.audit-integrity-check');
    if (!button || button.disabled) return;
    const granted = await requestElevatedAccess('校验安全审计日志');
    if (!granted) return;
    const stateNode = root.querySelector('.audit-integrity-state');
    const label = root.querySelector('.audit-integrity-label');
    const summary = root.querySelector('.audit-integrity-summary');
    const count = root.querySelector('.audit-integrity-count');
    button.disabled = true;
    stateNode.dataset.state = 'checking';
    label.textContent = '校验中';
    summary.textContent = '正在核验审计日志 HMAC 签名链…';
    try {
        const response = await apiCall('/api/security-audit/verify');
        const result = response.data || {};
        stateNode.dataset.state = 'valid';
        label.textContent = '本次校验通过';
        summary.textContent = '当前审计日志签名链完整';
        count.textContent = `已校验 ${Number(result.signed_records || 0).toLocaleString('zh-CN')} 条签名记录 · 仅代表本次校验`;
    } catch (error) {
        const invalid = Number(error?.status) === 409;
        stateNode.dataset.state = invalid ? 'invalid' : 'error';
        label.textContent = invalid ? '校验未通过' : '校验未完成';
        summary.textContent = invalid ? '审计签名链存在异常，请联系管理员核查' : '无法完成校验，请检查权限或服务状态';
        count.textContent = error?.message || '请稍后重试';
    } finally {
        button.disabled = false;
    }
}

function bindAuditIntegrityCheck() {
    const button = document.querySelector('#page-security-audit .audit-integrity-check');
    if (!button || button.dataset.auditBound) return;
    button.dataset.auditBound = 'true';
    button.addEventListener('click', verifySecurityAuditIntegrity);
}

function getAuditSourceLabel(source) {
    const tone = source === 'web' ? 'web' : source === 'cli' ? 'cli' : 'other';
    const label = source === 'web' ? 'Web' : source === 'cli' ? 'CLI' : source || '-';
    return `<span class="audit-source-badge audit-source-${tone}">${escapeHtml(label)}</span>`;
}

function getAuditStatusLabel(statusCode) {
    const code = Number(statusCode || 0);
    if (!code) return '<span class="audit-status-badge audit-status-unknown">-</span>';
    const tone = code >= 500 ? 'error' : code >= 400 ? 'warning' : code >= 200 && code < 400 ? 'success' : 'unknown';
    return `<span class="audit-status-badge audit-status-${tone}">${code}</span>`;
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
    bindSecurityAuditRowClicks(tbody);
}

function bindSecurityAuditRowClicks(tbody) {
    // 分页追加不能再次给旧行绑点击。重复监听会把同一次点击变成多次详情请求，
    // 后返回的旧请求还会盖掉用户随后点开的另一条记录。
    tbody.querySelectorAll('[data-audit-id]:not([data-audit-bound])').forEach(row => {
        row.setAttribute('data-audit-bound', '1');
        row.addEventListener('click', () => showSecurityAuditDetail(row.dataset.auditId));
    });
}

function buildSecurityAuditRowsHtml(records) {
    return records.map(record => {
        const userIpText = `${record.username || 'unknown'} / ${record.client_ip || '-'}`;
        const path = record.page ? `#${record.page}` : (record.path || '');
        const detail = [
            record.method || '',
            path,
            record.query && Object.keys(record.query).length ? JSON.stringify(record.query) : ''
        ].filter(Boolean).join(' ');
        const operation = record.operation || detail || '-';
        const operationLine = [operation, detail && detail !== operation ? detail : ''].filter(Boolean).join('  |  ');
        const duration = record.action_type === 'page_view' ? '-' : `${Number(record.duration_ms || 0).toLocaleString('zh-CN')} ms`;
        const rowTitle = [
            '点击查看审计详情',
            `时间: ${formatAuditTime(record.timestamp)}`,
            `用户/IP: ${userIpText}`,
            `操作: ${operationLine}`,
        ].join('\n');
        return `
            <tr class="audit-log-row" data-audit-id="${escapeHtml(record.id || '')}" title="${escapeHtml(rowTitle)}">
                <td class="audit-time-cell">${escapeHtml(formatAuditTime(record.timestamp))}</td>
                <td class="audit-source-cell">${getAuditSourceLabel(record.source)}</td>
                <td class="audit-status-cell">${getAuditStatusLabel(record.status_code)}</td>
                <td class="audit-user-cell" title="${escapeHtml(userIpText)}">${escapeHtml(userIpText)}</td>
                <td class="audit-operation-cell" title="${escapeHtml(operationLine)}">${escapeHtml(operationLine)}</td>
                <td class="audit-duration-cell">${escapeHtml(duration)}</td>
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
            <div class="audit-detail-block-title" style="margin-bottom: 8px; color: var(--text-primary);">${escapeHtml(title)}</div>
            <pre class="audit-detail-block-code" style="margin: 0; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word; line-height: 1.45; color: var(--text-secondary);">${escapeHtml(text)}</pre>
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
        <div class="modal-content modal-xl tech-dialog tech-dialog--audit-detail"><svg class="tech-frame" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M16 1H986L999 14V584L984 599H16L1 584V16Z" vector-effect="non-scaling-stroke"/></svg><span class="tech-corner-mark tech-corner-mark--tl" aria-hidden="true"></span><span class="tech-corner-mark tech-corner-mark--br" aria-hidden="true"></span>
            <div class="modal-header tech-dialog-header"><svg class="tech-header-rule" viewBox="0 0 1000 16" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 15H490L506 1H916" vector-effect="non-scaling-stroke"/></svg>
                <div class="tech-title-group"><span class="tech-title-glyph" aria-hidden="true"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#shield"></use></svg></span><span class="tech-title-divider" aria-hidden="true"></span><span class="modal-title">安全审计详情</span></div>
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
    ++securityAuditDetailRequestGeneration;
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

let securityAuditDetailRequestGeneration = 0;

async function showSecurityAuditDetail(auditId) {
    if (!auditId) return;
    const generation = ++securityAuditDetailRequestGeneration;
    const modal = ensureSecurityAuditDetailModal();
    const body = $('security-audit-detail-body');
    ModalManager.open('security-audit-detail-modal');
    body.innerHTML = '加载中...';

    try {
        const result = await apiCall(`/api/security-audit/detail/${encodeURIComponent(auditId)}`);
        if (generation !== securityAuditDetailRequestGeneration) return;
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
        if (generation !== securityAuditDetailRequestGeneration) return;
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
    requestGeneration: 0,
};

const AUDIT_OUTBOX_CHANNEL_LABELS = {
    manual: '手动',
    automation: '自动化',
    ops: '运维告警',
    redmine_reminder: 'Redmine',
    update_radar: '更新雷达',
};

async function switchSecurityAuditView(view) {
    const selected = ['logs', 'outbox', 'disk'].includes(view) ? view : 'logs';
    const views = {
        logs: document.getElementById('audit-main-view'),
        outbox: document.getElementById('audit-outbox-panel'),
        disk: document.getElementById('audit-disk-panel'),
    };
    if (!views.logs || !views.outbox || !views.disk) return;
    for (const [name, element] of Object.entries(views)) {
        element.style.display = name === selected ? 'flex' : 'none';
    }
    for (const [name, id] of [
        ['logs', 'audit-logs-tab'], ['outbox', 'audit-outbox-tab'], ['disk', 'audit-disk-tab']
    ]) {
        const tab = document.getElementById(id);
        tab?.classList.toggle('active', name === selected);
        tab?.setAttribute('aria-selected', String(name === selected));
    }
    try { sessionStorage.setItem('gms_audit_view', selected); } catch (_error) {}
    const allUsersLabel = document.getElementById('email-outbox-all-users-label');
    if (allUsersLabel) {
        allUsersLabel.style.display = state.currentUser?.role === 'admin' ? 'inline-flex' : 'none';
    }
    if (selected === 'outbox' && !auditOutboxState.loaded) await loadAuditOutbox(true);
    if (selected === 'disk' && (
        !diskArtifactAuditState.loaded || (state.authRequired && !state.elevated)
    )) await loadDiskArtifactAudit(true);
    if (selected === 'logs' && !securityAuditState.loaded) await loadSecurityAudit(true);
}

// Backward-compatible entry for code that used the earlier two-tab page.
async function auditOutboxToggle(show) {
    return switchSecurityAuditView(show ? 'outbox' : 'logs');
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
            ? '<span class="audit-mail-state sent">成功</span>'
            : `<span class="audit-mail-state failed" title="${escapeJsAttr(item.error || '')}">✗ 失败</span>`;
        const channelLabel = AUDIT_OUTBOX_CHANNEL_LABELS[item.channel]
            || escapeJsAttr(item.channel || '未知');
        const ref = item.ref_type && item.ref_id
            ? `<span title="${escapeJsAttr(item.ref_type + ':' + item.ref_id)}" style="color: var(--text-secondary); margin-left: 4px;">↳${escapeJsAttr(String(item.ref_id).slice(0, 12))}</span>`
            : '';
        // 一行一封邮件：主行 = 收件人 + 主题；次信息（发送者/回链/预览）
        // 全部进悬停提示，行内不再堆叠两行文本。
        const toText = (item.to_addrs || []).join(', ');
        const senderText = item.username || '系统';
        const hoverTitle = [
            `发件人: ${senderText}`,
            `主题: ${item.subject || ''}`,
            `正文预览: ${(item.body_preview || '').slice(0, 200)}`,
            item.error ? `错误: ${item.error}` : '',
            item.ref_type ? `关联: ${item.ref_type}:${item.ref_id}` : '',
        ].filter(Boolean).join('\n');
        return `<tr class="audit-mail-row" data-outbox-id="${escapeHtml(item.id || '')}">
            <td class="audit-mail-time" title="${escapeJsAttr(formatOutboxTime(item.created_at))} (UTC)">${escapeHtml(formatOutboxTime(item.created_at))}</td>
            <td class="audit-mail-channel" title="${escapeJsAttr(((item.username || '系统')) + (item.ref_type ? ` ↳${item.ref_type}:${item.ref_id}` : ''))}">${channelLabel}${ref}</td>
            <td class="audit-mail-recipient" title="${escapeJsAttr(toText)}">${escapeHtml(toText)}</td>
            <td class="audit-mail-subject" title="${escapeJsAttr(hoverTitle)}">${escapeHtml(String(item.subject || ''))}</td>
            <td class="audit-mail-result">${state}</td>
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

let outboxDetailRequestGeneration = 0;

function ensureOutboxDetailModal() {
    let modal = $('outbox-detail-modal');
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = 'outbox-detail-modal';
    modal.className = 'modal';
    modal.innerHTML = `
        <div class="modal-content modal-l tech-dialog tech-dialog--outbox-detail"><svg class="tech-frame" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M16 1H986L999 14V584L984 599H16L1 584V16Z" vector-effect="non-scaling-stroke"/></svg><span class="tech-corner-mark tech-corner-mark--tl" aria-hidden="true"></span><span class="tech-corner-mark tech-corner-mark--br" aria-hidden="true"></span>
            <div class="modal-header tech-dialog-header"><svg class="tech-header-rule" viewBox="0 0 1000 16" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path d="M0 15H490L506 1H916" vector-effect="non-scaling-stroke"/></svg>
                <div class="tech-title-group"><span class="tech-title-glyph" aria-hidden="true"><svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/static/icons/ui.svg#shield"></use></svg></span><span class="tech-title-divider" aria-hidden="true"></span><span class="modal-title">历史邮件详情</span></div>
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
    ++outboxDetailRequestGeneration;
    ModalManager.close('outbox-detail-modal');
}

function renderOutboxDetailField(label, value) {
    return `
        <div style="display: grid; grid-template-columns: 96px 1fr; gap: 6px; padding: 5px 0; border-bottom: 1px dashed var(--border-color); font-size: var(--ui-type-size-small);">
            <div style="color: var(--text-secondary);">${escapeHtml(label)}</div>
            <div style="color: var(--text-primary); word-break: break-all;">${value}</div>
        </div>
    `;
}

async function showOutboxDetail(emailId) {
    if (!emailId) return;
    const generation = ++outboxDetailRequestGeneration;
    const modal = ensureOutboxDetailModal();
    const body = $('outbox-detail-body');
    ModalManager.open('outbox-detail-modal');
    body.innerHTML = '加载中...';
    try {
        const result = await apiCall(`/api/email/outbox/${encodeURIComponent(emailId)}`);
        if (generation !== outboxDetailRequestGeneration) return;
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
        if (generation !== outboxDetailRequestGeneration) return;
        body.innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(error.message)}</div>`;
    }
}

window.showOutboxDetail = showOutboxDetail;
window.closeOutboxDetailModal = closeOutboxDetailModal;

function updateAuditOutboxSummary(summary) {
    const total = Number(summary.total || 0);
    const success = Number(summary.success || 0);
    const failed = Number(summary.failed || 0);
    const latest = formatOutboxTime(summary.latest_at) || '-';
    const setText = (id, value) => {
        const el = $(id);
        if (el) el.textContent = value;
    };
    const countText = (value) => value.toLocaleString('zh-CN');
    setText('audit-mail-total', countText(total));
    setText('audit-mail-success', countText(success));
    setText('audit-mail-failed', countText(failed));
    setText('audit-mail-latest', latest);
    setText('audit-mail-status-latest', latest);
    setText('audit-mail-donut-total', countText(total));
    const percentage = (value) => total ? (value * 100 / total).toFixed(1) : '0.0';
    setText('audit-mail-success-legend', countText(success) + ' (' + percentage(success) + '%)');
    setText('audit-mail-failed-legend', countText(failed) + ' (' + percentage(failed) + '%)');
    const donut = document.querySelector('#audit-outbox-panel .audit-mail-donut');
    if (donut) {
        donut.dataset.empty = String(total === 0);
        donut.style.setProperty('--audit-web-end', String(total ? success * 360 / total : 0) + 'deg');
        donut.style.setProperty('--audit-cli-end', '360deg');
        donut.setAttribute('aria-label', '邮件共 ' + total + ' 条，成功 ' + success + ' 条，失败 ' + failed + ' 条');
    }
}

function auditOutboxCsvCell(value) {
    const raw = String(value == null ? '' : value).replace(/\r\n?/g, '\n');
    // Avoid spreadsheet formula evaluation when exporting user-controlled mail fields.
    const safe = /^\s*[=+\-@]/.test(raw) ? "'" + raw : raw;
    return '"' + safe.replace(/"/g, '""') + '"';
}

async function exportAuditOutbox() {
    const button = $('audit-mail-export-button');
    if (button?.disabled) return;
    if (button) button.disabled = true;
    const filters = getAuditOutboxFilterParams();
    const columns = ['时间 (UTC)', '通道', '收件人', '主题', '结果', '发送者', '失败原因'];
    const lines = [columns.map(auditOutboxCsvCell).join(',')];
    try {
        let offset = 0;
        const pageSize = 200;
        // The store retains at most 5,000 entries; use the same server-side
        // owner/role checks for every exported page.
        while (offset < 5000) {
            const params = new URLSearchParams(filters);
            params.set('limit', String(pageSize));
            params.set('offset', String(offset));
            const response = await apiCall('/api/email/outbox?' + params.toString());
            const data = response?.data || {};
            const items = Array.isArray(data.items) ? data.items : [];
            for (const item of items) {
                lines.push([
                    formatOutboxTime(item.created_at),
                    AUDIT_OUTBOX_CHANNEL_LABELS[item.channel] || item.channel || '',
                    (item.to_addrs || []).join('; '),
                    item.subject || '',
                    item.sent ? '成功' : '失败',
                    item.username || '系统',
                    item.error || '',
                ].map(auditOutboxCsvCell).join(','));
            }
            offset += items.length;
            if (!items.length || offset >= Number(data.total || 0)) break;
        }
        const blob = new Blob(['\ufeff' + lines.join('\r\n') + '\r\n'], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = 'email-outbox-' + new Date().toISOString().slice(0, 10) + '.csv';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
        showToast('已导出当前筛选条件下的邮件台账', 'success');
    } catch (error) {
        showToast('导出历史邮件失败: ' + error.message, 'error');
    } finally {
        if (button) button.disabled = false;
    }
}

async function loadAuditOutbox(reset = false, append = false) {
    const tbody = $('email-outbox-table-body');
    if (!tbody) return;
    if (auditOutboxState.loading && !reset) return;
    const generation = reset
        ? ++auditOutboxState.requestGeneration
        : auditOutboxState.requestGeneration;
    const offset = reset ? 0 : auditOutboxState.offset;
    auditOutboxState.loading = true;
    tbody.setAttribute('aria-busy', 'true');
    if (reset && !append && !auditOutboxState.loaded) {
        tbody.innerHTML = '<tr><td colspan="5" class="table-placeholder">加载中...</td></tr>';
    }
    try {
        const params = getAuditOutboxFilterParams(reset);
        params.set('offset', String(offset));
        const response = await apiCall('/api/email/outbox?' + params.toString());
        if (generation !== auditOutboxState.requestGeneration) return;
        const data = response?.data || {};
        const items = Array.isArray(data.items) ? data.items : [];
        const total = Number(data.total || 0);
        auditOutboxState.hasMore = offset + items.length < total;
        auditOutboxState.offset = offset + items.length;
        auditOutboxState.loaded = true;
        const mailCount = document.querySelector('#audit-outbox-panel .audit-mail-count');
        if (mailCount) mailCount.textContent = '共 ' + total.toLocaleString('zh-CN') + ' 条';
        const scope = document.querySelector('#audit-outbox-panel .audit-mail-scope');
        if (scope) scope.textContent = $('email-outbox-all-users')?.checked ? '管理员全量视图' : '仅显示本人记录';
        updateAuditOutboxSummary(data.summary || {total});
        renderAuditOutboxRows(items, { append });
        const wrapper = $('email-outbox-load-more-wrapper');
        if (wrapper) {
            wrapper.innerHTML = auditOutboxState.hasMore
                ? '<button class="btn-xs" type="button" data-click="loadAuditOutboxMore">加载更多（共 ' + total + ' 条）</button>'
                : (items.length ? '<span class="audit-mail-list-complete">已显示全部记录 · 点击记录查看详情</span>' : '');
        }
    } catch (error) {
        if (generation !== auditOutboxState.requestGeneration) return;
        if (!append) {
            const mailCount = document.querySelector('#audit-outbox-panel .audit-mail-count');
            if (mailCount) mailCount.textContent = '加载失败';
            tbody.innerHTML = '<tr><td colspan="5" class="table-placeholder">加载历史邮件失败: '
                + escapeHtml(error.message) + '</td></tr>';
        } else {
            showToast('加载更多历史邮件失败: ' + error.message, 'error');
        }
    } finally {
        if (generation === auditOutboxState.requestGeneration) {
            auditOutboxState.loading = false;
            tbody.setAttribute('aria-busy', 'false');
        }
    }
}

async function loadAuditOutboxMore() {
    if (!auditOutboxState.loading && auditOutboxState.hasMore) {
        await loadAuditOutbox(false, true);
    }
}

window.exportAuditOutbox = exportAuditOutbox;

// 页签容器随登录就绪显示（模板与 users-view-tabs 一致）。
function auditViewTabsEnsureVisible() {
    if (!state.authReady) return;
    const tabs = document.getElementById('audit-view-tabs');
    if (tabs) tabs.style.display = 'flex';
    bindAuditIntegrityCheck();
}

runAfterAuthReady(auditViewTabsEnsureVisible);
window.addEventListener('gms:auth-ready', auditViewTabsEnsureVisible);

window.auditOutboxToggle = auditOutboxToggle;
window.loadAuditOutbox = loadAuditOutbox;
window.loadAuditOutboxMore = loadAuditOutboxMore;


// 磁盘审计详情与刷新逻辑独立载入 audit-disk.js。
