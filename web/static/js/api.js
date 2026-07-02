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
    return _apiCallOnce(url, method, data, { _elevationRetried: false });
}

async function _apiCallOnce(url, method, data, opts) {
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
            // Sensitive operation needs temporary admin elevation: prompt for
            // admin credentials, and on success replay this exact request once.
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
            const error = new Error(
                (detail && (detail.message || detail.detail)) ||
                result.error || result.message || 'Request failed'
            );
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
        // If the client-host detection modal is already open (e.g. the user was
        // filling it in when a background 401 raised the auth gate), recover
        // state incrementally instead of reloading, so that modal survives.
        const detectOpen = typeof ModalManager !== 'undefined' && ModalManager.isOpen('username-detect-modal');
        if (detectOpen) {
            debugLog('[Auth] Login succeeded with username-detect open; recovering state without reload');
            state.usernameDetectShown = true;  // keep the already-open modal, don't re-trigger
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
    const status = await fetchAuthStatus();
    if (!status.authenticated) {
        showAuthGate(Boolean(status.setup_required));
        return false;
    }
    state.currentUser = status.user || null;
    state.clientId = status.user?.id || null;
    state.authReady = true;
    state.elevated = Boolean(status.elevated);
    state.elevatedUntil = status.elevated_until || null;
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
