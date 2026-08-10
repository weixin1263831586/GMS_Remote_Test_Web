// ==================== 设备主机密码输入 ====================
function showDevicePasswordModal(deviceHost, action = 'usbip', onSaved = null) {
    pendingUsbipDeviceHost = deviceHost || pendingUsbipDeviceHost || '';
    pendingDevicePasswordAction = action || 'usbip';
    pendingDevicePasswordRetry = typeof onSaved === 'function' ? onSaved : null;
    // 显示层用可读的主机地址；后端回退值若是裸 client_id（非 user@ip）则优先
    // 用配置里的 device_host/usbip_device_host，仍无可读值时给友好提示。
    const displayHost = (() => {
        if (deviceHost && deviceHost.includes('@')) return deviceHost;
        const cfg = state.config || {};
        const configured = cfg.usbip_device_host || cfg.device_host;
        if (configured && configured.includes('@')) return configured;
        return deviceHost || configured || '（未配置主机）';
    })();
    const hostInput = document.getElementById('device-host-display');
    hostInput.value = displayHost;
    hostInput.readOnly = pendingDevicePasswordAction === 'terminal';
    const title = document.getElementById('device-password-modal-title');
    if (title) title.textContent = pendingDevicePasswordAction === 'terminal'
        ? '主机终端 SSH 密码'
        : '设备主机 SSH 密码';
    document.getElementById('device-pswd').value = '';
    ModalManager.open('device-password-modal');
    document.getElementById('device-pswd').focus();
}

function closeDevicePasswordModal() {
    ModalManager.close('device-password-modal');
    pendingDevicePasswordRetry = null;
}

// ==================== Username Detection Modal ====================
function showUsernameDetectModal(clientIp) {
    // 登录层显示时不叠加客户端主机识别弹框。
    const authGate = document.getElementById('auth-gate');
    if (authGate && authGate.style.display === 'flex') {
        debugLog('[UsernameDetect] Skipped: platform auth-gate is showing');
        return;
    }
    document.getElementById('username-detect-ip').value = clientIp;
    document.getElementById('username-detect-username').value = '';
    document.getElementById('username-detect-password').value = '';
    ModalManager.open('username-detect-modal');
    document.getElementById('username-detect-username').focus();
}

function closeUsernameDetectModal() {
    ModalManager.close('username-detect-modal');
}

// 敏感操作管理员二次认证。

let _elevationExpiryTimer = null;

function _clearElevationExpiryTimer() {
    if (_elevationExpiryTimer) {
        clearTimeout(_elevationExpiryTimer);
        _elevationExpiryTimer = null;
    }
}

function _markElevated(elevatedUntilIso) {
    state.elevated = true;
    state.elevatedUntil = elevatedUntilIso || null;
    _clearElevationExpiryTimer();
    if (elevatedUntilIso) {
        const ms = new Date(elevatedUntilIso).getTime() - Date.now();
        if (ms > 0) {
            _elevationExpiryTimer = setTimeout(() => {
                state.elevated = false;
                state.elevatedUntil = null;
                debugLog('[Elevation] expired, admin elevation cleared');
            }, ms);
        }
    }
}

/**
 * Ensure the current session has temporary admin elevation before running a
 * sensitive action. Resolves true if already elevated or after successful
 * re-authentication; false if the user cancels or fails.
 *
 * @param {string} actionLabel - what the elevation is for (shown in the modal)
 * @returns {Promise<boolean>}
 */
let _elevationRequestPromise = null;
async function requestElevatedAccess(actionLabel = '需要管理员权限', options = {}) {
    if (!state.authRequired) {
        if (options.allowAnonymousDev) return true;
    }
    if (state.elevated) {
        try {
            const status = await fetchAuthStatus();
            if (status.elevated) {
                _markElevated(status.elevated_until);
                debugLog('[Elevation] server confirmed the active elevation');
                return true;
            }
            state.elevated = false;
            state.elevatedUntil = null;
        } catch (error) {
            debugLog('[Elevation] could not verify the active elevation', error);
            return true;
        }
    }
    if (!state.currentUser && state.authSetupRequired) {
        showAuthGate(true);
        return false;
    }
    if (_elevationRequestPromise) {
        debugLog('[Elevation] sharing the active credential prompt');
        return _elevationRequestPromise;
    }
    const modal = document.getElementById('elevate-modal');
    if (!modal) {
        showToast('提权弹框未加载，无法执行此操作', 'error');
        return false;
    }
    const labelEl = document.getElementById('elevate-action-label');
    if (labelEl) labelEl.textContent = actionLabel;
    const userEl = document.getElementById('elevate-username');
    const pwdEl = document.getElementById('elevate-password');
    const msgEl = document.getElementById('elevate-message');
    if (userEl) userEl.value = '';
    if (pwdEl) pwdEl.value = '';
    if (msgEl) msgEl.textContent = '';
    // Prefill the current username for convenience.
    if (userEl && state.currentUser?.role === 'admin' && state.currentUser.username) {
        userEl.value = state.currentUser.username;
    }

    _elevationRequestPromise = new Promise(resolve => {
        const onGranted = (elevatedUntilIso) => {
            _markElevated(elevatedUntilIso);
            ModalManager.close('elevate-modal');
            resolve(true);
        };
        const onCancel = () => {
            ModalManager.close('elevate-modal');
            resolve(false);
        };
        // Wire one-shot handlers via onClose + submit; store for cleanup.
        window._elevateResolve = { onGranted, onCancel };
        ModalManager.onClose('elevate-modal', () => {
            if (window._elevateResolve) {
                window._elevateResolve.onCancel();
                window._elevateResolve = null;
            }
        });
        ModalManager.open('elevate-modal');
        setTimeout(() => pwdEl && pwdEl.focus(), 0);
    });
    try {
        return await _elevationRequestPromise;
    } finally {
        _elevationRequestPromise = null;
    }
}

async function submitElevateForm() {
    const username = document.getElementById('elevate-username')?.value.trim() || '';
    const password = document.getElementById('elevate-password')?.value || '';
    const msgEl = document.getElementById('elevate-message');
    const submitBtn = document.querySelector('#elevate-modal .btn-primary');
    if (msgEl) msgEl.textContent = '';
    if (!username || !password) {
        if (msgEl) msgEl.textContent = '请输入管理员账号和密码';
        return;
    }
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = '验证中...'; }
    try {
        const response = await fetch('/api/auth/elevate', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        const result = await response.json().catch(() => ({ success: false, error: '响应解析失败' }));
        if (!response.ok || result.success === false) {
            if (msgEl) msgEl.textContent = result.error || result.message || '管理员凭证无效';
            return;
        }
        if (result.user) {
            state.currentUser = result.user;
            state.clientId = result.client_id || result.user.id || state.clientId;
            applyRoleBasedUiAccess();
        }
        const resolve = window._elevateResolve;
        if (resolve) {
            const until = result.elevated_until || null;
            window._elevateResolve = null;
            resolve.onGranted(until);
        }
    } catch (error) {
        if (msgEl) msgEl.textContent = error.message || '提权失败';
    } finally {
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = '提权'; }
    }
}

function cancelElevate() {
    const resolve = window._elevateResolve;
    if (resolve) {
        window._elevateResolve = null;
        resolve.onCancel();
    } else {
        ModalManager.close('elevate-modal');
    }
}

function handleUsernameDetectKeyPress(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        submitUsernameDetect();
    }
}

async function postJson(url, payload) {
    const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    let data = {};
    try {
        data = await response.json();
    } catch (e) {
        data = {};
    }

    if (!response.ok) {
        const error = new Error(data.error || data.detail || `HTTP ${response.status}`);
        error.status = response.status;
        error.data = data;
        throw error;
    }

    return data;
}

function isUsernameManualFallbackError(errorOrMessage) {
    const message = String(errorOrMessage?.message || errorOrMessage || '').toLowerCase();
    return [
        'network is unreachable',
        'no route to host',
        'connection refused',
        'timed out',
        'timeout',
        '连接超时',
        '连接被拒绝',
        '网络不可达',
        '无法访问',
        'authentication',
        '认证失败'
    ].some(keyword => message.includes(keyword));
}

function updateUsernameDisplay(clientIp, username) {
    const display = `${username}@${clientIp}`;
    const identityEl = document.getElementById('client-identity');
    if (identityEl) {
        identityEl.textContent = display;
    }

    const deviceHostInput = document.getElementById('device-host');
    if (deviceHostInput) {
        deviceHostInput.value = display;
        deviceHostInput.placeholder = '设备主机';
    }
    const localServerInput = document.getElementById('local-server');
    if (localServerInput) {
        localServerInput.value = display;
    }
}

async function saveUsernameManually(clientIp, username) {
    const response = await postJson('/api/users/set-username', {
        ip: clientIp,
        username
    });

    const savedUsername = response.username || username;
    const clientId = response.client_id || `${savedUsername}@${clientIp}`;
    state.clientDisplayId = response.display_client_id || `${savedUsername}@${clientIp}`;
    state.clientId = clientId;
    localStorage.setItem(`gms_username_${clientIp}`, savedUsername);
    updateUsernameDisplay(clientIp, savedUsername);
    debugLog('[UsernameDetect] Saved username:', clientId);

    return savedUsername;
}

async function submitUsernameDetect() {
    const clientIp = document.getElementById('username-detect-ip').value;
    const username = document.getElementById('username-detect-username').value.trim();
    const password = document.getElementById('username-detect-password').value;

    if (!username) {
        showToast('请输入用户名', 'error');
        return;
    }

    const submitBtn = document.querySelector('#username-detect-modal .btn-primary');
    const originalText = submitBtn.textContent;
    try {
        submitBtn.textContent = password ? '验证中...' : '保存中...';
        submitBtn.disabled = true;

        let verifiedUsername = username;
        let verifiedBySsh = false;

        if (password) {
            const response = await postJson('/api/users/detect', {
                ip: clientIp,
                username,
                password
            });

            if (response.success) {
                verifiedUsername = response.username || username;
                verifiedBySsh = true;
            } else if (!response.manual_allowed) {
                showToast(`❌ 用户名验证失败: ${response.error || '未知错误'}`, 'error');
                return;
            } else {
                addLogEntry(`SSH 无法回连客户端，按手动用户名保存: ${username}@${clientIp}`, 'warning');
            }
        }

        const savedUsername = await saveUsernameManually(clientIp, verifiedUsername);
        showToast(verifiedBySsh ? `✅ 用户名验证成功: ${savedUsername}` : `✅ 已保存用户名: ${savedUsername}`, 'success');
        addLogEntry(`客户端识别成功: ${savedUsername}@${clientIp}`, 'success');

        closeUsernameDetectModal();
    } catch (error) {
        console.error('[UsernameDetect] Error:', error);
        if (password && isUsernameManualFallbackError(error)) {
            try {
                const savedUsername = await saveUsernameManually(clientIp, username);
                showToast(`✅ SSH 不可达，已保存用户名: ${savedUsername}`, 'success');
                addLogEntry(`SSH 无法回连客户端，已手动保存: ${savedUsername}@${clientIp}`, 'warning');
                closeUsernameDetectModal();
                return;
            } catch (saveError) {
                console.error('[UsernameDetect] Manual save failed:', saveError);
                showToast(`❌ 保存失败: ${saveError.message}`, 'error');
                return;
            }
        }
        showToast(`❌ 验证失败: ${error.message}`, 'error');
    } finally {
        submitBtn.textContent = originalText;
        submitBtn.disabled = false;
    }
}

function handleDevicePasswordKeyPress(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        submitDevicePassword();
    }
}

async function submitDevicePassword() {
    const password = document.getElementById('device-pswd').value;
    const deviceHost = document.getElementById('device-host-display').value.trim() || pendingUsbipDeviceHost;
    if (!password) {
        showToast('请输入密码', 'warning');
        return;
    }

    if (
        pendingDevicePasswordAction === 'sshd'
        || pendingDevicePasswordAction === 'terminal'
        || pendingDevicePasswordAction === 'usbip-list'
    ) {
        const action = pendingDevicePasswordAction;
        const retry = pendingDevicePasswordRetry;
        try {
            await apiCall('/api/config/client-ssh-credentials', 'POST', {
                device_host: deviceHost,
                password
            });
            closeDevicePasswordModal();
            showToast('SSH 凭据已保存', 'success');
            addLogEntry(`已保存 ${deviceHost} 的 SSH 凭据`, 'success');
            pendingDevicePasswordAction = 'usbip';
            if (action === 'terminal' || action === 'usbip-list') {
                if (retry) await retry();
            } else {
                addLogEntry('正在重新检查 SSHD...', 'info');
                await checkSshd();
            }
        } catch (error) {
            addLogEntry('保存 SSH 凭据失败: ' + error.message, 'error');
            showToast('保存失败: ' + error.message, 'error');
        }
        return;
    }

    try {
        // 显示正在连接的提示
        addLogEntry('正在连接 USB/IP...', 'info');
        showToast('正在连接...', 'info');

        // 立即关闭模态框
        closeDevicePasswordModal();

        const result = await apiCall('/api/usbip/connect', 'POST', {
            device_host: deviceHost,
            worker_id: activeUsbipSelection?.worker_id || '',
            busids: activeUsbipSelection?.busids || [],
            device_password: password,
            manual_connect: true
        });
        if (!isUsbipAdbReady(result)) {
            throw new Error(result.error || result.message || 'USB/IP 连接后尚未识别到 ADB 设备');
        }

        // 状态由主按钮处理。
        addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
        showToast('USB/IP 连接成功', 'success');

        // 刷新设备列表（使用防抖版本）
        setTimeout(() => debouncedRefreshDevices(), 3500);

        // 主函数返回后更新按钮状态。
        const btn = $('usbip-btn');
        if (btn) {
            state.usbipConnected = true;
            pendingUsbipDeviceHost = result.device_host || deviceHost || pendingUsbipDeviceHost;
            btn.textContent = '📱 断开设备';
            btn.disabled = false;
        }
    } catch (error) {
        addLogEntry('启动 USB/IP 失败: ' + error.message, 'error');
        showToast('连接失败: ' + error.message, 'error');

        // 确保按钮状态正确
        const btn = $('usbip-btn');
        if (btn) {
            btn.textContent = '📱 本地设备';
            btn.disabled = false;
        }
    }
}

