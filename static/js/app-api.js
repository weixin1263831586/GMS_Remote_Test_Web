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
    if (!state.clientId || state.clientId === 'unknown') {
        return {};
    }

    const separatorIndex = state.clientId.lastIndexOf('@');
    const username = separatorIndex >= 0
        ? state.clientId.slice(0, separatorIndex)
        : state.clientId;

    return {
        'X-Client-Username': safeHeaderPercentEncode(username),
        'X-Client-Username-Encoding': 'percent'
    };
}

function applyClientIdentityHeadersToXhr(xhr) {
    Object.entries(getClientIdentityHeaders()).forEach(([key, value]) => {
        xhr.setRequestHeader(key, value);
    });
}

async function apiCall(url, method = 'GET', data = null) {
    try {
        const headers = {
            'Content-Type': 'application/json',
            ...getClientIdentityHeaders()
        };

        const options = {
            method,
            headers
        };

        if (data && !['GET', 'HEAD'].includes(method.toUpperCase())) {
            options.body = JSON.stringify(data);
        }

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

window.AnalysisMode = AnalysisMode;
window.createFormData = createFormData;
window.getClientIdentityHeaders = getClientIdentityHeaders;
window.applyClientIdentityHeadersToXhr = applyClientIdentityHeadersToXhr;
window.apiCall = apiCall;
