// ==================== Agent 接入管理（2026-09-10 UI/交互整理） ====================
// 管理员创建一次性配对码并管理 Agent Service Token。原始 token 只在
// Agent 兑换配对码时返回一次，管理页只展示不敏感的 token 元数据。

let agentAccessLoaded = false;
let agentAccessScopesCache = null;
let agentAccessTokensCache = [];
let agentAccessLoadSequence = 0;

const AGENT_ACCESS_DEFAULT_SCOPES = [
    'devices.read', 'devices.lease', 'devices.use_leased',
    'tests.execute', 'tests.cancel', 'jobs.read', 'reports.read',
    // 只读证据/分析链：默认勾选让 mint 出的
    // enrollment 能直接跑通 SKILL 文档化的 Redmine 工作流；写操作类
    // scope（devices.inventory 等）仍需手动勾选。
    'redmine.read', 'artifacts.read_own', 'apk.analyze_own', 'sdk.read',
];

const AGENT_ACCESS_SCOPE_LABELS = {
    'devices.read': '读取设备清单',
    'devices.lease': '租用与认领设备',
    'devices.use_leased': '操作已租用设备',
    'devices.inventory': '管理设备清单',
    'tests.execute': '启动测试任务',
    'tests.cancel': '取消自己的测试任务',
    'jobs.read': '读取任务状态与事件',
    'reports.read': '读取测试报告',
    'redmine.read': '读取授权范围内的 Redmine 数据',
    'artifacts.read_own': '读取自己的证据与制品',
    'apk.analyze_own': '分析自己的 APK 制品',
    'sdk.read': '查询已配置的 SDK 源码',
};

function agentAccessPanelIsOpen() {
    const panel = document.getElementById('agent-access-panel');
    if (!panel) return false;
    // 状态判定不依赖 inline style（初始态来自 HTML 的
    // style="display:none"，toggle 后由 JS 改写）。computed style 同时
    // 覆盖"尚未触碰 inline style"与"已被 toggle"两种来源。
    if (panel.style.display === 'flex') return true;
    return getComputedStyle(panel).display !== 'none';
}

function agentAccessToggle(show) {
    const panel = document.getElementById('agent-access-panel');
    const mainView = document.getElementById('users-main-view');
    if (!panel || !mainView) return;
    const visible = show !== undefined ? Boolean(show) : !agentAccessPanelIsOpen();
    if (visible && !agentAccessIsAdmin()) {
        showToast('仅管理员可管理 Agent 接入', 'warning');
        return;
    }

    panel.style.display = visible ? 'flex' : 'none';
    mainView.style.display = visible ? 'none' : '';
    // 左上角页面标题保持“👥 用户管理”不变：视图切换由下方
    // “用户列表 / 🔑 Agent 接入”页签的 active 态表达，不重写标题。

    const usersTab = document.getElementById('users-list-tab');
    const agentTab = document.getElementById('agent-access-tab');
    usersTab?.classList.toggle('active', !visible);
    agentTab?.classList.toggle('active', visible);
    usersTab?.setAttribute('aria-selected', String(!visible));
    agentTab?.setAttribute('aria-selected', String(visible));

    if (visible) {
        if (typeof stopUsersAutoRefresh === 'function') stopUsersAutoRefresh();
        agentAccessReload();
    } else if (typeof currentPage === 'undefined' || currentPage === 'users') {
        if (typeof loadUsersList === 'function') loadUsersList();
        if (typeof startUsersAutoRefresh === 'function') startUsersAutoRefresh();
    }
}

function agentAccessIsAdmin() {
    const user = state.currentUser || {};
    return user.role === 'admin' || user.is_admin === true;
}

function agentAccessEnsureVisibleForRole() {
    if (!state.authReady) return;
    const enabled = agentAccessIsAdmin();
    const tabs = document.getElementById('users-view-tabs');
    if (tabs) tabs.style.display = enabled ? 'flex' : 'none';
    if (!enabled && agentAccessPanelIsOpen()) agentAccessToggle(false);
}

async function agentAccessReload(button) {
    const container = document.getElementById('agent-access-tokens');
    const refreshButton = button || document.getElementById('agent-access-refresh');
    if (!container || refreshButton?.disabled) return;
    const sequence = ++agentAccessLoadSequence;
    if (refreshButton) {
        refreshButton.disabled = true;
        refreshButton.textContent = '刷新中…';
    }
    if (!agentAccessLoaded) {
        container.innerHTML = '<tr><td colspan="9" class="agent-access-empty">正在加载 Agent Token…</td></tr>';
    }
    try {
        const [tokensResp, scopesResp] = await Promise.all([
            apiCall('/api/auth/agent-tokens', 'GET', null, {silentToast: true}),
            agentAccessScopesCache
                ? Promise.resolve({scopes: agentAccessScopesCache})
                : apiCall('/api/auth/agent-scopes', 'GET', null, {silentToast: true}),
        ]);
        if (sequence !== agentAccessLoadSequence) return;
        agentAccessScopesCache = agentAccessNormalizeScopes(scopesResp?.scopes);
        agentAccessTokensCache = Array.isArray(tokensResp?.tokens) ? tokensResp.tokens : [];
        agentAccessRenderScopeCheckboxes();
        agentAccessUpdateStats();
        agentAccessFilterTokens();
        agentAccessLoaded = true;
    } catch (error) {
        debugLog('[AgentAccess] load failed:', error);
        if (!agentAccessLoaded) {
            container.innerHTML = `<tr><td colspan="9" class="agent-access-empty">加载失败：${agentAccessEscape(error.message || '需要管理员会话')}</td></tr>`;
        } else {
            showToast(`Agent Token 刷新失败：${error.message}`, 'error');
        }
    } finally {
        if (sequence === agentAccessLoadSequence && refreshButton) {
            refreshButton.disabled = false;
            refreshButton.textContent = '↻ 刷新';
        }
    }
}

function agentAccessNormalizeScopes(raw) {
    if (Array.isArray(raw)) {
        return raw.map(entry => {
            if (typeof entry === 'string') {
                return {name: entry, description: AGENT_ACCESS_SCOPE_LABELS[entry] || ''};
            }
            const name = entry?.name;
            return name ? {
                name,
                description: AGENT_ACCESS_SCOPE_LABELS[name] || entry.description || '',
            } : null;
        }).filter(Boolean);
    }
    if (raw && typeof raw === 'object') {
        return Object.entries(raw).map(([name, description]) => ({
            name,
            description: AGENT_ACCESS_SCOPE_LABELS[name] || String(description || ''),
        }));
    }
    return [];
}

function agentAccessRenderScopeCheckboxes() {
    const wrap = document.getElementById('agent-access-scopes');
    if (!wrap || !Array.isArray(agentAccessScopesCache)) return;
    const previousSelection = new Set(Array.from(
        wrap.querySelectorAll('input:checked'), input => input.value
    ));
    wrap.innerHTML = '';
    agentAccessScopesCache.forEach(scope => {
        const label = document.createElement('label');
        label.className = 'agent-access-scope-option';
        label.title = scope.description || scope.name;
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = scope.name;
        input.className = 'agent-access-scope';
        input.checked = previousSelection.size
            ? previousSelection.has(scope.name)
            : AGENT_ACCESS_DEFAULT_SCOPES.includes(scope.name);
        const name = document.createElement('strong');
        name.textContent = scope.name;
        const description = document.createElement('small');
        description.textContent = scope.description || '自定义授权范围';
        label.append(input, name, description);
        wrap.appendChild(label);
    });
}

function agentAccessSetScopeSelection(mode) {
    document.querySelectorAll('#agent-access-scopes input').forEach(input => {
        input.checked = mode === 'all'
            || (mode === 'default' && AGENT_ACCESS_DEFAULT_SCOPES.includes(input.value));
    });
}

function agentAccessToggleCreate(force) {
    const form = document.getElementById('agent-access-enroll-form');
    const toggle = document.getElementById('agent-access-create-toggle');
    if (!form) return;
    const shouldOpen = force === undefined ? form.style.display === 'none' : Boolean(force);
    form.style.display = shouldOpen ? 'block' : 'none';
    toggle?.setAttribute('aria-expanded', String(shouldOpen));
    if (shouldOpen) {
        if (!agentAccessScopesCache) agentAccessReload();
        window.setTimeout(() => document.getElementById('agent-access-name')?.focus(), 0);
    }
}

async function agentAccessCreateEnrollment() {
    const nameInput = document.getElementById('agent-access-name');
    const submit = document.getElementById('agent-access-create-submit');
    const name = nameInput?.value?.trim() || '';
    const workers = document.getElementById('agent-access-workers')?.value?.trim() || '*';
    const devices = document.getElementById('agent-access-devices')?.value?.trim() || '*';
    const days = Number.parseInt(document.getElementById('agent-access-days')?.value || '90', 10);
    const scopes = Array.from(
        document.querySelectorAll('#agent-access-scopes input:checked'), input => input.value
    );
    if (!name) {
        nameInput?.focus();
        showToast('请填写 Agent 标识名', 'warning');
        return;
    }
    if (!Number.isInteger(days) || days < 1 || days > 365) {
        showToast('Token 有效期需为 1–365 天', 'warning');
        return;
    }
    if (scopes.length === 0) {
        showToast('请至少选择一个授权范围', 'warning');
        return;
    }
    const granted = window.requestElevatedAccess
        ? await window.requestElevatedAccess(`创建 Agent 配对码：${name}`)
        : false;
    if (!granted) return;

    if (submit) {
        submit.disabled = true;
        submit.textContent = '生成中…';
    }
    try {
        const resp = await apiCall('/api/auth/agent-enrollment-codes', 'POST', {
            name,
            scopes,
            allowed_workers: workers,
            allowed_devices: devices,
            expires_days: days,
        }, {silentToast: true});
        const enrollment = resp.enrollment || {};
        const code = String(enrollment.code || '');
        const codeCard = document.getElementById('agent-access-code-card');
        const codeResult = document.getElementById('agent-access-code-result');
        if (codeResult) codeResult.textContent = code;
        const expiry = document.getElementById('agent-access-code-expiry');
        if (expiry) expiry.textContent = enrollment.expires_at
            ? `有效至 ${agentAccessFormatDate(enrollment.expires_at)}，只能使用一次`
            : '5 分钟内有效，只能使用一次';
        if (codeCard) codeCard.hidden = false;
        agentAccessToggleCreate(false);
        if (nameInput) nameInput.value = '';
        showToast('配对码已生成，请立即复制到目标服务器', 'success');
        agentAccessReload();
    } catch (error) {
        debugLog('[AgentAccess] enrollment failed:', error);
        showToast(`配对码创建失败：${error.message}`, 'error');
    } finally {
        if (submit) {
            submit.disabled = false;
            submit.textContent = '生成配对码';
        }
    }
}

async function agentAccessCopyEnrollmentCode() {
    const code = document.getElementById('agent-access-code-result')?.textContent?.trim();
    if (!code) return;
    try {
        await navigator.clipboard.writeText(code);
        showToast('配对码已复制', 'success');
    } catch (error) {
        debugLog('[AgentAccess] clipboard failed:', error);
        showToast('复制失败，请手动选择配对码复制', 'warning');
    }
}

function agentAccessTokenStatus(token) {
    if (token.revoked_at) return 'revoked';
    const expiresAt = token.expires_at ? new Date(token.expires_at) : null;
    if (expiresAt && !Number.isNaN(expiresAt.getTime()) && expiresAt <= new Date()) return 'expired';
    return 'active';
}

function agentAccessUpdateStats() {
    const active = agentAccessTokensCache.filter(token => agentAccessTokenStatus(token) === 'active').length;
    const inactive = agentAccessTokensCache.length - active;
    const total = document.getElementById('agent-token-total-count');
    const activeCount = document.getElementById('agent-token-active-count');
    const inactiveCount = document.getElementById('agent-token-inactive-count');
    if (total) total.textContent = agentAccessTokensCache.length;
    if (activeCount) activeCount.textContent = active;
    if (inactiveCount) inactiveCount.textContent = inactive;
}

function agentAccessSetStatusFilter(status) {
    const select = document.getElementById('agent-access-status-filter');
    if (select) select.value = status || '';
    agentAccessFilterTokens();
}

function agentAccessFilterTokens() {
    const query = (document.getElementById('agent-access-search')?.value || '').trim().toLowerCase();
    const status = document.getElementById('agent-access-status-filter')?.value || '';
    const tokens = agentAccessTokensCache.filter(token => {
        const tokenStatus = agentAccessTokenStatus(token);
        if (status === 'inactive' && tokenStatus === 'active') return false;
        if (status && status !== 'inactive' && tokenStatus !== status) return false;
        if (!query) return true;
        return [token.name, token.id, token.owner_user_id, token.allowed_workers, token.allowed_devices]
            .join(' ').toLowerCase().includes(query);
    });
    document.querySelectorAll('[data-agent-token-status-card]').forEach(card => {
        card.classList.toggle('active', card.dataset.agentTokenStatusCard === status);
    });
    agentAccessRenderTokens(tokens);
}

function agentAccessRenderTokens(tokens) {
    const container = document.getElementById('agent-access-tokens');
    if (!container) return;
    if (!Array.isArray(tokens) || tokens.length === 0) {
        const filtersActive = Boolean(
            document.getElementById('agent-access-search')?.value
            || document.getElementById('agent-access-status-filter')?.value
        );
        container.innerHTML = `<tr><td colspan="9" class="agent-access-empty">${filtersActive ? '没有匹配的 Agent Token' : '尚无 Agent Service Token'}</td></tr>`;
        return;
    }
    const statusLabels = {active: '有效', expired: '已过期', revoked: '已吊销'};
    container.innerHTML = tokens.map(token => {
        const status = agentAccessTokenStatus(token);
        const scopes = Array.isArray(token.scopes)
            ? token.scopes
            : String(token.scopes || '').split(',').filter(Boolean);
        const scopesText = scopes.length ? scopes.join(', ') : '无授权范围';
        const revokeButton = status === 'active'
            ? `<button class="btn-xxs btn-danger" type="button" data-agent-token-id="${agentAccessEscape(token.id)}">吊销</button>`
            : '—';
        return `
            <tr>
                <td class="agent-token-cell-identity" title="${agentAccessEscape(`${token.name || ''} · ${token.id || ''}`)}">
                    <strong title="${agentAccessEscape(token.name || '')}">${agentAccessEscape(token.name || '未命名 Agent')}</strong>
                    <code title="${agentAccessEscape(token.id || '')}">${agentAccessEscape(token.id || '—')}</code>
                </td>
                <td><span class="agent-token-status ${status}">${statusLabels[status]}</span></td>
                <td class="agent-token-cell-scopes mono" title="${agentAccessEscape(scopesText)}">${agentAccessEscape(scopesText)}</td>
                <td class="agent-token-cell-acl mono" title="${agentAccessEscape(token.allowed_workers || '*')}">${agentAccessEscape(token.allowed_workers || '*')}</td>
                <td class="agent-token-cell-acl mono" title="${agentAccessEscape(token.allowed_devices || '*')}">${agentAccessEscape(token.allowed_devices || '*')}</td>
                <td title="${agentAccessEscape(token.owner_user_id || '')}">${agentAccessEscape(token.owner_user_id || '—')}</td>
                <td class="agent-token-cell-date" title="创建：${agentAccessFormatDate(token.created_at)}；到期：${agentAccessFormatDate(token.expires_at)}">${agentAccessFormatShortDate(token.created_at)} → ${agentAccessFormatShortDate(token.expires_at)}</td>
                <td class="agent-token-cell-date" title="${agentAccessFormatDate(token.last_used_at)}">${agentAccessFormatShortDate(token.last_used_at, true)}</td>
                <td>${revokeButton}</td>
            </tr>`;
    }).join('');
    container.querySelectorAll('[data-agent-token-id]').forEach(button => {
        button.addEventListener('click', () => agentAccessRevoke(button.dataset.agentTokenId, button));
    });
}

function agentAccessFormatShortDate(value, includeTime = false) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', includeTime
        ? {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}
        : {year: '2-digit', month: '2-digit', day: '2-digit'});
}

function agentAccessFormatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
    });
}

function agentAccessEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
}

async function agentAccessRevoke(tokenId, button) {
    if (!tokenId) return;
    const token = agentAccessTokensCache.find(item => item.id === tokenId);
    const confirmed = await showConfirmDialog(
        '吊销 Agent Token',
        `确定吊销 ${token?.name || tokenId} 吗？该 Agent 将立即失去平台访问权。`
    );
    if (!confirmed) return;
    const granted = window.requestElevatedAccess
        ? await window.requestElevatedAccess(`吊销 Agent Token：${token?.name || tokenId}`)
        : false;
    if (!granted) return;
    if (button) {
        button.disabled = true;
        button.textContent = '吊销中…';
    }
    try {
        await apiCall(`/api/auth/agent-tokens/${encodeURIComponent(tokenId)}`, 'DELETE', null, {silentToast: true});
        showToast('Agent Token 已吊销', 'success');
        await agentAccessReload();
    } catch (error) {
        debugLog('[AgentAccess] revoke failed:', error);
        showToast(`吊销失败：${error.message}`, 'error');
        if (button) {
            button.disabled = false;
            button.textContent = '吊销';
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    runAfterAuthReady(agentAccessEnsureVisibleForRole);
    window.addEventListener('gms:auth-ready', agentAccessEnsureVisibleForRole);
});
