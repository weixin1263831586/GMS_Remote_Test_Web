// ==================== VNC & Remote Control ====================
async function burnFirmware() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写固件的设备', 'warning');
        return;
    }
    if (!validateLocalUsbDeviceSelection('烧写固件')) return;
    const fastbootDevices = selectedFastbootDeviceIds();
    if (fastbootDevices.length > 0) {
        showToast(
            `普通固件烧写需要 ADB 设备；Fastboot/Fastbootd 请使用“烧写GSI”: ${fastbootDevices.join(', ')}`,
            'warning'
        );
        return;
    }

    // Show firmware configuration modal
    ModalManager.open('firmware-modal');
}

function closeFirmwareModal() {
    ModalManager.close('firmware-modal');
}

// 在UI上锁定设备（前端立即显示，不等待后端）
function lockDevicesInUI(devices) {
    devices.forEach(deviceId => {
        const device = state.devices.find(d => {
            const id = typeof d === 'string' ? d : d.device_id;
            return id === deviceId;
        });
        if (device) {
            if (typeof device === 'string') {
                const idx = state.devices.indexOf(device);
                state.devices[idx] = {
                    device_id: device,
                    locked: true,
                    locked_by: '当前用户',
                    locked_at: new Date().toISOString()
                };
            } else {
                device.locked = true;
                device.locked_by = '当前用户';
                device.locked_at = new Date().toISOString();
            }
        }
    });
    renderDevices();  // 立即更新UI
}

// Browse local file for firmware (uses native file picker)
function browseLocalFileForFirmware() {
    // 创建隐藏的文件输入框
    let fileInput = document.getElementById('firmware-file-input');
    if (!fileInput) {
        fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.id = 'firmware-file-input';
        fileInput.accept = '*.img,*.bin,*.update';
        fileInput.style.display = 'none';
        document.body.appendChild(fileInput);
    }

    fileInput.onchange = (e) => {
        const file = e.target.files[0];
        if (file) {
            const target = document.getElementById('firmware-path');
            if (target) {
                target.value = file.name;  // 只显示文件名
                const savedName = sessionStorage.getItem('firmwareUploadFileName');
                const savedSize = parseInt(sessionStorage.getItem('firmwareUploadFileSize') || '0');
                const savedLastModified = parseInt(sessionStorage.getItem('firmwareUploadLastModified') || '-1');
                const interrupted = sessionStorage.getItem('firmwareUploadInterrupted') === 'true';
                if (interrupted && savedName === file.name && savedSize === file.size && savedLastModified === (file.lastModified || 0)) {
                    showToast(`已选择同一固件，将从已上传分片续传: ${file.name}`, 'info');
                    addLogEntry(`已选择同一固件，准备断点续传: ${file.name}`, 'info');
                } else {
                    showToast(`已选择固件文件: ${file.name}`, 'info');
                }
            }
        }
    };
    fileInput.click();
}

async function browseRemoteFileForFirmware() {
    const fileInput = document.getElementById('firmware-file-input');
    if (fileInput) {
        fileInput.value = '';
    }

    state.fileBrowser.mode = 'firmware';
    state.fileBrowser.targetInputId = 'firmware-path';
    state.fileBrowser.selectedFile = null;
    document.getElementById('file-browser-title').textContent = '选择服务器固件';
    ModalManager.open('file-browser-modal');

    await loadFileDirectory(getDefaultSuitesPath());
}

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

// ---- 远端固件主机密码：会话级缓存 + 弹框 ----
function _shareFirmwarePwdKey(host) {
    return `firmware_share_pwd_${host || 'default'}`;
}
function getShareFirmwarePassword(host) {
    return sessionStorage.getItem(_shareFirmwarePwdKey(host)) || '';
}
function setShareFirmwarePassword(host, password) {
    if (password) {
        sessionStorage.setItem(_shareFirmwarePwdKey(host), password);
    } else {
        sessionStorage.removeItem(_shareFirmwarePwdKey(host));
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
                        <button class="btn-xxs" onclick="copyFirmwareShareLink('${id}')">分享</button>
                        <button class="btn-xxs" onclick="downloadFirmwareShare('${id}')">下载</button>
                        <button class="btn-xxs" onclick="deleteFirmwareShare('${id}')">删除</button>
                    </td>
                </tr>
            `;
        }).join('');
        tbody.dataset.loaded = 'true';
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

async function submitFirmwareBurn() {
    const firmwarePath = document.getElementById('firmware-path').value.trim();
    if (!firmwarePath) {
        showToast('请选择固件文件', 'error');
        return;
    }

    // 获取文件输入框
    const fileInput = document.getElementById('firmware-file-input');
    const selectedFirmwareFile = fileInput?.files?.[0] || null;

    const devices = Array.from(state.selectedDevices);
    if (!devices.length) {
        showToast('请重新选择要烧写的设备', 'warning');
        return;
    }
    try {
        const granted = await requestElevatedAccess('烧写设备固件');
        if (!granted) return;
        closeFirmwareModal();
        showToast('正在烧写固件...', 'info');
        addLogEntry(`开始烧写固件: ${firmwarePath}`, 'info');

        const warnBeforeRefresh = (e) => {
            e.preventDefault();
            e.returnValue = '固件上传中，刷新会暂停浏览器上传；重新选择同一文件后可从已上传分片续传。确定要离开吗？';
            return e.returnValue;
        };
        const cleanupUploadState = () => {
            if (selectedFirmwareFile) {
                window.removeEventListener('beforeunload', warnBeforeRefresh);
                clearFirmwareUploadState();
            }
        };

        const workerId = selectedClusterWorker();
        if (workerId) {
            if (!selectedFirmwareFile) {
                throw new Error('远端 Worker 烧写必须选择本机固件文件，以便安全分发并校验 SHA-256');
            }
            if (devices.length !== 1) {
                throw new Error('集群固件烧写一次只允许选择一台设备');
            }
            lockDevicesInUI(devices);
            const form = new FormData();
            form.append('worker_id', workerId);
            form.append('devices', devices.join(','));
            form.append('firmware_file', selectedFirmwareFile, selectedFirmwareFile.name);
            const staged = await apiCall('/api/cluster/firmware/stage', 'POST', form);
            addLogEntry(`固件已暂存，Worker 命令: ${staged.command_id}`, 'success');
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 2000));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(staged.command_id)}`);
                const command = status.command;
                if (command.status === 'completed') {
                    addLogEntry(`远端固件烧写完成: ${command.result?.device || devices[0]}`, 'success');
                    showToast('远端固件烧写完成', 'success');
                    break;
                }
                if (['failed', 'cancelled'].includes(command.status)) {
                    throw new Error(command.error || '远端固件烧写失败');
                }
            }
            await switchTestWorker();
            return;
        }

        if (selectedFirmwareFile) {
            const uploadId = getReusableFirmwareUploadId(selectedFirmwareFile);
            // 设置上传状态标记，防止刷新导致进度丢失
            saveFirmwareUploadState(
                selectedFirmwareFile.name,
                selectedFirmwareFile.size,
                Date.now(),
                0,
                0,
                selectedFirmwareFile.size,
                uploadId,
                selectedFirmwareFile.lastModified || 0
            );

            // 添加beforeunload事件监听，警告用户不要刷新
            window.addEventListener('beforeunload', warnBeforeRefresh);
        } else {
            addLogEntry(`使用服务器固件路径，跳过本机上传: ${firmwarePath}`, 'info');
        }

        let uploadResult;
        if (selectedFirmwareFile) {
            const uploadId = getReusableFirmwareUploadId(selectedFirmwareFile);
            const startedAt = parseInt(sessionStorage.getItem('firmwareUploadStartTime') || Date.now());
            notifyOperationResult('固件上传已启动', '固件分片上传任务已开始', 'info', 'firmware-burn');
            addLogEntry(`固件上传任务已启动，设备: ${devices.join(', ')}`, 'success');

            uploadResult = await uploadFileInChunks(
                selectedFirmwareFile,
                `/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`,
                {
                    chunkSize: 32 * 1024 * 1024,
                    concurrent: 4,
                    resume: true,
                    checkExisting: true,
                    uploadId,
                    extraFormData: {
                        stage_only: '1',
                    },
                    onResume: (status) => {
                        const progress = status.progress || 0;
                        const uploadedSize = status.uploaded_size || Math.round((status.chunks_uploaded / status.total_chunks) * selectedFirmwareFile.size);
                        addLogEntry(`检测到已上传分片，继续上传: ${progress.toFixed(1)}% (${formatBytes(uploadedSize)}/${formatBytes(selectedFirmwareFile.size)})`, 'info');
                        showToast('检测到已上传分片，正在续传', 'info');
                    },
                    onProgress: (progress, uploadedChunks, totalChunks) => {
                        const uploadedSize = Math.min(
                            selectedFirmwareFile.size,
                            Math.round((uploadedChunks / totalChunks) * selectedFirmwareFile.size)
                        );
                        saveFirmwareUploadState(
                            selectedFirmwareFile.name,
                            selectedFirmwareFile.size,
                            startedAt,
                            progress,
                            uploadedSize,
                            selectedFirmwareFile.size,
                            uploadId,
                            selectedFirmwareFile.lastModified || 0
                        );
                        updateUploadProgress(progress, selectedFirmwareFile.name, uploadedSize, selectedFirmwareFile.size);
                    }
                }
            );
            cleanupUploadState();
            if (uploadResult.staged) {
                updateUploadProgress(
                    100,
                    selectedFirmwareFile.name,
                    selectedFirmwareFile.size,
                    selectedFirmwareFile.size
                );
                addLogEntry('固件已完成可续传暂存，正在启动烧写', 'success');
                lockDevicesInUI(devices);
                const finalizeForm = new FormData();
                finalizeForm.append('finalize_upload', '1');
                finalizeForm.append('upload_id', uploadId);
                notifyOperationResult('固件烧写已启动', '固件暂存完成，烧写任务已开始', 'info', 'firmware-burn');
                uploadResult = await apiCall(
                    `/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`,
                    'POST',
                    finalizeForm
                );
            }
        } else {
            const formData = new FormData();
            formData.append('firmware_path', firmwarePath);
            lockDevicesInUI(devices);
            // 使用XMLHttpRequest提交服务器路径烧写请求
            uploadResult = await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();

                xhr.addEventListener('load', () => {
                    if (xhr.status === 200) {
                        try {
                            const result = JSON.parse(xhr.responseText);
                            resolve(result);
                        } catch (e) {
                            reject(new Error('Invalid response'));
                        }
                    } else {
                        reject(new Error(`HTTP ${xhr.status}`));
                    }
                });

                xhr.addEventListener('error', () => {
                    reject(new Error('Network error'));
                });

                xhr.addEventListener('abort', () => {
                    reject(new Error('Upload aborted'));
                });

                xhr.open('POST', `/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`);
                applyClientIdentityHeadersToXhr(xhr);
                // 烧写请求发出时立即提示已启动。
                // 否则会被后端烧写完成的通知晚到，导致时序颠倒。
                notifyOperationResult('固件烧写已启动', '烧写任务已开始', 'info', 'firmware-burn');
                addLogEntry(`固件烧写任务已启动，设备: ${devices.join(', ')}`, 'success');
                xhr.send(formData);
            });
        }

        const result = uploadResult;
        if (!result.success) {
            notifyOperationResult('固件烧写失败', result.error, 'error', 'firmware-burn');
            addLogEntry(`固件烧写失败: ${result.error}`, 'error');
        }
    } catch (error) {
        notifyOperationResult('固件烧写失败', error.message, 'error', 'firmware-burn');
        addLogEntry(`固件烧写异常: ${error.message}`, 'error');
        loadDevices(true).catch(refreshError => {
            console.error('[Firmware Burn] Failed to refresh devices after error:', refreshError);
        });
    }
}

async function burnGsiImage() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写GSI的设备', 'warning');
        return;
    }
    if (!validateLocalUsbDeviceSelection('烧写GSI')) return;

    // Set default script path
    const scriptInput = document.getElementById('gsi-script');
    if (scriptInput && !scriptInput.value) {
        scriptInput.value = `${getDefaultSuitesPath()}/run_GSI_Burn.sh`;
    }

    // Show GSI configuration modal
    ModalManager.open('gsi-modal');
}

function closeGsiModal() {
    ModalManager.close('gsi-modal');
}

// Browse remote file for GSI script
async function browseLocalFileForGsiScript() {
    const title = '选择GSI烧写脚本';

    // Set file browser state
    state.fileBrowser.mode = 'gsi-script';
    state.fileBrowser.targetInputId = 'gsi-script';
    state.fileBrowser.selectedFile = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    ModalManager.open('file-browser-modal');

    // Load initial directory (GMS-Suite)
    await loadFileDirectory(getDefaultSuitesPath());
}

// Browse remote file for GSI system image
async function browseLocalFileForGsiSystem() {
    if (selectedClusterWorker()) {
        const input = document.createElement('input');
        input.type = 'file'; input.accept = '.img';
        input.onchange = () => {
            state.gsiSystemFile = input.files?.[0] || null;
            if (state.gsiSystemFile) document.getElementById('gsi-system').value = state.gsiSystemFile.name;
        };
        input.click();
        return;
    }
    const title = '选择System镜像';

    // Set file browser state
    state.fileBrowser.mode = 'gsi-system';
    state.fileBrowser.targetInputId = 'gsi-system';
    state.fileBrowser.selectedFile = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    ModalManager.open('file-browser-modal');

    // Load initial directory (GMS-Suite)
    await loadFileDirectory(getDefaultSuitesPath());
}

// Browse local file for GSI vendor image
function browseLocalFileForGsiVendor() {
    let input = document.getElementById('gsi-vendor-file-input');
    if (!input) {
        input = document.createElement('input');
        input.type = 'file';
        input.id = 'gsi-vendor-file-input';
        input.accept = '*.img';
        input.style.display = 'none';
        document.body.appendChild(input);
    }

    input.onchange = (e) => {
        const file = e.target.files[0];
        if (!file) return;
        state.gsiVendorFile = file;
        const target = document.getElementById('gsi-vendor');
        if (target) {
            target.value = file.name;
        }
        showToast(`已选择本机Vendor Boot镜像: ${file.name}`, 'info');
        addLogEntry(`已选择本机Vendor Boot镜像: ${file.name}`, 'info');
    };
    input.click();
}

// Browse remote file for GSI vendor image
async function browseRemoteFileForGsiVendor() {
    state.gsiVendorFile = null;
    const input = document.getElementById('gsi-vendor-file-input');
    if (input) {
        input.value = '';
    }

    const title = '选择Vendor Boot镜像';

    state.fileBrowser.mode = 'gsi-vendor';
    state.fileBrowser.targetInputId = 'gsi-vendor';
    state.fileBrowser.selectedFile = null;

    document.getElementById('file-browser-title').textContent = title;
    ModalManager.open('file-browser-modal');

    await loadFileDirectory(getDefaultSuitesPath());
}

async function uploadGsiVendorBootToTestHost(file) {
    const granted = await requestElevatedAccess(
        '上传 Vendor Boot 镜像到测试主机'
    );
    if (!granted) throw new Error('已取消管理员提权');
    await apiCall('/api/terminal/open');
    const targetDir = getDefaultSuitesPath();
    const formData = new FormData();
    formData.append('file', file);
    formData.append('path', targetDir);

    addLogEntry(`正在上传Vendor Boot镜像到测试主机: ${file.name}`, 'info');
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.upload.addEventListener('progress', (e) => {
            if (!e.lengthComputable) return;
            const percentage = (e.loaded / e.total) * 100;
            updateUploadProgress(percentage, file.name, e.loaded, e.total);
        });
        xhr.addEventListener('load', () => {
            let result = {};
            try {
                result = JSON.parse(xhr.responseText || '{}');
            } catch (_e) {
                reject(new Error('Vendor Boot上传响应解析失败'));
                return;
            }
            if (xhr.status === 200 && result.success) {
                updateUploadProgress(100, file.name, file.size, file.size);
                addLogEntry(`Vendor Boot镜像上传完成: ${result.remote_path}`, 'success');
                resolve(result.remote_path);
                return;
            }
            reject(new Error(result.error || `Vendor Boot上传失败: HTTP ${xhr.status}`));
        });
        xhr.addEventListener('error', () => reject(new Error('Vendor Boot上传网络错误')));
        xhr.open('POST', '/api/terminal/push');
        xhr.send(formData);
    });
}

async function submitGsiBurn() {
    const scriptPath = document.getElementById('gsi-script').value.trim();
    const systemImg = document.getElementById('gsi-system').value.trim();
    let vendorImg = document.getElementById('gsi-vendor').value.trim();

    if (!scriptPath) {
        showToast('请选择GSI烧写脚本', 'error');
        return;
    }
    if (!systemImg && !vendorImg) {
        showToast('请至少选择 System 镜像或 Vendor Boot 镜像之一', 'error');
        return;
    }

    try {
        const workerId = selectedClusterWorker();
        if (workerId) {
            if (!state.gsiSystemFile && !state.gsiVendorFile) throw new Error('远端 GSI 烧写必须选择本机 System 或 Vendor Boot 镜像');
            if (state.selectedDevices.size !== 1) throw new Error('集群 GSI 烧写一次只允许一台设备');
            const form = new FormData();
            form.append('worker_id', workerId);
            form.append('devices', Array.from(state.selectedDevices).join(','));
            if (state.gsiSystemFile) form.append('system_file', state.gsiSystemFile, state.gsiSystemFile.name);
            if (state.gsiVendorFile) form.append('vendor_file', state.gsiVendorFile, state.gsiVendorFile.name);
            closeGsiModal();
            const staged = await apiCall('/api/cluster/gsi/stage', 'POST', form);
            addLogEntry(`GSI 已暂存，Worker 命令: ${staged.command_id}`, 'success');
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 2000));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(staged.command_id)}`);
                if (status.command.status === 'completed') break;
                if (['failed', 'cancelled'].includes(status.command.status)) throw new Error(status.command.error || 'GSI 烧写失败');
            }
            state.gsiSystemFile = null; state.gsiVendorFile = null;
            showToast('远端 GSI 烧写完成', 'success');
            await switchTestWorker();
            return;
        }
        if (state.gsiVendorFile) {
            vendorImg = await uploadGsiVendorBootToTestHost(state.gsiVendorFile);
            const vendorInput = document.getElementById('gsi-vendor');
            if (vendorInput) {
                vendorInput.value = vendorImg;
            }
            state.gsiVendorFile = null;
        }

        await executeBurnOperation('/api/burn/gsi', {
            system_img: systemImg,
            vendor_img: vendorImg,
            script_path: scriptPath
        }, '烧写GSI', closeGsiModal);
    } catch (error) {
        showToast(error.message, 'error');
        addLogEntry(`GSI Vendor Boot准备失败: ${error.message}`, 'error');
    }
}

async function burnSerialNumber() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写SN码的设备', 'warning');
        return;
    }

    // Show SN configuration modal
    ModalManager.open('sn-modal');
}

function closeSnModal() {
    ModalManager.close('sn-modal');
}

async function submitSnBurn() {
    const snCode = document.getElementById('sn-code').value.trim();
    if (!snCode) {
        showToast('SN码不能为空', 'error');
        return;
    }

    await executeBurnOperation('/api/burn/serial', {
        sn_code: snCode
    }, '烧写SN码', closeSnModal);
}

// ==================== 烧写操作辅助函数 ====================
async function executeBurnOperation(endpoint, data, operationName, closeModalFunc) {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要操作的设备', 'warning');
        return;
    }

    const devices = Array.from(state.selectedDevices);
    let stopDeviceProtocolRefresh = () => {};
    try {
        const granted = await requestElevatedAccess(operationName);
        if (!granted) return;
        if (closeModalFunc) {
            closeModalFunc();
        }

        addLogEntry(`正在${operationName}...`, 'info');
        showToast(`正在${operationName}...`, 'info');

        // 立即在UI上标记设备为锁定状态
        lockDevicesInUI(devices);

        if (endpoint === '/api/burn/gsi') {
            stopDeviceProtocolRefresh = startBurnDeviceProtocolRefresh(devices);
        }

        // 调用API
        const result = await apiCall(endpoint, 'POST', {
            ...data,
            devices: devices
        });

        if (result.success) {
            // 显示详细结果
            addLogEntry(`${operationName}完成`, 'success');
            if (result.results && result.results.length > 0) {
                result.results.forEach(item => {
                    if (item.success) {
                        addLogEntry(`  设备 ${item.device}: 成功`, 'success');
                    } else {
                        addLogEntry(`  设备 ${item.device}: 失败 - ${item.error || item.output}`, 'error');
                    }
                });
            }
        } else {
            addLogEntry(`${operationName}失败: ${result.error || '未知错误'}`, 'error');
            notifyOperationResult(`${operationName}失败`, result.error || '未知错误', 'error', 'burn-operation', {
                operation: operationName,
                endpoint
            });
        }
    } catch (error) {
        addLogEntry(`${operationName}失败: ${error.message}`, 'error');
        notifyOperationResult(`${operationName}失败`, error.message, 'error', 'burn-operation', {
            operation: operationName,
            endpoint
        });
    } finally {
        stopDeviceProtocolRefresh();
        try {
            await loadDevices(true);
            if (typeof currentPage !== 'undefined' && currentPage === 'devices' && typeof loadDevicesManagement === 'function') {
                await loadDevicesManagement();
            }
        } catch (refreshError) {
            console.warn('[Burn] Failed to refresh devices after operation:', refreshError);
        }
    }
}

async function initAndStartVnc(forceRestart = false) {
    try {
        const workerId = workspaceWorkerId();
        const logMsg = forceRestart
            ? '🔄 正在重启VNC环境（杀死旧进程并重新启动）...'
            : '🔄 正在启动VNC环境...';
        addLogEntry(logMsg, 'info');
        const request = {force_restart: forceRestart};
        request.worker_id = workerId;
        if (!isLocalWorkspaceWorker(workerId)) {
            const host = await resolveClusterHost(workerId);
            addLogEntry(`目标测试主机: ${workerId} (${host.address})`, 'info');
        }
        const result = isLocalWorkspaceWorker(workerId)
            ? await apiCall('/api/desktop/vnc/start', 'POST', request)
            : await apiCall(`/api/cluster/workers/${encodeURIComponent(workerId)}/restart-vnc`, 'POST');
        if (!result.success) {
            throw new Error(result.error || 'VNC 启动失败');
        }
        addLogEntry(result.message || 'VNC 服务已就绪', 'info');
        return result;
    } catch (error) {
        addLogEntry('启动 VNC 失败: ' + error.message, 'error');
        throw error;
    }
}

async function showDeviceScreen() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return;
    }

    try {
        const workerId = selectedClusterWorker();
        if (workerId) {
            addLogEntry(`正在 ${workerId} 启动设备投屏...`, 'info');
            const result = await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'scrcpy_start'
            });
            addLogEntry(`已在 ${workerId} 启动 ${result.summary?.success || state.selectedDevices.size} 个投屏窗口`, 'success');
            window.GmsWorkspace?.update({worker_id: workerId, origin_page: 'desktop'}, {source: 'device-screen'});
            switchPage('desktop');
            return;
        }
        addLogEntry('正在检查 VNC 服务...', 'info');
        await initAndStartVnc();

        addLogEntry('正在启动屏幕投屏...', 'info');
        const result = await apiCall('/api/devices/scrcpy', 'POST', {
            devices: Array.from(state.selectedDevices)
        });

        // Display result message
        if (result.success) {
            // Display the detailed message from backend
            if (result.message) {
                // Split multi-line message and log each part
                const lines = result.message.split('\n');
                lines.forEach(line => {
                    if (line.includes('✅')) {
                        addLogEntry(line, 'success');
                    } else if (line.includes('ℹ️')) {
                        addLogEntry(line, 'info');
                    } else if (line.includes('❌')) {
                        addLogEntry(line, 'error');
                    } else {
                        addLogEntry(line, 'success');
                    }
                });
            } else {
                addLogEntry(`屏幕投屏已启动，共 ${result.results?.length || 0} 个设备`, 'success');
            }

            // Display device info
            if (result.vnc_sessions && result.vnc_sessions.length > 0) {
                result.vnc_sessions.forEach(session => {
                    addLogEntry(`  设备 ${session.device}: ${session.message || '已启动'}`, 'info');
                });
            }

            // Show note if available
            if (result.note) {
                addLogEntry(`ℹ️ ${result.note}`, 'info');
            }

            // Auto-switch to desktop page
            setTimeout(() => {
                if (typeof switchPage === 'function') {
                    switchPage('desktop');
                } else {
                    console.error('switchPage function not found');
                }
            }, 500);

            // Show appropriate toast message
            if (result.already_running && result.already_running.length > 0) {
                if (result.newly_started && result.newly_started.length > 0) {
                    showToast(`已启动 ${result.newly_started.length} 个设备，${result.already_running.length} 个设备已在投屏`, 'success');
                } else {
                    showToast(`所有 ${result.already_running.length} 个设备已在投屏`, 'info');
                }
            } else {
                showToast('屏幕投屏已启动', 'success');
            }
        } else {
            // Screen casting failed - show errors
            addLogEntry(result.message || '屏幕投屏启动失败', 'error');

            // Display detailed error for each device
            if (result.errors && result.errors.length > 0) {
                result.errors.forEach(errorMsg => {
                    addLogEntry(`  ❌ ${errorMsg}`, 'error');
                });
            }

            // Show results for each device
            if (result.results && result.results.length > 0) {
                result.results.forEach(r => {
                    if (r.success) {
                        addLogEntry(`  ✅ ${r.device}: 已启动`, 'success');
                    } else {
                        addLogEntry(`  ❌ ${r.device}: ${r.error || r.running ? '进程未运行' : '启动失败'}`, 'error');
                    }
                });
            }

            showToast('屏幕投屏启动失败，请查看日志', 'error');
        }
    } catch (error) {
        addLogEntry('显示屏幕失败: ' + error.message, 'error');
        showToast('显示屏幕失败: ' + error.message, 'error');
    }
}

async function setupAdbPortForward() {
    const granted = await requestElevatedAccess('管理ADB');
    if (!granted) return;
    await openAdbProxyModal();
}

async function openAdbProxyModal() {
    const assignments = document.getElementById('adb-proxy-assignments');
    const message = document.getElementById('adb-proxy-message');
    const submit = document.getElementById('adb-proxy-connect-submit');
    if (!assignments || !message || !submit) return;
    const hadRenderedAssignments = assignments.dataset.loaded === 'true';
    assignments.setAttribute('aria-busy', 'true');
    if (!hadRenderedAssignments) assignments.textContent = '正在读取接入状态...';
    message.textContent = '正在读取设备来源和接入主机...';
    submit.disabled = true;
    ModalManager.open('adb-proxy-modal');
    startAdbProxyDeviceRefresh();
    try {
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        state.adbForwardRunning = Boolean(adbProxyStatus.connected);
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        updateAdbProxyButton();
    } catch (error) {
        if (!hadRenderedAssignments) assignments.textContent = '接入状态读取失败';
        message.textContent = `加载ADB接入信息失败：${error.message}`;
        addLogEntry('加载ADB接入信息失败: ' + error.message, 'error');
    } finally {
        assignments.setAttribute('aria-busy', 'false');
    }
}

function closeAdbProxyModal() {
    stopAdbProxyDeviceRefresh();
    ModalManager.close('adb-proxy-modal');
}

function adbProxySelectionSnapshot() {
    const deviceSelect = document.getElementById('adb-proxy-source-devices');
    return {
        sourceWorkerId: document.getElementById('adb-proxy-source-host')?.value || '',
        targetWorkerId: document.getElementById('adb-proxy-target-host')?.value || '',
        knownDeviceSerials: new Set(
            Array.from(deviceSelect?.options || []).map(option => option.value).filter(Boolean)
        ),
        selectedDeviceSerials: new Set(
            Array.from(deviceSelect?.selectedOptions || []).map(option => option.value).filter(Boolean)
        ),
    };
}

function stopAdbProxyDeviceRefresh() {
    if (adbProxyDeviceRefreshTimer) clearInterval(adbProxyDeviceRefreshTimer);
    adbProxyDeviceRefreshTimer = null;
    adbProxyDeviceRefreshRunning = false;
}

function startAdbProxyDeviceRefresh() {
    stopAdbProxyDeviceRefresh();
    const refresh = async () => {
        if (
            !ModalManager.isOpen('adb-proxy-modal')
            || adbProxyDeviceRefreshRunning
            || adbProxyOperationRunning
        ) return;
        adbProxyDeviceRefreshRunning = true;
        const selection = adbProxySelectionSnapshot();
        try {
            adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
            state.adbForwardRunning = Boolean(adbProxyStatus.connected);
            renderAdbProxyAssignments();
            renderAdbProxyHosts(selection);
            updateAdbProxyButton();
        } catch (error) {
            debugLog('[ADB Proxy] automatic source refresh failed:', error.message);
        } finally {
            adbProxyDeviceRefreshRunning = false;
        }
    };
    adbProxyDeviceRefreshTimer = setInterval(
        () => void refresh(),
        DEVICE_ROUTING_REFRESH_INTERVAL_MS
    );
    ModalManager.onClose('adb-proxy-modal', stopAdbProxyDeviceRefresh);
}

function adbProxyHostLabel(host) {
    return host.worker_id || '未知 Worker';
}

function toggleAdbProxyUbuntuSource(forceOpen) {
    const panel = document.getElementById('adb-proxy-ubuntu-source-panel');
    const toggle = document.getElementById('adb-proxy-add-ubuntu-toggle');
    if (!panel || !toggle) return;
    panel.hidden = typeof forceOpen === 'boolean' ? !forceOpen : !panel.hidden;
    toggle.textContent = panel.hidden ? '＋ 添加Ubuntu设备来源' : '收起Ubuntu设备来源';
    if (!panel.hidden) {
        document.getElementById('adb-proxy-ubuntu-host')?.focus();
    }
}

async function deployAdbProxyUbuntuSource() {
    const hostInput = document.getElementById('adb-proxy-ubuntu-host');
    const passwordInput = document.getElementById('adb-proxy-ubuntu-password');
    const button = document.getElementById('adb-proxy-ubuntu-deploy');
    const message = document.getElementById('adb-proxy-message');
    const sshHost = hostInput?.value.trim() || '';
    const password = passwordInput?.value || '';
    if (!/^[A-Za-z0-9._-]+@.+/.test(sshHost)) {
        showToast('SSH主机必须使用 用户名@IP 格式', 'warning');
        return;
    }
    if (!password) {
        showToast('请输入SSH密码', 'warning');
        return;
    }
    if (!button || !message) return;

    let finalMessage = '';
    adbProxyOperationRunning = true;
    button.disabled = true;
    button.textContent = '校验SSH指纹…';
    message.textContent = `正在读取 ${sshHost} 的SSH主机指纹…`;
    try {
        const scan = await apiCall(
            '/api/cluster/workers/ssh-host-key/scan',
            'POST',
            {ssh_host: sshHost}
        );
        const fingerprints = (scan.keys || []).map(
            key => `${key.key_type}  ${key.fingerprint}`
        ).join('\n');
        if (!fingerprints) throw new Error('目标主机没有返回可校验的SSH指纹');
        if (!await showConfirmDialog(
            '确认 SSH 主机指纹',
            `请核对 ${scan.host}:${scan.port} 的SSH指纹：\n\n`
            + `${fingerprints}\n\n确认无误后继续安装。`
        )) {
            throw new Error('已取消Ubuntu来源主机安装');
        }
        await apiCall(
            '/api/cluster/workers/ssh-host-key/trust',
            'POST',
            {ssh_host: sshHost, keys: scan.keys}
        );
        button.textContent = '安装adbproxy-rs…';
        message.textContent = `正在 ${sshHost} 安装adbproxy-rs和来源Agent…`;
        const deployed = await apiCall(
            '/api/cluster/workers/deploy-adb-proxy-source',
            'POST',
            {
                ssh_host: sshHost,
                password,
                controller_url: window.location.origin
            }
        );
        if (passwordInput) passwordInput.value = '';
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        const sourceSelect = document.getElementById('adb-proxy-source-host');
        if (
            sourceSelect
            && Array.from(sourceSelect.options).some(
                option => option.value === deployed.worker_id
            )
        ) {
            sourceSelect.value = deployed.worker_id;
            renderAdbProxySourceDevices();
        }
        toggleAdbProxyUbuntuSource(false);
        finalMessage = (
            `${sshHost} 已安装并添加为ADB设备来源`
            + (deployed.registered ? '，设备清单已同步。' : '。')
        );
        showToast('Ubuntu ADB设备来源添加成功', 'success');
    } catch (error) {
        finalMessage = `添加Ubuntu来源失败：${error.message}`;
        showToast('添加Ubuntu来源失败: ' + error.message, 'error');
        addLogEntry('添加Ubuntu ADB来源失败: ' + error.message, 'error');
    } finally {
        if (passwordInput) passwordInput.value = '';
        adbProxyOperationRunning = false;
        button.disabled = false;
        button.textContent = '安装并添加';
        updateAdbProxyButton();
        renderAdbProxyAssignments();
        renderAdbProxySourceDevices();
        if (finalMessage) message.textContent = finalMessage;
    }
}

function renderAdbProxyHosts(selection = null) {
    const sourceSelect = document.getElementById('adb-proxy-source-host');
    const targetSelect = document.getElementById('adb-proxy-target-host');
    const message = document.getElementById('adb-proxy-message');
    if (!sourceSelect || !targetSelect || !adbProxyStatus) return;
    const previousSource = selection?.sourceWorkerId || sourceSelect.value;
    const hosts = adbProxyStatus.hosts || [];
    const activeAssignments = adbProxyStatus.assignments || [];
    const activeTargets = new Set(
        activeAssignments.map(item => item.target_worker_id)
    );
    const sourceCapable = hosts.filter(host => (
        host.adb_proxy && ['online', 'busy'].includes(host.status)
    ));
    const localWorkerId = adbProxyStatus.local_worker_id || workspaceLocalWorkerId();
    // Keep an online source selectable while its last device is unplugged, so
    // the open modal can show the device again as soon as a heartbeat reports
    // the hotplug event. Targets that currently aggregate a source remain
    // excluded from becoming sources themselves.
    const sourceHosts = sourceCapable.filter(host => (
        !activeTargets.has(host.worker_id)
        && host.worker_id !== localWorkerId
    ));
    sourceSelect.replaceChildren();
    sourceHosts.forEach(host => sourceSelect.append(
        new Option(adbProxyHostLabel(host), host.worker_id)
    ));
    if (!sourceHosts.length) {
        sourceSelect.append(new Option('没有可用的ADB设备来源', ''));
    } else if (sourceHosts.some(host => host.worker_id === previousSource)) {
        sourceSelect.value = previousSource;
    }

    renderAdbProxySourceDevices(selection);
    if (!adbProxyStatus.cluster_enabled && sourceCapable.length < 2) {
        message.textContent = (
            '单机模式下本机ADB设备已直接可用，无需再次接入。若设备连接在另一台Ubuntu主机，'
            + '请部署Worker并启用集群模式。'
        );
    }
}

function adbProxyTargetUnavailableReason(host, activeSources) {
    if (!host.adb_proxy) return 'ADB Proxy未安装或版本不兼容';
    if (host.adb_proxy_source_only) return '仅可作为设备来源';
    if (activeSources.has(host.worker_id)) return '正在作为设备来源';
    if (['busy', 'draining'].includes(host.status)) return '测试中，不可用';
    if (host.status !== 'online') return '离线，不可用';
    return '';
}

function renderAdbProxySourceDevices(selection = null) {
    const sourceId = document.getElementById('adb-proxy-source-host')?.value || '';
    const targetSelect = document.getElementById('adb-proxy-target-host');
    const deviceSelect = document.getElementById('adb-proxy-source-devices');
    const submit = document.getElementById('adb-proxy-connect-submit');
    const message = document.getElementById('adb-proxy-message');
    if (!targetSelect || !deviceSelect || !submit || !message) return;
    const assignments = adbProxyStatus?.assignments || [];
    const existingAssignment = assignments.find(
        item => item.source_worker_id === sourceId
    );
    const activeSources = new Set(
        assignments.map(item => item.source_worker_id)
    );
    const hosts = adbProxyStatus?.hosts || [];
    const host = hosts.find(item => item.worker_id === sourceId);
    const previousTarget = selection?.targetWorkerId || targetSelect.value;
    const targetCandidates = existingAssignment
        ? hosts.filter(item => item.worker_id === existingAssignment.target_worker_id)
        : hosts.filter(item => item.worker_id !== sourceId);
    const targetOptions = targetCandidates.map(item => ({
        host: item,
        reason: adbProxyTargetUnavailableReason(item, activeSources)
    }));
    const targetHosts = targetOptions.filter(item => !item.reason).map(item => item.host);
    const unavailableTargets = targetOptions.filter(item => item.reason);
    targetSelect.replaceChildren();
    targetHosts.forEach(item => targetSelect.append(
        new Option(adbProxyHostLabel(item), item.worker_id)
    ));
    if (!targetHosts.length) {
        const placeholder = new Option('没有可用的ADB接入主机', '', true, true);
        placeholder.disabled = true;
        targetSelect.append(placeholder);
    } else {
        const preferred = existingAssignment?.target_worker_id
            || (targetHosts.some(item => item.worker_id === previousTarget)
                ? previousTarget
                : workspaceWorkerId());
        if (targetHosts.some(item => item.worker_id === preferred)) {
            targetSelect.value = preferred;
        }
    }
    unavailableTargets.forEach(({host: item, reason}) => {
        const option = new Option(
            `${adbProxyHostLabel(item)}（${reason}）`,
            item.worker_id
        );
        option.disabled = true;
        option.title = reason;
        targetSelect.append(option);
    });
    deviceSelect.replaceChildren();
    const assigned = new Set(existingAssignment?.devices || []);
    const devices = (host?.devices || []).filter(device => (
        device.state === 'available'
        && device.transport !== 'adb_proxy'
        && !assigned.has(device.serial)
    ));
    devices.forEach(device => {
        const detail = [device.model, device.transport].filter(Boolean).join(' · ');
        const option = new Option(
            `${device.serial}${detail ? ` · ${detail}` : ''}`,
            device.serial
        );
        option.selected = selection?.knownDeviceSerials?.has(device.serial)
            ? selection.selectedDeviceSerials.has(device.serial)
            : true;
        deviceSelect.append(option);
    });
    if (!devices.length) {
        deviceSelect.append(new Option('该来源没有可接入的ADB设备', ''));
    }
    deviceSelect.disabled = !devices.length;
    submit.disabled = (
        adbProxyOperationRunning || !sourceId || !targetSelect.value || !devices.length
    );
    if (adbProxyOperationRunning) {
        message.textContent = '正在更新ADB接入，请稍候...';
    } else if (sourceId && existingAssignment && devices.length && targetHosts.length) {
        message.textContent = (
            `该来源还有 ${devices.length} 台ADB设备可追加接入 `
            + `${existingAssignment.target_worker_id}。`
        );
    } else if (sourceId && devices.length && targetHosts.length) {
        message.textContent = `请选择要接入的ADB设备，共 ${devices.length} 台可用。`;
    } else if (
        sourceId && devices.length
        && unavailableTargets.some(item => item.reason === '测试中，不可用')
    ) {
        const busyHosts = unavailableTargets
            .filter(item => item.reason === '测试中，不可用')
            .map(item => adbProxyHostLabel(item.host))
            .join('、');
        message.textContent = `${busyHosts} 正在执行测试，暂不能作为ADB接入主机。`;
    } else if (sourceId && devices.length && unavailableTargets.length) {
        message.textContent = '接入主机当前不可用，请在下拉框中查看原因。';
    } else if (assignments.length) {
        message.textContent = '当前没有剩余可接入的ADB设备；已有接入可在上方查看或断开。';
    } else if (!sourceId) {
        message.textContent = '没有可用的ADB设备来源。';
    } else if (!targetHosts.length) {
        message.textContent = '没有可用于接入该来源设备的目标主机。';
    } else {
        message.textContent = '该来源当前没有可接入的ADB设备。';
    }
}

async function refreshAdbProxyAssignments() {
    const container = document.getElementById('adb-proxy-assignments');
    const hadRenderedAssignments = container?.dataset.loaded === 'true';
    if (container) {
        container.setAttribute('aria-busy', 'true');
        if (!hadRenderedAssignments) container.textContent = '正在刷新接入状态...';
    }
    try {
        const selection = adbProxySelectionSnapshot();
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        renderAdbProxyAssignments();
        renderAdbProxyHosts(selection);
    } catch (error) {
        if (container && !hadRenderedAssignments) container.textContent = `刷新失败: ${error.message}`;
        else showToast(`ADB接入状态刷新失败: ${error.message}`, 'error');
    } finally {
        if (container) container.setAttribute('aria-busy', 'false');
    }
}

function renderAdbProxyAssignments() {
    const container = document.getElementById('adb-proxy-assignments');
    if (!container) return;
    container.replaceChildren();
    const assignments = adbProxyStatus?.assignments || [];
    if (!assignments.length) {
        container.textContent = '当前没有通过adbproxy-rs接入的设备。';
        container.dataset.loaded = 'true';
        return;
    }
    assignments.forEach(assignment => {
        const row = document.createElement('div');
        row.className = 'adb-proxy-assignment';
        if (['connected', 'connecting', 'connect_failed', 'disconnect_failed', 'host_offline',
            'recovering', 'degraded_source', 'degraded_target', 'device_missing'].includes(assignment.status)) {
            row.classList.add(`routing-status-${assignment.status}`);
        }
        const info = document.createElement('div');
        info.className = 'adb-proxy-assignment-info';
        const statusLabels = {
            connected: '已接入',
            connecting: '正在接入',
            connect_failed: '接入失败',
            disconnect_failed: '断开失败，需重试',
            host_offline: '主机离线',
            recovering: '正在核对',
            degraded_source: '来源代理异常',
            degraded_target: '目标Hub异常',
            device_missing: '目标设备缺失',
        };
        const status = statusLabels[assignment.status] || '';
        info.textContent = (
            `${assignment.source_worker_id} → ${assignment.target_worker_id}`
            + `｜设备：${(assignment.devices || []).join(', ') || '无'}`
            + (status ? `｜${status}` : '')
        );
        const actions = document.createElement('div');
        actions.className = 'device-routing-actions';
        const canInspectFailure = [
            'connect_failed',
            'disconnect_failed',
            'host_offline',
            'degraded_source',
            'degraded_target',
            'device_missing',
        ].includes(assignment.status);
        if (canInspectFailure) {
            const inspectFailure = document.createElement('button');
            inspectFailure.type = 'button';
            inspectFailure.className = 'btn-xxs';
            inspectFailure.textContent = '查看原因';
            inspectFailure.addEventListener('click', () => showAdbProxyDiagnostics(assignment));
            actions.append(inspectFailure);
        }
        const disconnect = document.createElement('button');
        disconnect.type = 'button';
        disconnect.className = 'btn-xxs btn-danger';
        disconnect.textContent = '断开';
        disconnect.disabled = adbProxyOperationRunning;
        disconnect.addEventListener('click', () => disconnectAdbProxyAssignment(
            assignment.source_worker_id,
            assignment.target_worker_id
        ));
        actions.append(disconnect);
        row.append(info, actions);
        container.append(row);
    });
    container.dataset.loaded = 'true';
}

async function showAdbProxyDiagnostics(assignment) {
    const {modal, modalId} = createAnalysisModal(
        'adb-proxy-diagnostics',
        'ADB Proxy 诊断',
        '正在读取双端状态和最近日志...'
    );
    try {
        const workers = Array.from(new Set([
            assignment.source_worker_id,
            assignment.target_worker_id,
        ].filter(Boolean)));
        const logs = await Promise.all(workers.map(workerId => apiCall(
            '/api/adb-forward/logs?worker_id=' + encodeURIComponent(workerId),
            'GET'
        )));
        const body = modal.querySelector('.modal-body');
        body.replaceChildren();
        const status = document.createElement('pre');
        status.className = 'transport-diagnostics-output';
        status.textContent = JSON.stringify({
            status: assignment.status,
            generation: assignment.generation || 0,
            health: assignment.health || {},
        }, null, 2);
        body.append(status);
        logs.forEach(item => {
            const heading = document.createElement('h4');
            heading.textContent = item.worker_id;
            const output = document.createElement('pre');
            output.className = 'transport-diagnostics-output';
            output.textContent = [
                ...(item.notice ? [`说明：${item.notice}`] : []),
                '--- proxy.log ---', ...(item.proxy || []),
                '--- hub.log ---', ...(item.hub || []),
            ].join('\n');
            body.append(heading, output);
        });
    } catch (error) {
        showModalError(modal, error.message);
    }
    ModalManager.onClose(modalId, () => modal.remove());
}

async function submitAdbProxyConnect() {
    const sourceWorkerId = document.getElementById('adb-proxy-source-host')?.value || '';
    const targetWorkerId = document.getElementById('adb-proxy-target-host')?.value || '';
    const devices = Array.from(
        document.getElementById('adb-proxy-source-devices')?.selectedOptions || []
    ).map(option => option.value).filter(Boolean);
    if (!sourceWorkerId || !targetWorkerId || !devices.length) {
        showToast('请选择设备来源、接入主机和至少一台ADB设备', 'warning');
        return;
    }
    await runAdbProxyOperation(async () => {
        const result = await apiCall('/api/adb-forward/start', 'POST', {
            source_worker_id: sourceWorkerId,
            target_worker_id: targetWorkerId,
            devices
        });
        addLogEntry(result.message || 'ADB设备接入完成', 'success');
        return result;
    });
}

async function disconnectAdbProxyAssignment(sourceWorkerId, targetWorkerId) {
    await runAdbProxyOperation(async () => {
        const result = await apiCall('/api/adb-forward/stop', 'POST', {
            source_worker_id: sourceWorkerId,
            target_worker_id: targetWorkerId
        });
        addLogEntry(result.message || 'ADB设备接入已断开', 'success');
        return result;
    });
}

async function refreshAdbProxyTargetDevices(result) {
    const targetWorkerId = result?.assignment?.target_worker_id
        || result?.target_worker_id
        || '';
    if (!targetWorkerId || targetWorkerId !== workspaceWorkerId()) return;
    try {
        await loadDevices(true, {silent: true});
        addLogEntry(`已自动刷新 ${targetWorkerId} 的ADB设备列表`, 'info');
    } catch (error) {
        addLogEntry(`ADB接入已更新，但设备列表自动刷新失败: ${error.message}`, 'warning');
    }
}

async function runAdbProxyOperation(operation) {
    const message = document.getElementById('adb-proxy-message');
    let operationError = '';
    adbProxyOperationRunning = true;
    updateAdbProxyButton();
    renderAdbProxyAssignments();
    renderAdbProxySourceDevices();
    if (message) message.textContent = '正在更新ADB接入，请稍候...';
    try {
        const result = await operation();
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        state.adbForwardRunning = Boolean(adbProxyStatus.connected);
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        await refreshAdbProxyTargetDevices(result);
    } catch (error) {
        operationError = `ADB接入操作失败：${error.message}`;
        addLogEntry('ADB接入操作失败: ' + error.message, 'error');
        showToast('ADB接入操作失败: ' + error.message, 'error');
    } finally {
        adbProxyOperationRunning = false;
        updateAdbProxyButton();
        renderAdbProxyAssignments();
        renderAdbProxyHosts();
        if (operationError && message) message.textContent = operationError;
    }
}

function updateAdbProxyButton() {
    const button = document.getElementById('adb-forward-btn');
    if (!button) return;
    button.disabled = adbProxyOperationRunning;
    button.textContent = state.adbForwardRunning
        ? '🔌 管理ADB'
        : '🔌 ADB接入';
}

async function setupUsbipForward() {
    const btn = $('usbip-btn');
    if (!btn) return;

    if (btn.disabled) return;
    debugLog('[setupUsbipForward] Called, state.usbipConnected =', state.usbipConnected);
    const granted = await requestElevatedAccess('管理USB/IP设备接入');
    if (!granted) return;
    await openUsbipAttachModal();
}

function usbipSelectionSerials(group, busid) {
    const mapped = group?.device_serials_by_busid?.[busid];
    const values = Array.isArray(mapped) ? mapped : (group?.device_serials || []);
    return Array.from(new Set(values.map(value => String(value || '').trim()).filter(Boolean)));
}

function usbipAssignmentLabel(selection, busid) {
    const serials = usbipSelectionSerials(selection, busid);
    const rawStatus = selection?.statuses_by_busid?.[busid]
        || selection?.status
        || 'attached';
    const statusLabels = {
        attaching: '正在接入',
        attached: '已接入',
        unknown: '状态待确认',
        cleanup_required: '需断开清理',
        detaching: '正在断开',
    };
    return (
        `${selection.device_host} → ${selection.worker_id || 'Controller'} · ${busid}`
        + `｜设备：${serials.join('、') || '尚未识别'}`
        + `｜${statusLabels[rawStatus] || rawStatus}`
    );
}

function usbipAssignmentOperationKey(selection) {
    const host = String(selection?.device_host || '');
    const worker = String(selection?.worker_id || workspaceLocalWorkerId());
    const busids = (selection?.busids || [])
        .map(value => String(value || '').trim())
        .filter(Boolean)
        .sort()
        .join(',') || '*';
    return `${host}|${worker}|${busids}`;
}

function updateUsbipAssignmentOperationButtons() {
    document.querySelectorAll('[data-usbip-operation-key]').forEach(button => {
        const pending = usbipPendingAssignmentKeys.has(
            button.dataset.usbipOperationKey
        );
        const detaching = button.dataset.usbipDetaching === 'true';
        button.disabled = usbipRoutingOperationRunning || pending || detaching;
        button.textContent = pending || detaching
            ? '断开中...'
            : button.dataset.usbipIdleLabel || '断开';
    });
}

async function refreshUsbipAssignments() {
    await loadUsbipAssignments();
}

async function loadUsbipAssignments() {
    const container = document.getElementById('usbip-assignments');
    if (!container) return;
    const hadRenderedAssignments = container.dataset.loaded === 'true';
    container.setAttribute('aria-busy', 'true');
    if (!hadRenderedAssignments) container.textContent = '正在读取接入状态...';
    try {
        const statusPath = pendingUsbipDeviceHost
            ? '/api/usbip/status?device_host=' + encodeURIComponent(pendingUsbipDeviceHost)
            : '/api/usbip/status';
        const status = await apiCall(statusPath, 'GET');
        const selections = status.cluster_selections || [];
        const statusSource = status.device_host || pendingUsbipDeviceHost || '';
        if (statusSource) usbipAssignedBusidsBySource.set(statusSource, new Set());
        const rows = [];
        selections.forEach(group => {
            const assignedBusids = usbipAssignedBusidsBySource.get(group.device_host)
                || new Set();
            (group.busids || []).forEach(busid => {
                assignedBusids.add(busid);
                const deviceSerials = usbipSelectionSerials(group, busid);
                rows.push({
                    ...group,
                    busids: [busid],
                    device_serials: deviceSerials,
                });
            });
            usbipAssignedBusidsBySource.set(group.device_host, assignedBusids);
        });
        if (!rows.length && activeUsbipSelection?.busids?.length) {
            activeUsbipSelection.busids.forEach(busid => {
                rows.push({...activeUsbipSelection, busids: [busid]});
            });
        }
        container.replaceChildren();
        rows.forEach(selection => {
            const busid = selection.busids[0];
            const row = document.createElement('div');
            row.className = 'adb-proxy-assignment';
            const assignmentStatus = selection?.statuses_by_busid?.[busid]
                || selection?.status
                || 'attached';
            if (['attaching', 'attached', 'unknown', 'cleanup_required', 'detaching'].includes(assignmentStatus)) {
                row.classList.add(`routing-status-${assignmentStatus}`);
            }
            const info = document.createElement('div');
            info.className = 'adb-proxy-assignment-info';
            info.textContent = usbipAssignmentLabel(selection, busid);
            const actions = document.createElement('div');
            actions.className = 'device-routing-actions';
            if (['unknown', 'cleanup_required'].includes(assignmentStatus)) {
                const inspectFailure = document.createElement('button');
                inspectFailure.type = 'button';
                inspectFailure.className = 'btn-xxs';
                inspectFailure.textContent = '查看原因';
                inspectFailure.addEventListener('click', () => showUsbipDiagnostics(selection));
                actions.append(inspectFailure);
            }
            const disconnect = document.createElement('button');
            disconnect.type = 'button';
            disconnect.className = 'btn-xxs btn-danger';
            const idleLabel = assignmentStatus === 'cleanup_required'
                ? '清理' : assignmentStatus === 'unknown' ? '核对并断开' : '断开';
            disconnect.textContent = idleLabel;
            disconnect.dataset.usbipOperationKey = usbipAssignmentOperationKey(selection);
            disconnect.dataset.usbipIdleLabel = idleLabel;
            disconnect.dataset.usbipDetaching = String(assignmentStatus === 'detaching');
            disconnect.addEventListener('click', async () => {
                await performUsbipDisconnect([selection]);
            });
            actions.append(disconnect);
            row.append(info, actions);
            container.append(row);
        });
        if (!rows.length && status.connected) {
            const legacy = {
                device_host: status.device_host || pendingUsbipDeviceHost,
                worker_id: workspaceLocalWorkerId(),
            };
            const row = document.createElement('div');
            row.className = 'adb-proxy-assignment';
            const info = document.createElement('div');
            info.className = 'adb-proxy-assignment-info';
            info.textContent = `${legacy.device_host}｜历史USB/IP接入（无端口记录）`;
            const disconnect = document.createElement('button');
            disconnect.type = 'button';
            disconnect.className = 'btn-xxs btn-danger';
            disconnect.textContent = '断开';
            disconnect.dataset.usbipOperationKey = usbipAssignmentOperationKey(legacy);
            disconnect.dataset.usbipIdleLabel = '断开';
            disconnect.dataset.usbipDetaching = 'false';
            disconnect.addEventListener('click', async () => {
                await performUsbipDisconnect([legacy]);
            });
            row.append(info, disconnect);
            container.append(row);
        }
        if (!container.children.length) {
            container.textContent = '当前没有通过USB/IP接入的设备。';
        }
        container.dataset.loaded = 'true';
        state.usbipConnected = Boolean(rows.length || status.connected);
        updateUsbipButtonStatus(state.usbipConnected);
        updateUsbipAssignmentOperationButtons();
    } catch (error) {
        if (hadRenderedAssignments) showToast(`USB/IP接入状态刷新失败: ${error.message}`, 'error');
        else container.textContent = `读取USB/IP接入状态失败：${error.message}`;
    } finally {
        container.setAttribute('aria-busy', 'false');
    }
}

async function showUsbipDiagnostics(selection) {
    const {modal, modalId} = createAnalysisModal(
        'usbip-diagnostics',
        'USB/IP 诊断',
        '正在读取传输、协议和网络质量状态...'
    );
    try {
        const status = await apiCall(
            '/api/usbip/status?device_host=' + encodeURIComponent(selection.device_host),
            'GET'
        );
        const body = modal.querySelector('.modal-body');
        body.replaceChildren();
        const output = document.createElement('pre');
        output.className = 'transport-diagnostics-output';
        output.textContent = JSON.stringify(status, null, 2);
        const download = document.createElement('button');
        download.type = 'button';
        download.className = 'btn-xxs btn-primary';
        download.textContent = '导出诊断 JSON';
        download.addEventListener('click', () => {
            const blob = new Blob([JSON.stringify(status, null, 2)], {
                type: 'application/json'
            });
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = `usbip-diagnostics-${Date.now()}.json`;
            anchor.click();
            URL.revokeObjectURL(url);
        });
        body.append(download, output);
    } catch (error) {
        showModalError(modal, error.message);
    }
    ModalManager.onClose(modalId, () => modal.remove());
}

async function performUsbipDisconnect(selections) {
    const operationKeys = Array.from(new Set(
        (selections || []).map(usbipAssignmentOperationKey)
    ));
    if (!operationKeys.length) return;
    if (
        usbipRoutingOperationRunning
        || operationKeys.some(key => usbipPendingAssignmentKeys.has(key))
    ) {
        showToast('USB/IP操作正在进行，请等待完成', 'warning');
        return;
    }
    const btn = $('usbip-btn');
    const operationGeneration = ++usbipOperationGeneration;
    usbipRoutingOperationRunning = true;
    operationKeys.forEach(key => usbipPendingAssignmentKeys.add(key));
    updateUsbipAssignmentOperationButtons();
    try {
        btn.textContent = '📱 断开中...';
        btn.disabled = true;
        usbipManualDisconnectUntil = Date.now() + USBIP_MANUAL_DISCONNECT_SUPPRESS_MS;
        if (usbipReconnectTimer) {
            clearTimeout(usbipReconnectTimer);
            usbipReconnectTimer = null;
        }
        const workerBaselines = new Map();
        const expectedUsbipSerials = new Map();
        selections.forEach(selection => {
            const workerId = selection.worker_id || workspaceLocalWorkerId();
            if (!workerBaselines.has(workerId)) {
                const serials = new Set(
                    (state.devices || [])
                        .filter(device => (
                            device.worker_id === workerId
                            || String(device.device_id || '').startsWith(`${workerId}:`)
                            || (
                                workerId === workspaceLocalWorkerId()
                                && !device.worker_id
                            )
                        ))
                        .map(device => String(device.serial || device.device_id || '').split(':').pop())
                );
                workerBaselines.set(workerId, serials);
            }
            const expected = expectedUsbipSerials.get(workerId) || new Set();
            (selection.device_serials || []).forEach(serial => {
                if (serial) expected.add(String(serial));
            });
            expectedUsbipSerials.set(workerId, expected);
        });
        for (const selection of selections) {
            const disconnectPayload = {
                device_host: selection.device_host,
                source_host: selection.source_host || '',
                worker_id: selection.worker_id || '',
                busids: selection.busids || [],
            };
            const result = await apiCall(
                '/api/usbip/disconnect',
                'POST',
                disconnectPayload
            );
            addLogEntry(result.message || 'USB/IP设备已断开', 'success');
            const workerId = selection.worker_id || workspaceLocalWorkerId();
            const expected = expectedUsbipSerials.get(workerId) || new Set();
            (result.removed_devices || []).forEach(serial => {
                if (serial) expected.add(String(serial));
            });
            expectedUsbipSerials.set(workerId, expected);
            if (Array.isArray(result.remaining_devices) && result.remaining_devices.length) {
                addLogEntry('断开后仍在线: ' + result.remaining_devices.join(' '), 'warning');
            }
        }
        activeUsbipSelection = null;
        selections.forEach(selection => {
            usbipSourceDeviceCache.delete(selection.device_host);
        });
        await loadUsbipAssignments();
        // Source USB enumeration and the global ADB refresh can take tens of
        // seconds after a detach.  The backend has already confirmed the
        // operation, so release the UI now and finish those reads in the
        // background instead of holding the connect button disabled.
        void refreshUsbipAfterDisconnect(
            workerBaselines,
            expectedUsbipSerials,
            operationGeneration
        );
        setTimeout(() => {
            if (operationGeneration === usbipOperationGeneration) {
                checkUsbipStatus();
            }
        }, 500);
    } catch (error) {
        btn.textContent = '📱 断开设备';
        btn.disabled = false;
        addLogEntry('停止 USB/IP 失败: ' + error.message, 'error');
    } finally {
        operationKeys.forEach(key => usbipPendingAssignmentKeys.delete(key));
        usbipRoutingOperationRunning = false;
        if (btn) btn.disabled = false;
        updateUsbipAssignmentOperationButtons();
    }
}

async function refreshUsbipAfterDisconnect(
    workerBaselines,
    expectedUsbipSerials,
    operationGeneration
) {
    await Promise.allSettled([
        loadUsbipSourceDevices(true, {
            silent: true,
            preserveSelection: true,
        }),
        loadDevices(true, {silent: true}),
    ]);
    if (operationGeneration !== usbipOperationGeneration) return;
    await refreshUsbipDetachedWorkers(
        workerBaselines,
        expectedUsbipSerials,
        operationGeneration
    );
}

async function refreshUsbipDetachedWorkers(
    workerBaselines,
    expectedUsbipSerials = new Map(),
    operationGeneration = usbipOperationGeneration
) {
    // Compatibility with older cached callers that passed generation second.
    if (typeof expectedUsbipSerials === 'number') {
        operationGeneration = expectedUsbipSerials;
        expectedUsbipSerials = new Map();
    }
    for (const delay of [2000, 3000, 5000, 8000, 12000, 15000]) {
        await new Promise(resolve => setTimeout(resolve, delay));
        if (operationGeneration !== usbipOperationGeneration) return;
        let changed = false;
        for (const [workerId, baseline] of workerBaselines.entries()) {
            try {
                const devices = await fetchDevicesForWorker(workerId, true);
                const visible = new Set(
                    (devices || []).map(device => String(device.serial || device.device_id || '').split(':').pop())
                );
                const expected = expectedUsbipSerials.get(workerId) || new Set();
                const usbipVisible = new Set(
                    (devices || [])
                        .filter(device => (
                            device.transport === 'usbip'
                            || device.is_usbip === true
                            || device.properties?.is_usbip === true
                        ))
                        .map(device => String(device.serial || device.device_id || '').split(':').pop())
                );
                if (
                    (expected.size && ![...expected].some(serial => usbipVisible.has(serial)))
                    || (
                        !expected.size
                        && baseline.size
                        && [...baseline].some(serial => !visible.has(serial))
                    )
                ) {
                    const stillOnlineElsewhere = [...expected].filter(serial => (
                        visible.has(serial) && !usbipVisible.has(serial)
                    ));
                    if (stillOnlineElsewhere.length) {
                        addLogEntry(
                            'USB/IP已断开；同序列号设备仍通过其他ADB传输在线: '
                            + stillOnlineElsewhere.join(' '),
                            'warning'
                        );
                    }
                    changed = true;
                    break;
                }
            } catch (error) {
                debugLog('[USB/IP] Detached Worker refresh failed:', error.message);
            }
        }
        if (operationGeneration !== usbipOperationGeneration) return;
        if (changed) {
            await loadDevices(true);
            if (operationGeneration !== usbipOperationGeneration) return;
            addLogEntry('已自动刷新设备列表，USB/IP设备已从ADB移除', 'success');
            return;
        }
    }
    if (operationGeneration !== usbipOperationGeneration) return;
    await loadDevices(true);
    if (operationGeneration !== usbipOperationGeneration) return;
    addLogEntry('USB/IP已断开，但ADB设备状态更新较慢，已完成最终刷新', 'warning');
}

async function openUsbipAttachModal() {
    const sourceSelect = document.getElementById('usbip-source-host');
    const targetSelect = document.getElementById('usbip-target-worker');
    const message = document.getElementById('usbip-attach-message');
    const submit = document.getElementById('usbip-attach-submit');
    if (!sourceSelect || !targetSelect) return;

    const config = state.config || {};
    const sources = new Set();
    const isLoopbackSource = value => {
        const rawHost = String(value || '').split('@').pop().replace(/^\[|\]$/g, '');
        if (rawHost.toLowerCase() === '::1') return true;
        const host = rawHost.split(':')[0];
        return ['127.0.0.1', 'localhost', '::1'].includes(host.toLowerCase());
    };
    [config.usbip_device_host, config.device_host, pendingUsbipDeviceHost]
        .filter(value => value && String(value).includes('@') && !isLoopbackSource(value))
        .forEach(value => sources.add(String(value)));
    Object.entries(config.client_hosts || {}).forEach(([host, username]) => {
        if (host && username && !isLoopbackSource(`${username}@${host}`)) {
            sources.add(`${username}@${host}`);
        }
    });
    sourceSelect.innerHTML = '';
    if (!sources.size) {
        sourceSelect.append(new Option('未配置设备来源', ''));
    } else {
        sources.forEach(value => sourceSelect.append(new Option(value, value)));
    }

    const localWorkerId = workspaceLocalWorkerId();
    targetSelect.innerHTML = '';
    targetSelect.append(new Option(localWorkerId, localWorkerId));
    try {
        const response = await fetch('/api/cluster/hosts', {
            credentials: 'same-origin',
            cache: 'no-store'
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        (payload.hosts || []).forEach(host => {
            if (!host.worker_id || host.worker_id === localWorkerId) return;
            const option = new Option(host.worker_id, host.worker_id);
            const online = ['online', 'busy'].includes(host.status);
            const usbipCapable = host.capabilities?.usbip_client === true;
            option.disabled = !online || !usbipCapable;
            if (!online) option.textContent += '（离线）';
            else if (!usbipCapable) option.textContent += '（需重新部署以启用USB/IP）';
            targetSelect.append(option);
        });
    } catch (error) {
        debugLog('[USB/IP] Failed to load cluster hosts:', error.message);
        if (message) message.textContent = `加载集群主机失败：${error.message}；仍可接入 Controller。`;
    }
    const preferredWorker = workspaceWorkerId();
    targetSelect.value = Array.from(targetSelect.options)
        .some(option => option.value === preferredWorker && !option.disabled)
        ? preferredWorker : localWorkerId;
    if (submit) submit.disabled = !sourceSelect.value;
    ModalManager.open('usbip-attach-modal');
    await loadUsbipAssignments();
    await loadUsbipSourceDevices();
    // Source enumeration also repairs older assignments that were persisted
    // before ADB exposed their serial. Refresh the current rows afterwards.
    await loadUsbipAssignments();
    startUsbipSourceRefresh();
}

function closeUsbipAttachModal() {
    stopUsbipSourceRefresh();
    ModalManager.close('usbip-attach-modal');
}

function stopUsbipSourceRefresh() {
    if (usbipSourceRefreshTimer) clearInterval(usbipSourceRefreshTimer);
    usbipSourceRefreshTimer = null;
    usbipSourceRefreshRunning = false;
}

function startUsbipSourceRefresh() {
    stopUsbipSourceRefresh();
    const refresh = async () => {
        if (
            !ModalManager.isOpen('usbip-attach-modal')
            || usbipSourceRefreshRunning
            || usbipRoutingOperationRunning
        ) return;
        usbipSourceRefreshRunning = true;
        try {
            await loadUsbipSourceDevices(true, {
                silent: true,
                preserveSelection: true,
            });
        } catch (error) {
            debugLog('[USB/IP] automatic source refresh failed:', error.message);
        } finally {
            usbipSourceRefreshRunning = false;
        }
    };
    usbipSourceRefreshTimer = setInterval(
        () => void refresh(),
        DEVICE_ROUTING_REFRESH_INTERVAL_MS
    );
    ModalManager.onClose('usbip-attach-modal', stopUsbipSourceRefresh);
}

async function submitUsbipAttach() {
    if (usbipRoutingOperationRunning) {
        showToast('USB/IP操作正在进行，请等待完成', 'warning');
        return;
    }
    const deviceHost = document.getElementById('usbip-source-host')?.value || '';
    const workerId = document.getElementById('usbip-target-worker')?.value || '';
    const busids = Array.from(
        document.getElementById('usbip-source-device')?.selectedOptions || []
    ).map(option => option.value).filter(Boolean);
    if (!deviceHost) {
        showToast('请先配置设备来源', 'warning');
        return;
    }
    if (!workerId) {
        showToast('请选择接入主机', 'warning');
        return;
    }
    if (!busids.length) {
        showToast('请至少选择一个USB设备', 'warning');
        return;
    }
    const submit = document.getElementById('usbip-attach-submit');
    const message = document.getElementById('usbip-attach-message');
    if (submit) submit.disabled = true;
    if (message) message.textContent = '正在接入USB/IP设备，请稍候...';
    try {
        await connectUsbipDeviceHost(deviceHost, workerId, busids);
    } finally {
        const sourceDevice = document.getElementById('usbip-source-device');
        if (submit) {
            submit.disabled = (
                !sourceDevice
                || sourceDevice.disabled
                || !sourceDevice.value
            );
        }
    }
}

async function loadUsbipSourceDevices(force = false, options = {}) {
    const source = document.getElementById('usbip-source-host')?.value || '';
    const select = document.getElementById('usbip-source-device');
    const message = document.getElementById('usbip-attach-message');
    if (!select) return;
    const knownBusids = new Set(
        Array.from(select.options || []).map(option => option.value).filter(Boolean)
    );
    const selectedBusids = new Set(
        Array.from(select.selectedOptions || []).map(option => option.value).filter(Boolean)
    );
    if (!options.silent) {
        select.disabled = true;
        select.innerHTML = '<option value="">正在读取USB设备...</option>';
    }
    if (!source) return;
    const cached = usbipSourceDeviceCache.get(source);
    if (!force && cached && Date.now() - cached.timestamp < 5000) {
        renderUsbipSourceDevices(source, cached.devices, {
            ...options,
            knownBusids,
            selectedBusids,
        });
        return;
    }
    if (usbipSourceLoadPromise?.source === source) {
        await usbipSourceLoadPromise.promise;
        return;
    }
    const request = apiCall(
        '/api/usbip/source-devices?device_host=' + encodeURIComponent(source),
        'GET'
    );
    usbipSourceLoadPromise = {source, promise: request};
    try {
        const result = await request;
        const devices = result.devices || [];
        usbipSourceDeviceCache.set(source, {timestamp: Date.now(), devices});
        renderUsbipSourceDevices(source, devices, {
            ...options,
            knownBusids,
            selectedBusids,
        });
    } catch (error) {
        if (options.silent) {
            debugLog('[USB/IP] source device polling failed:', error.message);
        } else {
            select.innerHTML = '<option value="">USB设备加载失败</option>';
            if (message) message.textContent = `USB设备加载失败：${error.message}`;
        }
        if (!options.silent && (error.needPassword || error.need_password)) {
            showDevicePasswordModal(source, 'usbip-list', loadUsbipSourceDevices);
        }
    } finally {
        if (usbipSourceLoadPromise?.promise === request) {
            usbipSourceLoadPromise = null;
        }
    }
}

function renderUsbipSourceDevices(source, devices, options = {}) {
    if (document.getElementById('usbip-source-host')?.value !== source) return;
    const select = document.getElementById('usbip-source-device');
    const message = document.getElementById('usbip-attach-message');
    if (!select) return;
    select.innerHTML = '';
    const assignedBusids = usbipAssignedBusidsBySource.get(source) || new Set();
    const availableDevices = devices.filter(device => !assignedBusids.has(device.busid));
    availableDevices.forEach(device => {
        const option = new Option(device.label || device.busid, device.busid);
        option.selected = options.preserveSelection && options.knownBusids?.has(device.busid)
            ? options.selectedBusids.has(device.busid)
            : true;
        select.append(option);
    });
    if (!availableDevices.length) {
        select.append(new Option(
            devices.length ? '该来源设备均已接入' : '未发现Android USB设备',
            ''
        ));
    }
    select.disabled = !availableDevices.length;
    const submit = document.getElementById('usbip-attach-submit');
    if (submit) {
        submit.disabled = (
            usbipRoutingOperationRunning
            || !availableDevices.length
            || !select.value
        );
    }
    if (message) message.textContent = availableDevices.length
        ? `发现 ${availableDevices.length} 个可接入 USB 设备。多选时，Windows/Linux 按住 Ctrl，macOS 按住 Command。`
        : devices.length
        ? '该来源当前没有剩余可接入的Android USB设备。'
        : '设备源未发现可接入的Android USB设备。';
}

async function connectUsbipDeviceHost(deviceHost, workerId, busids) {
    if (usbipRoutingOperationRunning) {
        showToast('USB/IP操作正在进行，请等待完成', 'warning');
        return;
    }
    const btn = $('usbip-btn');
    const operationGeneration = ++usbipOperationGeneration;
    usbipRoutingOperationRunning = true;
    updateUsbipAssignmentOperationButtons();
    activeUsbipSelection = {device_host: deviceHost, worker_id: workerId, busids};
    debugLog('[USB/IP] Connecting source:', deviceHost);
    try {
        btn.textContent = '📱 连接中...';
        btn.disabled = true;
        usbipManualDisconnectUntil = 0;
        let targetSerialsBefore = new Set();
        try {
            const devicesBefore = await fetchDevicesForWorker(workerId, true);
            targetSerialsBefore = new Set(
                (devicesBefore || []).map(device => String(device.serial || device.device_id || ''))
            );
        } catch (error) {
            debugLog('[USB/IP] Failed to capture target device baseline:', error.message);
        }
        const result = await apiCall('/api/usbip/connect', 'POST', {
            device_host: deviceHost,
            worker_id: workerId,
            busids,
            manual_connect: true
        });
        if (isUsbipAdbReady(result)) {
            state.usbipConnected = true;
            pendingUsbipDeviceHost = result.device_host || deviceHost;
            activeUsbipSelection.source_host = result.source_host || '';
            activeUsbipSelection.device_serials = (
                result.device_serials || result.new_devices || result.device_list || []
            );
            btn.textContent = '📱 断开设备';
            btn.disabled = false;
            addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
            if (['warning', 'poor'].includes(result.network_quality?.rating)) {
                addLogEntry(
                    `USB/IP网络质量${result.network_quality.rating === 'poor' ? '较差' : '一般'}：`
                    + `RTT ${result.network_quality.average_rtt_ms ?? '-'}ms，`
                    + `丢包 ${result.network_quality.loss_percent ?? '-'}%；`
                    + '完整CTS或大流量操作建议改在来源Worker本地执行',
                    'warning'
                );
            }
            usbipSourceDeviceCache.delete(deviceHost);
            await loadUsbipAssignments();
            await loadUsbipSourceDevices(true);
            await loadUsbipAssignments();
            refreshUsbipTargetWorker(
                workerId,
                result.device_serials || result.new_devices || [],
                targetSerialsBefore,
                operationGeneration
            );
            return;
        }
        btn.textContent = '📱 本地设备';
        btn.disabled = false;
        if (result.need_password && result.device_host) {
            showDevicePasswordModal(result.device_host);
            addLogEntry('需要输入SSH密码以连接到 ' + result.device_host, 'warning');
        } else if (result.error && result.error.includes('SSH连接失败')) {
            addLogEntry('⚠️ SSH 连接失败，请点击 "📡 检查SSHD" 按钮检查SSH服务状态', 'warning');
        } else if (result.install_guide) {
            showInstallGuide('usbipd 安装指南', result.install_guide);
            addLogEntry('启动 USB/IP 失败: ' + (result.error || '未知错误'), 'error');
        } else {
            activeUsbipSelection = null;
            const remediation = result.remediation ? `；建议：${result.remediation}` : '';
            addLogEntry(
                '启动 USB/IP 失败: '
                + (result.error || result.message || '未知错误')
                + remediation,
                'error'
            );
        }
    } catch (error) {
        btn.textContent = '📱 本地设备';
        btn.disabled = false;
        if (error.needPassword && error.deviceHost) {
            showDevicePasswordModal(error.deviceHost);
            addLogEntry('需要输入SSH密码以连接到 ' + error.deviceHost, 'warning');
        } else if (error.installGuide) {
            showInstallGuide('usbipd 安装指南', error.installGuide);
            activeUsbipSelection = null;
        } else {
            activeUsbipSelection = null;
        }
        const remediation = error.remediation ? `；建议：${error.remediation}` : '';
        addLogEntry('启动 USB/IP 失败: ' + error.message + remediation, 'error');
    } finally {
        usbipRoutingOperationRunning = false;
        updateUsbipAssignmentOperationButtons();
    }
}

async function refreshUsbipTargetWorker(
    workerId,
    expectedSerials = [],
    serialsBefore = new Set(),
    operationGeneration = usbipOperationGeneration
) {
    if (workerId && workerId !== workspaceWorkerId()) {
        window.GmsWorkspace?.update({
            scope_mode: isLocalWorkspaceWorker(workerId) ? 'single' : 'cluster',
            worker_id: workerId,
            device_ids: []
        }, {source: 'usbip-attach'});
        syncWorkspaceWorkerSelectors(workerId);
        updateTestHostScopedControls(workerId);
    }
    const baseline = serialsBefore instanceof Set ? serialsBefore : new Set(serialsBefore || []);
    for (const delay of [1000, 3000, 6000, 10000, 15000]) {
        await new Promise(resolve => setTimeout(resolve, delay));
        if (operationGeneration !== usbipOperationGeneration) return;
        try {
            await loadDevices(true);
            if (operationGeneration !== usbipOperationGeneration) return;
            const visible = new Set(
                state.devices.map(device => (
                    String(device.serial || device.device_id || '').split(':').pop()
                ))
            );
            const discoveredSerials = expectedSerials.length
                ? expectedSerials.filter(serial => visible.has(serial))
                : [...visible].filter(serial => !baseline.has(serial));
            if (
                expectedSerials.length
                ? expectedSerials.every(serial => visible.has(serial))
                : [...visible].some(serial => !baseline.has(serial))
            ) {
                addLogEntry(
                    `已刷新 ${workerId} 设备列表，ADB在线：`
                    + (discoveredSerials.join(', ') || '序列号尚未识别'),
                    'success'
                );
                return;
            }
        } catch (error) {
            debugLog('[USB/IP] Target Worker refresh failed:', error.message);
        }
    }
    if (operationGeneration !== usbipOperationGeneration) return;
    addLogEntry(
        `USB/IP传输已连接，设备：${expectedSerials.join(', ') || '尚未识别'}；`
        + `${workerId} 尚未完成ADB枚举，请稍后刷新`,
        'warning'
    );
}

function scheduleUsbipReconnect(reason) {
    if (Date.now() <= usbipManualDisconnectUntil) return;
    if (usbipReconnectWaiting || usbipReconnectTimer) return;
    usbipReconnectWaiting = true;
    usbipReconnectAttempts = 0;
    const btn = $('usbip-btn');
    if (btn) {
        btn.textContent = '📱 等待重连...';
        btn.disabled = false;
    }
    addLogEntry((reason || '检测到 USB/IP 设备断开') + '，等待后端自动重连...', 'warning');
    usbipReconnectTimer = setTimeout(attemptUsbipReconnect, USBIP_RECONNECT_INITIAL_DELAY_MS);
}

function isUsbipAdbReady(result) {
    return !!(result && result.success && (result.transport_connected || (Array.isArray(result.device_list) && result.device_list.length > 0)));
}

function isUsbipProtocolVisible(status) {
    if (!status || !status.protocol_status) return false;
    const mode = status.protocol_status.mode;
    return ['adb', 'fastboot', 'recovery', 'unauthorized', 'offline', 'adb_non_device'].includes(mode);
}

async function attemptUsbipReconnect() {
    // 手动断开后立即终止重连循环——不要继续"自动重连等待"。
    // （scheduleUsbipReconnect 的入口守卫拦不住已在执行的循环，故在此复核。）
    if (Date.now() <= usbipManualDisconnectUntil) {
        usbipReconnectTimer = null;
        usbipReconnectWaiting = false;
        const btn = $('usbip-btn');
        if (btn) { btn.textContent = '📱 本地设备'; btn.disabled = false; }
        addLogEntry('已手动断开 USB/IP，停止自动重连', 'info');
        return;
    }
    const btn = $('usbip-btn');
    usbipReconnectAttempts += 1;
    try {
        usbipReconnectTimer = null;
        const statusPath = pendingUsbipDeviceHost
            ? '/api/usbip/status?device_host=' + encodeURIComponent(pendingUsbipDeviceHost)
            : '/api/usbip/status';
        const status = await apiCall(statusPath, 'GET');
        const devices = await loadDevices(true);
        const usbipDevices = devices.filter(device => device && device.is_usbip);
        if (status.connected && (status.adb_ready || usbipDevices.length > 0 || isUsbipProtocolVisible(status))) {
            state.usbipConnected = true;
            usbipReconnectWaiting = false;
            pendingUsbipDeviceHost = status.device_host || pendingUsbipDeviceHost || '';
            if (btn) {
                btn.textContent = '📱 断开设备';
                btn.disabled = false;
            }
            const protocolMode = status.protocol_status && status.protocol_status.mode;
            addLogEntry(protocolMode && protocolMode !== 'adb'
                ? `USB/IP 后端自动重连已恢复，当前状态: ${protocolMode}`
                : 'USB/IP 后端自动重连已恢复', 'success');
            return;
        }
        throw new Error(status.reconnecting ? '后端正在重连' : '设备尚未稳定在线');
    } catch (error) {
        if (Date.now() <= usbipManualDisconnectUntil) {
            usbipReconnectTimer = null;
            usbipReconnectWaiting = false;
            if (btn) { btn.textContent = '📱 本地设备'; btn.disabled = false; }
            addLogEntry('已手动断开 USB/IP，停止自动重连', 'info');
            return;
        }
        if (usbipReconnectAttempts < USBIP_RECONNECT_MAX_ATTEMPTS) {
            addLogEntry(`USB/IP 自动重连等待第 ${usbipReconnectAttempts} 次未恢复，继续等待...`, 'warning');
            usbipReconnectTimer = setTimeout(attemptUsbipReconnect, USBIP_RECONNECT_INTERVAL_MS);
            return;
        }
        if (btn) {
            btn.textContent = '📱 本地设备';
            btn.disabled = false;
        }
        state.usbipConnected = false;
        usbipReconnectWaiting = false;
        addLogEntry('USB/IP 自动重连失败: ' + error.message, 'error');
        showToast('USB/IP 自动重连失败', 'error');
    }
}
