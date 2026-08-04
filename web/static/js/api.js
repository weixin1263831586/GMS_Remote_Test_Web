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

async function apiCall(url, method = 'GET', data = null) {
    return _apiCallOnce(url, method, data, { _elevationRetried: false });
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
            const error = new Error(
                normalizeApiErrorMessage(rawMessage, response.status)
            );
            error.status = response.status;
            const structured = detail && typeof detail === 'object' ? detail : result;
            error.code = structured?.error_code || '';
            error.retryable = structured?.retryable === true;
            error.remediation = structured?.remediation || '';
            error.details = structured?.error_details || structured?.network_quality || {};
            if (response.status === 401) {
                error.suppressToast = true;
                if (state.authRequired) showAuthGate(false);
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

async function resetElevationForNewBrowserTab(status) {
    const tabKey = 'gms_browser_tab_session_v1';
    try {
        if (sessionStorage.getItem(tabKey)) return status;
        sessionStorage.setItem(tabKey, '1');
    } catch (error) {
        // If storage is unavailable, keep the server-side session behavior.
        debugLog('[Auth] browser tab session storage unavailable:', error);
        return status;
    }
    if (!status.authenticated) return status;
    try {
        const response = await fetch('/api/auth/elevation/reset', {
            method: 'POST',
            credentials: 'same-origin',
        });
        if (response.ok) {
            return { ...status, elevated: false, elevated_until: null };
        }
    } catch (error) {
        debugLog('[Auth] new browser tab elevation reset failed:', error);
    }
    return status;
}

function showAuthGate(setupRequired = false) {
    const gate = document.getElementById('auth-gate');
    if (!gate) return;
    const active = document.activeElement;
    const focusInsideGate = active && gate.contains(active);
    // 登录层已显示时不重复抢占输入焦点。
    const wasVisible = gate.style.display === 'flex';
    gate.style.display = 'flex';
    gate.classList.toggle('setup-mode', setupRequired);
    const title = document.getElementById('auth-title');
    const submit = document.getElementById('auth-submit');
    const displayNameRow = document.getElementById('auth-display-name-row');
    const usernameInput = document.getElementById('auth-username');
    const passwordInput = document.getElementById('auth-password');
    const usernameHelp = document.getElementById('auth-username-help');
    const passwordHelp = document.getElementById('auth-password-help');
    if (title) title.textContent = setupRequired ? '初始化管理员账户' : '登录';
    if (submit) submit.textContent = setupRequired ? '创建管理员并进入' : '登录';
    if (displayNameRow) displayNameRow.style.display = setupRequired ? 'flex' : 'none';
    if (usernameInput) {
        usernameInput.placeholder = setupRequired ? '管理员账号' : '正在读取客户端身份…';
    }
    if (passwordInput) {
        passwordInput.placeholder = setupRequired ? '管理员密码' : '客户端主机 SSH 登录密码';
    }
    if (usernameHelp) {
        usernameHelp.textContent = setupRequired
            ? '创建平台管理员账号；此处不使用客户端 SSH 账号。'
            : '格式：SSH用户名@客户端IP，例如 hcq@172.16.14.66。';
    }
    if (passwordHelp) {
        passwordHelp.textContent = setupRequired
            ? '请设置平台管理员密码。'
            : '请输入该账号的 SSH 密码，通常与系统登录/锁屏密码相同。';
    }
    const message = document.getElementById('auth-message');
    if (message) message.textContent = setupRequired ? '首次访问需要创建管理员账户。' : '';
    // 仅初始化模式允许关闭登录层并匿名进入。
    const closeBtn = document.getElementById('auth-close');
    if (closeBtn) closeBtn.style.display = setupRequired ? 'block' : 'none';
    if (!setupRequired) prefillAuthUsernameFromClient();
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
        if (usernameInput.placeholder === '正在读取客户端身份…') {
            usernameInput.placeholder = defaultPlaceholder;
        }
    }
}

function hideAuthGate() {
    const gate = document.getElementById('auth-gate');
    if (gate) gate.style.display = 'none';
}

function closeAuthGate() {
    // 不刷新页面，以匿名身份继续初始化应用。
    hideAuthGate();
    continueAppInitialization();
}

async function submitAuthForm() {
    const username = document.getElementById('auth-username')?.value.trim() || '';
    const password = document.getElementById('auth-password')?.value || '';
    const displayName = document.getElementById('auth-display-name')?.value.trim() || '';
    const setupRequired = document.getElementById('auth-gate')?.classList.contains('setup-mode');
    const message = document.getElementById('auth-message');
    const submit = document.getElementById('auth-submit');
    if (message) message.textContent = '';
    if (submit) submit.disabled = true;
    try {
        const response = await fetch(setupRequired ? '/api/auth/setup' : '/api/auth/login', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, display_name: displayName })
        });
        const result = await response.json().catch(() => ({ success: false, error: '认证响应解析失败' }));
        if (!response.ok || result.success === false) {
            throw new Error(result.error || result.message || '认证失败');
        }
        state.currentUser = result.user || null;
        state.clientId = result.client_id || result.user?.id || null;
        state.authReady = true;
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
    const status = await resetElevationForNewBrowserTab(await fetchAuthStatus());
    state.authRequired = status.auth_required !== false;
    state.authSetupRequired = Boolean(status.setup_required);
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
window.submitAuthForm = submitAuthForm;
window.prefillAuthUsernameFromClient = prefillAuthUsernameFromClient;
window.logoutCurrentUser = logoutCurrentUser;
window.applyRoleBasedUiAccess = applyRoleBasedUiAccess;
window.isPlatformAdmin = isPlatformAdmin;
