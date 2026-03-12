// 全局状态
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
    config: null,
    fileBrowser: { currentPath: '', selectedFile: null, targetInputId: null, mode: null },
    // 性能优化
    domCache: {},
    lastLogCount: 0,
    pendingDeviceRefresh: null,
    isRefreshingDevices: false
};

// 辅助函数
function validateDeviceSelection() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return false;
    }
    return true
}

// 性能优化工具
function $(id) {
    if (!state.domCache[id]) {
        state.domCache[id] = document.getElementById(id);
    }
    return state.domCache[id];
}

function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func(...args), wait);
    };
}

function throttle(func, limit) {
    let inThrottle;
    return function(...args) {
        if (!inThrottle) {
            func.apply(this, args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

// 模态框管理器
const ModalManager = {
    open(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) modal.classList.add('show');
    },

    close(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) modal.classList.remove('show');
    },

    closeAll() {
        document.querySelectorAll('.modal.show').forEach(m => m.classList.remove('show'));
    },

    toggle(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) modal.classList.toggle('show');
    },

    isOpen(modalId) {
        const modal = document.getElementById(modalId);
        return modal ? modal.classList.contains('show') : false;
    }
};

// 设备操作管理器
const DeviceOperation = {
    async execute(endpoint, operationName, data = {}, modalCloseFn = null) {
        if (!validateDeviceSelection()) return;

        try {
            if (modalCloseFn) modalCloseFn();
            addLogEntry(`正在${operationName}到 ${state.selectedDevices.size} 台设备...`, 'info');
            showToast(`正在${operationName}...`, 'info');

            const result = await apiCall(endpoint, 'POST', {
                devices: Array.from(state.selectedDevices),
                ...data
            });

            if (result.success) {
                this.handleResult(result, operationName);
            } else {
                addLogEntry(`${operationName}失败: ${result.error || '未知错误'}`, 'error');
                showToast(`${operationName}失败`, 'error');
            }
        } catch (error) {
            addLogEntry(`${operationName}失败: ${error.message}`, 'error');
            showToast(`${operationName}失败`, 'error');
        }
    },

    handleResult(result, operationName) {
        const results = result.results || [];
        const successCount = results.filter(r => r.success).length;
        const failCount = results.length - successCount;

        const logType = successCount === results.length ? 'success' : 'warning';
        addLogEntry(`${operationName}完成: 成功 ${successCount} 台, 失败 ${failCount} 台`, logType);

        if (failCount > 0) {
            results.forEach(r => {
                if (!r.success) {
                    addLogEntry(`  ${r.device}: ${r.error || '未知错误'}`, 'error');
                }
            });
        }

        showToast(`${operationName}完成 (成功: ${successCount}, 失败: ${failCount})`, logType);
    }
};

async function callDeviceApi(endpoint, additionalData = {}) {
    if (!validateDeviceSelection()) return;
    try {
        await apiCall(endpoint, 'POST', {
            devices: Array.from(state.selectedDevices),
            ...additionalData
        });
    } catch (error) {
        addLogEntry(`操作失败: ${error.message}`, 'error');
    }
}

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', async () => {
    initSocket();
    initEventListeners();

    // 立即检查USB/IP和VPN状态，避免按钮显示错误
    await Promise.all([
        checkUsbipStatus(),
        checkVpnStatus()
    ]);

    await loadConfig();
    loadDevices();
    initDragDrop();
    await checkInitialTestStatus();
    startStatusPolling();
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
    $('test-type').addEventListener('change', onTestTypeChange);

    // Test module/case input - 使用防抖优化
    const debouncedInputChange = debounce(onInputChange, 300);
    $('test-module').addEventListener('input', debouncedInputChange);
    $('test-case').addEventListener('input', debouncedInputChange);
    $('retry-result').addEventListener('input', debouncedInputChange);

    // Device host and local server confirm on Enter
    $('device-host').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') onDeviceHostConfirm();
    });
    $('local-server').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') onLocalServerConfirm();
    });
}

// ==================== Input Change Handlers ====================
function onInputChange() {
    // Handle mutual exclusivity between test module, test case, and retry report
    const testModule = $('test-module').value.trim();
    const testCase = $('test-case').value.trim();
    const retryResult = $('retry-result').value.trim();

    // If typing in retry-result, clear module and case
    if (document.activeElement.id === 'retry-result' && retryResult) {
        $('test-module').value = '';
        $('test-case').value = '';
    }
    // If typing in module or case, clear retry-result
    else if ((document.activeElement.id === 'test-module' || document.activeElement.id === 'test-case') && (testModule || testCase)) {
        $('retry-result').value = '';
    }
}

function onTestTypeChange() {
    const testType = $('test-type').value;
    addLogEntry(`测试类型已更改为: ${testType}`, 'info');
}

function onDeviceHostConfirm() {
    const deviceHost = $('device-host').value.trim();
    addLogEntry(`设备主机地址已更新: ${deviceHost}`, 'info');
    showToast('设备主机地址已更新', 'success');
    // Save to backend
    apiCall('/api/config', 'POST', { device_host: deviceHost });
}

function onLocalServerConfirm() {
    const localServer = $('local-server').value.trim();
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
            if (result.install_guide) error.installGuide = result.install_guide;
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
async function loadDevices(forceRefresh = false) {
    // 防止重复刷新
    if (state.isRefreshingDevices) {
        return;
    }

    state.isRefreshingDevices = true;

    try {
        // 添加 force_refresh 参数来强制绕过缓存
        const url = forceRefresh ? '/api/devices?force_refresh=1' : '/api/devices';
        const devices = await apiCall(url);
        state.devices = devices;
        renderDevices();

        // 显示设备信息，包含序列号
        let deviceInfo = `已刷新设备列表，找到 ${devices.length} 台设备`;
        if (devices.length > 0) {
            // 支持 device_id 和 serial 两种字段名
            const serials = devices.map(d => d.device_id || d.serial || '未知').filter(s => s).join(' ');
            if (serials) {
                deviceInfo += ` (${serials})`;
            }
        }
        addLogEntry(deviceInfo, 'info');

        // 不再自动检查 USB/IP 状态，避免覆盖连接状态
        // USB/IP 状态只在连接/断开操作时更新
    } catch (error) {
        addLogEntry('加载设备列表失败: ' + error.message, 'error');
    } finally {
        state.isRefreshingDevices = false;
    }
}

// 防抖版本的刷新函数
const debouncedRefreshDevices = debounce(() => loadDevices(false), 500);

function renderDevices() {
    const leftContainer = $('device-list-left');
    const rightContainer = $('device-list-right');

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

    // 使用DocumentFragment优化DOM操作
    const renderDeviceItem = ({ deviceId, isLocked, lockedBy }) => {
        const div = document.createElement('div');
        const isSelected = state.selectedDevices.has(deviceId);
        div.className = `device-item ${isSelected ? 'selected' : ''} ${isLocked ? 'locked' : ''}`;
        div.dataset.deviceId = deviceId;

        if (!isLocked) {
            div.onclick = () => toggleDevice(deviceId);
        }
        div.title = isLocked ? `已被 ${lockedBy} 占用` : '点击选择设备';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'device-checkbox';
        checkbox.checked = isSelected;
        if (isLocked) checkbox.disabled = true;
        if (!isLocked) {
            checkbox.onclick = (e) => {
                e.stopPropagation();
                toggleDevice(deviceId);
            };
        }

        const info = document.createElement('div');
        info.className = 'device-info';

        const idDiv = document.createElement('div');
        idDiv.className = 'device-id';
        idDiv.textContent = deviceId;
        info.appendChild(idDiv);

        if (isLocked) {
            const lockStatus = document.createElement('div');
            lockStatus.className = 'lock-status';
            lockStatus.textContent = `🔒 ${lockedBy}`;
            info.appendChild(lockStatus);
        }

        const status = document.createElement('span');
        status.className = 'device-status';
        status.textContent = isLocked ? 'Allocated' : 'Available';

        div.appendChild(checkbox);
        div.appendChild(info);
        div.appendChild(status);

        return div;
    };

    // 渲染左侧栏
    const leftFragment = document.createDocumentFragment();
    leftDevices.forEach(deviceInfo => {
        leftFragment.appendChild(renderDeviceItem(deviceInfo));
    });
    leftContainer.innerHTML = '';
    leftContainer.appendChild(leftFragment);

    // 渲染右侧栏
    const rightFragment = document.createDocumentFragment();
    rightDevices.forEach(deviceInfo => {
        rightFragment.appendChild(renderDeviceItem(deviceInfo));
    });
    rightContainer.innerHTML = '';
    rightContainer.appendChild(rightFragment);
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
    // 手动刷新时强制绕过缓存
    await loadDevices(true);
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
    if (!validateDeviceSelection()) return;
    if (!confirm(`确定要重启选中的 ${state.selectedDevices.size} 台设备吗？`)) return;

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
    await callDeviceApi('/api/devices/remount');
    addLogEntry('正在执行 remount...', 'info');
}

async function connectWifi() {
    if (!validateDeviceSelection()) return;
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
        // 立即关闭模态框
        closeWifiModal();

        addLogEntry(`正在连接 Wi-Fi (${ssid})...`, 'info');
        showToast('正在连接 Wi-Fi...', 'info');

        await apiCall('/api/devices/connect-wifi', 'POST', {
            devices: Array.from(state.selectedDevices),
            ssid: ssid,
            password: password
        });

        addLogEntry(`Wi-Fi 连接命令已发送 (${ssid})`, 'success');
    } catch (error) {
        addLogEntry('连接 WiFi 失败: ' + error.message, 'error');
    }
}

async function lockSelectedDevices(action) {
    await callDeviceApi('/api/devices/lock', { action });
    addLogEntry(`正在${action === 'lock' ? '锁定' : '解锁'}设备...`, 'info');
}

async function checkDeviceLockStatus() {
    if (!validateDeviceSelection()) return;
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
    if (!validateDeviceSelection()) return;
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
    if (!systemImg) {
        showToast('System 镜像路径不能为空', 'error');
        return;
    }

    await executeBurnOperation('/api/firmware/burn', {
        system_img: systemImg,
        vendor_img: document.getElementById('firmware-vendor').value.trim(),
        misc_img: document.getElementById('firmware-misc').value.trim()
    }, '烧写固件', closeFirmwareModal);
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
    const systemImg = document.getElementById('gsi-system').value.trim();
    if (!systemImg) {
        showToast('System 镜像路径不能为空', 'error');
        return;
    }

    await executeBurnOperation('/api/gsi/burn', {
        system_img: systemImg,
        vendor_img: document.getElementById('gsi-vendor').value.trim(),
        script_path: document.getElementById('gsi-script').value.trim()
    }, '烧写GSI', closeGsiModal);
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

    await executeBurnOperation('/api/sn/burn', {
        sn_code: snCode
    }, '烧写SN码', closeSnModal);
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
    const btn = $('usbip-btn');
    if (!btn) return;

    // 防止并发操作
    if (btn.disabled) return;

    console.log('[setupUsbipForward] Called, state.usbipConnected =', state.usbipConnected);

    if (state.usbipConnected) {
        // 断开连接
        console.log('[setupUsbipForward] Disconnecting...');
        try {
            btn.textContent = '📱 断开中...';
            btn.disabled = true;

            const result = await apiCall('/api/usbip/stop', 'POST', {});
            state.usbipConnected = false;
            btn.textContent = '📱 本地设备';
            btn.disabled = false;
            addLogEntry(result.message || '本地设备已断开', 'success');
            setTimeout(() => debouncedRefreshDevices(), 2500);
        } catch (error) {
            btn.textContent = '📱 断开设备';
            btn.disabled = false;
            addLogEntry('停止 USB/IP 失败: ' + error.message, 'error');
        }
    } else {
        // 连接
        console.log('[setupUsbipForward] Connecting...');
        try {
            btn.textContent = '📱 连接中...';
            btn.disabled = true;

            const result = await apiCall('/api/usbip/start', 'POST', {});

            // 只有确认成功后才设置状态
            if (result.success || result.devices) {
                state.usbipConnected = true;
                btn.textContent = '📱 断开设备';
                btn.disabled = false;
                addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
                setTimeout(() => debouncedRefreshDevices(), 3500);
            } else {
                btn.textContent = '📱 本地设备';
                btn.disabled = false;

                // 检查是否有安装指南
                if (result.install_guide) {
                    // 显示友好的安装指南弹窗
                    showInstallGuide('usbipd 安装指南', result.install_guide);
                    addLogEntry('启动 USB/IP 失败: ' + (result.error || '未知错误'), 'error');
                } else {
                    addLogEntry('启动 USB/IP 失败: ' + (result.error || result.message || '未知错误'), 'error');
                }
            }
        } catch (error) {
            btn.textContent = '📱 本地设备';
            btn.disabled = false;

            // 检查错误对象中是否有安装指南
            if (error.installGuide) {
                showInstallGuide('usbipd 安装指南', error.installGuide);
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
        // 显示正在连接的提示
        addLogEntry('正在连接 USB/IP...', 'info');
        showToast('正在连接...', 'info');

        // 立即关闭模态框
        closeDevicePasswordModal();

        const result = await apiCall('/api/usbip/start', 'POST', {
            device_password: password
        });

        // 不在这里设置状态，让主按钮处理
        addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
        showToast('USB/IP 连接成功', 'success');

        // 刷新设备列表（使用防抖版本）
        setTimeout(() => debouncedRefreshDevices(), 3500);

        // 手动更新按钮状态（因为主函数已经返回了）
        const btn = $('usbip-btn');
        if (btn) {
            state.usbipConnected = true;
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
    if (state.vpnConnected) {
        try {
            await apiCall('/api/vpn/disconnect', 'POST');
            updateVpnStatus(false);
            addLogEntry('VPN 已断开', 'info');
        } catch (error) {
            addLogEntry('断开 VPN 失败: ' + error.message, 'error');
        }
    } else {
        try {
            await apiCall('/api/vpn/connect', 'POST');
            updateVpnStatus(true);
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

// ==================== USB/IP Status Check ====================
async function checkUsbipStatus() {
    try {
        const result = await apiCall('/api/usbip/status', 'GET');
        updateUsbipButtonStatus(result.connected);
    } catch (error) {
        console.error('Failed to check USB/IP status:', error);
    }
}

function updateUsbipButtonStatus(connected) {
    const btn = $('usbip-btn');
    if (!btn) return;

    if (connected) {
        btn.textContent = '📱 断开设备';
        state.usbipConnected = true;
    } else {
        btn.textContent = '📱 本地设备';
        state.usbipConnected = false;
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

function formatBytes(bytes, hideIfZero = false) {
    /**
     * 格式化字节大小为人类可读格式
     * @param {number|string} bytes - 字节数
     * @param {boolean} hideIfZero - 如果为true，0值返回空字符串
     * @returns {string} 格式化后的大小字符串
     */
    if (hideIfZero && (!bytes || bytes === '0')) return '';
    const numBytes = parseInt(bytes) || 0;
    if (numBytes === 0) return '0 B';

    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(numBytes) / Math.log(k));
    return parseFloat((numBytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
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
        const sizeInfo = file.type === 'file' ? formatBytes(file.size, true) : '';

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
    const targetInput = document.getElementById(state.fileBrowser.targetInputId);

    // For suite path selection, use current directory (not file selection)
    if (state.fileBrowser.mode === 'suite') {
        // Use current directory path as base for auto-completion
        autoCompleteSuitePath(state.fileBrowser.currentPath);
        return;
    }

    // For other modes, require file selection
    if (!state.fileBrowser.selectedFile) {
        showToast('请先选择一个文件', 'warning');
        return;
    }

    const fullPath = `${state.fileBrowser.currentPath}/${state.fileBrowser.selectedFile}`;

    if (state.fileBrowser.mode === 'retry') {
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
            addLogEntry(`已选择文件: ${fullPath}`, 'info');
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

        // Refresh devices (强制刷新以获取最新状态)
        await loadDevices(true);
    } catch (error) {
        addLogEntry('停止测试失败: ' + error.message, 'error');
    }
}

function updateTestToggleButton(isTesting) {
    const btn = $('test-toggle-btn');
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
            <label>USB设备VID:PID:</label>
            <input type="text" id="config-usbip-vid-pid" value="${config.usbip_vid_pid || ''}" placeholder="例如: 2207:0006" />
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

function closeModal(modalId) {
    const id = modalId || 'config-modal';
    const modal = document.getElementById(id);
    if (modal) {
        modal.remove();
    }
}

async function saveConfig() {
    const ubuntuPassword = document.getElementById('config-ubuntu-pswd').value;
    const devicePassword = document.getElementById('config-device-pswd').value;
    const config = {
        ubuntu_user: document.getElementById('config-ubuntu-user').value,
        ubuntu_host: document.getElementById('config-ubuntu-host').value,
        device_host: document.getElementById('config-device-host').value,
        local_server: document.getElementById('config-local-server').value,
        suites_path: document.getElementById('config-suites-path').value,
        usbip_vid_pid: document.getElementById('config-usbip-vid-pid').value
    };

    // Only include passwords if they are not empty
    if (ubuntuPassword) {
        config.ubuntu_pswd = ubuntuPassword;
    }
    if (devicePassword) {
        config.device_pswd = devicePassword;
    }

    try {
        addLogEntry('正在保存配置...', 'info');
        showToast('正在保存配置...', 'info');

        // 立即关闭模态框
        closeModal();

        await apiCall('/api/config', 'POST', config);
        addLogEntry('配置已保存', 'success');
        showToast('配置保存成功', 'success');

        // Reload page to update config values
        setTimeout(() => location.reload(), 500);
    } catch (error) {
        addLogEntry('保存配置失败: ' + error.message, 'error');
        showToast('保存失败: ' + error.message, 'error');
    }
}

// ==================== Logging ====================
function addLogEntry(message, type = 'info') {
    const logOutput = $('log-output');
    const timestamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });

    const logEntry = document.createElement('div');
    logEntry.className = `log-entry log-${type}`;
    logEntry.textContent = `[${timestamp}] ${message}`;

    logOutput.appendChild(logEntry);
    logOutput.scrollTop = logOutput.scrollHeight;

    // 限制日志条目数量，防止内存溢出
    const maxLogs = 500;
    while (logOutput.children.length > maxLogs) {
        logOutput.removeChild(logOutput.firstChild);
    }
}

// ==================== Status Polling ====================
function startStatusPolling() {
    // 优化：从2秒改为5秒，减少服务器压力
    setInterval(async () => {
        try {
            // 只在必要时获取完整状态
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

            // 优化：使用状态中已有的日志数量而不是查询DOM
            if (status.logs && status.logs.length > 0) {
                if (status.logs.length > state.lastLogCount) {
                    status.logs.slice(state.lastLogCount).forEach(log => {
                        addLogEntry(log.message || log, log.type || 'info');
                    });
                    state.lastLogCount = status.logs.length;
                }
            }
        } catch (error) {
            console.error('Status polling error:', error);
        }
    }, 5000); // 从2000ms改为5000ms
}

async function checkInitialTestStatus() {
    try {
        const status = await apiCall('/api/status');
        state.testing = status.running;
        updateTestToggleButton(status.running);

        // Load existing logs if available
        if (status.logs && status.logs.length > 0) {
            const logOutput = $('log-output');
            logOutput.innerHTML = '';
            status.logs.forEach(log => {
                addLogEntry(log.message || log, log.type || 'info');
            });
            // 初始化日志计数
            state.lastLogCount = status.logs.length;
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

// ==================== Test Reports ====================
let reportsRefreshInterval = null;

async function loadTestReports() {
    try {
        const resp = await fetch('/api/reports/list');
        const data = await resp.json();

        if (data.reports) {
            displayTestReports(data.reports);
        }

        // 启动自动刷新（每15秒）
        if (!reportsRefreshInterval) {
            reportsRefreshInterval = setInterval(() => {
                if (currentPage === 'reports') {
                    loadTestReports();
                }
            }, 15000);
        }
    } catch (e) {
        console.error('[Reports] Error loading reports:', e);
        const tbody = document.getElementById('reports-table-body');
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                        加载失败
                    </td>
                </tr>
            `;
        }
    }
}

function displayTestReports(reports) {
    const tbody = document.getElementById('reports-table-body');
    if (!tbody) return;

    if (reports.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    暂无测试报告
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = reports.map(report => {
        const passCount = report.pass !== undefined ? report.pass : '-';
        const failCount = report.fail !== undefined ? report.fail : '-';
        const totalCount = report.total !== undefined ? report.total : '-';
        const passRate = report.total > 0 ? ((report.pass / report.total) * 100).toFixed(1) + '%' : '-';

        const passRateStyle = report.total > 0 ? (report.pass / report.total >= 0.9 ? 'color: var(--success-color);' : 'color: var(--warning-color);') : '';

        return `
            <tr style="border-bottom: 1px solid var(--border-color);">
                <td style="padding: 12px; font-family: monospace; font-size: 11px;">
                    ${report.timestamp}
                </td>
                <td style="padding: 12px; text-align: center; color: var(--success-color); font-weight: 600; font-size: 12px;">
                    ${passCount}
                </td>
                <td style="padding: 12px; text-align: center; color: var(--danger-color); font-weight: 600; font-size: 12px;">
                    ${failCount}
                </td>
                <td style="padding: 12px; text-align: center; font-weight: 600; font-size: 12px;">
                    ${totalCount}
                </td>
                <td style="padding: 12px; text-align: center; font-weight: 600; font-size: 12px; ${passRateStyle}">
                    ${passRate}
                </td>
                <td style="padding: 12px;">
                    <button class="btn-xxs" onclick="analyzeReport('${report.timestamp}')">🔍 分析</button>
                    <button class="btn-xxs" onclick="viewReportDetails('${report.timestamp}')">📄 文件</button>
                </td>
            </tr>
        `;
    }).join('');
}

async function analyzeReport(timestamp) {
    try {
        showToast('正在分析报告...', 'info');

        const resp = await fetch(`/api/reports/${timestamp}/analyze`);
        const data = await resp.json();

        if (!data.success) {
            showToast('分析失败: ' + (data.error || '未知错误'), 'error');
            return;
        }

        displayReportAnalysis(data.data, timestamp);
    } catch (e) {
        console.error('[Reports] Error analyzing report:', e);
        showToast('分析失败: ' + e.message, 'error');
    }
}

function displayReportAnalysis(data, timestamp) {
    // 创建分析结果弹窗
    const modalId = 'report-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'display: block; z-index: 10000;';

    let html = `
        <div class="modal-content" style="max-width: 900px; max-height: 80vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">📊 报告分析 - ${timestamp}</span>
                <span class="modal-close" onclick="closeReportAnalysisModal('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
    `;

    // 摘要信息
    if (data.summary) {
        html += `
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px;">
                <div style="background: var(--darker-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 8px;">总用例</div>
                    <div style="font-size: 24px; font-weight: 700; color: var(--primary-color);">${data.summary.total || 0}</div>
                </div>
                <div style="background: var(--darker-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 8px;">通过</div>
                    <div style="font-size: 24px; font-weight: 700; color: var(--success-color);">${data.summary.pass || 0}</div>
                </div>
                <div style="background: var(--darker-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 8px;">失败</div>
                    <div style="font-size: 24px; font-weight: 700; color: var(--danger-color);">${data.summary.fail || 0}</div>
                </div>
                <div style="background: var(--darker-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 8px;">通过率</div>
                    <div style="font-size: 24px; font-weight: 700; color: var(--info-color);">${data.summary.pass_rate || '0%'}</div>
                </div>
            </div>
        `;
    }

    // 设备信息
    if (data.device_info) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📱 设备信息</div>
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; font-size: 12px;">
                    ${data.device_info.device ? `<div><strong>设备:</strong> ${data.device_info.device}</div>` : ''}
                    ${data.device_info.manufacturer ? `<div><strong>厂商:</strong> ${data.device_info.manufacturer}</div>` : ''}
                    ${data.device_info.model ? `<div><strong>型号:</strong> ${data.device_info.model}</div>` : ''}
                    ${data.device_info.android_version ? `<div><strong>Android:</strong> ${data.device_info.android_version}</div>` : ''}
                    ${data.device_info.build_id ? `<div><strong>Build:</strong> ${data.device_info.build_id}</div>` : ''}
                    ${data.device_info.build_type ? `<div><strong>类型:</strong> ${data.device_info.build_type}</div>` : ''}
                </div>
            </div>
        `;
    }

    // 测试信息
    if (data.test_info) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">🧪 测试信息</div>
                <div style="font-size: 12px;">
                    ${data.test_info.suite_name ? `<div style="margin-bottom: 4px;"><strong>套件:</strong> ${data.test_info.suite_name}</div>` : ''}
                    ${data.test_info.suite_version ? `<div style="margin-bottom: 4px;"><strong>版本:</strong> ${data.test_info.suite_version}</div>` : ''}
                    ${data.test_info.start_time ? `<div style="margin-bottom: 4px;"><strong>开始:</strong> ${data.test_info.start_time}</div>` : ''}
                    ${data.test_info.end_time ? `<div style="margin-bottom: 4px;"><strong>结束:</strong> ${data.test_info.end_time}</div>` : ''}
                    ${data.test_info.duration ? `<div><strong>耗时:</strong> ${data.test_info.duration}</div>` : ''}
                </div>
            </div>
        `;
    }

    // 失败用例
    if (data.failures && data.failures.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                    <div style="font-size: 14px; font-weight: 600;">❌ 失败用例 (${data.total_failures || data.failures.length})</div>
                    <div style="display: flex; gap: 6px;">
                        ${data.failures.length > 0 ? `
                            <button onclick="analyzeSourceCode('${(data.failures[0].test_name || '').replace(/'/g, "\\'")}', '${(data.failures[0].message || '').replace(/'/g, "\\'").replace(/\n/g, '\\n')}')" style="font-size: 10px; padding: 4px 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 4px; cursor: pointer;">
                                🔍 源码分析
                            </button>
                            <button onclick="aiAnalyzeFailure('${(data.failures[0].test_name || '').replace(/'/g, "\\'")}', '${(data.failures[0].message || '').replace(/'/g, "\\'").replace(/\n/g, '\\n')}', '${(data.failures[0].module || '').replace(/'/g, "\\'")}')" style="font-size: 10px; padding: 4px 10px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; border: none; border-radius: 4px; cursor: pointer;">
                                🤖 AI分析
                            </button>
                        ` : ''}
                    </div>
                </div>
                <div style="max-height: 400px; overflow-y: auto;">
                    ${data.failures.map((f, idx) => `
                        <div style="background: var(--darker-bg); border-left: 3px solid var(--danger-color); border-radius: 4px; padding: 10px; margin-bottom: 8px;">
                            ${f.module ? `<div style="font-size: 11px; color: var(--warning-color); margin-bottom: 4px; font-weight: 600;">📦 模块: ${f.module}</div>` : ''}
                            <div style="font-weight: 600; font-size: 12px; margin-bottom: 6px; color: var(--danger-color);">🧪 ${f.test_name || f.test}</div>
                            ${f.message ? `
                                <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 8px;">
                                    <div style="margin-bottom: 4px; font-weight: 600;">💬 失败原因:</div>
                                    <div class="log-content" data-full-text="${f.message.replace(/"/g, '&quot;').replace(/'/g, '&#39;')}" style="background: rgba(255,0,0,0.05); padding: 8px; border-radius: 4px; font-family: monospace; white-space: pre-wrap; word-break: break-word; max-height: 60px; overflow: hidden;">${f.message}</div>
                                    ${f.message.length > 200 ? `<button class="expand-btn" style="margin-top: 4px; font-size: 10px; padding: 2px 8px; background: var(--primary-color); color: white; border: none; border-radius: 4px; cursor: pointer;">展开完整信息</button>` : ''}
                                </div>
                            ` : ''}
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    // 主机日志错误
    if (data.host_log_errors && data.host_log_errors.errors.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">💻 主机日志错误 (${data.host_log_errors.total_errors})</div>
                <div style="max-height: 250px; overflow-y: auto;">
                    ${data.host_log_errors.errors.map((e, idx) => `
                        <div style="background: var(--darker-bg); border-radius: 4px; padding: 8px; margin-bottom: 6px; font-size: 10px; font-family: monospace; color: var(--danger-color); white-space: pre-wrap; word-break: break-word;">
                            <div class="log-content" style="max-height: 80px; overflow: hidden;">${e}</div>
                            ${e.length > 300 ? `<button class="expand-btn" style="margin-top: 4px; font-size: 9px; padding: 2px 6px; background: var(--primary-color); color: white; border: none; border-radius: 4px; cursor: pointer;">展开</button>` : ''}
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    // 特殊块信息（新增 - 借鉴自 GMS Failure Extractor）
    if (data.host_log_errors && data.host_log_errors.special_blocks && data.host_log_errors.special_blocks.length > 0) {
        html += `
            <div style="background: linear-gradient(135deg, rgba(245, 87, 108, 0.05) 0%, rgba(255, 154, 158, 0.05) 100%); border-radius: 8px; padding: 16px; margin-bottom: 16px; border: 1px solid rgba(245, 87, 108, 0.2);">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">⚠️ 关键失败块 (${data.host_log_errors.special_blocks.length})</div>
                <div style="max-height: 400px; overflow-y: auto;">
                    ${data.host_log_errors.special_blocks.map(block => `
                        <div style="background: var(--darker-bg); border-radius: 6px; padding: 12px; margin-bottom: 10px;">
                            <div style="font-size: 11px; font-weight: 600; margin-bottom: 8px; color: var(--danger-color);">
                                ${block.type === 'ModuleListener' ? '🔴 ModuleListener FAILURE' : ''}
                                ${block.type === 'TestRunner' ? '🟠 TestRunner Exception' : ''}
                                ${block.type === 'WATCHDOG' ? '⚫ WATCHDOG Kill' : ''}
                            </div>
                            <div style="font-size: 10px; font-family: monospace; color: var(--text-color); white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto;">${block.content}</div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    // 设备日志错误
    if (data.device_log_errors && data.device_log_errors.errors.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📱 设备日志错误 (${data.device_log_errors.total_errors})</div>
                <div style="max-height: 250px; overflow-y: auto;">
                    ${data.device_log_errors.errors.map((e, idx) => `
                        <div style="background: var(--darker-bg); border-radius: 4px; padding: 8px; margin-bottom: 6px; font-size: 10px; font-family: monospace; color: var(--danger-color); white-space: pre-wrap; word-break: break-word;">
                            <div class="log-content" style="max-height: 80px; overflow: hidden;">${e}</div>
                            ${e.length > 300 ? `<button class="expand-btn" style="margin-top: 4px; font-size: 9px; padding: 2px 6px; background: var(--primary-color); color: white; border: none; border-radius: 4px; cursor: pointer;">展开</button>` : ''}
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    html += `
            </div>
            <div class="modal-buttons">
                <button class="btn-xs" onclick="closeReportAnalysisModal('${modalId}')">关闭</button>
            </div>
        </div>
    `;

    modal.innerHTML = html;
    document.body.appendChild(modal);

    // 添加展开按钮事件监听
    const expandButtons = modal.querySelectorAll('.expand-btn');
    expandButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const contentDiv = this.previousElementSibling;
            if (contentDiv.style.maxHeight === 'none') {
                // 收起
                contentDiv.style.maxHeight = contentDiv.classList.contains('log-content') ? '60px' : '80px';
                contentDiv.style.overflow = 'hidden';
                this.textContent = contentDiv.parentElement.querySelector('.log-content') ? '展开完整信息' : '展开';
            } else {
                // 展开
                contentDiv.style.maxHeight = 'none';
                contentDiv.style.overflow = 'auto';
                this.textContent = '收起';
            }
        });
    });

    // 点击外部关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeReportAnalysisModal(modalId);
        }
    });
}

function closeReportAnalysisModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.remove();
    }
}

async function viewReportDetails(timestamp) {
    try {
        const resp = await fetch(`/api/reports/${timestamp}/files`);
        const data = await resp.json();

        if (!data.success) {
            showToast('加载报告文件失败: ' + data.error, 'error');
            return;
        }

        // Show report details modal
        showReportDetailsModal(timestamp, data.files);
    } catch (e) {
        console.error('[Reports] Error loading report details:', e);
        showToast('加载报告详情失败: ' + e.message, 'error');
    }
}

function showReportDetailsModal(timestamp, files) {
    // Create modal if not exists
    let modal = document.getElementById('report-details-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'report-details-modal';
        modal.className = 'modal';
        modal.style.display = 'flex';
        modal.innerHTML = `
            <div class="modal-content" style="max-width: 700px; width: 90%; max-height: 80vh; overflow: hidden;">
                <div class="modal-header">
                    <span class="modal-title">测试报告详情</span>
                    <span class="modal-close" onclick="closeReportDetailsModal()">&times;</span>
                </div>
                <div class="modal-body" style="overflow-y: auto; max-height: calc(80vh - 120px);">
                    <div style="margin-bottom: 15px;">
                        <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;">时间戳</div>
                        <div id="report-timestamp" style="font-family: monospace; font-size: 13px; color: var(--text-primary);"></div>
                    </div>
                    <div style="margin-bottom: 15px;">
                        <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;">报告文件</div>
                        <div id="report-files-list" style="max-height: 300px; overflow-y: auto;"></div>
                    </div>
                    <div id="report-file-preview" style="display: none;">
                        <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;">文件预览</div>
                        <pre id="report-file-content" style="background: var(--darker-bg); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 11px; max-height: 300px; overflow-y: auto;"></pre>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    document.getElementById('report-timestamp').textContent = timestamp;

    const filesList = document.getElementById('report-files-list');
    filesList.innerHTML = files.map(file => {
        const sizeKB = (file.size / 1024).toFixed(1);
        return `
            <div style="display: flex; align-items: center; padding: 8px; border-bottom: 1px solid var(--border-color); cursor: pointer; hover:background: var(--light-bg);" onclick="viewReportFile('${file.path}', '${file.name}')">
                <span style="flex: 1; font-family: monospace; font-size: 11px; color: var(--text-primary);">${file.relative_path}</span>
                <span style="font-size: 10px; color: var(--text-secondary); margin-right: 10px;">${sizeKB} KB</span>
                <button class="btn-xxs" onclick="event.stopPropagation(); viewReportFile('${file.path}', '${file.name}')">📄 查看</button>
            </div>
        `;
    }).join('');

    modal.classList.add('show');
}

function closeReportDetailsModal() {
    const modal = document.getElementById('report-details-modal');
    if (modal) {
        modal.classList.remove('show');
        document.getElementById('report-file-preview').style.display = 'none';
    }
}

async function viewReportFile(filePath, fileName) {
    try {
        const resp = await fetch(`/api/reports/view?path=${encodeURIComponent(filePath)}`);
        const data = await resp.json();

        if (!data.success) {
            showToast('读取文件失败: ' + data.error, 'error');
            return;
        }

        // Show file preview
        const preview = document.getElementById('report-file-preview');
        const content = document.getElementById('report-file-content');

        preview.style.display = 'block';
        content.textContent = data.content.substring(0, 10000); // Limit to first 10KB

        if (data.content.length > 10000) {
            content.textContent += '\n\n... (文件过大，仅显示前 10KB)';
        }

        // Scroll to preview
        preview.scrollIntoView({ behavior: 'smooth' });
    } catch (e) {
        console.error('[Reports] Error viewing file:', e);
        showToast('查看文件失败: ' + e.message, 'error');
    }
}

// ==================== 安装指南弹窗 ====================
function showInstallGuide(title, guide) {
    // 创建或获取弹窗元素
    let modal = document.getElementById('install-guide-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'install-guide-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h2 id="install-guide-title">安装指南</h2>
                    <button class="close-btn" onclick="closeInstallGuide()">&times;</button>
                </div>
                <div class="modal-body">
                    <pre id="install-guide-content" style="white-space: pre-wrap; word-wrap: break-word; font-family: 'Consolas', 'Monaco', monospace; font-size: 14px; line-height: 1.6; color: #333;"></pre>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-primary" onclick="closeInstallGuide()">知道了</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    // 设置标题和内容
    document.getElementById('install-guide-title').textContent = title;
    document.getElementById('install-guide-content').textContent = guide;

    // 显示弹窗
    modal.classList.add('show');

    // 添加 ESC 键监听
    document.addEventListener('keydown', handleInstallGuideEsc);
}

function closeInstallGuide() {
    const modal = document.getElementById('install-guide-modal');
    if (modal) {
        modal.classList.remove('show');
    }
    document.removeEventListener('keydown', handleInstallGuideEsc);
}

function handleInstallGuideEsc(event) {
    if (event.key === 'Escape') {
        closeInstallGuide();
    }
}

// ==================== Report Analysis ====================

function initReportAnalysis() {
    const uploadZone = $('report-upload-zone');
    const fileInput = $('report-file-input');

    if (!uploadZone || !fileInput) return;

    // 拖拽事件
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });

    uploadZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
    });

    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');

        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleReportFile(files[0]);
        }
    });

    // 文件选择事件
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleReportFile(e.target.files[0]);
        }
    });
}

async function handleReportFile(file) {
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (!progress || !progressFill) return;

    // 显示进度
    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '0%';

    try {
        const formData = new FormData();
        formData.append('file', file);

        progressFill.style.width = '50%';

        const response = await fetch('/api/report/analyze', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        progressFill.style.width = '100%';

        if (result.success) {
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
            }, 300);
        } else {
            showToast('分析失败: ' + (result.error || '未知错误'), 'error');
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
        }
    } catch (error) {
        console.error('Report analysis error:', error);
        showToast('分析失败: ' + error.message, 'error');
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function displayReportAnalysis(data) {
    const resultDiv = $('report-analysis-result');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    if (!resultDiv) return;

    // 显示结果区域
    resultDiv.style.display = 'block';

    // 生成摘要
    if (summaryDiv && data.summary) {
        const summary = data.summary;
        summaryDiv.innerHTML = `
            ${data.details && data.details.test_type ? `
                <div>
                    <span class="summary-label">测试类型：</span>
                    <span class="summary-value">${data.details.test_type}</span>
                </div>
            ` : ''}
            ${data.details && data.details.android_version ? `
                <div>
                    <span class="summary-label">套件版本：</span>
                    <span class="summary-value">${data.details.android_version}</span>
                </div>
            ` : ''}
            <div>
                <span class="summary-label">总用例数：</span>
                <span class="summary-value">${summary.total || 0}</span>
            </div>
            <div>
                <span class="summary-label">通过：</span>
                <span class="summary-value pass">${summary.pass || 0}</span>
            </div>
            <div>
                <span class="summary-label">失败：</span>
                <span class="summary-value fail">${summary.fail || 0}</span>
            </div>
            <div>
                <span class="summary-label">通过率：</span>
                <span class="summary-value rate">${summary.pass_rate || '0%'}</span>
            </div>
        `;
    }

    // 显示详细信息
    if (detailsDiv && data.details) {
        detailsDiv.innerHTML = ``;
    }

    // 显示失败用例
    if (failuresDiv && failureList && data.failures && data.failures.length > 0) {
        failuresDiv.style.display = 'block';

        // 在标题行添加按钮
        const actionsDiv = $('report-failure-actions');
        if (actionsDiv && data.failures.length > 0) {
            const firstFailure = data.failures[0];
            const testClass = firstFailure.name || '未知用例';
            const reasonText = firstFailure.reason || '无失败原因';

            // 创建按钮元素
            const sourceBtn = document.createElement('button');
            sourceBtn.textContent = '🔍 源码分析';
            sourceBtn.style.cssText = 'font-size: 10px; padding: 4px 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 4px; cursor: pointer;';
            sourceBtn.onclick = () => analyzeFailureSource(testClass, reasonText.substring(0, 200));

            const aiBtn = document.createElement('button');
            aiBtn.textContent = '🤖 AI分析';
            aiBtn.style.cssText = 'font-size: 10px; padding: 4px 10px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; border: none; border-radius: 4px; cursor: pointer;';
            aiBtn.onclick = () => aiAnalyzeFailureReport(testClass, reasonText.substring(0, 500));

            actionsDiv.innerHTML = '';
            actionsDiv.appendChild(sourceBtn);
            actionsDiv.appendChild(aiBtn);
        }

        failureList.innerHTML = data.failures.map((failure, idx) => {
            // 解析失败信息
            const reasonText = failure.reason || '无失败原因';

            // 使用后端返回的模块名，如果没有则使用默认值
            const moduleName = failure.module || '未知模块';

            // 使用后端返回的测试用例名
            const testCaseName = failure.name || '未知用例';

            // 格式化完整堆栈信息，保留换行和缩进
            const formattedStackTrace = reasonText
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/\n/g, '<br>')
                .replace(/ /g, '&nbsp;');

            return `
                <div style="background: var(--darker-bg); border-left: 3px solid var(--danger-color); border-radius: 4px; padding: 12px; margin-bottom: 12px;">
                    <div style="margin-bottom: 8px;">
                        <div style="font-size: 12px; color: var(--text-secondary);">测试模块: <span style="font-weight: 600; color: var(--text-primary);">${moduleName}</span></div>
                    </div>
                    <div style="margin-bottom: 8px;">
                        <div style="font-size: 12px; color: var(--text-secondary);">测试用例: <span style="font-family: 'Courier New', monospace; color: var(--primary-color); word-break: break-all;">${testCaseName}</span></div>
                    </div>
                    <div>
                        <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 4px;">失败详情</div>
                        <div class="failure-reason" id="failure-reason-${idx}" style="font-size: 11px; font-family: 'Courier New', monospace; white-space: pre-wrap; word-wrap: break-word;">${formattedStackTrace}</div>
                    </div>
                </div>
            `;
        }).join('');
    } else if (failuresDiv) {
        failuresDiv.style.display = 'none';
        const actionsDiv = $('report-failure-actions');
        if (actionsDiv) actionsDiv.innerHTML = '';
    }
}

// AI分析失败用例
async function aiAnalyzeFailureReport(testName, errorMessage) {
    const modalId = 'ai-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'display: block; z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 700px; max-height: 80vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">🤖 AI 分析中...</span>
                <span class="modal-close" onclick="closeModal('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🤖</div>
                    <div style="color: var(--text-secondary);">正在分析失败原因，请稍候...</div>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    try {
        const response = await fetch('/api/test/ai-analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ test_name: testName, error_message: errorMessage })
        });

        const result = await response.json();

        // 更新模态框内容
        modal.querySelector('.modal-title').textContent = '🤖 AI 分析结果';
        modal.querySelector('.modal-body').innerHTML = result.success ?
            `<div style="white-space: pre-wrap; font-family: monospace; font-size: 12px; line-height: 1.6;">${result.data.analysis || '分析完成'}</div>` :
            `<div style="color: var(--danger-color);">分析失败: ${result.error}</div>`;

    } catch (error) {
        modal.querySelector('.modal-title').textContent = '❌ 分析失败';
        modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color);">请求失败: ${error.message}</div>`;
    }
}

// 源码分析失败用例
async function analyzeFailureSource(testName, errorMessage) {
    const modalId = 'source-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'display: block; z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 700px; max-height: 80vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">🔍 源码分析中...</span>
                <span class="modal-close" onclick="closeModal('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔍</div>
                    <div style="color: var(--text-secondary);">正在查找源码，请稍候...</div>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    try {
        const response = await fetch('/api/test/analyze-source', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ test_name: testName, error_message: errorMessage })
        });

        const result = await response.json();

        // 更新模态框内容
        modal.querySelector('.modal-title').textContent = '🔍 源码分析结果';
        modal.querySelector('.modal-body').innerHTML = result.success ?
            `<div style="white-space: pre-wrap; font-size: 12px; line-height: 1.6;">${result.data.result || '分析完成'}</div>` :
            `<div style="color: var(--danger-color);">分析失败: ${result.error}</div>`;

    } catch (error) {
        modal.querySelector('.modal-title').textContent = '❌ 分析失败';
        modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color);">请求失败: ${error.message}</div>`;
    }
}

function resetReportAnalysis() {
    const resultDiv = $('report-analysis-result');
    const uploadZone = $('report-upload-zone');
    const fileInput = $('report-file-input');

    if (resultDiv) resultDiv.style.display = 'none';
    if (fileInput) fileInput.value = '';

    showToast('已清除分析结果', 'success');
}

// 页面加载时初始化
document.addEventListener('DOMContentLoaded', () => {
    initReportAnalysis();
});

// ==================== Android Source Code Analysis ====================

/**
 * 分析测试失败的源码
 * @param {string} testName - 测试用例名称
 * @param {string} errorMessage - 错误消息
 */
async function analyzeSourceCode(testName, errorMessage) {
    try {
        // 显示加载提示
        showToast('正在分析源码...', 'info');

        const response = await fetch('/api/test/analyze-source', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                test_name: testName,
                error_message: errorMessage
            })
        });

        const result = await response.json();

        if (result.success) {
            displaySourceAnalysis(result.data);
        } else {
            showToast('源码分析失败: ' + result.error, 'error');
        }
    } catch (error) {
        console.error('源码分析错误:', error);
        showToast('源码分析请求失败', 'error');
    }
}

/**
 * 显示源码分析结果
 * @param {object} data - 分析数据
 */
function displaySourceAnalysis(data) {
    const modalId = 'source-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = `
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100% !important;
        height: 100% !important;
        background: rgba(0, 0, 0, 0.7) !important;
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        z-index: 10000 !important;
    `;

    let html = `
        <div style="background: var(--bg-color); border-radius: 12px; padding: 24px; max-width: 800px; max-height: 85vh; overflow-y: auto; width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.3); margin: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h2 style="margin: 0; font-size: 18px; font-weight: 600;">🔍 Android源码分析</h2>
                <button onclick="closeSourceAnalysisModal('${modalId}')" style="background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-secondary);">×</button>
            </div>
    `;

    // 测试信息
    if (data.test_info) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📋 测试信息</div>
                <div style="font-size: 12px;">
                    ${data.test_info.class ? `<div style="margin-bottom: 4px;"><strong>类名:</strong> <span style="color: var(--primary-color);">${data.test_info.class}</span></div>` : ''}
                    ${data.test_info.method ? `<div style="margin-bottom: 4px;"><strong>方法:</strong> ${data.test_info.method}</div>` : ''}
                    ${data.test_info.package ? `<div style="margin-bottom: 4px;"><strong>包名:</strong> ${data.test_info.package}</div>` : ''}
                </div>
            </div>
        `;
    }

    // 错误信息
    if (data.error_info) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">❌ 错误信息</div>
                <div style="font-size: 12px;">
                    ${data.error_info.type ? `<div style="margin-bottom: 4px;"><strong>错误类型:</strong> <span style="color: var(--danger-color);">${data.error_info.type}</span></div>` : ''}
                    ${data.error_info.message ? `<div style="margin-bottom: 4px;"><strong>错误消息:</strong> ${data.error_info.message.substring(0, 200)}${data.error_info.message.length > 200 ? '...' : ''}</div>` : ''}
                    ${data.error_info.keywords && data.error_info.keywords.length > 0 ? `
                        <div style="margin-top: 8px;">
                            <strong>关键词:</strong>
                            <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px;">
                                ${data.error_info.keywords.map(kw => `<span style="background: var(--warning-color); color: white; padding: 2px 8px; border-radius: 4px; font-size: 10px;">${kw}</span>`).join('')}
                            </div>
                        </div>
                    ` : ''}
                </div>
            </div>
        `;
    }

    // 分析结果
    if (data.analysis) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">🔬 分析结果</div>
                <div style="font-size: 12px;">
                    ${data.analysis.test_type && data.analysis.test_type !== 'unknown' ? `
                        <div style="margin-bottom: 8px;">
                            <strong>测试类型:</strong> <span style="color: var(--info-color); font-weight: 600;">${data.analysis.test_type}</span>
                        </div>
                    ` : ''}
                    ${data.analysis.possible_causes && data.analysis.possible_causes.length > 0 ? `
                        <div style="margin-bottom: 8px;">
                            <strong>可能原因:</strong>
                            <ul style="margin: 4px 0; padding-left: 20px;">
                                ${data.analysis.possible_causes.map(cause => `<li style="color: var(--text-secondary);">${cause}</li>`).join('')}
                            </ul>
                        </div>
                    ` : ''}
                    ${data.analysis.suggestions && data.analysis.suggestions.length > 0 ? `
                        <div style="margin-bottom: 8px;">
                            <strong>修复建议:</strong>
                            <ul style="margin: 4px 0; padding-left: 20px;">
                                ${data.analysis.suggestions.map(suggestion => `<li style="color: var(--success-color);">${suggestion}</li>`).join('')}
                            </ul>
                        </div>
                    ` : ''}
                </div>
            </div>
        `;
    }

    // 源码搜索链接
    if (data.search_links && data.search_links.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">🔗 源码搜索链接</div>
                <div style="display: flex; flex-direction: column; gap: 8px;">
                    ${data.search_links.map(link => `
                        <a href="${link.url}" target="_blank" style="display: flex; align-items: center; justify-content: space-between; padding: 10px; background: var(--darker-bg); border-radius: 6px; text-decoration: none; color: var(--text-color); transition: all 0.2s;">
                            <span style="font-size: 12px;">${link.title}</span>
                            <span style="font-size: 10px; color: var(--primary-color);">🔗 打开</span>
                        </a>
                    `).join('')}
                </div>
            </div>
        `;
    }

    html += `
            <div style="display: flex; gap: 10px; margin-top: 20px;">
                <button onclick="closeSourceAnalysisModal('${modalId}')" class="btn-xs">关闭</button>
            </div>
        </div>
    `;

    modal.innerHTML = html;
    document.body.appendChild(modal);

    // 点击外部关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeSourceAnalysisModal(modalId);
        }
    });
}

/**
 * 关闭源码分析模态框
 * @param {string} modalId - 模态框ID
 */
function closeSourceAnalysisModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.remove();
    }
}

/**
 * 使用AI分析测试失败
 * @param {string} testName - 测试用例名称
 * @param {string} errorMessage - 错误消息
 * @param {string} module - 测试模块
 */
async function aiAnalyzeFailure(testName, errorMessage, module = '') {
    try {
        // 显示加载提示
        showToast('🤖 AI正在分析...', 'info');

        const response = await fetch('/api/test/ai-analyze', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                test_name: testName,
                error_message: errorMessage,
                module: module
            })
        });

        const result = await response.json();

        if (result.success) {
            displayAIAnalysis(result.data, testName);
        } else {
            showToast('AI分析失败: ' + result.error, 'error');
        }
    } catch (error) {
        console.error('AI分析错误:', error);
        showToast('AI分析请求失败', 'error');
    }
}

/**
 * 显示AI分析结果
 * @param {object} data - AI分析数据
 * @param {string} testName - 测试用例名称
 */
function displayAIAnalysis(data, testName) {
    const modalId = 'ai-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = `
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100% !important;
        height: 100% !important;
        background: rgba(0, 0, 0, 0.7) !important;
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        z-index: 10000 !important;
    `;

    let html = `
        <div style="background: var(--bg-color); border-radius: 12px; padding: 24px; max-width: 900px; max-height: 85vh; overflow-y: auto; width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.3); margin: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h2 style="margin: 0; font-size: 18px; font-weight: 600;">🤖 AI分析报告</h2>
                <div style="display: flex; align-items: center; gap: 10px;">
                    ${data.ai_enabled === false ? '<span style="font-size: 10px; background: var(--warning-color); color: white; padding: 2px 8px; border-radius: 4px;">规则分析</span>' : '<span style="font-size: 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 2px 8px; border-radius: 4px;">AI增强</span>'}
                    <button onclick="closeAIAnalysisModal('${modalId}')" style="background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-secondary);">×</button>
                </div>
            </div>
    `;

    // 根本原因
    if (data.root_cause) {
        html += `
            <div style="background: linear-gradient(135deg, rgba(245, 87, 108, 0.1) 0%, rgba(250, 177, 160, 0.1) 100%); border-left: 4px solid #f5576c; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 8px; color: #f5576c;">🎯 根本原因</div>
                <div style="font-size: 13px; color: var(--text-color); line-height: 1.6;">${data.root_cause}</div>
            </div>
        `;
    }

    // 详细分析
    if (data.analysis) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📊 详细分析</div>
                <div style="font-size: 12px; line-height: 1.8; white-space: pre-wrap; word-break: break-word;">${data.analysis}</div>
            </div>
        `;
    }

    // 解决建议
    if (data.suggestions && data.suggestions.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">💡 解决建议</div>
                <div style="display: flex; flex-direction: column; gap: 10px;">
                    ${data.suggestions.map((suggestion, idx) => `
                        <div style="display: flex; gap: 10px; align-items: flex-start;">
                            <span style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; flex-shrink: 0;">${idx + 1}</span>
                            <span style="font-size: 12px; line-height: 1.6; color: var(--text-color);">${suggestion}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    // 相关文档
    if (data.related_docs && data.related_docs.length > 0) {
        html += `
            <div style="background: var(--light-bg); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">📚 相关文档</div>
                <div style="display: flex; flex-direction: column; gap: 8px;">
                    ${data.related_docs.map(doc => `
                        <a href="${doc.url}" target="_blank" style="display: flex; align-items: center; gap: 10px; padding: 10px; background: var(--darker-bg); border-radius: 6px; text-decoration: none; color: var(--text-color); transition: all 0.2s;">
                            <span style="font-size: 16px;">📖</span>
                            <span style="font-size: 12px; flex: 1;">${doc.title}</span>
                            <span style="font-size: 10px; color: var(--primary-color);">查看 →</span>
                        </a>
                    `).join('')}
                </div>
            </div>
        `;
    }

    html += `
            <div style="display: flex; gap: 10px; margin-top: 20px;">
                <button onclick="closeAIAnalysisModal('${modalId}')" class="btn-xs">关闭</button>
                <button onclick="copyAIAnalysis('${modalId}')" class="btn-xs" style="background: var(--success-color);">📋 复制分析报告</button>
            </div>
        </div>
    `;

    modal.innerHTML = html;
    document.body.appendChild(modal);

    // 点击外部关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeAIAnalysisModal(modalId);
        }
    });
}

/**
 * 关闭AI分析模态框
 * @param {string} modalId - 模态框ID
 */
function closeAIAnalysisModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.remove();
    }
}

/**
 * 复制AI分析报告
 * @param {string} modalId - 模态框ID
 */
function copyAIAnalysis(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;

    // 提取文本内容
    const textElements = modal.querySelectorAll('div[style*="font-size"]');
    let text = 'CTS测试失败AI分析报告\n';
    text += '=' .repeat(40) + '\n\n';

    textElements.forEach(el => {
        const content = el.textContent.trim();
        if (content && !content.startsWith('复制') && !content.startsWith('关闭')) {
            text += content + '\n\n';
        }
    });

    // 复制到剪贴板
    navigator.clipboard.writeText(text).then(() => {
        showToast('✓ 分析报告已复制', 'success');
    }).catch(() => {
        showToast('复制失败', 'error');
    });
}
