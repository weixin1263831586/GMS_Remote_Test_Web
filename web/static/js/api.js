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

async function apiCall(url, method = 'GET', data = null) {
    try {
        const options = {
            method,
            headers: { ...getClientIdentityHeaders() }
        };

        if (data && !['GET', 'HEAD'].includes(method.toUpperCase())) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(data);
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
            if (response.status === 401 && result.auth_required) {
                showAuthGate(Boolean(result.setup_required));
            }
            const error = new Error(result.error || result.message || 'Request failed');
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

function showAuthGate(setupRequired = false) {
    const gate = document.getElementById('auth-gate');
    if (!gate) return;
    const active = document.activeElement;
    const focusInsideGate = active && gate.contains(active);
    // If the gate is already visible, don't re-focus — repeated 401s from
    // background API calls would otherwise yank focus back to the username
    // field while the user is typing the password.
    const wasVisible = gate.style.display === 'flex';
    gate.style.display = 'flex';
    gate.classList.toggle('setup-mode', setupRequired);
    const title = document.getElementById('auth-title');
    const submit = document.getElementById('auth-submit');
    const displayNameRow = document.getElementById('auth-display-name-row');
    if (title) title.textContent = setupRequired ? '初始化管理员账户' : '登录';
    if (submit) submit.textContent = setupRequired ? '创建管理员并进入' : '登录';
    if (displayNameRow) displayNameRow.style.display = setupRequired ? 'flex' : 'none';
    const message = document.getElementById('auth-message');
    if (message) message.textContent = setupRequired ? '首次访问需要创建管理员账户。' : '';
    if (!wasVisible && !focusInsideGate) {
        setTimeout(() => {
            if (document.activeElement && gate.contains(document.activeElement)) return;
            const usernameInput = document.getElementById('auth-username');
            const passwordInput = document.getElementById('auth-password');
            (usernameInput?.value ? passwordInput : usernameInput)?.focus();
        }, 0);
    }
}

function hideAuthGate() {
    const gate = document.getElementById('auth-gate');
    if (gate) gate.style.display = 'none';
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
        window.location.reload();
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
    const status = await fetchAuthStatus();
    if (!status.authenticated) {
        showAuthGate(Boolean(status.setup_required));
        return false;
    }
    state.currentUser = status.user || null;
    state.clientId = status.user?.id || null;
    state.authReady = true;
    hideAuthGate();
    return true;
}

window.AnalysisMode = AnalysisMode;
window.createFormData = createFormData;
window.getClientIdentityHeaders = getClientIdentityHeaders;
window.applyClientIdentityHeadersToXhr = applyClientIdentityHeadersToXhr;
window.apiCall = apiCall;
window.ensureAuthenticatedBeforeAppStart = ensureAuthenticatedBeforeAppStart;
window.showAuthGate = showAuthGate;
window.submitAuthForm = submitAuthForm;
window.logoutCurrentUser = logoutCurrentUser;
