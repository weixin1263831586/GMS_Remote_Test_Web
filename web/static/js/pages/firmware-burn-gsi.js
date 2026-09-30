async function burnGsiImage() {
    if (selectedWorkspaceDeviceIds().length === 0) {
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

// Browse file for GSI system image.
// 集群模式默认浏览当前 Worker 主机的目录（可下拉切换其他 Worker）；
// 单机模式浏览测试主机目录；"本机"按钮始终选择浏览器所在电脑的文件。
async function browseLocalFileForGsiSystem() {
    const workerId = selectedClusterWorker();
    if (workerId) {
        await browseWorkerFileForGsiSystem(workerId);
        return;
    }
    const title = '选择System镜像';

    // Set file browser state
    state.fileBrowser.mode = 'gsi-system';
    state.fileBrowser.targetInputId = 'gsi-system';
    state.fileBrowser.selectedFile = null;
    state.gsiSystemFile = null;
    state.gsiSystemWorkerSource = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    ModalManager.open('file-browser-modal');

    // Load initial directory (GMS-Suite)
    await loadFileDirectory(getDefaultSuitesPath());
}

// 集群模式：打开 Worker 主机目录浏览器。
async function browseWorkerFileForGsiSystem(workerId) {
    state.fileBrowser.mode = 'gsi-system-worker';
    state.fileBrowser.targetInputId = 'gsi-system';
    state.fileBrowser.selectedFile = null;
    state.fileBrowser.workerBrowseId = workerId;
    state.gsiSystemFile = null;
    state.gsiSystemWorkerSource = null;
    document.getElementById('file-browser-title').textContent = '选择Worker主机上的System镜像';
    ModalManager.open('file-browser-modal');
    await populateFileBrowserWorkerSelect(workerId);
    await loadFileDirectory('');
}

// 选择浏览器所在电脑上的 System 镜像文件（集群模式下经 Controller 分发）。
function pickLocalGsiSystemFile() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.img';
    input.onchange = () => {
        state.gsiSystemFile = input.files?.[0] || null;
        if (state.gsiSystemFile) {
            document.getElementById('gsi-system').value = state.gsiSystemFile.name;
            state.gsiSystemWorkerSource = null;
            showToast(`已选择本机System镜像: ${state.gsiSystemFile.name}`, 'info');
        }
    };
    input.click();
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

    const uploadWorkerId = selectedClusterWorker() || workspaceWorkerId();
    addWorkerLog(uploadWorkerId, `正在上传Vendor Boot镜像到测试主机: ${file.name}`, 'info');
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
                addWorkerLog(uploadWorkerId, `Vendor Boot镜像上传完成: ${result.remote_path}`, 'success');
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

// 轮询集群命令直至终结状态；completed 返回 result，failed/cancelled 抛错。
async function pollClusterCommand(commandId, label) {
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(commandId)}`);
        const command = status.command || {};
        if (command.status === 'completed') return command.result || {};
        if (['failed', 'cancelled'].includes(command.status)) {
            throw new Error(command.error || `${label || '集群命令'}执行失败`);
        }
    }
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
        const devices = workerId
            ? selectedTestDeviceIds()
            : selectedWorkspaceDeviceIds();
        if (!devices.length) throw new Error('请重新选择要烧写GSI的设备');
        if (workerId) {
            const granted = await requestElevatedAccess('烧写GSI');
            if (!granted) return;
            const systemSource = state.gsiSystemWorkerSource || null;
            if (!systemSource && !state.gsiSystemFile && !state.gsiVendorFile) {
                throw new Error('远端 GSI 烧写必须选择 System 或 Vendor Boot 镜像');
            }
            if (devices.length !== 1) throw new Error('集群 GSI 烧写一次只允许一台设备');
            lockDevicesInUI(devices);
            if (systemSource) {
                // System 镜像来自 Worker/Controller 主机目录，无需浏览器上传。
                closeGsiModal();
                addWorkerLog(workerId, `System 镜像来源: ${systemSource.worker_id}:${systemSource.path}`, 'info');
                const form = new FormData();
                form.append('worker_id', workerId);
                form.append('devices', devices.join(','));
                form.append('source_worker_id', systemSource.worker_id);
                form.append('system_path', systemSource.path);
                if (state.gsiVendorFile) {
                    form.append('vendor_file', state.gsiVendorFile, state.gsiVendorFile.name);
                }
                const staged = await apiCall('/api/cluster/gsi/stage-from-source', 'POST', form);
                let flashCommandId = staged.command_id || '';
                if (Array.isArray(staged.pulls) && staged.pulls.length) {
                    const sourceWorkers = Array.from(
                        new Set(staged.pulls.map(item => item.worker_id))
                    ).join(', ');
                    addWorkerLog(workerId, `正在从 ${sourceWorkers} 传输镜像到测试主机...`, 'info');
                    for (const pull of staged.pulls) {
                        await pollClusterCommand(
                            pull.command_id,
                            `从 ${pull.worker_id} 传输 ${pull.kind} 镜像`
                        );
                    }
                    const finalizeForm = new FormData();
                    finalizeForm.append('worker_id', workerId);
                    finalizeForm.append('devices', devices.join(','));
                    finalizeForm.append(
                        'transfer_ids',
                        staged.pulls.map(pull => pull.transfer_id).join(',')
                    );
                    if (state.gsiVendorFile) {
                        finalizeForm.append('vendor_file', state.gsiVendorFile, state.gsiVendorFile.name);
                    }
                    const finalized = await apiCall(
                        '/api/cluster/gsi/stage-from-transfer', 'POST', finalizeForm
                    );
                    flashCommandId = finalized.command_id;
                }
                state.gsiSystemFile = null;
                state.gsiVendorFile = null;
                state.gsiSystemWorkerSource = null;
                // 远端 GSI 与固件一致：补"开始 + 命令 ID"系统日志
                // （此前只有完成 Toast，系统日志缺开始与命令 ID）。
                addWorkerLog(workerId, `远端 GSI 烧写开始: ${devices.join(', ')}（Worker: ${workerId}，命令: ${flashCommandId}）`, 'info');
                streamCommandEvents(flashCommandId, workerId);
                while (true) {
                    await new Promise(resolve => setTimeout(resolve, 2000));
                    const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(flashCommandId)}`);
                    if (status.command.status === 'completed') break;
                    if (['failed', 'cancelled'].includes(status.command.status)) {
                        addWorkerLog(workerId, `远端 GSI 烧写失败: ${status.command.error || 'GSI 烧写失败'}（Worker: ${workerId}，命令: ${flashCommandId}）`, 'error');
                        const failure = new Error(status.command.error || 'GSI 烧写失败');
                        failure.clusterCommandDispatched = true;
                        throw failure;
                    }
                }
                addWorkerLog(workerId, `远端 GSI 烧写完成: ${devices.join(', ')}（Worker: ${workerId}）`, 'success');
                showToast('远端 GSI 烧写完成', 'success');
                await refreshDevicesAfterBurn(workerId);
                return;
            }
            const form = new FormData();
            form.append('worker_id', workerId);
            form.append('devices', devices.join(','));
            if (state.gsiSystemFile) form.append('system_file', state.gsiSystemFile, state.gsiSystemFile.name);
            if (state.gsiVendorFile) form.append('vendor_file', state.gsiVendorFile, state.gsiVendorFile.name);
            closeGsiModal();
            const staged = await apiCall('/api/cluster/gsi/stage', 'POST', form);
            addWorkerLog(workerId, `GSI 已暂存，Worker 命令: ${staged.command_id}`, 'success');
            addWorkerLog(workerId, `远端 GSI 烧写开始: ${devices.join(', ')}（Worker: ${workerId}）`, 'info');
            streamCommandEvents(staged.command_id, workerId);
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 2000));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(staged.command_id)}`);
                if (status.command.status === 'completed') break;
                if (['failed', 'cancelled'].includes(status.command.status)) {
                    addWorkerLog(workerId, `远端 GSI 烧写失败: ${status.command.error || 'GSI 烧写失败'}（Worker: ${workerId}，命令: ${staged.command_id}）`, 'error');
                    const failure = new Error(status.command.error || 'GSI 烧写失败');
                    failure.clusterCommandDispatched = true;
                    throw failure;
                }
            }
            state.gsiSystemFile = null; state.gsiVendorFile = null; state.gsiSystemWorkerSource = null;
            addWorkerLog(workerId, `远端 GSI 烧写完成: ${devices.join(', ')}（Worker: ${workerId}）`, 'success');
            showToast('远端 GSI 烧写完成', 'success');
            await refreshDevicesAfterBurn(workerId);
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
        addLogEntry(`GSI 烧写失败: ${error.message}`, 'error');
        // Controller 终态通知已覆盖 Worker 命令创建后的失败；
        // 仅命令创建前（staging/参数校验）失败时本地通知。
        if (!error.clusterCommandDispatched) {
            notifyOperationResult('远端 GSI 烧写失败', error.message, 'error', 'gsi-burn');
        }
    }
}

