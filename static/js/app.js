// ==================== Global State ====================
const state = {
    connected: false,
    testing: false,
    devices: [],
    selectedDevices: new Set(),
    socket: null,
    sshConnected: false,
    vpnConnected: false,
    adbForwardRunning: false,
    usbipConnected: false,
    config: null,  // Will store configuration from server
    fileBrowser: {
        currentPath: '',
        selectedFile: null,
        targetInputId: null,  // 'suite-path' or 'retry-result' or 'gsi-system'
        mode: null  // 'suite' or 'retry' or 'gsi'
    }
};

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', async () => {
    initSocket();
    initEventListeners();
    await loadConfig();
    loadDevices();
    initDragDrop();
    await checkInitialTestStatus();
    startStatusPolling();
    checkVpnStatus();
});

// ==================== Configuration ====================
async function loadConfig() {
    try {
        const config = await apiCall('/api/config', 'GET');
        state.config = config;
    } catch (error) {
        console.error('Failed to load config:', error);
        state.config = { ubuntu_user: 'hcq' };  // Fallback
    }
}

// ==================== Socket.IO Connection ====================
function initSocket() {
    state.socket = io();

    state.socket.on('connect', () => {
        console.log('Connected to server');
        updateConnectionStatus(true);
    });

    state.socket.on('disconnect', () => {
        console.log('Disconnected from server');
        updateConnectionStatus(false);
    });

    state.socket.on('log_update', (data) => {
        addLogEntry(data.log, data.type || 'info');
    });

    state.socket.on('devices_updated', (devices) => {
        state.devices = devices;
        renderDevices();
    });

    state.socket.on('test_complete', () => {
        state.testing = false;
        updateTestToggleButton(false);
        addLogEntry('测试完成', 'success');
        showToast('测试完成', 'success');
    });

    state.socket.on('vpn_status_update', (data) => {
        updateVpnStatus(data.connected);
    });

    state.socket.on('upload_progress', (data) => {
        // Handled in upload handler
    });

    state.socket.on('terminal_data', (data) => {
        // Terminal data - handled in terminal.html
    });

    state.socket.on('terminal_error', (data) => {
        // Terminal error - handled in terminal.html
    });

    state.socket.on('terminal_connected', () => {
        // Terminal connected - handled in terminal.html
    });
}

// ==================== Event Listeners ====================
function initEventListeners() {
    // Test type change
    document.getElementById('test-type').addEventListener('change', onTestTypeChange);

    // Test module/case input
    document.getElementById('test-module').addEventListener('input', onInputChange);
    document.getElementById('test-case').addEventListener('input', onInputChange);
    document.getElementById('retry-result').addEventListener('input', onInputChange);

    // Device host and local server confirm on Enter
    document.getElementById('device-host').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') onDeviceHostConfirm();
    });
    document.getElementById('local-server').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') onLocalServerConfirm();
    });
}

// ==================== Input Change Handlers ====================
function onInputChange() {
    // Handle mutual exclusivity between test module, test case, and retry report
    const testModule = document.getElementById('test-module').value.trim();
    const testCase = document.getElementById('test-case').value.trim();
    const retryResult = document.getElementById('retry-result').value.trim();

    // If typing in retry-result, clear module and case
    if (document.activeElement.id === 'retry-result' && retryResult) {
        document.getElementById('test-module').value = '';
        document.getElementById('test-case').value = '';
    }
    // If typing in module or case, clear retry-result
    else if ((document.activeElement.id === 'test-module' || document.activeElement.id === 'test-case') && (testModule || testCase)) {
        document.getElementById('retry-result').value = '';
    }
}

function onTestTypeChange() {
    const testType = document.getElementById('test-type').value;
    addLogEntry(`测试类型已更改为: ${testType}`, 'info');
}

function onDeviceHostConfirm() {
    const deviceHost = document.getElementById('device-host').value.trim();
    addLogEntry(`设备主机地址已更新: ${deviceHost}`, 'info');
    showToast('设备主机地址已更新', 'success');
    // Save to backend
    apiCall('/api/config', 'POST', { device_host: deviceHost });
}

function onLocalServerConfirm() {
    const localServer = document.getElementById('local-server').value.trim();
    addLogEntry(`本地主机地址已更新: ${localServer}`, 'info');
    showToast('本地主机地址已更新', 'success');
    // Save to backend
    apiCall('/api/config', 'POST', { local_server: localServer });
}

// ==================== Drag and Drop ====================
function initDragDrop() {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('local-file');
    const dropZoneText = document.getElementById('drop-zone-text');
    const dropZoneFilename = document.getElementById('drop-zone-filename');

    // Click to select file
    dropZone.addEventListener('click', () => {
        fileInput.click();
    });

    // File input change handler
    fileInput.addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) {
            dropZoneText.style.display = 'none';
            dropZoneFilename.textContent = `📄 ${file.name}`;
            dropZoneFilename.style.display = 'block';
            addLogEntry(`已选择文件: ${file.name}`, 'info');
        }
    });

    // Drag over
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('drag-over');
    });

    // Drag leave
    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');
    });

    // Drop
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');

        const files = e.dataTransfer.files;
        if (files.length > 0) {
            // Set file to input
            const dataTransfer = new DataTransfer();
            dataTransfer.items.add(files[0]);
            fileInput.files = dataTransfer.files;

            // Update UI
            dropZoneText.style.display = 'none';
            dropZoneFilename.textContent = `📄 ${files[0].name}`;
            dropZoneFilename.style.display = 'block';
            addLogEntry(`已选择文件: ${files[0].name}`, 'info');
        }
    });
}

// ==================== API Calls ====================
async function apiCall(url, method = 'GET', data = null) {
    try {
        const options = {
            method,
            headers: {
                'Content-Type': 'application/json'
            }
        };

        // Only add body for POST/PUT/PATCH/DELETE methods (not GET/HEAD)
        if (data && !['GET', 'HEAD'].includes(method.toUpperCase())) {
            options.body = JSON.stringify(data);
        }

        const response = await fetch(url, options);
        const result = await response.json();

        if (!response.ok) {
            const error = new Error(result.error || 'Request failed');
            // Attach additional fields from error response
            if (result.need_password) {
                error.needPassword = true;
                error.suppressToast = true; // Don't show toast for password prompt
            }
            if (result.device_host) error.deviceHost = result.device_host;
            throw error;
        }

        return result;
    } catch (error) {
        console.error('API Error:', error);
        // Only show toast if not suppressed (e.g., for password prompt)
        if (!error.suppressToast) {
            showToast(error.message, 'error');
        }
        throw error;
    }
}

// ==================== Device Management ====================
async function loadDevices() {
    try {
        const devices = await apiCall('/api/devices');
        state.devices = devices;
        renderDevices();
        addLogEntry(`已刷新设备列表，找到 ${devices.length} 台设备`, 'info');

        // 检查 USB/IP 连接状态
        try {
            const status = await apiCall('/api/usbip/status', 'GET');
            const usbipBtn = document.getElementById('usbip-btn');
            if (usbipBtn) {
                state.usbipConnected = status.connected;
                usbipBtn.textContent = status.connected ? '📱 断开设备' : '📱 本地设备';
            }
        } catch (error) {
            console.error('Failed to check USB/IP status:', error);
        }
    } catch (error) {
        addLogEntry('加载设备列表失败: ' + error.message, 'error');
    }
}

function renderDevices() {
    const leftContainer = document.getElementById('device-list-left');
    const rightContainer = document.getElementById('device-list-right');

    if (state.devices.length === 0) {
        leftContainer.innerHTML = '<div class="empty-message">点击刷新按钮获取设备列表...</div>';
        rightContainer.innerHTML = '';
        return;
    }

    // 将设备交替分配到左右两栏
    const leftDevices = [];
    const rightDevices = [];
    state.devices.forEach((device, index) => {
        // Handle both string device IDs and device objects
        const deviceId = typeof device === 'string' ? device : device.device_id;
        const isLocked = typeof device === 'object' && device.locked;
        const lockedBy = typeof device === 'object' ? device.locked_by : '';

        if (index % 2 === 0) {
            leftDevices.push({ deviceId, isLocked, lockedBy });
        } else {
            rightDevices.push({ deviceId, isLocked, lockedBy });
        }
    });

    // 渲染左侧栏
    leftContainer.innerHTML = leftDevices.map(({ deviceId, isLocked, lockedBy }) => `
        <div class="device-item ${state.selectedDevices.has(deviceId) ? 'selected' : ''} ${isLocked ? 'locked' : ''}"
             onclick="${isLocked ? '' : `toggleDevice('${deviceId}')`}"
             title="${isLocked ? `已被 ${lockedBy} 占用` : '点击选择设备'}">
            <input type="checkbox"
                   class="device-checkbox"
                   ${state.selectedDevices.has(deviceId) ? 'checked' : ''}
                   ${isLocked ? 'disabled' : ''}
                   onclick="event.stopPropagation(); ${isLocked ? '' : `toggleDevice('${deviceId}')`}">
            <div class="device-info">
                <div class="device-id">${deviceId}</div>
                ${isLocked ? `<div class="lock-status">🔒 ${lockedBy}</div>` : ''}
            </div>
            <span class="device-status">${isLocked ? 'Allocated' : 'Available'}</span>
        </div>
    `).join('');

    // 渲染右侧栏
    if (rightDevices.length > 0) {
        rightContainer.innerHTML = rightDevices.map(({ deviceId, isLocked, lockedBy }) => `
            <div class="device-item ${state.selectedDevices.has(deviceId) ? 'selected' : ''} ${isLocked ? 'locked' : ''}"
                 onclick="${isLocked ? '' : `toggleDevice('${deviceId}')`}"
                 title="${isLocked ? `已被 ${lockedBy} 占用` : '点击选择设备'}">
                <input type="checkbox"
                       class="device-checkbox"
                       ${state.selectedDevices.has(deviceId) ? 'checked' : ''}
                       ${isLocked ? 'disabled' : ''}
                       onclick="event.stopPropagation(); ${isLocked ? '' : `toggleDevice('${deviceId}')`}">
                <div class="device-info">
                    <div class="device-id">${deviceId}</div>
                    ${isLocked ? `<div class="lock-status">🔒 ${lockedBy}</div>` : ''}
                </div>
                <span class="device-status">${isLocked ? 'Allocated' : 'Available'}</span>
            </div>
        `).join('');
    } else {
        rightContainer.innerHTML = '';
    }
}

function toggleDevice(deviceId) {
    if (state.selectedDevices.has(deviceId)) {
        state.selectedDevices.delete(deviceId);
    } else {
        state.selectedDevices.add(deviceId);
    }
    renderDevices();
}

async function refreshDevices() {
    await loadDevices();
    showToast('正在刷新设备列表...', 'info');
}

function selectAllDevices() {
    if (state.selectedDevices.size === state.devices.length) {
        // Deselect all
        state.selectedDevices.clear();
    } else {
        // Select all - handle both string and object device formats
        state.devices.forEach(device => {
            // Extract device_id from object or use string directly
            const deviceId = typeof device === 'string' ? device : device.device_id;
            state.selectedDevices.add(deviceId);
        });
    }
    renderDevices();
    addLogEntry(`已选择 ${state.selectedDevices.size} 台设备`, 'info');
}

async function rebootDevices() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要重启的设备', 'warning');
        return;
    }

    if (!confirm(`确定要重启选中的 ${state.selectedDevices.size} 台设备吗？`)) {
        return;
    }

    try {
        await apiCall('/api/devices/reboot', 'POST', {
            devices: Array.from(state.selectedDevices)
        });
        addLogEntry(`正在重启 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('设备正在重启', 'success');
    } catch (error) {
        addLogEntry('重启设备失败: ' + error.message, 'error');
    }
}

async function remountDevices() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return;
    }

    try {
        await apiCall('/api/devices/remount', 'POST', {
            devices: Array.from(state.selectedDevices)
        });
        addLogEntry('正在执行 remount...', 'info');
    } catch (error) {
        addLogEntry('Remount 失败: ' + error.message, 'error');
    }
}

async function connectWifi() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return;
    }

    // Show WiFi configuration modal
    const modal = document.getElementById('wifi-modal');
    modal.classList.add('show');
}

function closeWifiModal() {
    const modal = document.getElementById('wifi-modal');
    modal.classList.remove('show');
}

async function submitWifiConfig() {
    const ssid = document.getElementById('wifi-ssid').value.trim();
    const password = document.getElementById('wifi-password').value.trim();

    if (!ssid || !password) {
        showToast('SSID 和密码不能为空', 'error');
        return;
    }

    try {
        await apiCall('/api/devices/connect-wifi', 'POST', {
            devices: Array.from(state.selectedDevices),
            ssid: ssid,
            password: password
        });
        addLogEntry(`正在连接 Wi-Fi (${ssid})...`, 'info');
        showToast('正在连接 Wi-Fi...', 'info');
        closeWifiModal();
    } catch (error) {
        addLogEntry('连接 WiFi 失败: ' + error.message, 'error');
    }
}

async function lockSelectedDevices(action) {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return;
    }

    try {
        await apiCall('/api/devices/lock', 'POST', {
            devices: Array.from(state.selectedDevices),
            action: action
        });
        addLogEntry(`正在${action === 'lock' ? '锁定' : '解锁'}设备...`, 'info');
    } catch (error) {
        addLogEntry(`${action === 'lock' ? '锁定' : '解锁'}设备失败: ` + error.message, 'error');
    }
}

async function checkDeviceLockStatus() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return;
    }

    try {
        const result = await apiCall('/api/devices/lock-status', 'POST', {
            devices: Array.from(state.selectedDevices)
        });
        addLogEntry('设备锁定状态: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('获取锁定状态失败: ' + error.message, 'error');
    }
}

async function collectDeviceInfo() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return;
    }

    try {
        const result = await apiCall('/api/devices/info', 'POST', {
            devices: Array.from(state.selectedDevices)
        });
        addLogEntry('设备信息: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('获取设备信息失败: ' + error.message, 'error');
    }
}

// ==================== VNC & Remote Control ====================
async function burnFirmware() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写固件的设备', 'warning');
        return;
    }

    // Set default paths based on config
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    const miscInput = document.getElementById('firmware-misc');
    if (miscInput && !miscInput.value) {
        miscInput.value = `/home/${defaultUser}/GMS-Suite/misc.img`;
    }

    // Show firmware configuration modal
    const modal = document.getElementById('firmware-modal');
    modal.classList.add('show');
}

function closeFirmwareModal() {
    const modal = document.getElementById('firmware-modal');
    modal.classList.remove('show');
}

async function submitFirmwareBurn() {
    const systemImg = document.getElementById('firmware-system').value.trim();
    const vendorImg = document.getElementById('firmware-vendor').value.trim();
    const miscImg = document.getElementById('firmware-misc').value.trim();

    if (!systemImg) {
        showToast('System 镜像路径不能为空', 'error');
        return;
    }

    try {
        addLogEntry(`正在烧写固件到 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('正在烧写固件...', 'info');

        const result = await apiCall('/api/firmware/burn', 'POST', {
            devices: Array.from(state.selectedDevices),
            system_img: systemImg,
            vendor_img: vendorImg,
            misc_img: miscImg
        });

        if (result.success) {
            const successCount = result.results.filter(r => r.success).length;
            const failCount = result.results.length - successCount;

            addLogEntry(`固件烧写完成: 成功 ${successCount} 台, 失败 ${failCount} 台`, successCount === result.results.length ? 'success' : 'warning');

            if (failCount > 0) {
                result.results.forEach(r => {
                    if (!r.success) {
                        addLogEntry(`  ${r.device}: ${r.error || '未知错误'}`, 'error');
                    }
                });
            }

            showToast(`固件烧写完成 (成功: ${successCount}, 失败: ${failCount})`, successCount === result.results.length ? 'success' : 'warning');
        }

        closeFirmwareModal();
    } catch (error) {
        addLogEntry('烧写固件失败: ' + error.message, 'error');
    }
}

async function burnGsiImage() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写GSI的设备', 'warning');
        return;
    }

    // Set default paths based on config
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    const scriptInput = document.getElementById('gsi-script');
    if (scriptInput && !scriptInput.value) {
        scriptInput.value = `/home/${defaultUser}/GMS-Suite/run_GSI_Burn.sh`;
    }

    // Show GSI configuration modal
    const modal = document.getElementById('gsi-modal');
    modal.classList.add('show');
}

function closeGsiModal() {
    const modal = document.getElementById('gsi-modal');
    modal.classList.remove('show');
}

async function browseRemoteFileForGsi() {
    const targetInputId = 'gsi-system';
    const title = '选择System镜像';

    // Set file browser state
    state.fileBrowser.mode = 'gsi';
    state.fileBrowser.targetInputId = targetInputId;
    state.fileBrowser.selectedFile = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    const modal = document.getElementById('file-browser-modal');
    modal.classList.add('show');

    // Load initial directory (user home)
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    await loadFileDirectory(`/home/${defaultUser}`);
}

function browseLocalFileForVendor() {
    // For vendor boot image, we can use a local file input
    const input = document.createElement('input');
    input.type = 'file';
    input.onchange = (e) => {
        const file = e.target.files[0];
        if (file) {
            // For local files, we'll need to upload them first
            document.getElementById('gsi-vendor').value = file.path || file.name;
            showToast(`已选择: ${file.name}`, 'info');
        }
    };
    input.click();
}

// Browse remote file for firmware inputs (uses remote file browser modal)
async function browseRemoteFileForFirmware(targetInputId) {
    const title = '选择镜像文件';

    // Set file browser state
    state.fileBrowser.mode = 'firmware';
    state.fileBrowser.targetInputId = targetInputId;
    state.fileBrowser.selectedFile = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    const modal = document.getElementById('file-browser-modal');
    modal.classList.add('show');

    // Load initial directory (user home)
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    await loadFileDirectory(`/home/${defaultUser}`);
}

// Browse local file for firmware inputs (uses native file picker)
function browseLocalFileForFirmware(targetInputId) {
    const input = document.createElement('input');
    input.type = 'file';
    input.onchange = (e) => {
        const file = e.target.files[0];
        if (file) {
            const target = document.getElementById(targetInputId);
            if (target) {
                // For local selection, set file name; actual upload handled elsewhere
                target.value = file.path || file.name;
                showToast(`已选择本地文件: ${file.name}`, 'info');
            }
        }
    };
    input.click();
}

async function submitGsiBurn() {
    const scriptPath = document.getElementById('gsi-script').value.trim();
    const systemImg = document.getElementById('gsi-system').value.trim();
    const vendorImg = document.getElementById('gsi-vendor').value.trim();

    if (!systemImg) {
        showToast('System 镜像路径不能为空', 'error');
        return;
    }

    try {
        addLogEntry(`正在烧写GSI到 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('正在烧写GSI...', 'info');

        const result = await apiCall('/api/gsi/burn', 'POST', {
            devices: Array.from(state.selectedDevices),
            system_img: systemImg,
            vendor_img: vendorImg,
            script_path: scriptPath
        });

        if (result.success) {
            const successCount = result.results.filter(r => r.success).length;
            const failCount = result.results.length - successCount;

            addLogEntry(`GSI烧写完成: 成功 ${successCount} 台, 失败 ${failCount} 台`, successCount === result.results.length ? 'success' : 'warning');

            if (failCount > 0) {
                result.results.forEach(r => {
                    if (!r.success) {
                        addLogEntry(`  ${r.device}: ${r.error || '未知错误'}`, 'error');
                    }
                });
            }

            showToast(`GSI烧写完成 (成功: ${successCount}, 失败: ${failCount})`, successCount === result.results.length ? 'success' : 'warning');
        }

        closeGsiModal();
    } catch (error) {
        addLogEntry('烧写GSI失败: ' + error.message, 'error');
    }
}

async function burnSerialNumber() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写SN码的设备', 'warning');
        return;
    }

    // Show SN configuration modal
    const modal = document.getElementById('sn-modal');
    modal.classList.add('show');
}

function closeSnModal() {
    const modal = document.getElementById('sn-modal');
    modal.classList.remove('show');
}

async function submitSnBurn() {
    const snCode = document.getElementById('sn-code').value.trim();

    if (!snCode) {
        showToast('SN码不能为空', 'error');
        return;
    }

    try {
        addLogEntry(`正在烧写SN码到 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('正在烧写SN码...', 'info');

        const result = await apiCall('/api/sn/burn', 'POST', {
            devices: Array.from(state.selectedDevices),
            sn_code: snCode
        });

        if (result.success) {
            const successCount = result.results.filter(r => r.success).length;
            const failCount = result.results.length - successCount;

            addLogEntry(`SN码烧写完成: 成功 ${successCount} 台, 失败 ${failCount} 台`, successCount === result.results.length ? 'success' : 'warning');

            if (failCount > 0) {
                result.results.forEach(r => {
                    if (!r.success) {
                        addLogEntry(`  ${r.device}: ${r.error || '未知错误'}`, 'error');
                    }
                });
            }

            showToast(`SN码烧写完成 (成功: ${successCount}, 失败: ${failCount})`, successCount === result.results.length ? 'success' : 'warning');
        }

        closeSnModal();
    } catch (error) {
        addLogEntry('烧写SN码失败: ' + error.message, 'error');
    }
}

async function initAndStartVnc() {
    try {
        const result = await apiCall('/api/vnc/start', 'POST');
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
        addLogEntry('正在检查 VNC 服务...', 'info');
        await initAndStartVnc();

        addLogEntry('正在启动屏幕投屏...', 'info');
        const result = await apiCall('/api/screen/start', 'POST', {
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

// ==================== SSH Key Setup ====================
async function setupSshKey() {
    try {
        addLogEntry('正在设置 SSH 密钥认证...', 'info');
        const result = await apiCall('/api/ssh/setup-key', 'POST');

        if (result.success) {
            addLogEntry('SSH 密钥认证已设置成功', 'success');
            addLogEntry(`公钥: ${result.public_key}`, 'info');
            addLogEntry(`密钥路径: ${result.key_path}`, 'info');
            showToast('SSH 密钥认证设置成功', 'success');
        }
    } catch (error) {
        addLogEntry('设置 SSH 密钥失败: ' + error.message, 'error');
    }
}

async function setupAdbPortForward() {
    const btn = document.getElementById('adb-forward-btn');
    if (state.adbForwardRunning) {
        try {
            await apiCall('/api/adb-forward/stop', 'POST');
            state.adbForwardRunning = false;
            btn.textContent = '🔌 端口转发';
            addLogEntry('ADB 端口转发已停止', 'info');
        } catch (error) {
            addLogEntry('停止端口转发失败: ' + error.message, 'error');
        }
    } else {
        try {
            await apiCall('/api/adb-forward/start', 'POST');
            state.adbForwardRunning = true;
            btn.textContent = '🔌 停止转发';
            addLogEntry('ADB 端口转发已启动', 'success');
        } catch (error) {
            addLogEntry('启动端口转发失败: ' + error.message, 'error');
        }
    }
}

async function setupUsbipForward() {
    const btn = document.getElementById('usbip-btn');
    if (state.usbipConnected) {
        try {
            const result = await apiCall('/api/usbip/stop', 'POST', {});
            state.usbipConnected = false;
            btn.textContent = '📱 本地设备';
            addLogEntry(result.message || '本地设备已断开', 'success');
            // 刷新设备列表
            setTimeout(() => loadDevices(), 2500);
        } catch (error) {
            addLogEntry('停止 USB/IP 失败: ' + error.message, 'error');
        }
    } else {
        try {
            const result = await apiCall('/api/usbip/start', 'POST', {});
            state.usbipConnected = true;
            btn.textContent = '📱 断开设备';
            addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
            // 刷新设备列表
            setTimeout(() => loadDevices(), 3500);
        } catch (error) {
            // 检查是否需要密码
            if (error.needPassword) {
                // 使用当前客户端信息而不是后端返回的deviceHost
                const deviceHost = (typeof clientInfo !== 'undefined' && clientInfo.username && clientInfo.username !== 'unknown')
                    ? `${clientInfo.username}@${clientInfo.ip}`
                    : error.deviceHost || '';
                showDevicePasswordModal(deviceHost);
                return;
            }
            addLogEntry('启动 USB/IP 失败: ' + error.message, 'error');
        }
    }
}

// ==================== 设备主机密码输入 ====================
function showDevicePasswordModal(deviceHost) {
    document.getElementById('device-host-display').value = deviceHost;
    document.getElementById('device-pswd').value = '';
    const modal = document.getElementById('device-password-modal');
    modal.classList.add('show');
    document.getElementById('device-pswd').focus();

    // Add ESC key listener to close modal
    document.addEventListener('keydown', handleDevicePasswordEsc);
}

function closeDevicePasswordModal() {
    const modal = document.getElementById('device-password-modal');
    modal.classList.remove('show');

    // Remove ESC key listener
    document.removeEventListener('keydown', handleDevicePasswordEsc);
}

function handleDevicePasswordEsc(event) {
    if (event.key === 'Escape') {
        closeDevicePasswordModal();
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
    if (!password) {
        showToast('请输入密码', 'warning');
        return;
    }

    try {
        const result = await apiCall('/api/usbip/start', 'POST', {
            device_password: password
        });
        state.usbipConnected = true;
        document.getElementById('usbip-btn').textContent = '📱 断开设备';
        addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
        closeDevicePasswordModal();
        // 刷新设备列表
        setTimeout(() => loadDevices(), 3500);
    } catch (error) {
        addLogEntry('启动 USB/IP 失败: ' + error.message, 'error');
    }
}

// ==================== VPN Control ====================
async function checkSshd() {
    try {
        const result = await apiCall('/api/vpn/check-sshd', 'GET');
        addLogEntry(`SSHD 状态: ${result.running ? '运行中' : '未运行'}`, result.running ? 'success' : 'warning');
    } catch (error) {
        addLogEntry('检查 SSHD 失败: ' + error.message, 'error');
    }
}

async function checkRouting() {
    try {
        const result = await apiCall('/api/vpn/check-routing', 'GET');
        addLogEntry('路由检查: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('检查路由失败: ' + error.message, 'error');
    }
}

async function connectVpn() {
    const btn = document.getElementById('vpn-connect-btn');
    if (state.vpnConnected) {
        try {
            await apiCall('/api/vpn/disconnect', 'POST');
            state.vpnConnected = false;
            btn.textContent = '🔌 连接VPN';
            addLogEntry('VPN 已断开', 'info');
        } catch (error) {
            addLogEntry('断开 VPN 失败: ' + error.message, 'error');
        }
    } else {
        try {
            await apiCall('/api/vpn/connect', 'POST');
            state.vpnConnected = true;
            btn.textContent = '🔌 断开VPN';
            addLogEntry('VPN 已连接', 'success');
        } catch (error) {
            addLogEntry('连接 VPN 失败: ' + error.message, 'error');
        }
    }
}

async function checkVpnStatus() {
    try {
        const result = await apiCall('/api/vpn/status', 'GET');
        updateVpnStatus(result.connected);
        addLogEntry(`VPN 状态: ${result.connected ? '已连接' : '未连接'}`, result.connected ? 'success' : 'warning');
    } catch (error) {
        addLogEntry('检查 VPN 状态失败: ' + error.message, 'error');
    }
}

function updateVpnStatus(connected) {
    const label = document.getElementById('vpn-status-label');
    const btn = document.getElementById('vpn-connect-btn');

    if (connected) {
        label.textContent = '状态: 已连接';
        label.className = 'vpn-status-label connected';
        btn.textContent = '🔌 断开VPN';
        state.vpnConnected = true;
    } else {
        label.textContent = '状态: 未连接';
        label.className = 'vpn-status-label disconnected';
        btn.textContent = '🔌 连接VPN';
        state.vpnConnected = false;
    }
}

// ==================== File Upload ====================
async function handleUploadFile() {
    const fileInput = document.getElementById('local-file');
    const file = fileInput.files[0];

    if (!file) {
        showToast('请先选择要上传的文件', 'warning');
        return;
    }

    try {
        addLogEntry(`正在上传文件: ${file.name}`, 'info');
        const progressFill = document.getElementById('upload-progress-fill');
        const progressInfo = document.getElementById('progress-info');
        const startTime = Date.now();

        // Create FormData
        const formData = new FormData();
        formData.append('file', file);

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
                progressInfo.textContent = `上传中... ${percentage}% (${transferred}/${total}) ${speed}`;
            }
        });

        xhr.addEventListener('load', () => {
            if (xhr.status === 200) {
                const response = JSON.parse(xhr.responseText);
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
        xhr.open('POST', '/api/upload/file');
        xhr.send(formData);
    } catch (error) {
        addLogEntry('文件上传失败: ' + error.message, 'error');
        document.getElementById('upload-progress-fill').style.width = '0%';
    }
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// ==================== Browse Remote File ====================
async function browseRemoteFile(mode) {
    const targetInputId = mode === 'suite' ? 'suite-path' : 'retry-result';
    const title = mode === 'suite' ? '选择测试套件' : '选择测试报告';

    // Set file browser state
    state.fileBrowser.mode = mode;
    state.fileBrowser.targetInputId = targetInputId;
    state.fileBrowser.selectedFile = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    const modal = document.getElementById('file-browser-modal');
    modal.classList.add('show');

    // Load initial directory - use GMS-Suite for suite selection
    const defaultPath = mode === 'suite' ? `/home/${state.config?.ubuntu_user || 'hcq'}/GMS-Suite` : `/home/${state.config?.ubuntu_user || 'hcq'}`;
    await loadFileDirectory(defaultPath);
}

async function loadFileDirectory(path) {
    try {
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

function renderFileList(files) {
    const listContainer = document.getElementById('file-browser-list');
    const pathDisplay = document.getElementById('file-browser-current-path');

    // Update current path display
    pathDisplay.textContent = state.fileBrowser.currentPath;

    if (files.length === 0) {
        listContainer.innerHTML = '<div class="file-browser-item" style="cursor: default; color: var(--text-muted);">空目录</div>';
        return;
    }

    listContainer.innerHTML = files.map(file => {
        const icon = file.type === 'directory' ? '📁' : '📄';
        const sizeInfo = file.type === 'file' ? formatFileSize(file.size) : '';

        return `
            <div class="file-browser-item"
                 onclick="selectFileForSelection('${file.name}', '${file.type}')"
                 ondblclick="openFileOrDirectory('${file.name}', '${file.type}')">
                <span class="file-browser-icon">${icon}</span>
                <span class="file-browser-name">${file.name}</span>
                ${sizeInfo ? `<span style="color: var(--text-muted); font-size: 11px;">${sizeInfo}</span>` : ''}
            </div>
        `;
    }).join('');
}

function formatFileSize(bytes) {
    if (!bytes || bytes === '0') return '';
    const size = parseInt(bytes);
    if (size < 1024) return size + ' B';
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
    if (size < 1024 * 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + ' MB';
    return (size / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

function selectFileForSelection(name, type) {
    // Select file/directory (highlight it)
    state.fileBrowser.selectedFile = name;

    // Update UI to show selection
    document.querySelectorAll('.file-browser-item').forEach(item => {
        item.classList.remove('selected');
    });

    event.currentTarget.classList.add('selected');
}

function openFileOrDirectory(name, type) {
    if (type === 'directory') {
        // Navigate into directory
        const newPath = state.fileBrowser.currentPath === '/'
            ? `/${name}`
            : `${state.fileBrowser.currentPath}/${name}`;
        loadFileDirectory(newPath);
    } else {
        // For files, just select them
        selectFileForSelection(name, type);
    }
}

function selectFile(name, type) {
    if (type === 'directory') {
        // Navigate into directory
        const newPath = state.fileBrowser.currentPath === '/'
            ? `/${name}`
            : `${state.fileBrowser.currentPath}/${name}`;
        loadFileDirectory(newPath);
    } else {
        // Select file
        state.fileBrowser.selectedFile = name;

        // Update UI to show selection
        document.querySelectorAll('.file-browser-item').forEach(item => {
            item.classList.remove('selected');
        });

        event.currentTarget.classList.add('selected');
    }
}

function closeFileBrowserModal() {
    const modal = document.getElementById('file-browser-modal');
    modal.classList.remove('show');
    state.fileBrowser.selectedFile = null;
}

function confirmFileSelection() {
    if (!state.fileBrowser.selectedFile) {
        showToast('请先选择一个文件', 'warning');
        return;
    }

    const fullPath = `${state.fileBrowser.currentPath}/${state.fileBrowser.selectedFile}`;
    const targetInput = document.getElementById(state.fileBrowser.targetInputId);

    // For suite path selection, use auto-completion
    if (state.fileBrowser.mode === 'suite') {
        autoCompleteSuitePath(fullPath);
    } else if (state.fileBrowser.mode === 'retry') {
        // For retry result, use the selected path directly
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择测试报告: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi') {
        // For GSI system image, use the selected path directly
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择System镜像: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else {
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择${state.fileBrowser.mode === 'suite' ? '测试套件' : '文件'}: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    }
}

async function autoCompleteSuitePath(selectedPath) {
    const testType = document.getElementById('test-type').value;

    try {
        addLogEntry(`正在验证测试套件路径: ${selectedPath}`, 'info');

        const result = await apiCall('/api/test/autocomplete-suite', 'POST', {
            test_type: testType,
            base_path: selectedPath
        });

        if (result.success) {
            const suitePathInput = document.getElementById('suite-path');
            if (suitePathInput) {
                suitePathInput.value = result.path;

                if (result.autocompleted) {
                    addLogEntry(`✅ 测试套件路径已自动补齐: ${result.path}`, 'success');
                    if (result.binary) {
                        addLogEntry(`   测试二进制: ${result.binary}`, 'info');
                    }
                    showToast(`已自动补齐到: ${result.path}`, 'success');
                } else {
                    addLogEntry(`⚠️ ${result.warning || '未找到tools目录，使用原始路径'}`, 'warning');
                    showToast(`已选择: ${result.path}`, 'warning');
                }
            }
            closeFileBrowserModal();
        }
    } catch (error) {
        addLogEntry('自动补齐路径失败: ' + error.message, 'error');
        // Even if auto-completion fails, still use the selected path
        const suitePathInput = document.getElementById('suite-path');
        if (suitePathInput) {
            suitePathInput.value = selectedPath;
            addLogEntry(`使用选定路径: ${selectedPath}`, 'warning');
        }
        closeFileBrowserModal();
    }
}

// Navigate to parent directory
function navigateToParent() {
    const currentPath = state.fileBrowser.currentPath;
    if (currentPath === '/' || !currentPath.includes('/')) {
        return;  // Already at root
    }

    const parentPath = currentPath.substring(0, currentPath.lastIndexOf('/')) || '/';
    loadFileDirectory(parentPath);
}

// ==================== Test Control ====================
async function toggleTest() {
    if (state.testing) {
        //await stopTest();
    } else {
        await startTest();
    }
}

async function startTest() {
    if (state.testing) {
        showToast('测试已在运行中', 'warning');
        return;
    }

    if (state.selectedDevices.size === 0) {
        showToast('请先选择要测试的设备', 'warning');
        return;
    }

    const testType = document.getElementById('test-type').value;
    const testModule = document.getElementById('test-module').value.trim();
    const testCase = document.getElementById('test-case').value.trim();
    const retryResult = document.getElementById('retry-result').value.trim();
    const suitePath = document.getElementById('suite-path')?.value?.trim() || '';

    if (!suitePath) {
        showToast('请先选择测试套件路径（点击"浏览"按钮）', 'warning');
        return;
    }

    try {
        await apiCall('/api/test/start', 'POST', {
            devices: Array.from(state.selectedDevices),
            test_type: testType,
            test_module: testModule,
            test_case: testCase,
            retry_dir: retryResult,
            test_suite: suitePath,
            local_server: state.config?.local_server || ''
        });

        state.testing = true;
        updateTestToggleButton(true);
        addLogEntry('测试已启动', 'success');
        showToast('测试已启动', 'success');
    } catch (error) {
        addLogEntry('启动测试失败: ' + error.message, 'error');
    }
}

async function stopTest() {
    if (!state.testing) {
        showToast('没有正在运行的测试', 'warning');
        return;
    }

    try {
        addLogEntry('⏹ 用户请求停止测试...', 'info');

        // Kill tradefed processes
        await apiCall('/api/test/kill-tradefed', 'POST');

        // Update test state
        state.testing = false;
        updateTestToggleButton(false);

        addLogEntry('测试已停止', 'warning');
        showToast('测试已停止', 'warning');

        // Refresh devices
        await refreshDevices();
    } catch (error) {
        addLogEntry('停止测试失败: ' + error.message, 'error');
    }
}

function updateTestToggleButton(isTesting) {
    const btn = document.getElementById('test-toggle-btn');
    if (!btn) return;

    if (isTesting) {
        btn.textContent = '⏹ 停止测试';
        btn.className = 'btn-danger btn-lg';
    } else {
        btn.textContent = '▶ 开始测试';
        btn.className = 'btn-primary btn-lg';
    }
}

async function cleanTest() {
    try {
        await apiCall('/api/test/clean', 'POST');
        const logOutput = document.getElementById('log-output');
        logOutput.innerHTML = '<div class="log-entry">[系统] 日志已清除</div>';
        addLogEntry('测试日志已清除', 'info');
    } catch (error) {
        addLogEntry('清除日志失败: ' + error.message, 'error');
    }
}

async function downloadTestLog() {
    try {
        addLogEntry('正在保存日志...', 'info');

        // 获取当前日志区域的实际内容
        const logOutput = document.getElementById('log-output');
        const logContent = logOutput ? logOutput.innerText : '';

        if (!logContent.trim()) {
            showToast('没有可保存的日志内容', 'warning');
            return;
        }

        // 发送日志内容到后端保存
        const saveResult = await apiCall('/api/test/logs/save-current', 'POST', {
            content: logContent,
            test_type: state.testType || 'unknown'
        });

        if (saveResult.success) {
            addLogEntry(`✅ 日志已保存: ${saveResult.filename}`, 'success');

            // 然后触发下载
            const link = document.createElement('a');
            link.href = '/api/test/logs/download';
            link.download = saveResult.filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);

            showToast(`日志已保存: ${saveResult.filename}`, 'success');
        } else {
            throw new Error(saveResult.error || '保存失败');
        }
    } catch (error) {
        addLogEntry('保存日志失败: ' + error.message, 'error');
        showToast('保存日志失败: ' + error.message, 'error');
    }
}

async function showConfig() {
    const modal = document.getElementById('config-modal');
    const modalBody = document.getElementById('config-modal-body');

    // Fetch current config from API
    let config = {};
    try {
        config = await apiCall('/api/config', 'GET');
    } catch (error) {
        addLogEntry('获取配置失败: ' + error.message, 'error');
        return;
    }

    // Generate config form with actual values
    modalBody.innerHTML = `
        <div class="modal-form-row">
            <label>测试主机用户:</label>
            <input type="text" id="config-ubuntu-user" value="${config.ubuntu_user || ''}" />
        </div>
        <div class="modal-form-row">
            <label>测试主机地址:</label>
            <input type="text" id="config-ubuntu-host" value="${config.ubuntu_host || ''}" />
        </div>
        <div class="modal-form-row">
            <label>测试主机密码:</label>
            <input type="password" id="config-ubuntu-pswd" placeholder="输入测试主机SSH密码(留空保持不变)" />
        </div>
        <div class="modal-form-row">
            <label>设备主机地址:</label>
            <input type="text" id="config-device-host" value="${config.device_host || ''}" />
        </div>
        <div class="modal-form-row">
            <label>设备主机密码:</label>
            <input type="password" id="config-device-pswd" placeholder="输入设备主机SSH密码(留空保持不变)" />
        </div>
        <div class="modal-form-row">
            <label>本地主机地址:</label>
            <input type="text" id="config-local-server" value="${config.local_server || ''}" />
        </div>
        <div class="modal-form-row">
            <label>测试脚本路径:</label>
            <input type="text" id="config-script-path" class="readonly" value="${config.script_path || ''}" readonly />
        </div>
        <div class="modal-form-row">
            <label>测试套件路径:</label>
            <input type="text" id="config-suites-path" value="${config.suites_path || ''}" />
        </div>
        <div class="modal-buttons">
            <button class="btn-xxs" onclick="closeModal()">取消</button>
            <button class="btn-xxs btn-primary" onclick="saveConfig()">保存</button>
        </div>
    `;

    modal.classList.add('show');
}

function closeModal() {
    const modal = document.getElementById('config-modal');
    modal.classList.remove('show');
}

async function saveConfig() {
    const ubuntuPassword = document.getElementById('config-ubuntu-pswd').value;
    const devicePassword = document.getElementById('config-device-pswd').value;
    const config = {
        ubuntu_user: document.getElementById('config-ubuntu-user').value,
        ubuntu_host: document.getElementById('config-ubuntu-host').value,
        device_host: document.getElementById('config-device-host').value,
        local_server: document.getElementById('config-local-server').value,
        suites_path: document.getElementById('config-suites-path').value
    };

    // Only include passwords if they are not empty
    if (ubuntuPassword) {
        config.ubuntu_pswd = ubuntuPassword;
    }
    if (devicePassword) {
        config.device_pswd = devicePassword;
    }

    try {
        await apiCall('/api/config', 'POST', config);
        addLogEntry('配置已保存', 'success');
        showToast('配置保存成功', 'success');
        closeModal();
        // Reload page to update config values
        setTimeout(() => location.reload(), 500);
    } catch (error) {
        addLogEntry('保存配置失败: ' + error.message, 'error');
    }
}

// ==================== Logging ====================
function addLogEntry(message, type = 'info') {
    const logOutput = document.getElementById('log-output');
    const timestamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });

    const logEntry = document.createElement('div');
    logEntry.className = `log-entry log-${type}`;
    logEntry.textContent = `[${timestamp}] ${message}`;

    logOutput.appendChild(logEntry);
    logOutput.scrollTop = logOutput.scrollHeight;
}

// ==================== Status Polling ====================
function startStatusPolling() {
    setInterval(async () => {
        try {
            const status = await apiCall('/api/status');

            if (status.running && !state.testing) {
                state.testing = true;
                updateTestToggleButton(true);
            } else if (!status.running && state.testing) {
                state.testing = false;
                updateTestToggleButton(false);
            }

            // Update VPN status
            if (status.vpn_connected !== undefined) {
                updateVpnStatus(status.vpn_connected);
            }

            // Update logs if there are new ones
            if (status.logs && status.logs.length > 0) {
                const currentLogCount = document.querySelectorAll('.log-entry').length;
                if (status.logs.length > currentLogCount) {
                    status.logs.slice(currentLogCount).forEach(log => {
                        addLogEntry(log.message || log, log.type || 'info');
                    });
                }
            }
        } catch (error) {
            console.error('Status polling error:', error);
        }
    }, 2000);
}

async function checkInitialTestStatus() {
    try {
        const status = await apiCall('/api/status');
        state.testing = status.running;
        updateTestToggleButton(status.running);

        // Load existing logs if available
        if (status.logs && status.logs.length > 0) {
            const logOutput = document.getElementById('log-output');
            logOutput.innerHTML = '';
            status.logs.forEach(log => {
                addLogEntry(log.message || log, log.type || 'info');
            });
        }
    } catch (error) {
        console.error('Failed to check initial test status:', error);
    }
}

// ==================== UI Helpers ====================
function updateConnectionStatus(connected) {
    state.connected = connected;
    addLogEntry(connected ? '已连接到服务器' : '与服务器断开连接', connected ? 'success' : 'error');
}

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type} show`;

    // 根据消息类型自动调整显示时间
    const durationMap = {
        'success': 2000,  // 成功消息：2秒
        'info': 2500,     // 普通信息：2.5秒
        'warning': 3500,  // 警告消息：3.5秒
        'error': 5000     // 错误消息：5秒（需要更多时间阅读）
    };

    const duration = durationMap[type] || 3000;

    setTimeout(() => {
        toast.className = `toast ${type}`;
    }, duration);
}

// Close modal when clicking outside
window.onclick = function(event) {
    const configModal = document.getElementById('config-modal');
    const firmwareModal = document.getElementById('firmware-modal');
    const fileBrowserModal = document.getElementById('file-browser-modal');
    const gsiModal = document.getElementById('gsi-modal');
    const snModal = document.getElementById('sn-modal');
    if (event.target === configModal) {
        closeModal();
    }
    if (event.target === firmwareModal) {
        closeFirmwareModal();
    }
    if (event.target === fileBrowserModal) {
        closeFileBrowserModal();
    }
    if (event.target === gsiModal) {
        closeGsiModal();
    }
    if (event.target === snModal) {
        closeSnModal();
    }
}
