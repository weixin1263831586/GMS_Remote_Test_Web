// ==================== Agent 接入管理（2026-09-08 audit §二/§三） ====================
// 管理员在 Web 上创建一次性配对码并管理 Agent Service Token。
// 配对码 5 分钟有效且单次使用；原始 token 只在创建响应中出现一次，
// 服务端仅保存 SHA-256 哈希。
//
// 入口：对话Agent 页面底部的 "Agent 接入管理" 面板（仅 admin + 提权会话可见）。

let agentAccessLoaded = false;
let agentAccessScopesCache = null;

function agentAccessToggle(show) {
    const panel = document.getElementById('agent-access-panel');
    const entry = document.getElementById('agent-access-entry');
    if (!panel || !entry) return;
    const visible = show !== undefined ? show : panel.style.display === 'none';
    panel.style.display = visible ? 'block' : 'none';
    entry.style.display = visible ? 'none' : 'block';
    if (visible) agentAccessReload();
}

function agentAccessIsAdmin() {
    const user = state.currentUser || {};
    return user.role === 'admin' || user.is_admin === true;
}

function agentAccessEnsureVisibleForRole() {
    // 只有管理员（登录后）显示入口；普通用户完全不暴露该面板。
    if (!state.authReady) return;
    const enabled = agentAccessIsAdmin();
    const entry = document.getElementById('agent-access-entry');
    const panel = document.getElementById('agent-access-panel');
    if (!entry || !panel) return;
    entry.style.display = enabled && panel.style.display === 'none' ? 'block' : 'none';
}

async function agentAccessReload() {
    const container = document.getElementById('agent-access-tokens');
    if (!container) return;
    container.innerHTML = '<div class="suite-empty">加载中...</div>';
    try {
        const [tokensResp, scopesResp] = await Promise.all([
            apiCall('/api/auth/agent-tokens', 'GET'),
            agentAccessScopesCache
                ? Promise.resolve({ scopes: agentAccessScopesCache })
                : apiCall('/api/auth/agent-scopes', 'GET'),
        ]);
        // R10：服务端返回 {scope: 描述} 对象，统一规范化后再渲染。
        agentAccessScopesCache = agentAccessNormalizeScopes(
            scopesResp && scopesResp.scopes
        );
        agentAccessRenderScopeCheckboxes();
        agentAccessRenderTokens(tokensResp.tokens || []);
        agentAccessLoaded = true;
    } catch (error) {
        debugLog('[AgentAccess] load failed:', error);
        container.innerHTML = '<div class="suite-empty">加载失败（需要管理员会话）</div>';
    }
}

function agentAccessNormalizeScopes(raw) {
    // R10（2026-09-08 审核）：服务端 AGENT_SCOPES 是 {scope: 描述} 对象；
    // 兼容对象、字符串数组与 {name} 对象数组三种形态，统一成 name 数组。
    if (Array.isArray(raw)) {
        return raw
            .map((entry) => (typeof entry === 'string' ? entry : entry && entry.name))
            .filter((name) => typeof name === 'string' && name);
    }
    if (raw && typeof raw === 'object') {
        return Object.keys(raw).filter((name) => typeof name === 'string' && name);
    }
    return [];
}

function agentAccessRenderScopeCheckboxes() {
    const wrap = document.getElementById('agent-access-scopes');
    if (!wrap || !Array.isArray(agentAccessScopesCache)) return;
    wrap.innerHTML = '';
    const defaultScopes = [
        'system.read', 'devices.read', 'devices.lease', 'devices.use_leased',
        'tests.execute', 'tests.cancel', 'jobs.read', 'reports.read',
    ];
    agentAccessScopesCache.forEach((name) => {
        const label = document.createElement('label');
        label.style.cssText = 'font-size:11px;display:flex;align-items:center;gap:3px;';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = name;
        input.className = 'agent-access-scope';
        if (defaultScopes.includes(name)) input.checked = true;
        label.appendChild(input);
        label.appendChild(document.createTextNode(name));
        wrap.appendChild(label);
    });
}

function agentAccessToggleCreate() {
    const form = document.getElementById('agent-access-enroll-form');
    if (!form) return;
    form.style.display = form.style.display === 'none' ? 'block' : 'none';
    if (form.style.display === 'block' && !agentAccessScopesCache) {
        agentAccessReload();
    }
}

async function agentAccessCreateEnrollment() {
    const resultEl = document.getElementById('agent-access-code-result');
    if (!resultEl) return;
    const name = document.getElementById('agent-access-name')?.value?.trim() || '';
    const workers = document.getElementById('agent-access-workers')?.value?.trim() || '*';
    const devices = document.getElementById('agent-access-devices')?.value?.trim() || '*';
    const days = parseInt(document.getElementById('agent-access-days')?.value || '90', 10);
    const scopes = Array.from(
        document.querySelectorAll('#agent-access-scopes input:checked')
    ).map((input) => input.value);
    if (!name) {
        resultEl.textContent = '⚠ 请填写名称';
        return;
    }
    resultEl.textContent = '生成中…';
    try {
        const resp = await apiCall('/api/auth/agent-enrollment-codes', 'POST', {
            name,
            scopes,
            allowed_workers: workers,
            allowed_devices: devices,
            expires_days: Number.isFinite(days) ? days : 90,
        });
        const enrollment = resp.enrollment || {};
        resultEl.textContent = `配对码: ${enrollment.code}（5 分钟内有效，仅可用一次）`;
        agentAccessReload();
    } catch (error) {
        debugLog('[AgentAccess] enrollment failed:', error);
        resultEl.textContent = '⚠ 创建失败（需要管理员提权会话）';
    }
}

function agentAccessRenderTokens(tokens) {
    const container = document.getElementById('agent-access-tokens');
    if (!container) return;
    if (!Array.isArray(tokens) || tokens.length === 0) {
        container.innerHTML = '<div class="suite-empty">尚无 Agent Service Token</div>';
        return;
    }
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
    const rows = tokens.map((token) => {
        const revoked = token.revoked_at;
        const expired = token.expires_at && new Date(token.expires_at) < new Date();
        const status = revoked ? '已吊销' : expired ? '已过期' : '有效';
        const statusColor = (revoked || expired) ? 'var(--danger-color, #c00)' : 'var(--success-color, #090)';
        const scopes = Array.isArray(token.scopes) ? token.scopes.join(', ') : token.scopes || '';
        const revokeButton = revoked
            ? ''
            : `<button class="btn-xs" onclick="agentAccessRevoke('${esc(token.id)}')">吊销</button>`;
        return `
            <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:6px 8px;border:1px solid var(--border-color);border-radius:6px;margin-bottom:4px;font-size:12px;">
                <strong>${esc(token.name)}</strong>
                <span class="mono" style="color:var(--text-secondary);">${esc(token.id)}</span>
                <span style="color:${statusColor};font-size:11px;">${status}</span>
                <span style="color:var(--text-secondary);font-size:11px;">scopes: ${esc(scopes) || '—'}</span>
                <span style="color:var(--text-secondary);font-size:11px;">workers: ${esc(token.allowed_workers)}</span>
                <span style="color:var(--text-secondary);font-size:11px;">devices: ${esc(token.allowed_devices)}</span>
                <span style="color:var(--text-secondary);font-size:11px;">到期: ${esc((token.expires_at || '').slice(0, 10)) || '—'}</span>
                <span style="flex:1;"></span>
                ${revokeButton}
            </div>`;
    });
    container.innerHTML = rows.join('');
}

async function agentAccessRevoke(tokenId) {
    if (!tokenId || !window.confirm(`确定吊销 Agent Token ${tokenId}？该编译服务器上的 Agent 将立即失去访问权。`)) {
        return;
    }
    try {
        await apiCall(`/api/auth/agent-tokens/${encodeURIComponent(tokenId)}`, 'DELETE');
        agentAccessReload();
    } catch (error) {
        debugLog('[AgentAccess] revoke failed:', error);
        alert('吊销失败（需要管理员提权会话）');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // R10（2026-09-08 审核）：入口可见性跟随登录状态事件，而不是只在
    // 加载后 800ms 检查一次——登录较慢或稍后登录（auth-ready 晚于检查）
    // 时入口也能显示；退出后重登同样通过事件同步。
    runAfterAuthReady(agentAccessEnsureVisibleForRole);
    window.addEventListener('gms:auth-ready', agentAccessEnsureVisibleForRole);
});
