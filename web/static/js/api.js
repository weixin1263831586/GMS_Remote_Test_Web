// Shared API helpers.

const AnalysisMode = {
    UPLOAD: 'upload',
    SAVED: 'saved',
    AI: 'ai'
};

function createFormData(mode, params = {}, files = {}) {
    const formData = new FormData();
    formData.append('mode', mode);

    for (const [key, value] of Object.entries(params)) {
        if (value !== undefined && value !== null) {
            formData.append(key, value);
        }
    }

    for (const [key, file] of Object.entries(files)) {
        if (file instanceof File) {
            formData.append(key, file);
        }
    }

    return formData;
}

function getClientIdentityHeaders() {
    return {};
}

function applyClientIdentityHeadersToXhr(xhr) {
    Object.entries(getClientIdentityHeaders()).forEach(([key, value]) => {
        xhr.setRequestHeader(key, value);
    });
}

function normalizeApiErrorMessage(message, status) {
    const text = String(message || '').trim();
    if (status !== 409) return text || 'Request failed';
    if (
        text === 'worker capacity is exhausted'
        || text === 'worker already has the maximum number of active jobs'
    ) {
        return 'Worker 已达到最大并发任务数，请等待当前测试结束，或由管理员调整 max_jobs';
    }
    const claimed = text.match(/^device is already claimed by (.+)$/);
    if (claimed) {
        return `所选设备正被 ${claimed[1]} 占用，请等待任务结束或由管理员释放设备`;
    }
    const unavailable = text.match(/^device is not available: (.+)$/);
    if (unavailable) {
        return `设备当前不可用：${unavailable[1]}，请刷新设备状态后重试`;
    }
    if (text === 'Selected suite is not available on the Worker') {
        return '所选测试套件在目标 Worker 上不可用，请重新选择该 Worker 已安装的套件';
    }
    return text || '请求与当前资源状态冲突';
}

async function apiCall(url, method = 'GET', data = null, opts = {}) {
    return _apiCallOnce(url, method, data, { _elevationRetried: false, ...opts });
}

async function _apiCallOnce(url, method, data, opts) {
    try {
        const options = {
            method,
            headers: { ...getClientIdentityHeaders() }
        };

        if (data && !['GET', 'HEAD'].includes(method.toUpperCase())) {
            if (data instanceof FormData) {
                options.body = data;
            } else {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(data);
            }
        }

        options.credentials = 'same-origin';
        const response = await fetch(url, options);
        const contentType = response.headers.get('content-type') || '';
        let result = null;

        if (contentType.includes('application/json')) {
            try {
                result = await response.json();
            } catch (jsonError) {
                result = { success: response.ok, error: '响应 JSON 解析失败' };
            }
        } else {
            const text = await response.text();
            result = text ? { success: response.ok, message: normalizeApiTextError(text) } : { success: response.ok };
        }

        if (!result || typeof result !== 'object') {
            result = { success: response.ok };
        }

        if (result.client_id) {
            const oldClientId = state.clientId;
            state.clientId = result.client_id;

            if (result.client_id.startsWith('unknown@')) {
                debugLog(`[apiCall] Detected unknown client: ${result.client_id}`);

                if (!state.usernameDetectShown) {
                    state.usernameDetectShown = true;
                    debugLog('[apiCall] Showing username detect modal for:', result.ip);

                    setTimeout(() => {
                        showUsernameDetectModal(result.ip);
                    }, 500);
                }
            } else if (oldClientId !== result.client_id) {
                debugLog(`[apiCall] Updated state.clientId: ${oldClientId} -> ${result.client_id}`);
            }
        }

        if (!response.ok) {
            const detail = result && typeof result === 'object' ? result.detail : null;
            const needsElevation = response.status === 403
                && detail
                && (detail.elevation_required || (typeof detail === 'object' && detail.elevation_required));
            if (needsElevation && !opts._elevationRetried) {
                debugLog('[apiCall] 403 elevation_required — prompting for admin credentials');
                const granted = await (window.requestElevatedAccess
                    ? window.requestElevatedAccess('需要管理员权限执行此操作')
                    : Promise.resolve(false));
                if (granted) {
                    return _apiCallOnce(url, method, data, { _elevationRetried: true });
                }
            }
            const rawMessage = (
                (typeof detail === 'string'
                    ? detail
                    : detail && (detail.message || detail.detail))
                || result.error
                || result.message
                || 'Request failed'
            );
            // 后端某些校验错误（如 FastAPI 422）的 detail 是数组/对象；
            // 直接 String() 会变成 "[object Object]"，必须先做结构化提取。
            const friendlyMessage = typeof rawMessage === 'string'
                ? rawMessage
                : (() => {
                    try {
                        if (Array.isArray(rawMessage)) {
                            return rawMessage.map(item => item?.msg || JSON.stringify(item)).join('; ');
                        }
                        return rawMessage?.message || JSON.stringify(rawMessage);
                    } catch (e) {
                        return 'Request failed';
                    }
                })();
            const error = new Error(
                normalizeApiErrorMessage(friendlyMessage, response.status)
            );
            error.status = response.status;
            const structured = detail && typeof detail === 'object' ? detail : result;
            error.code = structured?.error_code || '';
            error.retryable = structured?.retryable === true;
            error.remediation = structured?.remediation || '';
            error.details = structured?.error_details || structured?.network_quality || {};
            if (response.status === 401) {
                error.suppressToast = true;
                // 用户主动发起的请求失败才重新弹出登录层；后台轮询
                // （options.background）保持静默，尊重用户手动关闭的选择。
                if (state.authRequired && !opts.background) {
                    state.authGateDismissed = false;
                    showAuthGate(false);
                }
            }
            if (needsElevation) error.suppressToast = true;
            if (result.need_password) {
                error.needPassword = true;
                error.suppressToast = true;
            }
            if (result.device_host) error.deviceHost = result.device_host;
            if (result.install_guide) error.installGuide = result.install_guide;
            throw error;
        }

        return result;
    } catch (error) {
        debugLog('API Error:', error);
        if (!error.suppressToast) {
            showToast(error.message, 'error');
        }
        throw error;
    }
}

async function fetchAuthStatus() {
    const response = await fetch('/api/auth/status', { credentials: 'same-origin' });
    if (!response.ok) {
        throw new Error('认证状态检查失败');
    }
    return response.json();
}

function runAfterAuthReady(callback) {
    const invoke = () => {
        if (state.authReady) callback();
    };
    if (state.authReady) {
        invoke();
        return;
    }
    window.addEventListener('gms:auth-ready', invoke, {once: true});
}

function showAuthGate(setupRequired = false) {
    const gate = document.getElementById('auth-gate');
    if (!gate) return;
    // 用户手动关闭后仍处于未认证状态：后台轮询/初始化请求的 401 静默
    // 跳过（避免刚关掉就被弹回），只有主动操作会清除 dismissed 再弹出。
    if (!setupRequired && state.authGateDismissed) return;
    const active = document.activeElement;
    const focusInsideGate = active && gate.contains(active);
    // 登录层已显示时不重复抢占输入焦点。
    const wasVisible = gate.style.display === 'flex';
    gate.style.display = 'flex';
    gate.classList.toggle('setup-mode', setupRequired);
    const title = document.getElementById('auth-title');
    const submit = document.getElementById('auth-submit');
    const displayNameRow = document.getElementById('auth-display-name-row');
    const bootstrapTokenRow = document.getElementById('auth-bootstrap-token-row');
    const bootstrapTokenInput = document.getElementById('auth-bootstrap-token');
    const usernameInput = document.getElementById('auth-username');
    const passwordInput = document.getElementById('auth-password');
    const displayNameInput = document.getElementById('auth-display-name');
    const usernameHelp = document.getElementById('auth-username-help');
    const passwordHelp = document.getElementById('auth-password-help');
    if (title) title.textContent = setupRequired ? '初始化管理员账户' : '登录';
    if (submit) submit.textContent = setupRequired ? '创建管理员并进入' : '登录';
    if (displayNameRow) displayNameRow.style.display = setupRequired ? 'flex' : 'none';
    if (bootstrapTokenRow) {
        bootstrapTokenRow.style.display = setupRequired && state.authBootstrapTokenRequired !== false
            ? 'flex'
            : 'none';
    }
    if (bootstrapTokenInput) {
        bootstrapTokenInput.required = setupRequired && state.authBootstrapTokenRequired !== false;
    }
    if (usernameInput) {
        usernameInput.placeholder = setupRequired ? '管理员账号' : '管理员账号，或正在读取客户端身份…';
        usernameInput.autocomplete = setupRequired ? 'off' : 'username';
        if (setupRequired && usernameInput.dataset.setupDefaultApplied !== 'true') {
            usernameInput.value = 'gms';
            usernameInput.dataset.setupDefaultApplied = 'true';
            delete usernameInput.dataset.autoFilled;
        }
    }
    if (passwordInput) {
        passwordInput.placeholder = setupRequired ? '管理员密码' : '管理员密码或客户端 SSH 密码';
        passwordInput.autocomplete = setupRequired ? 'new-password' : 'current-password';
    }
    if (setupRequired && displayNameInput && !displayNameInput.value.trim()) {
        displayNameInput.value = 'gms';
    }
    if (usernameHelp) {
        usernameHelp.textContent = setupRequired
            ? '创建平台管理员账号；此处不使用客户端 SSH 账号。'
            : '可使用平台管理员账号，或 SSH用户名@客户端IP，例如 hcq@172.16.14.66。';
    }
    if (passwordHelp) {
        passwordHelp.textContent = setupRequired
            ? '请设置平台管理员密码。'
            : '管理员请输入平台密码；客户端账号请输入 SSH 密码（通常与系统登录/锁屏密码相同）。';
    }
    const message = document.getElementById('auth-message');
    if (message) message.textContent = setupRequired ? '首次访问需要创建管理员账户。' : '';
    // 所有模式均允许关闭登录层：关闭后以未认证状态继续，受限操作会
    // 再次弹出登录层（而非把用户困在全屏遮罩里）。
    const closeBtn = document.getElementById('auth-close');
    if (closeBtn) closeBtn.style.display = 'block';
    if (setupRequired) {
        hideClientSshWarning();
    } else {
        if (usernameInput && usernameInput.dataset.sshProbeBound !== 'true') {
            usernameInput.dataset.sshProbeBound = 'true';
            usernameInput.addEventListener('input', scheduleClientSshCheck);
        }
        // 先完成客户端身份预填，再仅对 SSH用户@来源IP 账号探测端口。
        prefillAuthUsernameFromClient().finally(checkClientSshAtGate);
    }
    if (!wasVisible && !focusInsideGate) {
        setTimeout(() => {
            if (document.activeElement && gate.contains(document.activeElement)) return;
            const usernameInput = document.getElementById('auth-username');
            const passwordInput = document.getElementById('auth-password');
            (usernameInput?.value ? passwordInput : usernameInput)?.focus();
        }, 0);
    }
}

async function prefillAuthUsernameFromClient() {
    const usernameInput = document.getElementById('auth-username');
    const usernameHelp = document.getElementById('auth-username-help');
    const defaultPlaceholder = '用户名@客户端IP，例如 hcq@172.16.14.66';
    if (!usernameInput) return;
    if (usernameInput.value.trim()) {
        usernameInput.placeholder = defaultPlaceholder;
        return;
    }
    try {
        const response = await fetch('/api/users/current', { credentials: 'same-origin' });
        if (!response.ok) return;
        const client = await response.json();
        const clientIp = String(client.ip || '').trim();
        const identity = String(
            client.display_client_id
            || (client.username && client.ip ? `${client.username}@${client.ip}` : '')
            || ''
        ).trim();
        if (identity.includes('@') && identity !== 'unknown@unknown') {
            usernameInput.value = identity;
            usernameInput.dataset.autoFilled = 'true';
        } else if (clientIp && clientIp !== 'unknown') {
            usernameInput.placeholder = `用户名@${clientIp}，例如 hcq@${clientIp}`;
            if (usernameHelp) {
                usernameHelp.textContent = `检测到客户端 IP ${clientIp}，但尚不知道 SSH 用户名；请填写完整账号，例如 hcq@${clientIp}。`;
            }
        }
    } catch (error) {
        debugLog('[Auth] client identity prefill failed:', error);
    } finally {
        if (usernameInput.placeholder.includes('正在读取客户端身份')) {
            usernameInput.placeholder = defaultPlaceholder;
        }
    }
}

function hideAuthGate() {
    const gate = document.getElementById('auth-gate');
    if (gate) gate.style.display = 'none';
}

function closeAuthGate() {
    // 关闭后以未认证状态继续初始化应用；记录 dismissed 让后台 401
    // 不再自动弹回，用户主动操作触发 401 时会重新弹出登录层。
    state.authGateDismissed = !state.authSetupRequired;
    hideAuthGate();
    continueAppInitialization();
}

document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const gate = document.getElementById('auth-gate');
    if (gate && gate.style.display === 'flex') {
        // 登录层不是 ModalManager 管理的弹框；在其可见时优先关闭登录层，
        // 避免被底层弹框的 Esc 处理抢占。
        event.preventDefault();
        event.stopImmediatePropagation();
        closeAuthGate();
    }
}, true);

let clientSshCheckTimer = null;
let clientSshCheckGeneration = 0;

function hideClientSshWarning() {
    const banner = document.getElementById('auth-ssh-warning');
    if (banner) banner.style.display = 'none';
}

function scheduleClientSshCheck() {
    clientSshCheckGeneration += 1;
    hideClientSshWarning();
    if (clientSshCheckTimer) clearTimeout(clientSshCheckTimer);
    clientSshCheckTimer = setTimeout(() => {
        clientSshCheckTimer = null;
        checkClientSshAtGate();
    }, 350);
}

async function checkClientSshAtGate() {
    const banner = document.getElementById('auth-ssh-warning');
    const username = document.getElementById('auth-username')?.value.trim() || '';
    const at = username.lastIndexOf('@');
    const loginHost = at > 0 ? username.slice(at + 1).trim() : '';
    const looksLikeIp = /^(?:\d{1,3}\.){3}\d{1,3}$/.test(loginHost) || loginHost.includes(':');
    if (!banner || !looksLikeIp) {
        hideClientSshWarning();
        return;
    }
    const generation = ++clientSshCheckGeneration;
    let result = null;
    try {
        const response = await fetch('/api/auth/client-ssh-status', { credentials: 'same-origin' });
        if (response.ok) result = await response.json();
    } catch (error) {
        debugLog('[Auth] client SSH probe failed:', error);
    }
    if (generation !== clientSshCheckGeneration) return;
    // 只解释“当前浏览器来源主机”的客户端 SSH 登录。管理员账号或
    // 显式填写其他主机时不展示无关警告。
    if (!result || result.ssh_reachable || loginHost !== String(result.client_ip || '')) {
        hideClientSshWarning();
        return;
    }
    const textEl = document.getElementById('auth-ssh-warning-text');
    if (textEl) textEl.textContent = result.hint || '未检测到本机 SSH 服务（端口 22），登录验证需要它。';
    const link = document.getElementById('auth-ssh-warning-link');
    if (link) {
        link.onclick = () => showAuthSshdGuide(
            result.hint || '无法通过 SSH 连接到客户端主机',
            result.install_guide || ''
        );
    }
    banner.style.display = 'block';
}

function showAuthSshdGuide(reason, guide) {
    const panel = document.getElementById('auth-sshd-guide');
    const form = document.getElementById('auth-gate')?.querySelector('form.auth-panel');
    if (!panel || !form) return;
    const reasonEl = document.getElementById('auth-sshd-guide-reason');
    const guideEl = document.getElementById('auth-sshd-guide-content');
    if (reasonEl) reasonEl.textContent = reason;
    if (guideEl) guideEl.textContent = guide || '未获取到安装指南，请在 Windows 上以管理员身份安装 OpenSSH Server 并启动 sshd 服务。';
    form.style.display = 'none';
    panel.style.display = 'block';
    panel.setAttribute('aria-hidden', 'false');
}

function closeAuthSshdGuide() {
    const panel = document.getElementById('auth-sshd-guide');
    const form = document.getElementById('auth-gate')?.querySelector('form.auth-panel');
    if (panel) {
        panel.style.display = 'none';
        panel.setAttribute('aria-hidden', 'true');
    }
    if (form) form.style.display = '';
    const message = document.getElementById('auth-message');
    if (message) message.textContent = '';
    const passwordInput = document.getElementById('auth-password');
    if (passwordInput) passwordInput.focus();
}

async function submitAuthForm() {
    const username = document.getElementById('auth-username')?.value.trim() || '';
    const password = document.getElementById('auth-password')?.value || '';
    const displayName = document.getElementById('auth-display-name')?.value.trim() || '';
    const bootstrapToken = document.getElementById('auth-bootstrap-token')?.value || '';
    const setupRequired = document.getElementById('auth-gate')?.classList.contains('setup-mode');
    const message = document.getElementById('auth-message');
    const submit = document.getElementById('auth-submit');
    if (message) message.textContent = '';
    if (submit) submit.disabled = true;
    try {
        const headers = { 'Content-Type': 'application/json' };
        if (setupRequired && bootstrapToken) {
            headers['X-GMS-Bootstrap-Token'] = bootstrapToken;
        }
        const response = await fetch(setupRequired ? '/api/auth/setup' : '/api/auth/login', {
            method: 'POST',
            credentials: 'same-origin',
            headers,
            body: JSON.stringify({ username, password, display_name: displayName })
        });
        const result = await response.json().catch(() => ({ success: false, error: '认证响应解析失败' }));
        if (!response.ok || result.success === false) {
            // 客户端 SSH 服务不可达（未安装 SSHD 等）：登录验证依赖回连客户端
            // 的 SSH，此时在登录层内直接展示安装指南，用户装好后返回重试即可。
            if (result.error_code === 'client_ssh_unavailable') {
                showAuthSshdGuide(result.error || '无法通过 SSH 连接到客户端主机', result.install_guide || '');
                return;
            }
            throw new Error(result.error || result.message || '认证失败');
        }
        state.currentUser = result.user || null;
        state.clientId = result.client_id || result.user?.id || null;
        state.authReady = true;
        state.authGateDismissed = false;
        hideAuthGate();
        // 主机识别弹框已打开时增量恢复状态，不刷新页面。
        const detectOpen = typeof ModalManager !== 'undefined' && ModalManager.isOpen('username-detect-modal');
        if (detectOpen) {
            debugLog('[Auth] Login succeeded with username-detect open; recovering state without reload');
            state.usernameDetectShown = true;
            try {
                const cur = await fetch('/api/users/current', { credentials: 'same-origin' });
                if (cur.ok) {
                    const ud = await cur.json();
                    if (ud.client_id) state.clientId = ud.client_id;
                }
            } catch (e) {
                debugLog('[Auth] post-login /api/users/current refresh failed:', e);
            }
        } else {
            // No conflicting modal: a full reload is the simplest way to refresh
            // all auth-gated data and re-run initialization.
            window.location.reload();
        }
    } catch (error) {
        if (message) message.textContent = error.message;
    } finally {
        if (submit) submit.disabled = false;
    }
}

async function logoutCurrentUser() {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
    state.currentUser = null;
    state.clientId = null;
    showAuthGate(false);
}

async function ensureAuthenticatedBeforeAppStart() {
    // Elevation is a fixed-TTL grant bound to the authenticated platform
    // session. Browser tabs share that session cookie, so clearing the grant
    // when any new tab opens would also revoke it in every existing tab.
    const status = await fetchAuthStatus();
    state.authRequired = status.auth_required !== false;
    state.authSetupRequired = Boolean(status.setup_required);
    state.authBootstrapTokenRequired = typeof status.bootstrap_token_required === 'boolean'
        ? status.bootstrap_token_required
        : null;
    if (!status.authenticated) {
        state.currentUser = null;
        state.clientId = null;
        state.authReady = !state.authRequired;
        state.elevated = false;
        state.elevatedUntil = null;
        if (state.authRequired) {
            showAuthGate(Boolean(status.setup_required));
            return false;
        }
        hideAuthGate();
        applyRoleBasedUiAccess();
        return true;
    }
    state.currentUser = status.user || null;
    state.clientId = status.user?.id || null;
    state.authReady = true;
    state.elevated = Boolean(status.elevated);
    state.elevatedUntil = status.elevated_until || null;
    hideAuthGate();
    applyRoleBasedUiAccess();
    return true;
}

function applyRoleBasedUiAccess() {
    const isAdmin = isPlatformAdmin();
    document.querySelectorAll('[data-admin-only]').forEach(element => {
        if (!isAdmin && element.contains(document.activeElement)) {
            document.activeElement.blur();
        }
        element.hidden = !isAdmin;
        element.setAttribute('aria-hidden', isAdmin ? 'false' : 'true');
    });
}

function isPlatformAdmin() {
    return !state.authRequired || state.currentUser?.role === 'admin';
}

window.AnalysisMode = AnalysisMode;
window.createFormData = createFormData;
window.getClientIdentityHeaders = getClientIdentityHeaders;
window.applyClientIdentityHeadersToXhr = applyClientIdentityHeadersToXhr;
window.apiCall = apiCall;
window.ensureAuthenticatedBeforeAppStart = ensureAuthenticatedBeforeAppStart;
window.showAuthGate = showAuthGate;
window.hideAuthGate = hideAuthGate;
window.closeAuthGate = closeAuthGate;
window.showAuthSshdGuide = showAuthSshdGuide;
window.closeAuthSshdGuide = closeAuthSshdGuide;
window.submitAuthForm = submitAuthForm;
window.prefillAuthUsernameFromClient = prefillAuthUsernameFromClient;
window.runAfterAuthReady = runAfterAuthReady;
window.logoutCurrentUser = logoutCurrentUser;
window.applyRoleBasedUiAccess = applyRoleBasedUiAccess;
window.isPlatformAdmin = isPlatformAdmin;
