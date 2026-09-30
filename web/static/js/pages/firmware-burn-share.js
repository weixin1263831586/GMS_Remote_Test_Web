function firmwareShareSetValidation(message, type = 'info') {
    const el = document.getElementById('firmware-share-validation');
    if (!el) return;
    const colorMap = {
        success: 'var(--success-color)',
        error: 'var(--danger-color)',
        warning: 'var(--warning-color)',
        info: 'var(--text-secondary)',
    };
    el.style.color = colorMap[type] || colorMap.info;
    el.textContent = message || '';
}

function firmwareShareDefaults() {
    const config = state.config || {};
    const share = config.firmware_shares || {};
    const configuredRemote = String(share.default_remote || '').trim();
    const match = configuredRemote.match(/^(?:([^@:/]+)@)?([^:]+):(\/.*)$/);
    if (match) {
        const user = match[1] || share.default_user || config.ubuntu_user || '';
        return {user, host: match[2], path: match[3], remote: configuredRemote};
    }
    const connection = String(share.default_host || config.local_server || '').trim();
    const at = connection.lastIndexOf('@');
    const user = String(
        share.default_user
        || (at > 0 ? connection.slice(0, at) : '')
        || config.ubuntu_user
        || ''
    ).trim();
    const host = String(
        at > 0 ? connection.slice(at + 1) : (connection || config.ubuntu_host || '')
    ).trim();
    const path = String(share.default_path || '').trim();
    return {user, host, path, remote: ''};
}

function shareFirmware() {
    const input = document.getElementById('firmware-share-remote');
    const defaults = firmwareShareDefaults();
    if (input) {
        if (!input.value.trim() && defaults.remote) input.value = defaults.remote;
        input.placeholder = defaults.host
            ? `${defaults.user ? `${defaults.user}@` : ''}${defaults.host}:${defaults.path}/firmware.img`
            : 'user@host:/absolute/path/to/firmware.img';
    }
    firmwareShareSetValidation('');
    ModalManager.open('firmware-share-modal');
    loadFirmwareShares();
}

async function browseRemoteFileForFirmwareShare() {
    const defaults = firmwareShareDefaults();
    if (!defaults.host || !defaults.user) {
        showToast('请在 config.json 的 firmware_shares 或 local_server 中配置共享固件主机', 'warning');
        return;
    }
    state.fileBrowser.mode = 'firmware-share';
    state.fileBrowser.targetInputId = 'firmware-share-remote';
    state.fileBrowser.selectedFile = null;
    state.fileBrowser.remoteHost = defaults.host;
    state.fileBrowser.remoteUser = defaults.user;
    document.getElementById('file-browser-title').textContent = '选择共享固件';
    ModalManager.open('file-browser-modal');
    await loadFileDirectory(defaults.path);
}

function closeFirmwareShareModal() {
    ModalManager.close('firmware-share-modal');
}

async function firmwareShareApi(path, options = {}, elevationRetried = false) {
    const response = await fetch(path, {
        credentials: 'same-origin',
        headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
        ...options,
    });
    const data = await response.json().catch(() => ({}));
    const detail = data?.detail;
    if (
        response.status === 403
        && !elevationRetried
        && detail
        && typeof detail === 'object'
        && detail.elevation_required
    ) {
        const granted = await requestElevatedAccess('管理远端固件分享');
        if (granted) return firmwareShareApi(path, options, true);
    }
    if (!response.ok || data.success === false) {
        throw new Error(
            data.error
            || (typeof detail === 'object' ? detail.message : detail)
            || `HTTP ${response.status}`
        );
    }
    return data;
}

// ---- 远端固件主机密码：仅内存缓存（不进 sessionStorage，
// 避免 DOM XSS 读取明文密码）；刷新后需重新输入。----
const _firmwareSharePasswords = {};
function getShareFirmwarePassword(host) {
    return _firmwareSharePasswords[host || 'default'] || '';
}
function setShareFirmwarePassword(host, password) {
    const key = host || 'default';
    if (password) {
        _firmwareSharePasswords[key] = password;
    } else {
        delete _firmwareSharePasswords[key];
    }
}

let _firmwareSharePasswordResolver = null;
function promptFirmwareSharePassword(host, message) {
    return new Promise((resolve) => {
        _firmwareSharePasswordResolver = resolve;
        document.getElementById('firmware-share-password-host').value = host || '';
        const input = document.getElementById('firmware-share-password-input');
        input.value = '';
        const info = document.querySelector('#firmware-share-password-modal .modal-info-text');
        if (info) {
            info.textContent = message
                ? `⚠️ ${message}（仅本会话使用，不持久保存）`
                : '⚠️ 连接远端固件主机认证失败，请输入该主机的 SSH 登录密码（仅本会话使用，不持久保存）。';
        }
        ModalManager.open('firmware-share-password-modal');
        setTimeout(() => input.focus(), 50);
    });
}
function closeFirmwareSharePasswordModal() {
    ModalManager.close('firmware-share-password-modal');
    if (_firmwareSharePasswordResolver) {
        const resolver = _firmwareSharePasswordResolver;
        _firmwareSharePasswordResolver = null;
        resolver(null);
    }
}
function handleFirmwareSharePasswordKeyPress(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        submitFirmwareSharePassword();
    }
}
function submitFirmwareSharePassword() {
    const password = document.getElementById('firmware-share-password-input').value;
    ModalManager.close('firmware-share-password-modal');
    if (_firmwareSharePasswordResolver) {
        const resolver = _firmwareSharePasswordResolver;
        _firmwareSharePasswordResolver = null;
        resolver(password || null);
    }
}

// 带认证重试的固件分享 API 调用：
// 使用会话密码发送 body；401 或连接失败时提示输入并重试一次。
// host 用于缓存密码与弹框展示。返回与 firmwareShareApi 一致的成功数据；失败抛 Error。
async function firmwareShareApiWithAuth(path, body, host) {
    const buildOptions = (password) => ({
        method: 'POST',
        body: JSON.stringify({ ...body, ...(password ? { password } : {}) }),
    });
    const send = async (password, elevationRetried = false) => {
        const response = await fetch(path, {
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            ...buildOptions(password),
        });
        const data = await response.json().catch(() => ({}));
        const detail = data?.detail;
        if (
            response.status === 403
            && !elevationRetried
            && detail
            && typeof detail === 'object'
            && detail.elevation_required
        ) {
            const granted = await requestElevatedAccess('访问远端固件主机');
            if (granted) return send(password, true);
        }
        return {response, data};
    };
    const cached = getShareFirmwarePassword(host);
    const initial = await send(cached);
    const response = initial.response;
    const data = initial.data;

    // 401（认证失败）或 400（连接超时/网络不通）时，都弹框让用户输入密码重试。
    // 因为 "timed out" 等网络错误可能源于认证环节，给用户输入密码的机会更合理。
    const isAuthOrConnectionError = response.status === 401
        || (response.status === 400 && !cached);
    if (isAuthOrConnectionError) {
        const message = data.error || '连接远端固件主机失败，请输入 SSH 登录密码重试';
        const password = await promptFirmwareSharePassword(host, message);
        if (!password) {
            throw new Error(message);
        }
        setShareFirmwarePassword(host, password);
        const retried = await send(password);
        const retry = retried.response;
        const retryData = retried.data;
        if (!retry.ok || retryData.success === false) {
            // 密码错误也清除缓存，避免反复用错密码
            if (retry.status === 401) setShareFirmwarePassword(host, '');
            const retryDetail = retryData?.detail;
            throw new Error(
                retryData.error
                || (typeof retryDetail === 'object'
                    ? retryDetail.message : retryDetail)
                || `HTTP ${retry.status}`
            );
        }
        return retryData;
    }
    if (!response.ok || data.success === false) {
        const detail = data?.detail;
        throw new Error(
            data.error
            || (typeof detail === 'object' ? detail.message : detail)
            || `HTTP ${response.status}`
        );
    }
    return data;
}

function firmwareShareRemoteText(record) {
    const user = record.user ? `${record.user}@` : '';
    return `${user}${record.host}:${record.path}`;
}

function firmwareShareDate(ts) {
    if (!ts) return '-';
    const date = new Date(Number(ts) * 1000);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString();
}

async function loadFirmwareShares() {
    const tbody = document.getElementById('firmware-share-list');
    if (!tbody) return;
    const hadRenderedShares = tbody.dataset.loaded === 'true';
    tbody.setAttribute('aria-busy', 'true');
    if (!hadRenderedShares) {
        tbody.innerHTML = '<tr><td colspan="5" style="padding: 14px; color: var(--text-secondary); text-align: center;">加载中...</td></tr>';
    }
    try {
        const result = await firmwareShareApi('/api/firmware-shares');
        const records = result.data?.records || [];
        if (!records.length) {
            tbody.innerHTML = '<tr><td colspan="5" style="padding: 14px; color: var(--text-secondary); text-align: center;">暂无共享固件</td></tr>';
            tbody.dataset.loaded = 'true';
            return;
        }
        tbody.innerHTML = records.map(record => {
            const name = escapeHtml(record.name || record.filename || record.id);
            const remote = escapeHtml(firmwareShareRemoteText(record));
            const id = escapeHtml(record.id);
            const title = escapeHtml(`${firmwareShareRemoteText(record)}\n创建: ${firmwareShareDate(record.created_at)}\n修改: ${firmwareShareDate(record.mtime)}`);
            return `
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${name}">${name}</td>
                    <td style="padding: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: monospace; font-size: 12px;" title="${title}">${remote}</td>
                    <td style="padding: 8px; text-align: right;">${formatBytes(record.size || 0, true) || '-'}</td>
                    <td style="padding: 8px; text-align: center;">${record.downloads || 0}</td>
                    <td style="padding: 8px; text-align: center; white-space: nowrap;" class="firmware-share-actions">
                        <button class="btn-xxs" type="button" data-action="copy-share-link" data-id="${id}">分享</button>
                        <button class="btn-xxs" type="button" data-action="download-share" data-id="${id}">下载</button>
                        <button class="btn-xxs" type="button" data-action="delete-share" data-id="${id}">删除</button>
                    </td>
                </tr>
            `;
        }).join('');
        tbody.dataset.loaded = 'true';
        tbody.querySelectorAll('[data-action="copy-share-link"]').forEach((btn) => {
            btn.addEventListener('click', () => copyFirmwareShareLink(btn.dataset.id));
        });
        tbody.querySelectorAll('[data-action="download-share"]').forEach((btn) => {
            btn.addEventListener('click', () => downloadFirmwareShare(btn.dataset.id));
        });
        tbody.querySelectorAll('[data-action="delete-share"]').forEach((btn) => {
            btn.addEventListener('click', () => deleteFirmwareShare(btn.dataset.id));
        });
    } catch (error) {
        if (hadRenderedShares) showToast(`共享固件列表刷新失败: ${error.message}`, 'error');
        else tbody.innerHTML = `<tr><td colspan="5" style="padding: 14px; color: var(--danger-color); text-align: center;">${escapeHtml(error.message)}</td></tr>`;
    } finally {
        tbody.setAttribute('aria-busy', 'false');
    }
}

// 从 "user@host:/path" 中解析出 host，用于密码缓存与弹框展示。
function parseShareFirmwareHost(remote) {
    const match = String(remote || '').trim().match(/^(?:[^@:/]+@)?([^:/]+):/);
    return match ? match[1] : '';
}

async function validateFirmwareShare() {
    const remote = document.getElementById('firmware-share-remote')?.value?.trim() || '';
    if (!remote) {
        firmwareShareSetValidation('请输入远端固件路径', 'error');
        return;
    }
    firmwareShareSetValidation('正在校验远端固件...', 'info');
    try {
        const result = await firmwareShareApiWithAuth('/api/firmware-shares/validate', { remote }, parseShareFirmwareHost(remote));
        const info = result.data || {};
        firmwareShareSetValidation(`校验通过: ${info.filename || ''} ${formatBytes(info.size || 0)} 修改时间 ${firmwareShareDate(info.mtime)}`, 'success');
    } catch (error) {
        firmwareShareSetValidation(error.message, 'error');
    }
}

async function createFirmwareShare() {
    const remote = document.getElementById('firmware-share-remote')?.value?.trim() || '';
    const name = document.getElementById('firmware-share-name')?.value?.trim() || '';
    const expiresDays = parseInt(document.getElementById('firmware-share-expire-days')?.value || '0', 10) || 0;
    if (!remote) {
        firmwareShareSetValidation('请输入远端固件路径', 'error');
        return;
    }
    firmwareShareSetValidation('正在创建分享...', 'info');
    try {
        await firmwareShareApiWithAuth('/api/firmware-shares', { remote, name, expires_days: expiresDays }, parseShareFirmwareHost(remote));
        firmwareShareSetValidation('固件分享已创建', 'success');
        showToast('固件分享已创建', 'success');
        await loadFirmwareShares();
    } catch (error) {
        firmwareShareSetValidation(error.message, 'error');
        showToast(`创建分享失败: ${error.message}`, 'error');
    }
}

async function ensureFirmwareShareReady(id) {
    if (!id) return;
    try {
        await firmwareShareApi(`/api/firmware-shares/${encodeURIComponent(id)}/check`);
        return true;
    } catch (error) {
        const message = error.message || '远端认证失败';
        if (!message.includes('认证失败') && !message.includes('Authentication')) {
            showToast(`共享固件校验失败: ${message}`, 'error');
            return false;
        }
        const password = await promptFirmwareSharePassword('', '该共享固件缺少有效远端 SSH 密码，请输入后保存到此分享记录');
        if (!password) {
            showToast('已取消操作', 'warning');
            return false;
        }
        try {
            await firmwareShareApi(`/api/firmware-shares/${encodeURIComponent(id)}/credentials`, {
                method: 'POST',
                body: JSON.stringify({ password }),
            });
            showToast('远端凭据已更新', 'success');
            await loadFirmwareShares();
            return true;
        } catch (saveError) {
            showToast(`远端凭据更新失败: ${saveError.message}`, 'error');
            return false;
        }
    }
}

async function downloadFirmwareShare(id) {
    if (!await ensureFirmwareShareReady(id)) return;
    triggerDownload(`/api/firmware-shares/${encodeURIComponent(id)}/download`, '');
}

async function copyFirmwareShareLink(id) {
    if (!id) return;
    if (!await ensureFirmwareShareReady(id)) return;
    const url = `${window.location.origin}/api/firmware-shares/${encodeURIComponent(id)}/download`;
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(url);
        } else {
            fallbackCopyText(url);
        }
        showToast('分享链接已复制，无需登录即可打开下载', 'success');
    } catch (error) {
        fallbackCopyText(url);
        showToast('分享链接已复制，无需登录即可打开下载', 'success');
    }
}

async function deleteFirmwareShare(id) {
    const confirmed = await showConfirmDialog('删除共享固件', '确定删除这条固件分享记录吗？不会删除远端固件文件。');
    if (!confirmed) return;
    try {
        await firmwareShareApi(`/api/firmware-shares/${encodeURIComponent(id)}`, { method: 'DELETE' });
        showToast('固件分享已删除', 'success');
        await loadFirmwareShares();
    } catch (error) {
        showToast(`删除失败: ${error.message}`, 'error');
    }
}

