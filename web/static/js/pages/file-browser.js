// ==================== File Upload ====================
async function handleUploadFile() {
    const fileInput = document.getElementById('local-file');
    const file = fileInput.files[0];

    if (!file) {
        showToast('请先选择要上传的文件', 'warning');
        return;
    }
    const granted = await requestElevatedAccess('上传文件到测试主机');
    if (!granted) return;

    try {
        await apiCall('/api/terminal/open');
        addLogEntry(`正在上传文件: ${file.name}`, 'info');
        const progressFill = document.getElementById('upload-progress-fill');
        const progressInfo = document.getElementById('progress-info');
        const startTime = Date.now();

        // Create FormData
        const formData = new FormData();
        formData.append('file', file);
        const workerId = workspaceWorkerId();
        if (!isLocalWorkspaceWorker(workerId)) {
            const host = await resolveClusterHost(workerId);
            formData.append('worker_id', workerId);
            formData.append('host', host.address);
            formData.append('user', host.ssh_user);
            addLogEntry(`上传目标: ${workerId} (${host.ssh_user}@${host.address})`, 'info');
        }

        // Use XMLHttpRequest for upload progress
        const xhr = new XMLHttpRequest();

        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const percentage = Math.round((e.loaded / e.total) * 100);
                const transferred = formatBytes(e.loaded);
                const total = formatBytes(e.total);
                const elapsed = (Date.now() - startTime) / 1000;
                const speed = elapsed > 0 ? formatBytes(e.loaded / elapsed) + '/s' : '';

                progressFill.style.width = percentage + '%';
                progressInfo.textContent = `上传中... ${percentage.toFixed(1)}% (${transferred}/${total}) ${speed}`;
            }
        });

        xhr.addEventListener('load', () => {
            if (xhr.status === 200) {
                let response;
                try {
                    response = JSON.parse(xhr.responseText);
                } catch (e) {
                    addLogEntry('上传失败: 服务端返回非 JSON 响应', 'error');
                    progressFill.style.width = '0%';
                    progressInfo.textContent = '';
                    return;
                }
                if (response.success) {
                    progressFill.style.width = '100%';
                    progressInfo.textContent = `上传完成 (${formatBytes(file.size)})`;
                    addLogEntry(`文件上传成功: ${response.remote_path || file.name}`, 'success');
                    showToast('文件上传成功', 'success');

                    setTimeout(() => {
                        progressFill.style.width = '0%';
                        progressInfo.textContent = '';
                        fileInput.value = ''; // Clear file input
                        // Reset drop zone UI
                        document.getElementById('drop-zone-text').style.display = 'block';
                        document.getElementById('drop-zone-filename').style.display = 'none';
                        document.getElementById('drop-zone-filename').textContent = '';
                    }, 3000);
                } else {
                    addLogEntry('上传失败: ' + (response.error || '未知错误'), 'error');
                    progressFill.style.width = '0%';
                    progressInfo.textContent = '';
                }
            } else {
                addLogEntry(`上传失败: HTTP ${xhr.status}`, 'error');
                progressFill.style.width = '0%';
                progressInfo.textContent = '';
            }
        });

        xhr.addEventListener('error', () => {
            addLogEntry('上传失败: 网络错误', 'error');
            progressFill.style.width = '0%';
            progressInfo.textContent = '';
        });

        // Start upload
        xhr.open('POST', '/api/terminal/push');
        xhr.send(formData);
    } catch (error) {
        addLogEntry('文件上传失败: ' + error.message, 'error');
        document.getElementById('upload-progress-fill').style.width = '0%';
    }
}

// 固件上传状态管理。

/**
 * 保存固件上传状态到 sessionStorage
 */
async function getFirmwareUploadFingerprint(file) {
    const sampleSize = 64 * 1024;
    const offsets = [
        0,
        Math.max(0, Math.floor((file.size - sampleSize) / 2)),
        Math.max(0, file.size - sampleSize),
    ];
    const samples = await Promise.all(
        offsets.map(offset => file.slice(offset, Math.min(file.size, offset + sampleSize)).arrayBuffer())
    );
    const metadata = new TextEncoder().encode(
        `${file.name}\0${file.size}\0${file.lastModified || 0}\0`
    );
    const totalLength = metadata.byteLength + samples.reduce((sum, sample) => sum + sample.byteLength, 0);
    const input = new Uint8Array(totalLength);
    input.set(metadata, 0);
    let cursor = metadata.byteLength;
    for (const sample of samples) {
        input.set(new Uint8Array(sample), cursor);
        cursor += sample.byteLength;
    }
    const digest = await crypto.subtle.digest('SHA-256', input);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

async function getFirmwareUploadId(file) {
    const fingerprint = await getFirmwareUploadFingerprint(file);
    return {uploadId: `fw-v2-${fingerprint}`, fingerprint};
}

async function getReusableFirmwareUploadId(file) {
    const fingerprint = await getFirmwareUploadFingerprint(file);
    const savedName = sessionStorage.getItem('firmwareUploadFileName');
    const savedSize = parseInt(sessionStorage.getItem('firmwareUploadFileSize') || '0');
    const savedLastModified = parseInt(sessionStorage.getItem('firmwareUploadLastModified') || '-1');
    const savedFingerprint = sessionStorage.getItem('firmwareUploadFingerprint') || '';
    const savedId = sessionStorage.getItem('firmwareUploadId');
    if (
        savedId
        && savedName === file.name
        && savedSize === file.size
        && savedLastModified === (file.lastModified || 0)
        && savedFingerprint === fingerprint
    ) {
        return {uploadId: savedId, fingerprint};
    }
    return {uploadId: `fw-v2-${fingerprint}`, fingerprint};
}

function saveFirmwareUploadState(fileName, fileSize, startTime, progress = 0, uploadedSize = 0, totalSize = 0, uploadId = '', lastModified = 0, fingerprint = '') {
    sessionStorage.setItem('firmwareUploadInProgress', 'true');
    sessionStorage.setItem('firmwareUploadFileName', fileName);
    sessionStorage.setItem('firmwareUploadFileSize', fileSize);
    sessionStorage.setItem('firmwareUploadLastModified', String(lastModified || 0));
    sessionStorage.setItem('firmwareUploadFingerprint', fingerprint || '');
    sessionStorage.setItem('firmwareUploadStartTime', startTime.toString());
    if (uploadId) {
        sessionStorage.setItem('firmwareUploadId', uploadId);
        sessionStorage.removeItem(`firmwareUploadWarningShown:${uploadId}`);
    }
    sessionStorage.removeItem('firmwareUploadInterrupted');
    if (progress > 0) {
        sessionStorage.setItem('firmwareUploadProgress', progress.toString());
        sessionStorage.setItem('firmwareUploadedSize', uploadedSize.toString());
        sessionStorage.setItem('firmwareTotalSize', totalSize.toString());
    }
}

/**
 * 清理固件上传状态
 */
function clearFirmwareUploadState() {
    sessionStorage.removeItem('firmwareUploadInProgress');
    sessionStorage.removeItem('firmwareUploadFileName');
    sessionStorage.removeItem('firmwareUploadFileSize');
    sessionStorage.removeItem('firmwareUploadLastModified');
    sessionStorage.removeItem('firmwareUploadFingerprint');
    sessionStorage.removeItem('firmwareUploadStartTime');
    sessionStorage.removeItem('firmwareUploadProgress');
    sessionStorage.removeItem('firmwareUploadedSize');
    sessionStorage.removeItem('firmwareTotalSize');
    const uploadId = sessionStorage.getItem('firmwareUploadId');
    if (uploadId) {
        sessionStorage.removeItem(`firmwareUploadWarningShown:${uploadId}`);
    }
    sessionStorage.removeItem('firmwareUploadId');
    sessionStorage.removeItem('firmwareUploadInterrupted');
}

// 导出到全局
window.saveFirmwareUploadState = saveFirmwareUploadState;
window.clearFirmwareUploadState = clearFirmwareUploadState;

// 通用上传进度更新函数（用于固件上传等）
function updateUploadProgress(percentage, filename, uploadedSize, totalSize) {

    const progressFill = document.getElementById('upload-progress-fill');
    const progressInfo = document.getElementById('progress-info');

    if (progressFill && progressInfo) {
        progressFill.style.width = percentage + '%';

        const transferred = formatBytes(uploadedSize);
        const total = formatBytes(totalSize);

        if (percentage >= 100) {
            progressInfo.textContent = `✅ ${filename} 上传完成 (${total})`;
            // 3秒后重置进度条
            setTimeout(() => {
                progressFill.style.width = '0%';
                progressInfo.textContent = '';
            }, 3000);
        } else {
            progressInfo.textContent = `📤 ${filename} 上传中... ${percentage.toFixed(1)}% (${transferred}/${total})`;
        }
    } else {
        console.error('[updateUploadProgress] Progress elements not found!');
    }
}

// ==================== Browse Remote File ====================
async function browseRemoteFile(mode) {
    if (mode !== 'retry') {
        showToast('该功能暂不支持', 'warning');
        return;
    }

    const targetInputId = 'retry-result';
    const title = '选择测试报告';

    // Set file browser state
    state.fileBrowser.mode = mode;
    state.fileBrowser.targetInputId = targetInputId;
    state.fileBrowser.selectedFile = null;
    state.fileBrowser.clusterWorkerId = '';
    state.fileBrowser.clusterSuitePath = '';

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    ModalManager.open('file-browser-modal');

    // Load initial directory - use test suite results directory
    let defaultPath = getDefaultSuitesPath();

    // Get current test suite selection
    const testSuiteSelect = document.getElementById('test-suite');
    const toolsPath = testSuiteSelect?.value || '';
    const workerId = workspaceWorkerId();

    if (!toolsPath) {
        if (!isLocalWorkspaceWorker(workerId)) {
            showToast('请先选择当前 Worker 上的测试套件', 'warning');
            return;
        }
        addLogEntry(`未选择测试套件，使用默认路径: ${defaultPath}`, 'info');
        await loadFileDirectory(defaultPath);
        return;
    }

    if (!isLocalWorkspaceWorker(workerId)) {
        state.fileBrowser.clusterWorkerId = workerId;
        state.fileBrowser.clusterSuitePath = toolsPath;
        addLogEntry(`自动导航到 ${workerId} 测试套件 results 目录`, 'info');
        await loadFileDirectory('results');
        return;
    }

    // Convert tools path to results path
    if (toolsPath.includes('/tools')) {
        defaultPath = toolsPath.replace('/tools', '/results');
        addLogEntry(`自动导航到测试套件results目录: ${defaultPath}`, 'info');
    } else {
        addLogEntry(`测试套件路径格式异常，使用默认路径: ${defaultPath}`, 'warning');
    }

    await loadFileDirectory(defaultPath);
}

async function loadFileDirectory(path) {
    try {
        if (state.fileBrowser.mode === 'firmware-share') {
            await loadFirmwareShareRemoteDirectory(path);
            return;
        }
        if (state.fileBrowser.mode === 'gsi-system-worker') {
            await loadWorkerFileDirectory(path);
            return;
        }
        if (state.fileBrowser.mode === 'retry' && state.fileBrowser.clusterWorkerId) {
            const params = new URLSearchParams({
                worker_id: state.fileBrowser.clusterWorkerId,
                suite_path: state.fileBrowser.clusterSuitePath,
                path: path || '',
            });
            const result = await apiCall(`/api/cluster/suites/files?${params.toString()}`);
            const data = result.data || {};
            state.fileBrowser.currentPath = data.path || '';
            renderFileList(data.items || []);
            return;
        }
        const result = await apiCall('/api/files/list', 'POST', { path });

        if (result.success) {
            state.fileBrowser.currentPath = result.path;
            renderFileList(result.files);
        } else {
            showToast('加载文件列表失败: ' + result.error, 'error');
        }
    } catch (error) {
        showToast('加载文件列表失败: ' + error.message, 'error');
    }
}

// ---- Worker 主机目录浏览（集群 GSI System 镜像选择） ----
async function populateFileBrowserWorkerSelect(selectedWorkerId) {
    const row = document.getElementById('file-browser-worker-row');
    const select = document.getElementById('file-browser-worker-select');
    if (!row || !select) return;
    row.style.display = 'flex';
    select.innerHTML = '';
    let workers = [];
    try {
        if (typeof window.GmsWorkspace?.loadClusterWorkers === 'function') {
            workers = await window.GmsWorkspace.loadClusterWorkers();
        } else if (typeof window.loadClusterWorkers === 'function') {
            workers = await window.loadClusterWorkers();
        }
    } catch (error) {
        console.warn('[FileBrowser] Worker list unavailable:', error);
    }
    const localId = workspaceLocalWorkerId();
    const options = [{value: localId, label: `${localId} (Controller)`}];
    for (const worker of workers || []) {
        if (worker.id === localId || worker.status === 'offline') continue;
        options.push({value: worker.id, label: worker.status ? `${worker.id} (${worker.status})` : worker.id});
    }
    if (!options.some(option => option.value === selectedWorkerId)) {
        options.unshift({value: selectedWorkerId, label: selectedWorkerId});
    }
    const fragment = document.createDocumentFragment();
    for (const option of options) {
        const element = document.createElement('option');
        element.value = option.value;
        element.textContent = option.label;
        element.selected = option.value === selectedWorkerId;
        fragment.appendChild(element);
    }
    select.replaceChildren(fragment);
}

async function onFileBrowserWorkerChange() {
    const select = document.getElementById('file-browser-worker-select');
    if (!select || !select.value) return;
    state.fileBrowser.workerBrowseId = select.value;
    state.fileBrowser.currentPath = '';
    state.fileBrowser.selectedFile = null;
    await loadFileDirectory('');
}

async function loadWorkerFileDirectory(path) {
    const workerId = state.fileBrowser.workerBrowseId;
    if (!workerId) {
        showToast('未选择要浏览的 Worker 主机', 'warning');
        return;
    }
    try {
        if (isLocalWorkspaceWorker(workerId)) {
            const result = await apiCall('/api/files/list', 'POST', { path });
            if (!result.success) throw new Error(result.error || '加载目录失败');
            state.fileBrowser.currentPath = result.path;
            renderFileList(result.files || []);
            return;
        }
        const params = new URLSearchParams({worker_id: workerId, path: path || ''});
        const result = await apiCall(`/api/cluster/files/browse?${params.toString()}`);
        const data = result.data || {};
        state.fileBrowser.currentPath = data.path || '';
        renderFileList(data.files || []);
    } catch (error) {
        showToast(`加载 Worker ${workerId} 目录失败: ` + error.message, 'error');
    }
}

async function loadFirmwareShareRemoteDirectory(path) {
    const defaults = firmwareShareDefaults();
    const host = state.fileBrowser.remoteHost || defaults.host;
    const user = state.fileBrowser.remoteUser || defaults.user;
    if (!host || !user) {
        renderFirmwareShareBrowseError('', '未配置共享固件主机');
        return;
    }
    try {
        const result = await firmwareShareApiWithAuth('/api/firmware-shares/browse', {
            host,
            user,
            path,
        }, host);
        const data = result.data || {};
        state.fileBrowser.currentPath = data.path || path;
        state.fileBrowser.remoteHost = data.host || state.fileBrowser.remoteHost || host;
        state.fileBrowser.remoteUser = data.user || user;
        renderFileList(data.files || []);
    } catch (error) {
        showToast('加载远端固件目录失败: ' + error.message, 'error');
        renderFirmwareShareBrowseError(host, error.message);
    }
}

function renderFirmwareShareBrowseError(host, message) {
    const list = document.getElementById('file-browser-list');
    if (!list) return;
    const routeBtn = host
        ? `<button class="btn-xxs" style="margin-top: 4px;" onclick="checkRouting('${escapeHtml(host)}')">📡 检查路由</button>`
        : '';
    list.innerHTML = `
        <div class="file-browser-item" style="cursor: default; flex-direction: column; align-items: flex-start; gap: 6px;">
            <div style="color: var(--danger-color);">⚠️ 无法加载远端固件目录</div>
            <div style="color: var(--text-secondary); font-size: 12px;">主机 ${escapeHtml(host || '')}：${escapeHtml(message || '')}</div>
            <div style="color: var(--text-muted); font-size: 11px;">请确认主机可达且 SSH 凭据正确；若仍失败可关闭后重试。</div>
            ${routeBtn}
        </div>
    `;
}

function formatFileBrowserDate(timestamp) {
    const ts = Number(timestamp);
    if (!ts) return '';
    const d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderFileList(files) {
    const listContainer = document.getElementById('file-browser-list');
    const pathDisplay = document.getElementById('file-browser-current-path');

    // Update current path display
    pathDisplay.textContent = state.fileBrowser.currentPath;

    if (files.length === 0) {
        listContainer.innerHTML = '<div class="file-browser-item" style="cursor: default; color: var(--text-muted);">空目录</div>';
        return;
    }

    listContainer.innerHTML = '';
    files.forEach(file => {
        const item = document.createElement('div');
        item.className = 'file-browser-item';
        item.addEventListener('click', (event) => selectFileForSelection(file.name, file.type, event));
        item.addEventListener('dblclick', () => openFileOrDirectory(file.name, file.type));

        const icon = document.createElement('span');
        icon.className = 'file-browser-icon';
        icon.textContent = file.type === 'directory' ? '📁' : '📄';
        item.appendChild(icon);

        const name = document.createElement('span');
        name.className = 'file-browser-name';
        name.textContent = file.name;
        item.appendChild(name);

        const sizeInfo = document.createElement('span');
        sizeInfo.className = 'file-browser-meta';
        sizeInfo.style.textAlign = 'right';
        sizeInfo.textContent = file.type === 'file' ? formatBytes(file.size, true) : '—';
        item.appendChild(sizeInfo);

        const mtime = document.createElement('span');
        mtime.className = 'file-browser-meta';
        mtime.style.textAlign = 'right';
        mtime.textContent = (file.modified || file.mtime) ? formatFileBrowserDate(file.modified || file.mtime) : '';
        item.appendChild(mtime);

        listContainer.appendChild(item);
    });
}

function selectFileForSelection(name, type, sourceEvent) {
    // Select file/directory (highlight it)
    state.fileBrowser.selectedFile = { name, type };

    // Update UI to show selection
    document.querySelectorAll('.file-browser-item').forEach(item => {
        item.classList.remove('selected');
    });

    const eventSource = sourceEvent || window.event;
    if (eventSource && eventSource.currentTarget) {
        eventSource.currentTarget.classList.add('selected');
    }
}

function openFileOrDirectory(name, type) {
    if (type === 'directory') {
        if (state.fileBrowser.mode === 'utility-tool') {
            const current = state.fileBrowser.currentPath;
            const newPath = current ? current + '/' + name : name;
            ut_loadToolDir(newPath);
        } else {
            // Navigate into directory
            const newPath = state.fileBrowser.currentPath === '/'
                ? `/${name}`
                : `${state.fileBrowser.currentPath}/${name}`;
            loadFileDirectory(newPath);
        }
    } else {
        // 双击文件：先选中再直接确认回填，省去手动点"确认"。
        selectFileForSelection(name, type);
        confirmFileSelection();
    }
}

function closeFileBrowserModal() {
    ModalManager.close('file-browser-modal');
    state.fileBrowser.selectedFile = null;
    const workerRow = document.getElementById('file-browser-worker-row');
    if (workerRow) {
        workerRow.style.display = 'none';
    }
}

function confirmFileSelection() {
    const targetInput = document.getElementById(state.fileBrowser.targetInputId);

    // For other modes, require file selection
    if (!state.fileBrowser.selectedFile) {
        showToast('请先选择一个文件', 'warning');
        return;
    }

    // Get selected item info
    const selectedItem = state.fileBrowser.selectedFile;
    const isDirectory = selectedItem.type === 'directory';

    // For retry mode, handle directory and file differently
    let fullPath;
    if (state.fileBrowser.mode === 'retry') {
        if (isDirectory) {
            // 重试模式选择目录时使用当前路径。
            fullPath = state.fileBrowser.currentPath;
        } else {
            // For file selection, include the filename
            fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        }

        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择测试报告: ${fullPath}`, 'info');
        }

        // 选择重试报告后清空模块和用例。
        const testModuleInput = $('test-module');
        const testCaseInput = $('test-case');
        if (testModuleInput) {
            testModuleInput.value = '';
        }
        if (testCaseInput) {
            testCaseInput.value = '';
        }
        addLogEntry('已清空测试模块和测试用例', 'info');

        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi' || state.fileBrowser.mode === 'gsi-system') {
        // For GSI system image, use the selected path directly
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            targetInput.value = fullPath;
            state.gsiSystemFile = null;
            state.gsiSystemWorkerSource = null;
            addLogEntry(`已选择System镜像: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi-system-worker') {
        // 集群模式：选择 Worker 主机上的 System 镜像，记录来源 Worker。
        if (isDirectory) {
            showToast('请选择一个镜像文件，而非文件夹', 'warning');
            return;
        }
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            targetInput.value = fullPath;
            state.gsiSystemWorkerSource = {
                worker_id: state.fileBrowser.workerBrowseId,
                path: fullPath,
            };
            state.gsiSystemFile = null;
            addLogEntry(`已选择 Worker ${state.fileBrowser.workerBrowseId} 上的System镜像: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi-script') {
        // For GSI script, use the selected path directly
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择GSI脚本: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi-vendor') {
        // For GSI vendor image, use the selected path directly
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            targetInput.value = fullPath;
            state.gsiVendorFile = null;
            const localVendorInput = document.getElementById('gsi-vendor-file-input');
            if (localVendorInput) {
                localVendorInput.value = '';
            }
            addLogEntry(`已选择Vendor镜像: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'firmware') {
        // For firmware, use the selected path directly
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            targetInput.value = fullPath;
            const localFirmwareInput = document.getElementById('firmware-file-input');
            if (localFirmwareInput) {
                localFirmwareInput.value = '';
            }
            addLogEntry(`已选择固件文件: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'firmware-share') {
        if (isDirectory) {
            showToast('请选择一个固件文件，而非文件夹', 'warning');
            return;
        }
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            const defaults = firmwareShareDefaults();
            const user = state.fileBrowser.remoteUser || defaults.user;
            const host = state.fileBrowser.remoteHost || defaults.host;
            if (!user || !host) {
                showToast('共享固件主机配置不完整', 'error');
                return;
            }
            targetInput.value = `${user}@${host}:${fullPath}`;
            addLogEntry(`已选择共享固件: ${targetInput.value}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'local-suite') {
        // 添加本地测试套件：必须是已解压的目录
        if (!isDirectory) {
            showToast('请选择一个目录（已解压的测试套件），而非文件', 'warning');
            return;
        }
        fullPath = state.fileBrowser.currentPath + '/';
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择测试套件目录: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'utility-tool') {
        if (isDirectory) {
            showToast('请选择一个文件，而非文件夹', 'warning');
            return;
        }
        fullPath = state.fileBrowser.currentPath
            ? state.fileBrowser.currentPath + '/' + selectedItem.name
            : selectedItem.name;
        if (targetInput) {
            targetInput.value = fullPath;
        }
        closeFileBrowserModal();
    } else {
        // Default behavior
        fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择文件: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    }
}

// Navigate to parent directory
function navigateToParent() {
    const currentPath = state.fileBrowser.currentPath;

    if (state.fileBrowser.mode === 'utility-tool') {
        if (!currentPath || !currentPath.includes('/')) {
            showToast('已到达 tools/ 根目录', 'info');
            return;
        }
        const parentPath = currentPath.substring(0, currentPath.lastIndexOf('/'));
        ut_loadToolDir(parentPath);
        return;
    }

    if (currentPath === '/' || !currentPath.includes('/')) {
        showToast('已到达根目录', 'info');
        return;  // Already at root
    }

    const parentPath = currentPath.substring(0, currentPath.lastIndexOf('/')) || '/';
    loadFileDirectory(parentPath);
}

// Navigate to root directory
function navigateToRoot() {
    if (state.fileBrowser.mode === 'utility-tool') {
        ut_loadToolDir('');
        return;
    }

    if (state.fileBrowser.mode === 'gsi-system-worker') {
        loadFileDirectory('');
        addLogEntry('导航到 Worker 套件目录 (GMS-Suite)', 'info');
        return;
    }

    if (state.fileBrowser.mode === 'retry' && state.fileBrowser.clusterWorkerId) {
        loadFileDirectory('results');
        addLogEntry('导航到 Worker 测试报告目录: results', 'info');
        return;
    }

    const rootPath = getDefaultSuitesPath();

    // Always navigate to GMS-Suite root directory
    loadFileDirectory(rootPath);
    addLogEntry(`导航到根目录: ${rootPath}`, 'info');
}

// Refresh current directory
function refreshCurrentDirectory() {
    const currentPath = state.fileBrowser.currentPath;
    if (state.fileBrowser.mode === 'utility-tool') {
        ut_loadToolDir(currentPath || '');
        return;
    }
    if (currentPath) {
        loadFileDirectory(currentPath);
        addLogEntry(`刷新目录: ${currentPath}`, 'info');
    } else {
        showToast('没有可刷新的目录', 'warning');
    }
}
