// ==================== Agent 接入管理（2026-09-10 UI/交互整理） ====================
// 管理员创建一次性配对码并管理 Agent Service Token。原始 token 只在
// Agent 兑换配对码时返回一次，管理页只展示不敏感的 token 元数据。

let agentAccessLoaded = false;
let agentAccessScopesCache = null;
let agentAccessTokensCache = [];
let agentAccessLoadSequence = 0;
let agentAccessCreateSequence = 0;

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
    'suites.read': '读取 Worker 测试套件文件',
    'redmine.read': '读取授权范围内的 Redmine 数据',
    'artifacts.read_own': '读取自己的证据与制品',
    'apk.analyze_own': '分析自己的 APK 制品',
    'sdk.read': '查询已配置的 SDK 源码',
    'knowledge.read': '检索 Android 背景知识',
    'build.read': '查看编译环境与配置',
    'build.execute': '创建与执行编译任务',
    'build.cancel': '取消自己的编译任务',
};

const AGENT_ACCESS_SCOPE_GROUPS = [
    {label: '设备与调试', prefixes: ['devices.']},
    {label: '测试与报告', prefixes: ['tests.', 'jobs.', 'reports.', 'suites.']},
    {label: '证据与分析', prefixes: ['redmine.', 'artifacts.', 'apk.', 'sdk.', 'knowledge.']},
    {label: '编译构建', prefixes: ['build.']},
    {label: '其他能力', prefixes: []},
];

function agentAccessScopeGroup(name) {
    return AGENT_ACCESS_SCOPE_GROUPS.find(group => group.prefixes.some(prefix => name.startsWith(prefix)))
        || AGENT_ACCESS_SCOPE_GROUPS[AGENT_ACCESS_SCOPE_GROUPS.length - 1];
}

function agentAccessPanelIsOpen() {
    const panel = document.getElementById('agent-access-panel');
    if (!panel) return false;
    // 状态判定不依赖 inline style（初始态来自 HTML 的
    // style="display:none"，toggle 后由 JS 改写）。computed style 同时
    // 覆盖"尚未触碰 inline style"与"已被 toggle"两种来源。
    if (panel.style.display === 'flex') return true;
    return getComputedStyle(panel).display !== 'none';
}

async function agentAccessToggle(show) {
    const panel = document.getElementById('agent-access-panel');
    const mainView = document.getElementById('users-main-view');
    if (!panel || !mainView) return;
    const visible = show !== undefined ? Boolean(show) : !agentAccessPanelIsOpen();
    if (visible && !agentAccessIsAdmin() && !state.elevated) {
        const granted = window.requestElevatedAccess
            ? await window.requestElevatedAccess('管理 Agent 接入')
            : false;
        if (!granted) return;
    }

    panel.style.display = visible ? 'flex' : 'none';
    mainView.style.display = visible ? 'none' : '';
    // 页签状态随 F5 刷新恢复（页面初始化经 runPageInitializers 读取）。
    try { sessionStorage.setItem('gms_users_view', visible ? 'agent' : 'list'); } catch (_error) {}
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
        agentAccessLoadSdkSources();
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
    const tabs = document.getElementById('users-view-tabs');
    // 入口对所有已登录用户可见；打开管理面板前仍要求管理员身份或
    // 当前会话已完成管理员验证，不能仅靠前端可见性放宽权限。
    if (tabs) tabs.style.display = 'flex';
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
        container.innerHTML = '<tr><td colspan="7" class="agent-access-empty">正在加载接入凭据…</td></tr>';
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
            container.innerHTML = `<tr><td colspan="7" class="agent-access-empty">加载失败：${agentAccessEscape(error.message || '需要管理员会话')}</td></tr>`;
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
    const initialized = Boolean(wrap.querySelector('input'));
    const previousSelection = new Set(Array.from(
        wrap.querySelectorAll('input:checked'), input => input.value
    ));
    wrap.replaceChildren();
    const groups = new Map();
    AGENT_ACCESS_SCOPE_GROUPS.forEach(group => {
        const entries = agentAccessScopesCache.filter(scope => agentAccessScopeGroup(scope.name) === group);
        if (!entries.length) return;
        const section = document.createElement('section');
        section.className = 'agent-access-scope-group';
        const heading = document.createElement('h4');
        heading.textContent = group.label;
        const options = document.createElement('div');
        options.className = 'agent-access-scope-options';
        section.append(heading, options);
        wrap.appendChild(section);
        groups.set(group, options);
    });
    agentAccessScopesCache.forEach(scope => {
        const label = document.createElement('label');
        label.className = 'agent-access-scope-option';
        label.title = scope.description || scope.name;
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = scope.name;
        input.className = 'agent-access-scope';
        input.checked = initialized
            ? previousSelection.has(scope.name)
            : AGENT_ACCESS_DEFAULT_SCOPES.includes(scope.name);
        const name = document.createElement('strong');
        name.textContent = scope.description || scope.name;
        const description = document.createElement('small');
        description.textContent = scope.name;
        label.append(input, name, description);
        groups.get(agentAccessScopeGroup(scope.name)).appendChild(label);
    });
}

function agentAccessSetScopeSelection(mode) {
    document.querySelectorAll('#agent-access-scopes input').forEach(input => {
        input.checked = mode === 'all'
            || (mode === 'default' && AGENT_ACCESS_DEFAULT_SCOPES.includes(input.value));
    });
}

function agentAccessUpdateTargetSummary() {
    const summary = document.getElementById('agent-access-target-summary');
    if (!summary) return;
    const descriptions = [['workers', 'Worker'], ['devices', '设备']].map(([id, label]) => {
        const value = document.getElementById('agent-access-' + id)?.value.trim() || '*';
        return value.split(',').some(part => part.trim() === '*')
            ? '全部 ' + label
            : `${value.split(',').filter(part => part.trim()).length} 个 ${label}`;
    });
    summary.textContent = descriptions.join(' · ');
}

function agentAccessToggleCreate(force) {
    const form = document.getElementById('agent-access-enroll-form');
    const toggle = document.getElementById('agent-access-create-toggle');
    if (!form) return;
    const modalId = 'agent-access-create-modal';
    const shouldOpen = force === undefined ? !ModalManager.isOpen(modalId) : Boolean(force);
    if (shouldOpen) {
        agentAccessCreateSequence += 1;
        agentAccessClearCode();
        form.hidden = false;
        document.getElementById('agent-access-create-actions').hidden = false;
        document.getElementById('agent-access-result-actions').hidden = true;
        document.getElementById('agent-access-create-title').textContent = '创建配对码';
        form.querySelectorAll('.agent-access-details').forEach(details => { details.open = true; });
        agentAccessUpdateTargetSummary();
        if (!agentAccessScopesCache) agentAccessReload();
        ModalManager.onClose(modalId, () => {
            agentAccessCreateSequence += 1;
            toggle?.setAttribute('aria-expanded', 'false');
            agentAccessClearCode();
        });
        toggle?.setAttribute('aria-expanded', 'true');
        ModalManager.open(modalId);
        document.querySelector('#agent-access-create-modal .modal-body').scrollTop = 0;
    } else {
        ModalManager.close(modalId);
    }
}

async function agentAccessCreateEnrollment() {
    const nameInput = document.getElementById('agent-access-name');
    const submit = document.getElementById('agent-access-create-submit');
    if (submit?.disabled || !ModalManager.isOpen('agent-access-create-modal')) return;
    const sequence = agentAccessCreateSequence;
    const name = nameInput?.value?.trim() || '';
    const workers = document.getElementById('agent-access-workers')?.value?.trim() || '*';
    const devices = document.getElementById('agent-access-devices')?.value?.trim() || '*';
    const days = Number(document.getElementById('agent-access-days')?.value || '90');
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
    if (submit) {
        submit.disabled = true;
        submit.textContent = '生成中…';
    }
    try {
        const granted = window.requestElevatedAccess
            ? await window.requestElevatedAccess(`创建 Agent 配对码：${name}`)
            : false;
        if (!granted || sequence !== agentAccessCreateSequence || !ModalManager.isOpen('agent-access-create-modal')) return;
        const resp = await apiCall('/api/auth/agent-enrollment-codes', 'POST', {
            name,
            scopes,
            allowed_workers: workers,
            allowed_devices: devices,
            expires_days: days,
        }, {silentToast: true});
        if (sequence !== agentAccessCreateSequence || !ModalManager.isOpen('agent-access-create-modal')) return;
        const enrollment = resp.enrollment || {};
        const code = String(enrollment.code || '');
        if (!code) throw new Error('服务未返回配对码，请重试');
        const codeCard = document.getElementById('agent-access-code-card');
        const codeResult = document.getElementById('agent-access-code-result');
        if (codeResult) codeResult.textContent = code;
        const context = document.getElementById('agent-access-code-context');
        if (context) context.textContent = `${name} · ${scopes.length} 项权限 · 接入凭据 ${days} 天有效`;
        const expiry = document.getElementById('agent-access-code-expiry');
        if (expiry) expiry.textContent = enrollment.expires_at
            ? `有效至 ${agentAccessFormatDate(enrollment.expires_at)}，只能使用一次`
            : '5 分钟内有效，只能使用一次';
        document.getElementById('agent-access-enroll-form').hidden = true;
        document.getElementById('agent-access-create-actions').hidden = true;
        document.getElementById('agent-access-result-actions').hidden = false;
        document.getElementById('agent-access-create-title').textContent = '配对码已生成';
        if (codeCard) codeCard.hidden = false;
        if (nameInput) nameInput.value = '';
        showToast('配对码已生成，请立即复制到目标服务器', 'success');
        agentAccessReload();
        document.querySelector('#agent-access-create-modal .modal-body').scrollTop = 0;
        document.getElementById('agent-access-copy-code')?.focus({preventScroll: true});
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

function agentAccessClearCode() {
    const card = document.getElementById('agent-access-code-card');
    if (card) card.hidden = true;
    const code = document.getElementById('agent-access-code-result');
    if (code) code.textContent = '';
}

function agentAccessDismissCode() {
    ModalManager.close('agent-access-create-modal');
}

async function agentAccessCopyEnrollmentCode() {
    const code = document.getElementById('agent-access-code-result')?.textContent?.trim();
    if (!code) {
        showToast('暂无可复制的配对码，请先生成', 'warning');
        return;
    }
    if (navigator.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(code);
            showToast('配对码已复制', 'success');
            return;
        } catch (error) {
            // 权限被拒或焦点竞争：落到 execCommand 降级路径。
            debugLog('[AgentAccess] clipboard API failed:', error);
        }
    }
    // 降级复制：隐藏 textarea + execCommand，兼容无 Clipboard API 的
    // 环境（HTTP 非安全上下文、旧浏览器）。
    try {
        const helper = document.createElement('textarea');
        helper.value = code;
        helper.setAttribute('readonly', '');
        helper.style.position = 'fixed';
        helper.style.opacity = '0';
        document.body.appendChild(helper);
        helper.select();
        const copied = document.execCommand('copy');
        helper.remove();
        if (copied) {
            showToast('配对码已复制', 'success');
            return;
        }
        throw new Error('execCommand returned false');
    } catch (error) {
        debugLog('[AgentAccess] copy fallback failed:', error);
        showToast('复制失败，请手动选中配对码后按 Ctrl+C', 'warning');
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
        card.setAttribute('aria-pressed', String(card.dataset.agentTokenStatusCard === status));
    });
    const count = document.getElementById('agent-access-list-count');
    if (count) count.textContent = `显示 ${tokens.length} / ${agentAccessTokensCache.length} 个凭据 · 原始 Token 不会再次显示`;
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
        container.innerHTML = `<tr><td colspan="7" class="agent-access-empty">${filtersActive ? '没有匹配的接入凭据，请调整搜索或筛选条件。' : '尚无 Agent 接入。点击「创建配对码」，完成目标服务器的安装与配对。'}</td></tr>`;
        return;
    }
    const statusLabels = {active: '有效', expired: '已过期', revoked: '已吊销'};
    container.innerHTML = tokens.map(token => {
        const status = agentAccessTokenStatus(token);
        const scopes = Array.isArray(token.scopes)
            ? token.scopes
            : String(token.scopes || '').split(',').filter(Boolean);
        const scopesText = scopes.length ? scopes.join(', ') : '无授权范围';
        const groupLabels = [...new Set(scopes.map(scope => agentAccessScopeGroup(scope).label))];
        const workers = token.allowed_workers === '*' || !token.allowed_workers ? '全部 Worker' : 'Worker：' + token.allowed_workers;
        const devices = token.allowed_devices === '*' || !token.allowed_devices ? '全部设备' : '设备：' + token.allowed_devices;
        const revokeButton = status === 'active'
            ? `<button class="btn-xxs btn-danger" type="button" data-agent-token-id="${agentAccessEscape(token.id)}">吊销</button>`
            : '—';
        return `
            <tr>
                <td class="agent-token-cell-name" title="${agentAccessEscape(token.name || '')}">
                    <strong>${agentAccessEscape(token.name || '未命名 Agent')}</strong>
                    <code class="agent-token-id" title="${agentAccessEscape(token.id || '')}">${agentAccessEscape(token.id || '—')}</code>
                </td>
                <td><span class="agent-token-status ${status}">${statusLabels[status]}</span></td>
                <td class="agent-token-cell-scopes" title="${agentAccessEscape(scopesText)}">
                    <button class="agent-token-scope-link" type="button" data-click="agentAccessShowScopes" data-a0="${agentAccessEscape(token.id)}">${scopes.length} 项权限</button>
                    <span class="agent-token-secondary">${agentAccessEscape(groupLabels.join(' · ') || '未授权')}</span>
                </td>
                <td class="agent-token-cell-acl">
                    <span title="${agentAccessEscape(token.allowed_workers || '*')}">${agentAccessEscape(workers)}</span>
                    <span class="agent-token-secondary" title="${agentAccessEscape(token.allowed_devices || '*')}">${agentAccessEscape(devices)}</span>
                </td>
                <td title="${agentAccessEscape(token.owner_user_id || '')}">${agentAccessEscape(token.owner_user_id || '—')}</td>
                <td class="agent-token-cell-date" title="创建：${agentAccessFormatDate(token.created_at)}；到期：${agentAccessFormatDate(token.expires_at)}">
                    <span>${token.expires_at ? agentAccessFormatShortDate(token.expires_at) + ' 到期' : '长期有效'}</span>
                    <span class="agent-token-secondary" title="${agentAccessFormatDate(token.last_used_at)}">· ${token.last_used_at ? '最近 ' + agentAccessFormatShortDate(token.last_used_at, true) : '尚未使用'}</span>
                </td>
                <td>${revokeButton}</td>
            </tr>`;
    }).join('');
    container.querySelectorAll('[data-agent-token-id]').forEach(button => {
        button.addEventListener('click', () => agentAccessRevoke(button.dataset.agentTokenId, button));
    });
}

function agentAccessShowScopes(tokenId) {
    const token = agentAccessTokensCache.find(item => item.id === tokenId);
    if (!token) return;
    const body = document.getElementById('agent-access-token-scope-list');
    if (!body) return;
    const scopes = Array.isArray(token.scopes) ? token.scopes : String(token.scopes || '').split(',').filter(Boolean);
    body.replaceChildren();
    const identity = document.createElement('p');
    identity.className = 'agent-access-form-hint';
    identity.textContent = `${token.name || '未命名 Agent'} · ${token.id}`;
    body.appendChild(identity);
    const list = document.createElement('ul');
    list.className = 'agent-access-token-scope-list';
    scopes.forEach(scope => {
        const item = document.createElement('li');
        const name = document.createElement('span');
        name.textContent = AGENT_ACCESS_SCOPE_LABELS[scope] || scope;
        const code = document.createElement('code');
        code.textContent = scope;
        item.append(name, code);
        list.appendChild(item);
    });
    if (!scopes.length) list.textContent = '无授权范围';
    body.appendChild(list);
    ModalManager.open('agent-access-scopes-modal');
}

function agentAccessCloseScopes() {
    ModalManager.close('agent-access-scopes-modal');
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

// ==================== 已配置的 SDK 源码源（只读展示 + 添加SDK 管理） ====================
// 列表 = GET /api/sdk/sources（registry 全量，sdk.read）叠加
// GET /api/sdk/manage/sources（用户注册源 + 每次刷新的远端路径校验，
// human-only）。管理面（probe/添加/删除）走 /api/sdk/manage/*：
// 读需登录人类，写需管理员提权（服务端 require_elevated_admin 强制）。
// repo_root 永不出现在只读列表里；SSH 口令任何响应都不回传。

let agentSdkSourcesLoaded = false;
let agentSdkSourcesSequence = 0;
let agentSdkSourcesCache = [];
let agentSdkUserLiveCache = {};
let agentSdkServersCache = [];

const AGENT_SDK_PROVIDER_LABELS = {
    local_git: '本机 Git 仓库',
    ssh_git: '编译服务器（SSH 只读）',
    codesearch: 'OpenGrok 代码索引',
};

async function agentAccessLoadSdkSources(button) {
    const tbody = document.getElementById('agent-sdk-sources-tbody');
    if (!tbody || (button && button.disabled)) return;
    if (agentSdkSourcesLoaded && !button) return; // 打开页签时自动加载一次
    const sequence = ++agentSdkSourcesSequence;
    if (button) {
        button.disabled = true;
        const label = button.lastElementChild?.nextSibling;
        if (label) label.textContent = ' 刷新中…';
    }
    try {
        const [registryResp, manageResp] = await Promise.allSettled([
            apiCall('/api/sdk/sources', 'GET', null, {silentToast: true}),
            apiCall('/api/sdk/manage/sources', 'GET', null, {silentToast: true}),
        ]);
        if (sequence !== agentSdkSourcesSequence) return;
        const sources = Array.isArray(registryResp?.value?.data?.sources)
            ? registryResp.value.data.sources : [];
        agentSdkUserLiveCache = {};
        if (manageResp.status === 'fulfilled') {
            (manageResp.value?.data?.sources || []).forEach(source => {
                agentSdkUserLiveCache[source.source_id] = source;
            });
        }
        // manage 面不可用（如权限不足）时只降级为无校验列，不阻塞展示。
        agentSdkSourcesCache = sources;
        agentAccessRenderSdkSources(tbody, sources);
        agentSdkSourcesLoaded = true;
    } catch (error) {
        if (sequence !== agentSdkSourcesSequence) return;
        tbody.innerHTML = `<tr><td colspan="7" class="agent-access-empty">加载失败：${agentAccessEscape(error.message || '需要 sdk.read 权限')}</td></tr>`;
    } finally {
        if (sequence === agentSdkSourcesSequence && button) {
            button.disabled = false;
            const label = button.lastElementChild?.nextSibling;
            if (label) label.textContent = ' 刷新';
        }
    }
}

function agentAccessRenderSdkSources(tbody, sources) {
    const count = document.getElementById('agent-sdk-sources-count');
    if (count) {
        count.textContent = sources.length
            ? `分析 Redmine 问题时的只读源码取证来源，共 ${sources.length} 个（repo_root 不对外暴露）。`
            : '尚未配置任何 SDK 源；点击「添加SDK」绑定编译服务器目录，或由管理员写入部署配置。';
    }
    if (!sources.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="agent-access-empty">未配置 SDK 源。点击右上角「＋ 添加SDK」从编译服务器选择 SDK 根目录绑定。</td></tr>';
        return;
    }
    tbody.innerHTML = sources.map(source => {
        const providerLabel = AGENT_SDK_PROVIDER_LABELS[source.provider] || agentAccessEscape(source.provider);
        let location = '—';
        if (source.provider === 'ssh_git') {
            location = `${agentAccessEscape(source.ssh_username || '')}@${agentAccessEscape(source.ssh_host || '')}:${source.ssh_port ?? 22}`;
        } else if (source.provider === 'codesearch') {
            location = 'OpenGrok 服务（管理员配置）';
        } else {
            location = '本机仓库（路径不对外暴露）';
        }
        let mode = '—';
        if (source.provider === 'ssh_git') {
            mode = source.discover_repos
                ? '全量自动发现（TTL 缓存）'
                : (source.multi_repo ? '多仓清单' : '单仓');
        } else if (source.provider === 'codesearch') {
            mode = '动态索引';
        } else {
            mode = 'commit 绑定';
        }
        const revision = source.default_revision
            ? agentAccessEscape(source.default_revision)
            : '<span class="agent-access-empty">未设置</span>';
        // 全部条目现在都是用户注册源（静态配置已迁移）：显示实时校验
        // 结果 + 删除操作。manage 面不可用时不渲染删除（避免误报）。
        const userRecord = agentSdkUserLiveCache[source.source_id];
        let live = '<span class="agent-access-empty">—</span>';
        let action = '<span class="agent-access-empty">—</span>';
        if (userRecord) {
            const ok = Boolean(userRecord.live?.verified);
            const reason = agentAccessEscape(userRecord.live?.reason || '');
            const title = ok
                ? ' title="最近一次刷新时路径存在且含 git 仓库"'
                : ` title="${reason}"`;
            const verifiedAt = userRecord.last_verified_at
                ? agentAccessEscape(userRecord.last_verified_at) : '';
            const badgeText = ok ? '✓ 存在' : `✗ ${reason || '不可用'}`;
            live = `<span class="agent-access-status-badge ${ok ? 'ok' : 'bad'}"${title}${verifiedAt ? ` data-verified="${verifiedAt}"` : ''}>${badgeText}</span>`;
            action = `<button class="btn-xxs" type="button" data-click="agentAccessDeleteSdkSource" data-a0="${agentAccessEscape(source.source_id)}">删除</button>`;
        }
        return `<tr>
            <td>${agentAccessEscape(source.source_id)}</td>
            <td>${providerLabel}</td>
            <td>${location}</td>
            <td>${mode}</td>
            <td>${revision}</td>
            <td>${live}</td>
            <td>${action}</td>
        </tr>`;
    }).join('');
}

// ---------- 「添加SDK」弹框 ----------

function agentAccessOnSdkTypeChange() {
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const sshOnly = document.querySelectorAll('.agent-sdk-ssh-only');
    sshOnly.forEach(el => { el.hidden = type === 'local_git'; });
    agentAccessResetSdkPathOptions();
}

function agentAccessResetSdkPathOptions() {
    const select = document.getElementById('agent-sdk-add-path');
    if (select) {
        select.innerHTML = '<option value="">请先填写服务器并按回车「发现目录」</option>';
    }
}

function agentAccessOnSdkTargetChange() {
    // 目标变化后旧候选目录失效
    agentAccessResetSdkPathOptions();
}

function agentAccessOnSdkTargetKeydown() {
    // 编译服务器输入框回车 = 自动发现目录（data-key=Enter 已过滤）。
    // data-prevent 已阻止表单默认提交；发现进行中由按钮 disabled 防抖。
    return agentAccessDiscoverSdkRoots();
}

function agentAccessToggleAddSdk(force) {
    const modalId = 'agent-sdk-add-modal';
    const toggle = document.getElementById('agent-sdk-sources-add');
    const shouldOpen = force === undefined ? !ModalManager.isOpen(modalId) : Boolean(force);
    if (shouldOpen) {
        const result = document.getElementById('agent-sdk-add-probe-result');
        if (result) result.hidden = true;
        agentAccessOnSdkTypeChange(); // 按当前类型显隐字段并重置目录下拉
        ModalManager.onClose(modalId, () => {
            toggle?.setAttribute('aria-expanded', 'false');
        });
        toggle?.setAttribute('aria-expanded', 'true');
        ModalManager.open(modalId);
    } else {
        ModalManager.close(modalId);
    }
}

function agentAccessSdkTarget() {
    return document.getElementById('agent-sdk-add-target')?.value?.trim() || '';
}

function agentAccessSdkResolvedPath() {
    // 手动兜底优先于下拉
    const manual = document.getElementById('agent-sdk-add-path-manual')?.value?.trim() || '';
    if (manual) return manual;
    const select = document.getElementById('agent-sdk-add-path');
    return select?.value?.trim() || '';
}

function agentAccessDiscoverSdkRoots() {
    // 回车触发点：act-bridge 对 async handler 已兜底上报，这里保持同步
    // 调用避免表单默认提交；防抖由 discover 进行中的按钮 disabled 承担。
    return agentAccessDiscoverSdkRootsInner();
}

async function agentAccessDiscoverSdkRootsInner() {
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const target = agentAccessSdkTarget();
    const button = document.getElementById('agent-sdk-add-discover-btn');
    const select = document.getElementById('agent-sdk-add-path');
    const hint = document.getElementById('agent-sdk-add-path-hint');
    const password = document.getElementById('agent-sdk-add-password')?.value || '';
    if (type !== 'local_git' && !target) {
        showToast('请先填写编译服务器地址（user@host）', 'warning');
        return;
    }
    if (button) { button.disabled = true; button.textContent = '发现中…'; }
    if (select) select.innerHTML = '<option value="">发现中…</option>';
    try {
        let candidates;
        if (type === 'local_git') {
            // 本机：扫 home 一级目录 + 当前项目根的同级（有界，本地无 SSH）
            candidates = await agentAccessDiscoverLocalRoots();
        } else {
            const resp = await apiCall('/api/sdk/manage/discover', 'POST', {target, password}, {silentToast: true});
            candidates = Array.isArray(resp?.data?.candidates) ? resp.data.candidates : [];
        }
        if (!candidates.length) {
            if (select) select.innerHTML = '<option value="">未发现候选目录；可改用手动路径兜底</option>';
            if (hint) hint.textContent = '下拉为空时，在「手动路径兜底」输入完整绝对路径。';
            return;
        }
        if (select) {
            select.innerHTML = '<option value="">请选择 SDK 根目录…</option>' + candidates.map(c =>
                `<option value="${agentAccessEscape(c.path)}">[${agentAccessEscape(c.kind_label || c.kind)}] ${agentAccessEscape(c.path)}</option>`
            ).join('');
            if (hint) hint.textContent = `发现 ${candidates.length} 个候选；不在列表中的目录用「手动路径兜底」。`;
        }
    } catch (error) {
        if (select) select.innerHTML = `<option value="">发现失败：${agentAccessEscape(error.message || '')}</option>`;
        if (hint) hint.textContent = '发现失败时可直接在「手动路径兜底」输入完整路径。';
    } finally {
        if (button) { button.disabled = false; button.textContent = '发现目录'; }
    }
}

async function agentAccessDiscoverLocalRoots() {
    // 本机候选：复用 discover 语义但走本地 API（provider=local_git 时
    // 由后端 _probe_local_path 校验；这里只列 home 一级 + 项目根同级）。
    try {
        const resp = await apiCall('/api/sdk/manage/discover-local', 'POST', {}, {silentToast: true});
        return Array.isArray(resp?.data?.candidates) ? resp.data.candidates : [];
    } catch (error) {
        return [];
    }
}

async function agentAccessProbeSdkPath() {
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const repoRoot = agentAccessSdkResolvedPath();
    const target = agentAccessSdkTarget();
    const button = document.getElementById('agent-sdk-add-probe-btn');
    const box = document.getElementById('agent-sdk-add-probe-result');
    const text = document.getElementById('agent-sdk-add-probe-text');
    if (type === 'ssh_git' && !target) {
        showToast('请先填写编译服务器地址（user@host）', 'warning');
        return;
    }
    if (!repoRoot) {
        showToast('请先选择或填写 SDK 路径', 'warning');
        return;
    }
    if (button) { button.disabled = true; button.textContent = '校验中…'; }
    if (box) box.hidden = true;
    try {
        const resp = await apiCall('/api/sdk/manage/probe', 'POST', {
            provider: type, target, repo_root: repoRoot,
            password: document.getElementById('agent-sdk-add-password')?.value || '',
        }, {silentToast: true});
        const probe = resp?.data?.probe || {};
        if (text) {
            const detail = probe.path_exists
                ? `${probe.summary || '目录存在'}${probe.is_git_repo ? '；单 git 仓' : ''}${probe.is_repo_workspace ? '；repo 工作区' : ''}`
                : '路径不存在';
            text.textContent = `${repoRoot}：${detail}`;
            text.style.color = probe.path_exists && (probe.git_repo_count || probe.is_git_repo || probe.is_repo_workspace)
                ? 'var(--success-color, #2e7d32)' : 'var(--danger-color, #c62828)';
        }
        if (box) box.hidden = false;
    } catch (error) {
        if (text) {
            text.textContent = `校验失败：${error.message || '未知错误'}`;
            text.style.color = 'var(--danger-color, #c62828)';
        }
        if (box) box.hidden = false;
    } finally {
        if (button) { button.disabled = false; button.textContent = '校验路径'; }
    }
}

async function agentAccessSubmitAddSdk(event) {
    event?.preventDefault?.();
    const submit = document.getElementById('agent-sdk-add-submit');
    if (submit?.disabled || !ModalManager.isOpen('agent-sdk-add-modal')) return;
    const type = document.getElementById('agent-sdk-add-type')?.value || 'ssh_git';
    const target = agentAccessSdkTarget();
    const repoRoot = agentAccessSdkResolvedPath();
    const sourceId = document.getElementById('agent-sdk-add-id')?.value?.trim() || '';
    const password = document.getElementById('agent-sdk-add-password')?.value || '';
    const revision = document.getElementById('agent-sdk-add-revision')?.value?.trim() || '';
    if (type === 'ssh_git' && !target) { showToast('请填写编译服务器地址（user@host）', 'warning'); return; }
    if (!repoRoot) { showToast('请先「发现目录」并选择，或在手动路径兜底里填写', 'warning'); return; }
    if (submit) { submit.disabled = true; submit.textContent = '添加中…'; }
    try {
        const granted = window.requestElevatedAccess
            ? await window.requestElevatedAccess(`添加 SDK 源码源：${repoRoot}`)
            : true;
        if (!granted || !ModalManager.isOpen('agent-sdk-add-modal')) return;
        const resp = await apiCall('/api/sdk/manage/sources', 'POST', {
            provider: type,
            target,
            repo_root: repoRoot,
            source_id: sourceId,
            password,
            default_revision: revision,
        }, {silentToast: true});
        const created = resp?.data?.source || {};
        showToast(`已添加 SDK 源「${created.source_id || repoRoot}」，Agent 检索立即可用`, 'success');
        ModalManager.close('agent-sdk-add-modal');
        document.getElementById('agent-sdk-add-path-manual').value = '';
        document.getElementById('agent-sdk-add-id').value = '';
        document.getElementById('agent-sdk-add-password').value = '';
        const probeBox = document.getElementById('agent-sdk-add-probe-result');
        if (probeBox) probeBox.hidden = true;
        agentSdkSourcesLoaded = false;
        agentAccessLoadSdkSources();
    } catch (error) {
        showToast(`添加失败：${error.message || '未知错误'}`, 'error');
    } finally {
        if (submit) { submit.disabled = false; submit.textContent = '添加'; }
    }
}

async function agentAccessDeleteSdkSource(sourceId) {
    const record = agentSdkUserLiveCache[sourceId];
    if (!record) return;
    const isLocal = record.provider === 'local_git';
    const target = isLocal
        ? `本机路径 ${record.repo_root || ''}`
        : `${record.ssh_username}@${record.ssh_host}:${record.ssh_port ?? 22} 上的 ${record.repo_root || ''}`;
    const confirmed = await showConfirmDialog(
        '删除 SDK 源码源',
        `确定删除「${sourceId}」吗？\n\n仅解除本平台与 ${target} 的检索绑定，不删除、不修改任何代码文件。`,
    );
    if (!confirmed) return;
    try {
        const granted = window.requestElevatedAccess
            ? await window.requestElevatedAccess(`删除 SDK 源码源：${sourceId}`)
            : true;
        if (!granted) return;
        await apiCall(`/api/sdk/manage/sources/${encodeURIComponent(sourceId)}`, 'DELETE', null, {silentToast: true});
        showToast(`已删除 SDK 源「${sourceId}」`, 'success');
        delete agentSdkUserLiveCache[sourceId];
        agentSdkSourcesLoaded = false;
        agentAccessLoadSdkSources();
    } catch (error) {
        showToast(`删除失败：${error.message || '未知错误'}`, 'error');
    }
}
