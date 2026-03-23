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
    // 先设置 client username,再初始化 Socket.IO
    try {
        // 检测客户端用户名
        const detectResponse = await fetch('/api/client-info/detect', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        if (detectResponse.ok) {
            const detectData = await detectResponse.json();
            console.log('[Init] Detected client username:', detectData.username);
            // 更新 session 中的用户名
            await fetch('/api/client-info', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: detectData.username})
            });
        }
    } catch (error) {
        console.warn('[Init] Failed to detect client username:', error);
    }

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

// ==================== WebSocket Connection (FastAPI) ====================
function initWebSocket() {
    // 获取客户端ID
    apiCall('/api/client-info', 'GET').then(data => {
        const clientId = data.client_id || 'unknown';
        state.clientId = clientId;

        // 建立WebSocket连接
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/${clientId}`;

        console.log(`[WebSocket] Connecting to: ${wsUrl}`);
        state.websocket = new WebSocket(wsUrl);

        state.websocket.onopen = () => {
            console.log('[WebSocket] Connected');
            updateConnectionStatus(true);
            addLogEntry(`WebSocket已连接 (Client ID: ${clientId})`, 'success');
        };

        state.websocket.onclose = () => {
            console.log('[WebSocket] Disconnected');
            updateConnectionStatus(false);
            addLogEntry('WebSocket连接已断开', 'warning');
            // 5秒后重连
            setTimeout(() => {
                if (typeof io === 'undefined') {
                    console.log('[WebSocket] Attempting to reconnect...');
                    initWebSocket();
                }
            }, 5000);
        };

        state.websocket.onerror = (error) => {
            console.error('[WebSocket] Error:', error);
        };

        state.websocket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const messageType = data.type;

                switch (messageType) {
                    case 'log_update':
                        console.log('[WebSocket] log_update:', data.log);
                        // 所有日志都添加到日志区域
                        addLogEntry(data.log, data.log_type || 'info');
                        break;

                    case 'test_complete':
                        state.testing = false;
                        state.currentBurningProgress = 0;  // 重置进度
                        updateTestToggleButton(false);
                        addLogEntry('测试完成', 'success');
                        showToast('测试完成', 'success');
                        break;

                    case 'devices_updated':
                        state.devices = data.devices;
                        renderDevices();
                        break;

                    case 'device_lock_update':
                        // 快速更新设备锁定状态（不需要重新查询设备列表）
                        console.log('[WebSocket] device_lock_update:', data);
                        if (data.devices && Array.isArray(data.devices)) {
                            data.devices.forEach(update => {
                                const deviceId = update.device_id;
                                console.log(`[Device Lock] Updating ${deviceId}: locked=${update.locked}, by=${update.locked_by}`);
                                // 更新 state.devices 中的锁定状态
                                const device = state.devices.find(d => {
                                    const id = typeof d === 'string' ? d : d.device_id;
                                    return id === deviceId;
                                });
                                if (device) {
                                    if (typeof device === 'string') {
                                        // 转换为对象格式
                                        const idx = state.devices.indexOf(device);
                                        state.devices[idx] = {
                                            device_id: device,
                                            locked: update.locked,
                                            locked_by: update.locked_by || '',
                                            locked_at: update.locked_at || ''
                                        };
                                        console.log(`[Device Lock] Converted to object:`, state.devices[idx]);
                                    } else {
                                        // 更新现有对象
                                        device.locked = update.locked;
                                        device.locked_by = update.locked_by || '';
                                        device.locked_at = update.locked_at || '';
                                        console.log(`[Device Lock] Updated device:`, device);
                                    }
                                } else {
                                    console.warn(`[Device Lock] Device ${deviceId} not found in state.devices`);
                                }
                            });
                            // 重新渲染设备列表
                            console.log('[Device Lock] Re-rendering devices...');
                            renderDevices();
                        }
                        break;

                    case 'devices_changed':
                        // USB设备插拔事件，自动刷新设备列表
                        console.log('[WebSocket] devices_changed:', data.devices);
                        // 保存旧设备列表用于比较
                        const oldDevices = new Set(state.devices.map(d => typeof d === 'string' ? d : d.device_id));
                        // 自动刷新设备列表
                        loadDevices(true).then(() => {
                            const newDevices = new Set(state.devices.map(d => typeof d === 'string' ? d : d.device_id));
                            // 找出连接的设备（在新列表中但不在旧列表中）
                            const connected = [...newDevices].filter(d => !oldDevices.has(d));
                            // 找出断开的设备（在旧列表中但不在新列表中）
                            const disconnected = [...oldDevices].filter(d => !newDevices.has(d));

                            // 构建设备变化消息
                            let changeMessage = '检测到USB设备变化';
                            if (connected.length > 0) {
                                changeMessage += `，连接: ${connected.join(' ')}`;
                            }
                            if (disconnected.length > 0) {
                                changeMessage += `，断开: ${disconnected.join(' ')}`;
                            }
                            addLogEntry(changeMessage, 'info');

                            let message = '设备列表已更新';
                            if (connected.length > 0) {
                                message += `，连接: ${connected.join(' ')}`;
                            }
                            if (disconnected.length > 0) {
                                message += `，断开: ${disconnected.join(' ')}`;
                            }
                            showToast(message, 'success');
                        }).catch(err => {
                            console.error('Failed to refresh devices:', err);
                        });
                        break;

                    case 'firmware_progress':
                        // 固件烧写进度更新
                        console.log('[WebSocket] firmware_progress:', data.percentage);
                        if (data.percentage !== undefined) {
                            // 只在百分比大于等于当前值时才更新（避免跳动）
                            const currentProgress = state.currentBurningProgress || 0;
                            if (data.percentage >= currentProgress) {
                                state.currentBurningProgress = data.percentage;
                                updateProgressBar(data.percentage, '', '烧写固件');
                            }
                        }
                        break;

                    case 'file_upload_progress':
                        // 文件上传进度更新（通用，用于固件上传等）
                        console.log('[WebSocket] file_upload_progress:', data);
                        updateUploadProgress(data.percentage, data.filename, data.uploaded_size, data.total_size);
                        break;

                    case 'upload_progress':
                        // 固件上传进度更新（已弃用，保留以防兼容性）
                        console.log('[WebSocket] upload_progress:', data);
                        break;

                    case 'vpn_status_update':
                        updateVpnStatus(data.connected);
                        break;

                    case 'ping':
                        // 响应心跳
                        if (state.websocket.readyState === WebSocket.OPEN) {
                            state.websocket.send(JSON.stringify({ type: 'pong' }));
                        }
                        break;

                    default:
                        console.log('[WebSocket] Unknown message type:', messageType, data);
                }
            } catch (error) {
                console.error('[WebSocket] Error parsing message:', error);
            }
        };
    }).catch(error => {
        console.error('[WebSocket] Failed to get client ID:', error);
        // 3秒后重试
        setTimeout(() => {
            if (typeof io === 'undefined') {
                initWebSocket();
            }
        }, 3000);
    });
}

// ==================== Socket.IO Connection (Flask) ====================
function initSocket() {
    // 检查Socket.IO是否可用（FastAPI版本使用WebSocket）
    if (typeof io === 'undefined') {
        console.warn('[Socket.IO] Not available, using WebSocket instead (FastAPI)');
        initWebSocket();
        return;
    }

    state.socket = io();

    state.socket.on('connect', () => {
        console.log('Connected to server');
        updateConnectionStatus(true);
    });

    state.socket.on('disconnect', () => {
        console.log('Disconnected from server');
        updateConnectionStatus(false);
    });

    state.socket.on('connected', (data) => {
        console.log('[Socket.IO] Server confirmed connection, client_id:', data.client_id);
        // 在日志区域显示连接信息
        setTimeout(() => {
            const logOutput = document.getElementById('log-output');
            if (logOutput) {
                const entry = document.createElement('div');
                entry.className = 'log-entry log-success';
                entry.textContent = `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] [Socket.IO] 已连接, Client ID: ${data.client_id}`;
                entry.style.fontWeight = 'bold';
                entry.style.color = 'green';
                logOutput.appendChild(entry);
            }
        }, 100);
    });

    state.socket.on('log_update', (data) => {
        console.log('[Socket.IO] Received log_update:', data);
        // 直接添加日志
        const logOutput = document.getElementById('log-output');
        if (logOutput) {
            addLogEntry(data.log, data.type || 'info');
        } else {
            console.warn('[Socket.IO] log-output element not found!');
        }
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
    const deviceCanvas = $('device-canvas');

    if (state.devices.length === 0) {
        // 隐藏两个列，显示居中的空消息
        leftContainer.style.display = 'none';
        rightContainer.style.display = 'none';
        deviceCanvas.innerHTML = '<div class="empty-message">点击刷新按钮获取设备列表...</div>';
        deviceCanvas.style.display = 'flex';
        deviceCanvas.style.justifyContent = 'center';
        deviceCanvas.style.alignItems = 'center';
        return;
    }

    // 恢复正常显示
    leftContainer.style.display = '';
    rightContainer.style.display = '';
    deviceCanvas.style.display = '';
    deviceCanvas.style.justifyContent = '';
    deviceCanvas.style.alignItems = '';

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
            // 直接显示后端发送的完整值 (username@ip格式)
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

    // Show firmware configuration modal
    const modal = document.getElementById('firmware-modal');
    modal.classList.add('show');
}

function closeFirmwareModal() {
    const modal = document.getElementById('firmware-modal');
    modal.classList.remove('show');
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
                showToast(`已选择固件文件: ${file.name}`, 'info');
            }
        }
    };
    fileInput.click();
}

async function submitFirmwareBurn() {
    const firmwarePath = document.getElementById('firmware-path').value.trim();
    if (!firmwarePath) {
        showToast('请选择固件文件', 'error');
        return;
    }

    const devices = Array.from(state.selectedDevices);
    try {
        closeFirmwareModal();
        showToast('正在烧写固件...', 'info');
        addLogEntry(`开始烧写固件: ${firmwarePath}`, 'info');

        // 立即在UI上标记设备为锁定状态
        lockDevicesInUI(devices);

        // 准备FormData
        const formData = new FormData();
        formData.append('firmware_path', firmwarePath);
        const fileInput = document.getElementById('firmware-file-input');
        if (fileInput && fileInput.files && fileInput.files[0]) {
            formData.append('firmware_file', fileInput.files[0]);
        }

        // 发送请求
        const response = await fetch(`/api/firmware/burn?devices=${encodeURIComponent(devices.join(','))}`, {
            method: 'POST',
            body: formData
        });

        const result = await response.json();
        if (result.success) {
            showToast('固件烧写任务已启动', 'success');
            addLogEntry(`固件烧写任务已启动，设备: ${devices.join(', ')}`, 'success');
        } else {
            showToast(`烧写失败: ${result.error}`, 'error');
            addLogEntry(`固件烧写失败: ${result.error}`, 'error');
        }
    } catch (error) {
        showToast(`烧写失败: ${error.message}`, 'error');
        addLogEntry(`固件烧写异常: ${error.message}`, 'error');
    }
}

async function burnGsiImage() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写GSI的设备', 'warning');
        return;
    }

    // Set default script path
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
    const modal = document.getElementById('file-browser-modal');
    modal.classList.add('show');

    // Load initial directory (GMS-Suite)
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    await loadFileDirectory(`/home/${defaultUser}/GMS-Suite`);
}

// Browse remote file for GSI system image
async function browseLocalFileForGsiSystem() {
    const title = '选择System镜像';

    // Set file browser state
    state.fileBrowser.mode = 'gsi-system';
    state.fileBrowser.targetInputId = 'gsi-system';
    state.fileBrowser.selectedFile = null;

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    const modal = document.getElementById('file-browser-modal');
    modal.classList.add('show');

    // Load initial directory (GMS-Suite)
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    await loadFileDirectory(`/home/${defaultUser}/GMS-Suite`);
}

// Browse local file for GSI vendor image (from local computer)
function browseLocalFileForGsiVendor() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '*.img';
    input.onchange = (e) => {
        const file = e.target.files[0];
        if (file) {
            const target = document.getElementById('gsi-vendor');
            if (target) {
                target.value = file.path || file.name;
                showToast(`已选择Vendor镜像: ${file.name}`, 'info');
            }
        }
    };
    input.click();
}

async function submitGsiBurn() {
    const scriptPath = document.getElementById('gsi-script').value.trim();
    const systemImg = document.getElementById('gsi-system').value.trim();
    const vendorImg = document.getElementById('gsi-vendor').value.trim();

    if (!scriptPath) {
        showToast('请选择GSI烧写脚本', 'error');
        return;
    }
    if (!systemImg) {
        showToast('请选择System镜像', 'error');
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

// ==================== 烧写操作辅助函数 ====================
async function executeBurnOperation(endpoint, data, operationName, closeModalFunc) {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要操作的设备', 'warning');
        return;
    }

    const devices = Array.from(state.selectedDevices);
    try {
        if (closeModalFunc) {
            closeModalFunc();
        }

        addLogEntry(`正在${operationName}...`, 'info');
        showToast(`正在${operationName}...`, 'info');

        // 立即在UI上标记设备为锁定状态
        lockDevicesInUI(devices);

        // 调用API
        const result = await apiCall(endpoint, 'POST', {
            ...data,
            devices: devices
        });

        if (result.success) {
            addLogEntry(`${operationName}完成`, 'success');
            showToast(`${operationName}完成`, 'success');

            // 显示详细结果
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
            showToast(`${operationName}失败: ${result.error || '未知错误'}`, 'error');
        }
    } catch (error) {
        addLogEntry(`${operationName}失败: ${error.message}`, 'error');
        showToast(`${operationName}失败: ${error.message}`, 'error');
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

// 通用上传进度更新函数（用于固件上传等）
function updateUploadProgress(percentage, filename, uploadedSize, totalSize) {
    console.log('[updateUploadProgress] Called with:', { percentage, filename, uploadedSize, totalSize });

    const progressFill = document.getElementById('upload-progress-fill');
    const progressInfo = document.getElementById('progress-info');

    console.log('[updateUploadProgress] Elements:', { progressFill, progressInfo });

    if (progressFill && progressInfo) {
        progressFill.style.width = percentage + '%';

        const transferred = formatBytes(uploadedSize);
        const total = formatBytes(totalSize);

        console.log('[updateUploadProgress] Updating UI:', { percentage, transferred, total });

        if (percentage >= 100) {
            progressInfo.textContent = `✅ ${filename} 上传完成 (${total})`;
            // 3秒后重置进度条
            setTimeout(() => {
                progressFill.style.width = '0%';
                progressInfo.textContent = '';
            }, 3000);
        } else {
            progressInfo.textContent = `📤 ${filename} 上传中... ${percentage}% (${transferred}/${total})`;
        }
    } else {
        console.error('[updateUploadProgress] Progress elements not found!');
    }
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

    // Load initial directory - use GMS-Suite for both suite and retry
    const defaultUser = state.config?.ubuntu_user || 'hcq';
    const defaultPath = `/home/${defaultUser}/GMS-Suite`;
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
    } else if (state.fileBrowser.mode === 'gsi' || state.fileBrowser.mode === 'gsi-system') {
        // For GSI system image, use the selected path directly
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择System镜像: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi-script') {
        // For GSI script, use the selected path directly
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择GSI脚本: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'gsi-vendor') {
        // For GSI vendor image, use the selected path directly
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择Vendor镜像: ${fullPath}`, 'info');
        }
        closeFileBrowserModal();
    } else if (state.fileBrowser.mode === 'firmware') {
        // For firmware, use the selected path directly
        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择固件文件: ${fullPath}`, 'info');
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
        // 清空日志输出并重置计数
        const logOutput = $('log-output');
        if (logOutput) {
            logOutput.innerHTML = '';
        }
        state.lastLogCount = 0;

        await apiCall('/api/test/start', 'POST', {
            devices: Array.from(state.selectedDevices),
            test_type: testType,
            test_module: testModule,
            test_case: testCase,
            retry_dir: retryResult,
            test_suite: suitePath,
            local_server: state.config?.local_server || ''
        });

        console.log('[startTest] API call successful, setting testing = true');
        state.testing = true;
        updateTestToggleButton(true);
        addLogEntry('测试已启动', 'success');
        showToast('测试已启动', 'success');

        // 刷新设备列表以更新锁定状态
        await refreshDevices();
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

        // 使用新的 stop 接口（支持多用户隔离）
        await apiCall('/api/test/stop', 'POST');

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
        // 对于动态创建的模态框（直接移除）
        if (id.startsWith('source-analysis-modal-') || id.startsWith('ai-analysis-modal-')) {
            modal.style.display = 'none';
            // 延迟删除，确保动画完成
            setTimeout(() => {
                if (modal && modal.parentNode) {
                    modal.parentNode.removeChild(modal);
                }
            }, 300);
        } else {
            // 对于静态模态框（使用class控制）
            modal.classList.remove('show');
        }
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
    // 不使用缓存的$函数，直接获取元素（避免缓存null的问题）
    const logOutput = document.getElementById('log-output');
    if (!logOutput) {
        console.warn('[Log] log-output element not found, message:', message);
        return;
    }

    const timestamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });

    const logEntry = document.createElement('div');
    logEntry.className = `log-entry log-${type}`;
    logEntry.textContent = `[${timestamp}] ${message}`;

    logOutput.appendChild(logEntry);
    logOutput.scrollTop = logOutput.scrollHeight;

    // 限制日志条目数量（500条足够），防止内存溢出和卡顿
    const maxLogs = 500;
    if (logOutput.children.length > maxLogs) {
        // 批量删除旧日志，减少DOM操作
        const removeCount = logOutput.children.length - maxLogs;
        for (let i = 0; i < removeCount; i++) {
            logOutput.removeChild(logOutput.firstChild);
        }
    }
}

// 更新进度条 - 使用固件上传的进度条
function updateProgressBar(percentage, message = '', title = '进度') {
    console.log('[Progress] updateProgressBar called:', percentage, message, title);

    const progressContainer = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-fill');
    const progressInfo = document.getElementById('progress-info');

    if (!progressContainer || !progressFill || !progressInfo) {
        console.warn('[Progress] Progress bar elements not found');
        return;
    }

    // 显示进度条
    progressContainer.style.display = 'flex';

    // 更新进度
    progressFill.style.width = `${percentage}%`;

    // 显示标题和百分比在进度条右侧
    progressInfo.textContent = `${title} ${percentage}%`;

    // 如果有消息，显示在日志中
    if (message) {
        addLogEntry(message, 'info');
    }

    console.log('[Progress] Updated to:', percentage);

    // 如果进度完成，3秒后隐藏进度条
    if (percentage >= 100) {
        setTimeout(() => {
            progressContainer.style.display = 'none';
            progressFill.style.width = '0%';
            progressInfo.textContent = '';
            state.currentBurningProgress = 0;  // 重置进度状态
        }, 3000);
    }
}

// 上传文件进度
function updateUploadProgress(percentage, filename, uploadedSize, totalSize) {
    // 只更新进度条，不显示日志消息
    updateProgressBar(percentage, '', `上传文件`);
}

// ==================== Status Polling ====================
function startStatusPolling() {
    // 轮询状态和日志（同时支持Socket.IO和WebSocket）
    let shownPyudevWarning = false;  // 标记是否已显示过 pyudev 警告

    setInterval(async () => {
        try {
            // 检查是否有实时连接（Socket.IO 或 WebSocket）
            const hasRealtimeConnection = (state.socket && typeof io !== 'undefined') ||
                                        (state.websocket && state.websocket.readyState === WebSocket.OPEN);

            console.log('[Poll] hasRealtimeConnection:', hasRealtimeConnection,
                       'Socket.IO:', !!state.socket,
                       'WebSocket:', state.websocket ? state.websocket.readyState : 'none');

            // 如果没有实时连接，获取日志；否则只获取状态
            const status = await apiCall(hasRealtimeConnection ? '/api/status?logs=false' : '/api/status');

            // 检查 USB 监控器状态并提示（仅显示一次）
            if (!shownPyudevWarning && status.usb_monitor) {
                const { mode, running, pyudev_available } = status.usb_monitor;
                if (running && mode === 'polling' && !pyudev_available) {
                    shownPyudevWarning = true;
                    const message = '💡 提示：安装 pyudev 可获得更好的USB监控性能（实时响应，低CPU占用）\n' +
                                   '安装命令：pip install pyudev\n' +
                                   '当前使用轮询模式（2秒检查间隔）';
                    addLogEntry(message, 'warning');

                    // 也可以在页面显示一次提示
                    if (!localStorage.getItem('pyudev_warning_shown')) {
                        showToast('建议安装 pyudev 以提升性能', 'info');
                        localStorage.setItem('pyudev_warning_shown', 'true');
                    }
                }
            }

            // 更新测试状态按钮
            console.log('[Poll] Status:', status.running, 'State.testing:', state.testing);
            if (status.running && !state.testing) {
                console.log('[Poll] Setting testing = TRUE');
                state.testing = true;
                updateTestToggleButton(true);
            } else if (!status.running && state.testing) {
                console.log('[Poll] Setting testing = FALSE');
                state.testing = false;
                updateTestToggleButton(false);
            }

            // Update VPN status
            if (status.vpn_connected !== undefined) {
                updateVpnStatus(status.vpn_connected);
            }

            // 如果没有实时连接，处理日志更新
            if (!hasRealtimeConnection && status.logs && status.logs.length > 0) {
                const logOutput = document.getElementById('log-output');
                if (logOutput && status.logs.length > state.lastLogCount) {
                    // 显示新增的日志
                    const newLogs = status.logs.slice(state.lastLogCount);
                    newLogs.forEach(log => {
                        // 日志已经是字符串格式（包含时间戳），直接显示
                        if (typeof log === 'string') {
                            // 移除时间戳（因为addLogEntry会再次添加）
                            const message = log.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '');
                            // 提取原始日志类型（如果有）
                            let logType = 'info';
                            if (message.includes('✅') || message.includes('Test completed')) {
                                logType = 'success';
                            } else if (message.includes('❌') || message.includes('ERROR') || message.includes('[STDERR]')) {
                                logType = 'error';
                            } else if (message.includes('⚠️') || message.includes('WARNING')) {
                                logType = 'warning';
                            }
                            addLogEntry(message, logType);
                        } else {
                            // 兼容对象格式
                            addLogEntry(log.message || log.log || '', log.type || log.log_type || 'info');
                        }
                    });
                    state.lastLogCount = status.logs.length;
                }
            }
        } catch (error) {
            console.error('Status polling error:', error);
        }
    }, 2000);  // FastAPI版本使用更短的轮询间隔（2秒）
}

async function checkInitialTestStatus() {
    try {
        const status = await apiCall('/api/status');
        state.testing = status.running;
        updateTestToggleButton(status.running);

        // 页面刷新时加载历史日志（限制最近100条，避免卡顿）
        if (status.logs && status.logs.length > 0) {
            // 直接获取元素，不使用缓存
            const logOutput = document.getElementById('log-output');
            if (!logOutput) {
                console.warn('[Init] log-output element not found');
                return;
            }

            logOutput.innerHTML = '';

            // 只显示最近100条历史日志，避免卡顿
            const recentLogs = status.logs.slice(-100);

            // 使用DocumentFragment批量添加，减少DOM操作
            const fragment = document.createDocumentFragment();
            recentLogs.forEach(log => {
                const logEntry = document.createElement('div');
                const message = typeof log === 'string' ? log : (log.message || log.log || log);
                const type = typeof log === 'object' ? (log.type || 'info') : 'info';
                const timestamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });

                logEntry.className = `log-entry log-${type}`;
                logEntry.textContent = `[${timestamp}] ${message}`;
                fragment.appendChild(logEntry);
            });

            logOutput.appendChild(fragment);
            logOutput.scrollTop = logOutput.scrollHeight;

            state.lastLogCount = status.log_count || status.logs.length;
        } else {
            state.lastLogCount = 0;
        }
    } catch (error) {
        console.error('Failed to check initial test status:', error);
        state.lastLogCount = 0;
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
let currentUserFilter = false;  // 当前是否只显示本用户报告

async function loadTestReports(userOnly = false) {
    try {
        const url = userOnly ? '/api/reports/list?user_only=true' : '/api/reports/list';
        const resp = await fetch(url);
        const data = await resp.json();

        if (data.reports) {
            displayTestReports(data.reports);
        }

        // 启动自动刷新（每15秒）
        if (!reportsRefreshInterval) {
            reportsRefreshInterval = setInterval(() => {
                if (currentPage === 'reports') {
                    loadTestReports(currentUserFilter);
                }
            }, 15000);
        }
    } catch (e) {
        console.error('[Reports] Error loading reports:', e);
        const tbody = document.getElementById('reports-table-body');
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="8" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                        加载失败
                    </td>
                </tr>
            `;
        }
    }
}

function toggleUserReports() {
    const checkbox = document.getElementById('filter-user-checkbox');
    currentUserFilter = checkbox.checked;

    // 重新加载报告列表
    loadTestReports(currentUserFilter);
}

function displayTestReports(reports) {
    const tbody = document.getElementById('reports-table-body');
    if (!tbody) return;

    if (reports.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="8" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    暂无测试报告
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = reports.map(report => {
        const testType = report.test_type || '-';
        // 显示完整的 client_id 格式（例如：hcq@172.16.14.233）
        const displayClient = report.client_id || report.user || '-';
        const passCount = report.pass !== undefined ? report.pass : '-';
        const failCount = report.fail !== undefined ? report.fail : '-';
        const totalCount = report.total !== undefined ? report.total : '-';
        const passRate = report.total > 0 ? ((report.pass / report.total) * 100).toFixed(1) + '%' : '-';
        // 使用 result_dir 或 path 作为报告路径
        const reportPath = report.result_dir || report.path || '';

        const passRateStyle = report.total > 0 ? (report.pass / report.total >= 0.9 ? 'color: var(--success-color);' : 'color: var(--warning-color);') : '';

        // 测试类型颜色
        const typeColors = {
            'CTS': 'color: #3B82F6;',  // 蓝色
            'GTS': 'color: #10B981;',  // 绿色
            'STS': 'color: #F59E0B;',  // 黄色
            'VTS': 'color: #8B5CF6;',  // 紫色
            'XTS': 'color: #EC4899;',  // 粉色
        };
        const typeStyle = typeColors[testType] || 'color: var(--text-secondary);';

        return `
            <tr style="border-bottom: 1px solid var(--border-color);">
                <td style="padding: 12px; text-align: center; font-weight: 700; font-size: 12px; ${typeStyle}">
                    ${testType}
                </td>
                <td style="padding: 12px; text-align: center; font-family: monospace; font-size: 11px;">
                    ${displayClient}
                </td>
                <td style="padding: 12px; text-align: center; font-family: monospace; font-size: 11px;">
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
                <td style="padding: 12px; text-align: center;">
                    <button class="btn-xxs" onclick="event.stopPropagation(); analyzeReport('${report.timestamp}')">📈 分析报告</button>
                    <button class="btn-xxs" onclick="event.stopPropagation(); viewReportDetails('${report.timestamp}')">📄 查看报告</button>
                    <button class="btn-xxs" onclick="event.stopPropagation(); downloadReport('${report.timestamp}')" style="background: var(--success-color);">⬇️ 下载报告</button>
                    <button class="btn-xxs" onclick="event.stopPropagation(); deleteReport('${report.timestamp}')" style="background: var(--danger-color);">🗑️ 删除报告</button>
                </td>
            </tr>
        `;
    }).join('');
}

async function deleteReport(timestamp) {
    if (!confirm(`确定要删除报告 ${timestamp} 吗？此操作不可恢复。`)) {
        return;
    }

    try {
        const response = await fetch(`/api/reports/delete?timestamp=${encodeURIComponent(timestamp)}`, {
            method: 'DELETE'
        });

        const result = await response.json();

        if (result.success) {
            showToast('报告已删除', 'success');
            // 刷新报告列表
            await loadTestReports();
        } else {
            showToast('删除失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (error) {
        console.error('Delete report error:', error);
        showToast('删除失败: ' + error.message, 'error');
    }
}

async function downloadReport(timestamp) {
    try {
        showToast('正在下载报告...', 'info');

        const response = await fetch(`/api/reports/${timestamp}/download`);

        // 检查响应状态
        if (!response.ok) {
            let errorMsg = `HTTP ${response.status}`;
            try {
                const errorData = await response.json();
                errorMsg = errorData.error || errorMsg;
            } catch (e) {
                // 如果无法解析JSON，使用默认错误消息
            }
            console.error('Download failed:', response.status, errorMsg);
            showToast('下载失败: ' + errorMsg, 'error');
            return;
        }

        // 检查Content-Type
        const contentType = response.headers.get('Content-Type');
        console.log('Response Content-Type:', contentType);

        if (contentType && contentType.includes('application/json')) {
            // 如果返回的是JSON而不是文件，说明有错误
            const errorData = await response.json();
            console.error('Server returned error:', errorData);
            showToast('下载失败: ' + (errorData.error || '服务器错误'), 'error');
            return;
        }

        // 获取文件名
        const contentDisposition = response.headers.get('Content-Disposition');
        let filename = `report_${timestamp}.zip`;

        if (contentDisposition) {
            const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
            if (filenameMatch && filenameMatch[1]) {
                filename = filenameMatch[1].replace(/['"]/g, '');
            }
        }
        console.log('Downloading file as:', filename);

        // 下载文件
        const blob = await response.blob();
        console.log('Blob size:', blob.size, 'bytes');

        if (blob.size === 0) {
            showToast('下载失败: 文件为空', 'error');
            return;
        }

        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();

        // 延迟清理
        setTimeout(() => {
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
        }, 100);

        showToast('报告下载成功', 'success');
    } catch (error) {
        console.error('Download report error:', error);
        showToast('下载失败: ' + error.message, 'error');
    }
}

function openReportAnalysis(timestamp) {
    // 切换到报告分析页面
    const sidebarItem = document.querySelector('[data-page="report-analysis"]');
    if (sidebarItem) {
        sidebarItem.click();
    }

    // 等待页面切换完成后，自动加载并分析报告
    setTimeout(() => {
        analyzeReport(timestamp);
    }, 300);
}

async function analyzeReport(timestamp) {
    try {
        // 切换到报告分析页面
        const sidebarItem = document.querySelector('[data-page="report-analysis"]');
        if (sidebarItem) {
            sidebarItem.click();
        }

        // 等待页面切换完成后，自动加载并分析报告
        setTimeout(async () => {
            showToast('正在分析报告...', 'info');

            const resp = await fetch(`/api/reports/${timestamp}/analyze`);
            const data = await resp.json();

            if (!data.success) {
                showToast('分析失败: ' + (data.error || '未知错误'), 'error');
                return;
            }

            // 使用与手动上传相同的显示函数，保持布局一致
            displayReportAnalysis(data.data);
        }, 300);
    } catch (e) {
        console.error('[Reports] Error analyzing report:', e);
        showToast('分析失败: ' + e.message, 'error');
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

    // 使用show类来显示modal，不要直接设置style.display
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

function selectReportSource() {
    // 创建选择对话框
    const modal = document.createElement('div');
    modal.id = 'report-source-modal';
    modal.className = 'modal';
    modal.style.cssText = 'display: block; z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 400px;">
            <div class="modal-header">
                <span class="modal-title">选择上传方式</span>
                <span class="modal-close" onclick="closeReportSourceModal()">&times;</span>
            </div>
            <div class="modal-body" style="padding: 20px;">
                <div style="display: flex; flex-direction: column; gap: 12px;">
                    <button class="btn-md" onclick="selectReportFile()" style="width: 100%; justify-content: center;">
                        📄 上传文件
                    </button>
                    <div style="font-size: 10px; color: var(--text-secondary); text-align: center;">
                        支持 .xml, .zip, .tar.gz
                    </div>
                    <button class="btn-md" onclick="selectReportFolder()" style="width: 100%; justify-content: center;">
                        📁 上传文件夹
                    </button>
                    <div style="font-size: 10px; color: var(--text-secondary); text-align: center;">
                        选择包含 test_result.xml 的文件夹
                    </div>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeReportSourceModal();
        }
    });
}

function closeReportSourceModal() {
    const modal = document.getElementById('report-source-modal');
    if (modal) {
        modal.remove();
    }
}

function selectReportFile() {
    closeReportSourceModal();
    document.getElementById('report-file-input').click();
}

function selectReportFolder() {
    closeReportSourceModal();
    document.getElementById('report-folder-input').click();
}

function initReportAnalysis() {
    const uploadZone = $('report-upload-zone');
    const fileInput = $('report-file-input');
    const folderInput = $('report-folder-input');

    if (!uploadZone || !fileInput || !folderInput) return;

    // 初始化时添加上传空状态类（占满屏幕）
    uploadZone.classList.add('upload-empty');

    // 拖拽事件
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });

    uploadZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
    });

    uploadZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');

        const items = e.dataTransfer.items;

        // 如果有 items，尝试使用 DataTransferItem API（支持文件夹）
        if (items && items.length > 0) {
            const files = [];

            // 递归读取文件夹中的所有文件
            const readFileEntries = async (entries) => {
                for (const entry of entries) {
                    if (entry.isFile) {
                        await new Promise((resolve) => {
                            entry.file((file) => {
                                // 保留相对路径
                                Object.defineProperty(file, 'webkitRelativePath', {
                                    value: entry.fullPath.replace(/^\//, ''),
                                    writable: false
                                });
                                files.push(file);
                                resolve();
                            });
                        });
                    } else if (entry.isDirectory) {
                        const reader = entry.createReader();
                        // readEntries 可能需要多次调用才能读取所有条目
                        let allEntries = [];
                        const readBatch = async () => {
                            const batch = await new Promise((resolve) => {
                                reader.readEntries(resolve);
                            });
                            if (batch.length > 0) {
                                allEntries = allEntries.concat(batch);
                                await readBatch(); // 继续读取下一批
                            }
                        };
                        await readBatch();
                        await readFileEntries(allEntries);
                    }
                }
            };

            // 处理所有 items
            const itemEntries = [];
            for (let i = 0; i < items.length; i++) {
                const item = items[i];
                if (item.kind === 'file') {
                    const entry = item.webkitGetAsEntry();
                    if (entry) {
                        itemEntries.push(entry);
                    }
                }
            }

            if (itemEntries.length > 0) {
                await readFileEntries(itemEntries);

                if (files.length === 0) {
                    showToast('未找到可上传的文件', 'warning');
                    return;
                }

                if (files.length === 1 && !files[0].webkitRelativePath.includes('/')) {
                    // 单文件
                    handleReportFile(files[0]);
                } else {
                    // 文件夹或多文件
                    handleReportFolder(files);
                }
                return;
            }
        }

        // 回退到使用 files 属性（单文件或旧浏览器）
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            if (files.length === 1) {
                handleReportFile(files[0]);
            } else {
                handleReportFolder(files);
            }
        }
    });

    // 文件选择事件
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleReportFile(e.target.files[0]);
        }
    });

    // 文件夹选择事件
    folderInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleReportFolder(e.target.files);
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

async function handleReportFolder(files) {
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

        // 添加所有文件到 FormData，保持文件夹结构
        let fileCount = 0;
        for (let i = 0; i < files.length; i++) {
            const file = files[i];

            // 使用 webkitRelativePath 或文件名
            const filename = file.webkitRelativePath || file.name;

            // 创建新的 File 对象，确保文件名正确
            const fileWithPath = new File([file], filename, {
                type: file.type,
                lastModified: file.lastModified
            });

            formData.append('files[]', fileWithPath);
            fileCount++;
        }

        console.log(`Uploading ${fileCount} files...`);
        progressFill.style.width = '30%';

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
                showToast(`成功分析 ${fileCount} 个文件`, 'success');
            }, 300);
        } else {
            showToast('分析失败: ' + (result.error || '未知错误'), 'error');
            if (result.message) {
                console.error('Analysis error details:', result.message);
            }
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
        }
    } catch (error) {
        console.error('Report folder analysis error:', error);
        showToast('分析失败: ' + error.message, 'error');
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function displayReportAnalysis(data) {
    console.log('[displayReportAnalysis] Called with data:', data);

    const resultDiv = $('report-analysis-result');
    const uploadZone = $('report-upload-zone');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    // 移除上传空状态类（缩小到固定高度）
    if (uploadZone) uploadZone.classList.remove('upload-empty');

    console.log('[displayReportAnalysis] Elements:', {
        resultDiv,
        summaryDiv,
        detailsDiv,
        failuresDiv,
        failureList
    });

    if (!resultDiv) {
        console.error('[displayReportAnalysis] resultDiv not found!');
        return;
    }

    // 显示结果区域
    resultDiv.style.display = 'block';
    console.log('[displayReportAnalysis] resultDiv display set to block');

    // 生成摘要
    if (summaryDiv && data.summary) {
        const summary = data.summary;
        console.log('[displayReportAnalysis] Generating summary with:', summary);

        const summaryHTML = `
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

        summaryDiv.innerHTML = summaryHTML;
        console.log('[displayReportAnalysis] Summary HTML set, length:', summaryHTML.length);
        console.log('[displayReportAnalysis] Summary div content after setting:', summaryDiv.innerHTML.substring(0, 200));
    } else {
        console.error('[displayReportAnalysis] Summary not generated. summaryDiv:', summaryDiv, 'data.summary:', data.summary);
    }

    // 显示详细信息
    if (detailsDiv && data.details) {
        detailsDiv.innerHTML = ``;
    }

    // 显示失败用例
    if (failuresDiv && failureList && data.failures && data.failures.length > 0) {
        failuresDiv.style.display = 'block';

        // 清空标题行按钮区域（改为在每个用例显示）
        const actionsDiv = $('report-failure-actions');
        if (actionsDiv) actionsDiv.innerHTML = '';

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
                <div style="background: var(--darker-bg); border-left: 3px solid var(--danger-color); border-radius: 4px; padding: 12px; margin-bottom: 12px; position: relative;">
                    <!-- 右上角按钮 -->
                    <div style="position: absolute; top: 8px; right: 8px; display: flex; gap: 6px;">
                        <button onclick="aiAnalyzeFailureReport('${testCaseName}', \`${reasonText.substring(0, 500).replace(/`/g, '\\`')}\`)" style="font-size: 11px; padding: 4px 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; font-weight: 500; box-shadow: 0 2px 4px rgba(102, 126, 234, 0.3);">🤖 AI+源码分析</button>
                    </div>

                    <div style="margin-bottom: 8px; padding-right: 140px;">
                        <div style="font-size: 12px; color: var(--text-secondary);">测试模块: <span style="font-weight: 600; color: var(--text-primary);">${moduleName}</span></div>
                    </div>
                    <div style="margin-bottom: 8px; padding-right: 150px;">
                        <div style="font-size: 12px; color: var(--text-secondary);">测试用例: <span style="font-family: 'Courier New', monospace; color: var(--primary-color); word-break: break-all;">${testCaseName}</span></div>
                    </div>
                    <div style="padding-right: 150px;">
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
    modal.className = 'modal show';
    modal.style.cssText = 'display: flex; z-index: 10000;';

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

        if (result.success) {
            const data = result.data;
            let content = '';

            // 根本原因
            if (data.root_cause) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid var(--warning-color);">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--warning-color);">🎯 根本原因</div>';
                content += `<div style="font-size: 13px; line-height: 1.6;">${data.root_cause}</div>`;
                content += '</div>';
            }

            // 详细分析
            if (data.analysis) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--primary-color);">📊 详细分析</div>';
                content += `<div style="font-size: 13px; line-height: 1.6; white-space: pre-wrap;">${data.analysis}</div>`;
                content += '</div>';
            }

            // 解决建议
            if (data.suggestions && data.suggestions.length > 0) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--success-color);">✅ 解决建议</div>';
                content += '<ol style="margin: 4px 0; padding-left: 20px; font-size: 13px; line-height: 1.8;">';
                data.suggestions.forEach((suggestion, index) => {
                    content += `<li style="margin-bottom: 6px;">${suggestion}</li>`;
                });
                content += '</ol></div>';
            }

            // 相关文档
            if (data.related_docs && data.related_docs.length > 0) {
                content += '<div style="padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--info-color);">📚 相关文档</div>';
                content += '<div style="display: flex; flex-direction: column; gap: 8px;">';
                data.related_docs.forEach(doc => {
                    content += `<a href="${doc.url}" target="_blank" style="display: block; padding: 8px 12px; background: var(--info-color); color: white; text-decoration: none; border-radius: 4px; font-size: 12px; transition: opacity 0.2s;" onmouseover="this.style.opacity='0.8'" onmouseout="this.style.opacity='1'">${doc.title} ↗</a>`;
                });
                content += '</div></div>';
            }

            // AI标记
            if (data.ai_enabled === false) {
                content += '<div style="margin-top: 12px; padding: 8px; background: rgba(255, 193, 7, 0.1); border-radius: 4px; text-align: center;">';
                content += '<div style="font-size: 11px; color: var(--text-secondary);">💡 基于规则的分析（AI未配置或不可用）</div>';
                content += '</div>';
            }

            modal.querySelector('.modal-body').innerHTML = content;
        } else {
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color);">分析失败: ${result.error}</div>`;
        }

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
    modal.className = 'modal show';
    modal.style.cssText = 'display: flex; z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 800px; max-height: 80vh;">
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

        // 确保modal-content使用正确的flex布局（使header固定）
        const modalContent = modal.querySelector('.modal-content');
        if (modalContent) {
            modalContent.style.display = 'flex';
            modalContent.style.flexDirection = 'column';
            modalContent.style.overflow = 'hidden';
        }

        // 确保modal-header固定
        const modalHeader = modal.querySelector('.modal-header');
        if (modalHeader) {
            modalHeader.style.flexShrink = '0';
            modalHeader.style.position = 'relative';
            modalHeader.style.zIndex = '10';
        }

        // 确保modal-body可滚动
        const modalBody = modal.querySelector('.modal-body');
        if (modalBody) {
            modalBody.style.overflowY = 'auto';
            modalBody.style.overflowX = 'hidden';
            modalBody.style.flex = '1';
            modalBody.style.minHeight = '0';
        }

        if (result.success) {
            const data = result.data;
            let content = '';

            // 测试信息
            if (data.test_info) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--primary-color);">📋 测试信息</div>';
                if (data.test_info.class) {
                    content += `<div style="font-size: 12px; margin-bottom: 4px;"><strong>类名:</strong> ${data.test_info.class}</div>`;
                }
                if (data.test_info.method) {
                    content += `<div style="font-size: 12px; margin-bottom: 4px;"><strong>方法:</strong> ${data.test_info.method}</div>`;
                }
                if (data.test_info.package) {
                    content += `<div style="font-size: 12px;"><strong>包名:</strong> ${data.test_info.package}</div>`;
                }
                content += '</div>';
            }

            // 分析结果
            if (data.analysis) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--primary-color);">🔬 分析结果</div>';
                if (data.analysis.test_type && data.analysis.test_type !== 'unknown') {
                    content += `<div style="font-size: 12px; margin-bottom: 8px;"><strong>测试类型:</strong> ${data.analysis.test_type}</div>`;
                }
                if (data.analysis.possible_causes && data.analysis.possible_causes.length > 0) {
                    content += '<div style="font-size: 12px; margin-bottom: 8px;"><strong>可能原因:</strong></div>';
                    content += '<ul style="margin: 4px 0; padding-left: 20px; font-size: 12px;">';
                    data.analysis.possible_causes.forEach(cause => {
                        content += `<li>${cause}</li>`;
                    });
                    content += '</ul>';
                }
                if (data.analysis.suggestions && data.analysis.suggestions.length > 0) {
                    content += '<div style="font-size: 12px; margin-bottom: 8px;"><strong>建议:</strong></div>';
                    content += '<ul style="margin: 4px 0; padding-left: 20px; font-size: 12px;">';
                    data.analysis.suggestions.forEach(suggestion => {
                        content += `<li>${suggestion}</li>`;
                    });
                    content += '</ul>';
                }
                content += '</div>';
            }

            // 源码分析结果
            if (data.source_analysis) {
                const sourceAnalysis = data.source_analysis;
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 12px; color: var(--primary-color);">💻 智能源码分析</div>';

                if (sourceAnalysis.source_found) {
                    content += `<div style="font-size: 12px; margin-bottom: 8px; color: var(--success-color);">✓ 已成功获取并分析源码</div>`;

                    // 显示源码路径
                    if (sourceAnalysis.file_path) {
                        content += `<div style="font-size: 12px; margin-bottom: 8px;"><strong>📁 文件路径:</strong> <span style="color: var(--primary-color);">${sourceAnalysis.file_path}</span></div>`;
                    }

                    // 解决方案部分（最重要）
                    if (sourceAnalysis.solution) {
                        const solution = sourceAnalysis.solution;
                        content += '<div style="margin: 12px 0; padding: 10px; background: rgba(76, 175, 80, 0.1); border-left: 3px solid var(--success-color); border-radius: 4px;">';
                        content += '<div style="font-size: 13px; margin-bottom: 8px; font-weight: 600; color: var(--success-color);">🎯 问题诊断</div>';
                        if (solution.problem_description) {
                            content += `<div style="font-size: 12px; margin-bottom: 8px; line-height: 1.5;">${solution.problem_description.replace(/\n/g, '<br>')}</div>`;
                        }
                        if (solution.error_type) {
                            content += `<div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 6px;">错误类型: <code style="background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 3px;">${solution.error_type}</code></div>`;
                        }
                        if (solution.fix_strategy) {
                            const strategyNames = {
                                'verify_expectations': '验证预期值',
                                'add_null_checks': '添加空值检查',
                                'verify_state': '验证状态',
                                'adjust_timeout': '调整超时',
                                'generic': '通用修复'
                            };
                            const strategyName = strategyNames[solution.fix_strategy] || solution.fix_strategy;
                            content += `<div style="font-size: 11px; color: var(--text-secondary);">修复策略: <span style="color: var(--primary-color);">${strategyName}</span></div>`;
                        }
                        content += '</div>';
                    }

                    // 显示相关代码片段
                    if (sourceAnalysis.relevant_code && sourceAnalysis.relevant_code.length > 0) {
                        content += '<div style="margin: 12px 0;">';
                        content += '<div style="font-size: 12px; margin-bottom: 8px; font-weight: 600;">📝 相关代码片段:</div>';
                        sourceAnalysis.relevant_code.slice(0, 2).forEach(code => {
                            const displayName = code.name || code.keyword || '代码片段';
                            content += '<div style="margin: 8px 0; padding: 8px; background: rgba(0,0,0,0.3); border-radius: 4px; font-family: monospace; font-size: 11px; overflow-x: auto; white-space: pre-wrap; border-left: 2px solid var(--primary-color);">';
                            content += `<div style="color: var(--primary-color); margin-bottom: 6px; font-size: 10px; text-transform: uppercase;">// ${code.type}: ${displayName}</div>`;
                            const codeText = code.code.substring(0, 800);
                            content += codeText + (code.code.length > 800 ? '\n... (代码已截断)' : '');
                            content += '</div>';
                        });
                        content += '</div>';
                    }

                    // 具体的修改建议
                    if (sourceAnalysis.suggestions && sourceAnalysis.suggestions.length > 0) {
                        content += '<div style="margin: 12px 0; padding: 10px; background: rgba(33, 150, 243, 0.1); border-radius: 4px;">';
                        content += '<div style="font-size: 12px; margin-bottom: 8px; font-weight: 600; color: var(--primary-color);">💡 修复建议</div>';
                        sourceAnalysis.suggestions.forEach((suggestion, index) => {
                            content += `<div style="font-size: 12px; margin: 6px 0; padding-left: 16px; position: relative;">`;
                            content += `<span style="position: absolute; left: 0; color: var(--primary-color); font-weight: bold;">${index + 1}.</span> ${suggestion}`;
                            content += `</div>`;
                        });
                        content += '</div>';
                    }

                    // 显示额外的分析结果
                    if (sourceAnalysis.analysis && sourceAnalysis.analysis.length > 0) {
                        content += '<div style="margin: 8px 0;">';
                        content += '<div style="font-size: 12px; margin-bottom: 4px; font-weight: 600;">🔍 详细分析:</div>';
                        sourceAnalysis.analysis.forEach(analysis => {
                            content += `<div style="font-size: 12px; margin: 4px 0; color: var(--text-secondary);">• ${analysis}</div>`;
                        });
                        content += '</div>';
                    }

                } else {
                    // 未找到源码时的错误模式分析
                    content += `<div style="font-size: 12px; color: var(--warning-color); margin-bottom: 8px;">⚠ 无法自动获取源码，基于错误模式进行分析</div>`;

                    // 显示基于错误模式的分析结果
                    if (sourceAnalysis.solution) {
                        const solution = sourceAnalysis.solution;
                        content += '<div style="margin: 12px 0; padding: 10px; background: rgba(255, 152, 0, 0.1); border-left: 3px solid var(--warning-color); border-radius: 4px;">';
                        content += '<div style="font-size: 13px; margin-bottom: 8px; font-weight: 600; color: var(--warning-color);">🎯 问题诊断</div>';
                        if (solution.problem_description) {
                            content += `<div style="font-size: 12px; margin-bottom: 8px;">${solution.problem_description}</div>`;
                        }
                        content += '</div>';
                    }

                    if (sourceAnalysis.suggestions && sourceAnalysis.suggestions.length > 0) {
                        content += '<div style="margin: 12px 0; padding: 10px; background: rgba(33, 150, 243, 0.1); border-radius: 4px;">';
                        content += '<div style="font-size: 12px; margin-bottom: 8px; font-weight: 600; color: var(--primary-color);">💡 修复建议</div>';
                        sourceAnalysis.suggestions.forEach((suggestion, index) => {
                            content += `<div style="font-size: 12px; margin: 6px 0; padding-left: 16px; position: relative;">`;
                            content += `<span style="position: absolute; left: 0; color: var(--primary-color); font-weight: bold;">${index + 1}.</span> ${suggestion}`;
                            content += `</div>`;
                        });
                        content += '</div>';
                    }

                    if (sourceAnalysis.error) {
                        content += `<div style="font-size: 11px; color: var(--text-secondary); margin-top: 8px;">技术详情: ${sourceAnalysis.error}</div>`;
                    }
                }

                content += '</div>';
            }

            // 搜索链接
            if (data.search_links && data.search_links.length > 0) {
                content += '<div style="padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--primary-color);">🔗 源码搜索链接</div>';
                content += '<div style="display: flex; flex-direction: column; gap: 8px;">';
                data.search_links.forEach(link => {
                    content += `<a href="${link.url}" target="_blank" style="display: block; padding: 8px 12px; background: var(--primary-color); color: white; text-decoration: none; border-radius: 4px; font-size: 12px; transition: opacity 0.2s;" onmouseover="this.style.opacity='0.8'" onmouseout="this.style.opacity='1'">${link.title} ↗</a>`;
                });
                content += '</div></div>';
            }

            modal.querySelector('.modal-body').innerHTML = content;
        } else {
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color);">分析失败: ${result.error}</div>`;
        }

    } catch (error) {
        modal.querySelector('.modal-title').textContent = '❌ 分析失败';
        modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color);">请求失败: ${error.message}</div>`;
    }
}

function resetReportAnalysis() {
    const resultDiv = $('report-analysis-result');
    const uploadZone = $('report-upload-zone');
    const fileInput = $('report-file-input');
    const folderInput = $('report-folder-input');

    if (resultDiv) resultDiv.style.display = 'none';
    if (fileInput) fileInput.value = '';
    if (folderInput) folderInput.value = '';

    // 重新添加上传空状态类（恢复占满屏幕）
    if (uploadZone) uploadZone.classList.add('upload-empty');

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
            displayAIAnalysis(result.data, testName, errorMessage);
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
 * @param {string} errorMessage - 错误消息
 */
function displayAIAnalysis(data, testName, errorMessage = '') {
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
                <h2 style="margin: 0; font-size: 18px; font-weight: 600;">🤖 AI+源码分析报告</h2>
                <div style="display: flex; align-items: center; gap: 10px;">
                    ${data.source_code_fetched ? '<span style="font-size: 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 3px 10px; border-radius: 4px;">✓ 源码已获取</span>' : ''}
                    ${data.ai_enabled === false ? '<span style="font-size: 10px; background: var(--warning-color); color: white; padding: 2px 8px; border-radius: 4px;">规则分析</span>' : '<span style="font-size: 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 2px 8px; border-radius: 4px;">AI增强</span>'}
                    ${data.ai_model ? `<span style="font-size: 10px; background: var(--success-color); color: white; padding: 2px 8px; border-radius: 4px;">${data.ai_model}</span>` : ''}
                    <button onclick="closeAIAnalysisModal('${modalId}')" style="background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-secondary);">×</button>
                </div>
            </div>
    `;

    // 源码信息
    if (data.source_code_fetched && data.source_url) {
        html += `
            <div style="background: linear-gradient(135deg, rgba(102, 126, 234, 0.1) 0%, rgba(118, 75, 162, 0.1) 100%); border-left: 4px solid #667eea; border-radius: 8px; padding: 14px; margin-bottom: 16px;">
                <div style="font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #667eea;">💻 源码信息</div>
                <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 6px;">文件路径: ${data.source_file_path || 'N/A'}</div>
                <a href="${data.source_url}" target="_blank" style="font-size: 11px; color: #667eea; text-decoration: none; display: inline-flex; align-items: center; gap: 4px;">
                    🔗 查看源码
                    <svg style="width: 12px; height: 12px;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path>
                    </svg>
                </a>
            </div>
        `;
    }


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

    // 深度源码分析按钮区域
    html += `
            <div style="background: linear-gradient(135deg, rgba(102, 126, 234, 0.05) 0%, rgba(118, 75, 162, 0.05) 100%); border: 1px dashed #667eea; border-radius: 8px; padding: 16px; margin-top: 20px; margin-bottom: 16px;">
                <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px;">
                    <div style="flex: 1;">
                        <div style="font-size: 13px; font-weight: 600; margin-bottom: 4px; color: #667eea;">💻 深度源码分析</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">从 Android 源码仓库获取完整测试用例代码，进行深入的规则分析</div>
                    </div>
                    <button onclick="closeAIAnalysisModal('${modalId}'); analyzeFailureSource('${testName.replace(/'/g, "\\'")}', '${(data.error_message || '').replace(/'/g, "\\'")}')" class="btn-xs" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); white-space: nowrap; padding: 8px 16px;">
                        🔍 开始深度分析
                    </button>
                </div>
            </div>
    `;

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
