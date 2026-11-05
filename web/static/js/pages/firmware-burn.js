// ==================== VNC & Remote Control ====================
let firmwareBurnSubmitting = false;
async function burnFirmware() {
    if (selectedBurnableDeviceIds().length === 0) {
        showToast('请先选择要烧写固件的设备', 'warning');
        return;
    }
    if (!validateLocalUsbDeviceSelection('烧写固件')) return;

    renderFirmwareTargetDevices();
    hookFirmwareFileMetaInput();
    updateFirmwareFileMeta();
    const loading = loadFirmwareAoaProfiles();
    ModalManager.open('firmware-modal');
    await loading;
}

// 弹框内回显烧写目标（serial + 型号），避免回到设备列表逐一核对。
function renderFirmwareTargetDevices() {
    const target = document.getElementById('firmware-target-devices');
    if (!target) return;
    const devices = selectedBurnableDeviceIds();
    target.replaceChildren();
    if (!devices.length) {
        target.textContent = '未选择';
        return;
    }
    devices.forEach(id => {
        const device = state.devices.find(d =>
            (typeof d === 'string' ? d : d.device_id || d.serial_no) === id);
        const model = device?.model || device?.properties?.model || '';
        const chip = document.createElement('span');
        chip.className = 'firmware-target-chip';
        chip.textContent = model ? `${id}（${model}）` : id;
        target.appendChild(chip);
    });
}

// 本机文件显示名称+大小；服务器路径说明跳过浏览器上传，防止误判。
function updateFirmwareFileMeta() {
    const meta = document.getElementById('firmware-file-meta');
    if (!meta) return;
    const path = document.getElementById('firmware-path').value.trim();
    if (!path) {
        // 占位行常驻（高度恒定）但空文本不显示，避免选中固件后弹框跳动。
        meta.classList.add('is-empty');
        meta.textContent = '';
        return;
    }
    const file = document.getElementById('firmware-file-input')?.files?.[0] || null;
    meta.classList.remove('is-empty');
    meta.textContent = file
        ? `${file.name}（${formatBytes(file.size)}）· 同名支持续传`
        : `服务器路径 ${path} · 无需浏览器上传`;
}

function hookFirmwareFileMetaInput() {
    const pathInput = document.getElementById('firmware-path');
    if (!pathInput || pathInput.dataset.metaHook) return;
    pathInput.addEventListener('input', updateFirmwareFileMeta);
    pathInput.dataset.metaHook = '1';
}

let firmwareAoaLoadRevision = 0;

function toggleFirmwareAoa() {
    const enabled = document.getElementById('firmware-aoa-enabled').checked;
    document.getElementById('firmware-aoa-profile').disabled = !enabled;
    document.getElementById('firmware-aoa-boot-wait').disabled = !enabled;
    document.getElementById('firmware-aoa-options').classList.toggle('is-disabled', !enabled);
    const hint = document.getElementById('firmware-aoa-hint');
    if (!enabled && hint.dataset.state === 'error') {
        delete hint.dataset.state;
        hint.removeAttribute('title');
        hint.textContent = '已关闭自动 AOA，本次仅烧写固件。';
    }
}

async function loadFirmwareAoaProfiles() {
    const revision = ++firmwareAoaLoadRevision;
    const select = document.getElementById('firmware-aoa-profile');
    const previous = select.value;
    const hint = document.getElementById('firmware-aoa-hint');
    delete hint.dataset.state;
    hint.removeAttribute('title');
    select.replaceChildren(new Option('正在加载配置…', ''));
    hint.textContent = '触摸配置须有实测标定及目标固件兼容声明；镜像不符时在烧写前停止，回放后验证 ADB。';
    toggleFirmwareAoa();
    try {
        const response = await apiCall('/api/devices/aoa/profiles');
        if (revision !== firmwareAoaLoadRevision) return;
        const profiles = response.profiles || [];
        select.replaceChildren(new Option(profiles.length ? '请选择已验证的 AOA 配置' : '暂无配置，请先在 AOA 工作台保存', ''));
        profiles.forEach(profile => select.add(new Option(`${profile.name}（${profile.commands.length} 条，ADB 等待 ${profile.wait_adb_seconds || 60}s）`, profile.id)));
        if (profiles.some(profile => profile.id === previous)) select.value = previous;
    } catch (error) {
        if (revision !== firmwareAoaLoadRevision) return;
        select.replaceChildren(new Option('配置加载失败，请重新打开弹框重试', ''));
        hint.textContent = `AOA 配置加载失败：${error.message}`;
    }
    toggleFirmwareAoa();
}

function closeFirmwareModal() {
    ModalManager.close('firmware-modal');
}

// 设备前端乐观锁定/解锁共用助手；后端锁由其 finally 释放，刷新同步。
function applyDevicesLockState(devices, locked) {
    devices.forEach(deviceId => {
        const idx = state.devices.findIndex(d =>
            (typeof d === 'string' ? d : d.device_id) === deviceId);
        if (idx === -1) return;
        const device = state.devices[idx];
        if (typeof device === 'string') {
            if (locked) {
                state.devices[idx] = { device_id: device, locked: true, locked_by: '当前用户', locked_at: new Date().toISOString() };
            }
        } else if (locked) {
            device.locked = true;
            device.locked_by = '当前用户';
            device.locked_at = new Date().toISOString();
        } else {
            delete device.locked; delete device.locked_by; delete device.locked_at;
        }
    });
    renderDevices();
}

function lockDevicesInUI(d) { applyDevicesLockState(d, true); }
function unlockDevicesInUI(d) { applyDevicesLockState(d, false); }

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
                updateFirmwareFileMeta();
                const savedName = sessionStorage.getItem('firmwareUploadFileName');
                const savedSize = parseInt(sessionStorage.getItem('firmwareUploadFileSize') || '0');
                const savedLastModified = parseInt(sessionStorage.getItem('firmwareUploadLastModified') || '-1');
                const interrupted = sessionStorage.getItem('firmwareUploadInterrupted') === 'true';
                if (interrupted && savedName === file.name && savedSize === file.size && savedLastModified === (file.lastModified || 0)) {
                    showToast(`已选择同名固件，将校验内容指纹后续传: ${file.name}`, 'info');
                    addLogEntry(`已选择同名固件，准备校验内容指纹: ${file.name}`, 'info');
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

async function submitFirmwareBurn() {
    if (firmwareBurnSubmitting) return;
    const firmwarePath = document.getElementById('firmware-path').value.trim();
    if (!firmwarePath) {
        showToast('请选择固件文件', 'error');
        return;
    }

    const fileInput = document.getElementById('firmware-file-input');
    const selectedFirmwareFile = fileInput?.files?.[0] || null;

    const devices = selectedBurnableDeviceIds();
    if (!devices.length) {
        showToast('请重新选择要烧写的设备', 'warning');
        return;
    }
    const aoaEnabled = document.getElementById('firmware-aoa-enabled').checked;
    const aoaProfile = document.getElementById('firmware-aoa-profile').value;
    const bootWaitText = document.getElementById('firmware-aoa-boot-wait').value.trim();
    const bootWait = Number(bootWaitText);
    if (aoaEnabled && (devices.length !== 1 || !aoaProfile || !bootWaitText || !Number.isInteger(bootWait) || bootWait < 0 || bootWait > 600)) {
        showToast('启用 AOA 时请选择一台设备、一个已验证的配置，并填写 0..600 秒的启动等待', 'warning');
        return;
    }
    const workerId = selectedClusterWorker();
    if (workerId && (!selectedFirmwareFile || devices.length !== 1)) {
        showToast('远端 Worker 烧写须选择一台设备和本机固件文件', 'warning'); return;
    }
    const appendAoaOptions = (form) => {
        if (aoaEnabled) {
            form.append('aoa_profile_id', aoaProfile);
            form.append('aoa_boot_wait_seconds', String(bootWait));
        }
    };
    // 声明在 try 外：catch 中也要能移除刷新拦截。
    const warnBeforeRefresh = (e) => {
        e.preventDefault();
        e.returnValue = '固件上传中，刷新会暂停浏览器上传；重新选择同一文件后可从已上传分片续传。确定要离开吗？';
        return e.returnValue;
    };
    const submitButton = document.querySelector('#firmware-modal [data-click="submitFirmwareBurn"]');
    firmwareBurnSubmitting = true;
    submitButton.disabled = true;
    let burnStarted = false;
    try {
        if (aoaEnabled) await checkFirmwareAoaBeforeUpload(workerId, devices, aoaProfile, bootWait);
        if (!ModalManager.isOpen('firmware-modal')) return;
        const granted = await requestElevatedAccess('烧写设备固件');
        if (!granted) return;
        if (!ModalManager.isOpen('firmware-modal')) return;
        if (workerId !== selectedClusterWorker() || JSON.stringify(devices) !== JSON.stringify(selectedBurnableDeviceIds())
                || firmwarePath !== document.getElementById('firmware-path').value.trim()
                || selectedFirmwareFile !== (document.getElementById('firmware-file-input')?.files?.[0] || null)
                || aoaEnabled !== document.getElementById('firmware-aoa-enabled').checked
                || (aoaEnabled && (aoaProfile !== document.getElementById('firmware-aoa-profile').value
                    || bootWaitText !== document.getElementById('firmware-aoa-boot-wait').value.trim()))) {
            throw new Error('检查期间设备、固件或 AOA 选项已改变，请核对后重试');
        }
        burnStarted = true;
        closeFirmwareModal();
        showToast('正在烧写固件...', 'info');
        addLogEntry(`开始烧写固件: ${firmwarePath}`, 'info');

        const cleanupUploadState = () => {
            if (selectedFirmwareFile) {
                window.removeEventListener('beforeunload', warnBeforeRefresh);
                clearFirmwareUploadState();
            }
        };

        if (workerId) {
            lockDevicesInUI(devices);
            const form = new FormData();
            form.append('worker_id', workerId);
            form.append('devices', devices.join(','));
            form.append('firmware_file', selectedFirmwareFile, selectedFirmwareFile.name);
            appendAoaOptions(form);
            const staged = await apiCall('/api/cluster/firmware/stage', 'POST', form);
            addWorkerLog(workerId, `固件已暂存，Worker 命令: ${staged.command_id}`, 'success');
            addWorkerLog(workerId, `远端固件烧写开始: ${devices.join(', ')}（Worker: ${workerId}）`, 'info');
            await streamCommandEvents(staged.command_id, workerId);
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 2000));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(staged.command_id)}`);
                const command = status.command;
                if (command.status === 'completed') {
                    addWorkerLog(workerId, `远端固件烧写完成: ${command.result?.device || devices[0]}`, 'success');
                    showToast(aoaEnabled ? '固件烧写、AOA 回放及 ADB 验证完成' : '远端固件烧写完成', 'success');
                    break;
                }
                if (['failed', 'cancelled'].includes(command.status)) {
                    addWorkerLog(workerId, `远端固件烧写失败: ${command.error || '远端固件烧写失败'}（Worker: ${workerId}）`, 'error');
                    const failure = new Error(command.error || '远端固件烧写失败');
                    failure.clusterCommandDispatched = true;
                    throw failure;
                }
            }
            await refreshDevicesAfterBurn(workerId);
            return;
        }

        let firmwareUploadId = '';
        let firmwareFingerprint = '';
        const persistUploadState = (startedAt, progress, uploadedSize) => saveFirmwareUploadState(
            selectedFirmwareFile.name,
            selectedFirmwareFile.size,
            startedAt,
            progress,
            uploadedSize,
            selectedFirmwareFile.size,
            firmwareUploadId,
            selectedFirmwareFile.lastModified || 0,
            firmwareFingerprint
        );
        if (selectedFirmwareFile) {
            const identity = await getReusableFirmwareUploadId(selectedFirmwareFile);
            firmwareUploadId = identity.uploadId;
            firmwareFingerprint = identity.fingerprint;
            // 设置上传状态标记，防止刷新导致进度丢失
            persistUploadState(Date.now(), 0, 0);

            // 添加beforeunload事件监听，警告用户不要刷新
            window.addEventListener('beforeunload', warnBeforeRefresh);
        } else {
            addLogEntry(`使用服务器固件路径，跳过本机上传: ${firmwarePath}`, 'info');
        }

        let uploadResult;
        if (selectedFirmwareFile) {
            const uploadId = firmwareUploadId;
            const startedAt = parseInt(sessionStorage.getItem('firmwareUploadStartTime') || Date.now());
            notifyOperationResult('固件上传已启动', '固件分片上传任务已开始', 'info', 'firmware-burn');
            addLogEntry(`固件上传任务已启动，设备: ${devices.join(', ')}`, 'success');

            // 提权过期恢复统一在 chunk-upload.js（uploadChunksWithElevationRecovery）。
            uploadResult = await uploadChunksWithElevationRecovery(
                selectedFirmwareFile,
                `/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`,
                {
                    chunkSize: 32 * 1024 * 1024,
                    concurrent: 4,
                    resume: true,
                    checkExisting: true,
                    uploadId,
                    contentFingerprint: firmwareFingerprint,
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
                        persistUploadState(startedAt, progress, uploadedSize);
                        updateUploadProgress(progress, selectedFirmwareFile.name, uploadedSize, selectedFirmwareFile.size);
                    },
                    onReElevate: async () => {
                        addLogEntry('管理员提权已过期，固件上传已暂停', 'warning');
                        return requestElevatedAccess('继续固件上传（管理员验证已过期）');
                    },
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
                appendAoaOptions(finalizeForm);
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
            appendAoaOptions(formData);
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
                        let detail = `HTTP ${xhr.status}`;
                        try {
                            const body = JSON.parse(xhr.responseText);
                            detail = body.error || body.message || detail;
                        } catch (_) { /* 保留 HTTP 状态 */ }
                        reject(new Error(detail));
                    }
                });

                xhr.addEventListener('error', () => {
                    reject(new Error('Network error'));
                });

                xhr.addEventListener('abort', () => {
                    reject(new Error('Upload aborted'));
                });

                xhr.open('POST', `/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`);
                // 烧写请求发出时立即提示已启动。
                // 否则会被后端烧写完成的通知晚到，导致时序颠倒。
                notifyOperationResult('固件烧写已启动', '烧写任务已开始', 'info', 'firmware-burn');
                addLogEntry(`固件烧写任务已启动，设备: ${devices.join(', ')}`, 'success');
                xhr.send(formData);
            });
        }

        const result = uploadResult;
        if (!result.success) {
            // 后端对烧写失败已通过 WebSocket 推送通知；这里只写页面日志，
            // 不再回存通知中心，避免重复通知。200+success:false（服务器
            // 路径 XHR 分支）同样回滚前端乐观锁定。
            unlockDevicesInUI(devices);
            addLogEntry(`固件烧写失败: ${result.error}`, 'error');
        } else {
            // 本地烧写请求在烧写+AOA 恢复（含 ADB 验证）全部结束后才返回：
            // 返回即设备已回到可用状态，立即刷新设备列表并解除乐观锁定，
            // 无需等待周期性自动刷新。
            unlockDevicesInUI(devices);
            addLogEntry(aoaEnabled ? '固件烧写与 AOA 恢复完成，已刷新设备列表'
                                   : '固件烧写完成，已刷新设备列表', 'success');
            await refreshDevicesAfterBurn();
        }
    } catch (error) {
        if (!burnStarted) {
            firmwareAoaPreflightFailed(error);
            return;
        }
        // 网络层异常（后端不可达等）后端无法自行通知，保留本地通知。
        // Worker 命令已创建的失败由 Controller 终态通知负责，不重复回存。
        if (!error.clusterCommandDispatched) {
            notifyOperationResult('固件烧写失败', error.message, 'error', 'firmware-burn');
        }
        addLogEntry(`固件烧写异常: ${error.message}`, 'error');
        // 上传失败后移除刷新拦截，避免 beforeunload 警告常驻并随重试叠加；
        // 续传状态（sessionStorage）保留，刷新后仍可校验指纹续传。
        if (selectedFirmwareFile) {
            window.removeEventListener('beforeunload', warnBeforeRefresh);
        }
        // 回滚前端乐观锁定；后端锁由其 finally 释放并经下方刷新同步。
        unlockDevicesInUI(devices);
        loadDevices(true).catch(refreshError => {
            console.error('[Firmware Burn] Failed to refresh devices after error:', refreshError);
        });
    } finally {
        firmwareBurnSubmitting = false;
        submitButton.disabled = false;
    }
}

// 烧写完成后的"刷新"不能复用 switchTestWorker()——那是完整的
// 用户切机生命周期（清设备/清 job/清套件缓存）。这里只做 scoped 设备
// 刷新：仅当用户还停留在发起烧写的 Worker 上时刷新设备清单，避免旧
// Promise 竞态重置用户已切到的另一台 Worker 的上下文。
async function refreshDevicesAfterBurn(originalWorkerId) {
    const target = originalWorkerId || workspaceWorkerId();
    if (workspaceWorkerId() !== target) {
        // 用户已切走：不碰当前上下文，后端终态通知会告知结果。
        addLogEntry(`烧写完成，但当前已切换到其他测试主机，跳过设备刷新（原主机: ${target}）`, 'info');
        return;
    }
    try {
        await loadDevices(true);
    } catch (error) {
        showToast(`刷新设备列表失败: ${error.message}`, 'warning');
    }
}

// 拉取 Worker 烧写命令的实时过程日志（command events），
// 输出到系统日志面板（带 worker scope）。后台低频轮询，命令终态后停止。
function streamCommandEvents(commandId, workerId) {
    let sequence = -1;
    let stopped = false;
    const poll = async () => {
        while (!stopped) {
            try {
                const response = await apiCall(
                    `/api/cluster/commands/${encodeURIComponent(commandId)}/events?after=${sequence}&limit=500`,
                    'GET', null, {background: true}
                );
                const events = response.events || [];
                if (events.length) {
                    sequence = Math.max(...events.map(event => Number(event.sequence)));
                    events.forEach(event => addLogEntry(
                        event.message, event.level === 'error' ? 'error' : 'info', true, 'system', workerId
                    ));
                }
                const status = response.command?.status || '';
                if (['completed', 'failed', 'cancelled'].includes(status)) {
                    stopped = true;
                    return;
                }
            } catch (error) {
                // 轮询失败（网络/权限）不中断烧写主流程，静默重试。
            }
            await new Promise(resolve => setTimeout(resolve, 2000));
        }
    };
    poll();  // 后台执行，不 await：烧写状态轮询仍由主循环负责。
    return () => { stopped = true; };
}

async function burnSerialNumber() {
    if (selectedTestDeviceIds().length === 0) {
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
    const devices = endpoint === '/api/burn/gsi'
        ? selectedWorkspaceDeviceIds()
        : selectedTestDeviceIds();
    if (!devices.length) {
        showToast('请重新选择要操作的设备', 'warning');
        return;
    }
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

