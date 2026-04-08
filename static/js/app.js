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

// Debug flag - set to false in production to disable console logs
const DEBUG = false;

// API文档缓存（全局变量，避免重复请求）
let apiDocsCache = null;
let apiDocsCacheTime = 0;
let allApiDocs = []; // 所有API文档数据（已排序）
const API_DOCS_CACHE_DURATION = 5 * 60 * 1000; // 5分钟缓存（生产环境）

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

// Debug logger wrapper (only logs when DEBUG is true)
function debugLog(...args) {
    if (DEBUG) {
        console.log(...args);
    }
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

    // 立即加载配置
    await loadConfig();
    loadDevices();
    initDragDrop();
    await checkInitialTestStatus();
    startStatusPolling();

    // 延迟执行耗时操作，不阻塞页面加载
    setTimeout(async () => {
        // 立即获取客户端信息（使用/api/users/current）
        try {
            const currentUserResponse = await fetch('/api/users/current');
            if (currentUserResponse.ok) {
                const userData = await currentUserResponse.json();
                if (userData.client_id) {
                    state.clientId = userData.client_id;
                    debugLog('[Init] Set state.clientId from /api/users/current:', state.clientId);

                    // 检查是否是unknown用户（apiCall中会统一处理弹框）
                    if (userData.client_id.startsWith('unknown@')) {
                        console.warn('[Init] Detected unknown client, will show username modal via apiCall');
                    }
                }
            } else {
                console.warn('[Init] Failed to call /api/users/current');
            }
        } catch (error) {
            console.warn('[Init] Error getting current user:', error);
        }

        // 检查状态
        await Promise.all([
            checkUsbipStatus(),
            checkVpnStatus()
        ]);

        // 自动启动 VNC 服务
        try {
            await initAndStartVnc();
        } catch (error) {
            console.warn('[Init] Failed to auto-start VNC:', error);
        }
    }, 100);  // 减少延迟时间，更快获取客户端信息
});

// ==================== Configuration ====================
async function loadConfig() {
    try {
        const config = await apiCall('/api/config/read', 'GET');
        state.config = config;
    } catch (error) {
        console.error('Failed to load config:', error);
        state.config = { ubuntu_user: 'hcq' };  // Fallback
    }
}

// ==================== WebSocket Connection (FastAPI) ====================
function initWebSocket() {
    // 获取客户端ID
    apiCall('/api/users/current', 'GET').then(data => {
        const clientId = data.client_id || 'unknown';
        state.clientId = clientId;

        // 建立WebSocket连接
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/api/system/websocket/${clientId}`;

        debugLog(`[WebSocket] Connecting to: ${wsUrl}`);
        state.websocket = new WebSocket(wsUrl);

        state.websocket.onopen = () => {
            debugLog('[WebSocket] Connected');
            updateConnectionStatus(true);
            addLogEntry(`WebSocket已连接 (Client ID: ${clientId})`, 'success');
        };

        state.websocket.onclose = () => {
            debugLog('[WebSocket] Disconnected');
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
                            let updated = false;
                            data.devices.forEach(update => {
                                const deviceId = update.device_id;
                                console.log(`[Device Lock] Updating ${deviceId}: locked=${update.locked}, by=${update.locked_by}`);
                                // 更新 state.devices 中的锁定状态
                                const device = state.devices.find(d => {
                                    const id = typeof d === 'string' ? d : d.device_id;
                                    return id === deviceId;
                                });
                                if (device) {
                                    updated = true;
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
                            if (updated) {
                                console.log('[Device Lock] Re-rendering devices...');
                                try {
                                    renderDevices();
                                    console.log('[Device Lock] Render completed successfully');
                                } catch (error) {
                                    console.error('[Device Lock] Render failed:', error);
                                }
                            } else {
                                console.warn('[Device Lock] No devices were updated, skipping render');
                            }
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
        debugLog('Connected to server');
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
        // 刷新设备列表，更新设备锁定状态
        loadDevices(true);
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
    apiCall('/api/config/update', 'POST', { device_host: deviceHost });
}

function onLocalServerConfirm() {
    const localServer = $('local-server').value.trim();
    addLogEntry(`本地主机地址已更新: ${localServer}`, 'info');
    showToast('本地主机地址已更新', 'success');
    // Save to backend
    apiCall('/api/config/update', 'POST', { local_server: localServer });
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
        const headers = {
            'Content-Type': 'application/json'
        };

        // 添加客户端用户名请求头（如果可用）
        if (state.clientId && state.clientId !== 'unknown') {
            const username = state.clientId.split('@')[0];
            headers['X-Client-Username'] = username;
            console.log(`[apiCall] Adding X-Client-Username: ${username} for URL: ${url}`);
        } else {
            console.warn(`[apiCall] No valid clientId available. state.clientId: ${state.clientId}, URL: ${url}`);
        }

        const options = {
            method,
            headers
        };

        // Only add body for POST/PUT/PATCH/DELETE methods (not GET/HEAD)
        if (data && !['GET', 'HEAD'].includes(method.toUpperCase())) {
            options.body = JSON.stringify(data);
        }

        const response = await fetch(url, options);
        const result = await response.json();

        // 如果API返回了client_id，更新state.clientId
        if (result.client_id) {
            const oldClientId = state.clientId;
            state.clientId = result.client_id;

            // 检查是否是unknown用户
            if (result.client_id.startsWith('unknown@')) {
                console.warn(`[apiCall] Detected unknown client: ${result.client_id}`);

                // 只在第一次检测到unknown时显示弹框（避免重复弹窗）
                if (!state.usernameDetectShown) {
                    state.usernameDetectShown = true;
                    console.log('[apiCall] Showing username detect modal for:', result.ip);

                    // 延迟显示弹框，确保页面已加载完成
                    setTimeout(() => {
                        showUsernameDetectModal(result.ip);
                    }, 500);
                }
            } else if (oldClientId !== result.client_id) {
                console.log(`[apiCall] Updated state.clientId: ${oldClientId} → ${result.client_id}`);
            }
        }

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
        const url = forceRefresh ? '/api/devices/list?force_refresh=1' : '/api/devices/list';
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

    // Early return if containers not ready
    if (!leftContainer || !rightContainer || !deviceCanvas) return;

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
        const response = await fetch(`/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`, {
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

    await executeBurnOperation('/api/burn/gsi', {
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
        const result = await apiCall('/api/desktop/vnc/start', 'POST');
        addLogEntry(result.message || 'VNC 服务已就绪', 'info');
        return result;
    } catch (error) {
        addLogEntry('启动 VNC 失败: ' + error.message, 'error');
        throw error;
    }
}

// 启动默认主机VNC服务的共享函数
async function startDefaultHostVNC(defaultHost, defaultPassword, vncPassword, fallbackUrl) {
    try {
        showToast('正在启动默认主机VNC服务...', 'info');
        const result = await apiCall('/api/desktop/vnc/start', 'POST', {
            host: defaultHost,
            password: defaultPassword,
            vnc_password: vncPassword || ''
        });

        if (result.success && result.url) {
            console.log('[Desktop] Default host VNC started');
            return result.url;
        } else {
            // API失败，使用备用URL
            return fallbackUrl;
        }
    } catch (e) {
        console.error('[Desktop] Failed to start default host VNC:', e);
        // 异常时也使用备用URL
        return fallbackUrl;
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
        const result = await apiCall('/api/devices/screen', 'POST', {
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

                // 检查是否需要SSH密码
                if (result.need_password && result.device_host) {
                    showDevicePasswordModal(result.device_host);
                    addLogEntry('需要输入SSH密码以连接到 ' + result.device_host, 'warning');
                } else if (result.error && result.error.includes('SSH连接失败')) {
                    addLogEntry('⚠️ SSH 连接失败，请点击 "📡 检查SSHD" 按钮检查SSH服务状态', 'warning');
                } else if (result.install_guide) {
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

            // 检查是否需要SSH密码
            if (error.needPassword && error.deviceHost) {
                showDevicePasswordModal(error.deviceHost);
                addLogEntry('需要输入SSH密码以连接到 ' + error.deviceHost, 'warning');
            } else if (error.installGuide) {
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

// ==================== Username Detection Modal ====================
function showUsernameDetectModal(clientIp) {
    document.getElementById('username-detect-ip').value = clientIp;
    document.getElementById('username-detect-username').value = '';
    document.getElementById('username-detect-password').value = '';
    const modal = document.getElementById('username-detect-modal');
    modal.classList.add('show');
    document.getElementById('username-detect-username').focus();

    // Add ESC key listener
    document.addEventListener('keydown', handleUsernameDetectEsc);
}

function closeUsernameDetectModal() {
    const modal = document.getElementById('username-detect-modal');
    modal.classList.remove('show');
    document.removeEventListener('keydown', handleUsernameDetectEsc);
}

function handleUsernameDetectEsc(event) {
    if (event.key === 'Escape') {
        closeUsernameDetectModal();
    }
}

function handleUsernameDetectKeyPress(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        if (event.target.id === 'username-detect-password') {
            submitUsernameDetect();
        }
    }
}

async function submitUsernameDetect() {
    const clientIp = document.getElementById('username-detect-ip').value;
    const username = document.getElementById('username-detect-username').value.trim();
    const password = document.getElementById('username-detect-password').value;

    if (!username) {
        showToast('请输入用户名', 'error');
        return;
    }

    if (!password) {
        showToast('请输入SSH密码', 'error');
        return;
    }

    try {
        // 显示加载状态
        const submitBtn = document.querySelector('#username-detect-modal .btn-primary');
        const originalText = submitBtn.textContent;
        submitBtn.textContent = '验证中...';
        submitBtn.disabled = true;

        // 调用用户名检测API
        const response = await apiCall('/api/users/detect', 'POST', {
            ip: clientIp,
            username: username,
            password: password
        });

        if (response.success) {
            showToast(`✅ 用户名验证成功: ${username}`, 'success');
            addLogEntry(`客户端识别成功: ${username}@${clientIp}`, 'success');

            // 更新state.clientId
            state.clientId = `${username}@${clientIp}`;
            debugLog('[UsernameDetect] Updated state.clientId:', state.clientId);

            closeUsernameDetectModal();
        } else {
            showToast(`❌ 用户名验证失败: ${response.error || '未知错误'}`, 'error');
        }
    } catch (error) {
        console.error('[UsernameDetect] Error:', error);
        showToast(`❌ 验证失败: ${error.message}`, 'error');
    } finally {
        // 恢复按钮状态
        const submitBtn = document.querySelector('#username-detect-modal .btn-primary');
        submitBtn.textContent = '确定';
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
        const result = await apiCall('/api/ssh/sshd-check', 'GET');

        if (!result.installed) {
            // SSHD 未安装，显示安装指南
            // 优先使用 API 返回的指南，否则从服务器获取
            let guide = result.install_guide;
            if (!guide) {
                guide = await getSshdInstallGuide();
            }
            showSshdInstallGuide(guide);
        } else if (result.running) {
            addLogEntry(`SSHD 状态: 运行中`, 'success');
        } else {
            addLogEntry(`SSHD 状态: 已安装但未运行`, 'warning');
        }

        // 如果有错误信息，显示警告
        if (result.error) {
            addLogEntry(`⚠️ ${result.error}`, 'warning');
        }
    } catch (error) {
        addLogEntry('检查 SSHD 失败: ' + error.message, 'error');
        // 即使检查失败，也尝试从服务器获取安装指南
        try {
            const guide = await getSshdInstallGuide();
            showSshdInstallGuide(guide);
        } catch (guideError) {
            addLogEntry('无法加载安装指南', 'error');
        }
    }
}

// 获取 SSHD 安装指南（从服务器加载）
async function getSshdInstallGuide() {
    try {
        const result = await apiCall('/api/ssh/sshd-guide', 'GET');
        return result.install_guide || '无法加载安装指南，请刷新页面重试';
    } catch (error) {
        console.error('Failed to load SSHD install guide:', error);
        return '无法加载安装指南，请检查网络连接后重试';
    }
}

async function checkRouting() {
    // 创建弹框
    const dialog = document.createElement('div');
    dialog.className = 'route-check-dialog';
    dialog.innerHTML = `
        <div class="route-check-content">
            <div class="route-check-header">
                <h3>📡 检查路由连通性</h3>
                <button class="route-check-close" aria-label="关闭">&times;</button>
            </div>
            <div class="route-check-form">
                <div class="form-group">
                    <label for="test-host-ip">测试主机IP:</label>
                    <input type="text" id="test-host-ip" placeholder="例如: 192.168.1.100" />
                    <small>从配置文件读取的ubuntu_host</small>
                </div>
                <div class="form-group">
                    <label for="client-ip">客户端IP:</label>
                    <input type="text" id="client-ip" placeholder="例如: 192.168.2.100" />
                    <small>您当前浏览器的IP地址</small>
                </div>
                <div class="route-check-actions">
                    <button id="ping-test-btn" class="btn-primary">🔍 测试连通性</button>
                    <button id="close-dialog-btn" class="btn-secondary">关闭</button>
                </div>
                <div id="ping-result" class="ping-result"></div>
            </div>
        </div>
    `;

    document.body.appendChild(dialog);

    // 获取配置中的默认值
    try {
        const config = await apiCall('/api/config/read', 'GET');
        if (config.ubuntu_host) {
            const testHostIp = document.getElementById('test-host-ip');
            testHostIp.value = config.ubuntu_host.split('@').pop(); // 提取IP部分
        }
    } catch (error) {
        console.error('获取配置失败:', error);
    }

    // 绑定事件
    const pingTestBtn = document.getElementById('ping-test-btn');
    const closeDialogBtn = document.getElementById('close-dialog-btn');
    const closeXBtn = dialog.querySelector('.route-check-close');
    const pingResult = document.getElementById('ping-result');

    // X 按钮关闭
    closeXBtn.addEventListener('click', () => {
        document.body.removeChild(dialog);
    });

    closeDialogBtn.addEventListener('click', () => {
        document.body.removeChild(dialog);
    });

    pingTestBtn.addEventListener('click', async () => {
        const testHostIp = document.getElementById('test-host-ip').value.trim();
        const clientIp = document.getElementById('client-ip').value.trim();

        if (!testHostIp || !clientIp) {
            pingResult.innerHTML = '<div class="ping-error">请填写测试主机IP和客户端IP</div>';
            return;
        }

        // 验证IP格式
        function isValidIP(ip) {
            const parts = ip.split('.');
            if (parts.length !== 4) return false;
            return parts.every(part => {
                const num = parseInt(part, 10);
                return !isNaN(num) && num >= 0 && num <= 255 && part === num.toString();
            });
        }

        if (!isValidIP(testHostIp) || !isValidIP(clientIp)) {
            pingResult.innerHTML = '<div class="ping-error">IP地址格式不正确，请输入有效的IPv4地址 (例如: 192.168.1.100)</div>';
            return;
        }

        pingResult.innerHTML = '<div class="ping-testing">🔄 正在测试连通性，请稍候...</div>';

        try {
            // 首先尝试使用新的POST API
            let result;
            try {
                result = await apiCall('/api/ssh/route/ping', 'POST', {
                    test_host_ip: testHostIp,
                    client_ip: clientIp
                });
            } catch (postError) {
                // 如果POST API不可用（服务器未重启），使用GET API作为后备
                console.log('POST API不可用，使用GET API作为后备');
                pingResult.innerHTML = '<div class="ping-testing">🔄 使用备用方法测试中...</div>';

                // 使用现有的GET API，但手动分析结果
                const testNetwork = testHostIp.split('.').slice(0, 3).join('.') + '.0';
                const clientNetwork = clientIp.split('.').slice(0, 3).join('.') + '.0';
                const sameNetwork = (testNetwork === clientNetwork);

                // 生成路由命令
                // 注意：这些命令应该在测试主机上执行
                // 需要通过测试主机的网关来访问客户端网段
                const testGateway = testNetwork.split('.').slice(0, 3).join('.1');  // 测试主机网关 (例如: 172.16.14.1)

                const routeCommands = {
                    windows: [
                        `# 在测试主机上执行以下命令:`,
                        `# 如果客户端主机在不同网段，需要添加路由到客户端主机所在的网关`,
                        `route add ${clientNetwork} mask 255.255.255.0 ${testGateway}`,
                        `# 检查路由表: route print`,
                        `# 删除路由: route delete ${clientNetwork}`
                    ],
                    linux: [
                        `# 在测试主机上执行以下命令:`,
                        `# 如果客户端主机在不同网段，需要添加路由到客户端主机所在的网关`,
                        `sudo ip route add ${clientNetwork}/24 via ${testGateway}`,
                        `# 检查路由表: ip route show`,
                        `# 删除路由: sudo ip route del ${clientNetwork}/24`
                    ],
                    note: [
                        `⚠️ 重要提示:`,
                        `1. 这些路由命令应该在测试主机上执行`,
                        `2. ${testGateway} 是测试主机的网关地址`,
                        `3. 确保网关地址可以ping通后再添加路由`,
                        `4. 如果已经在同一网段，不需要添加路由`,
                        `5. 删除路由前请确保不会影响SSH连接`
                    ]
                };

                result = {
                    success: true,
                    reachable: sameNetwork,
                    latency: sameNetwork ? '<1ms (同一网段)' : 'N/A',
                    same_network: sameNetwork,
                    test_host_ip: testHostIp,
                    client_ip: clientIp,
                    test_network: testNetwork,
                    client_network: clientNetwork,
                    route_commands: routeCommands
                };
            }

            if (result.success) {
                if (result.reachable) {
                    pingResult.innerHTML = `
                        <div class="ping-success">
                            <h4>✅ 连通性测试通过</h4>
                            <p><strong>测试主机:</strong> ${result.test_host_ip || testHostIp}</p>
                            <p><strong>测试主机网段:</strong> ${result.test_network || 'N/A'}</p>
                            <p><strong>客户端:</strong> ${result.client_ip || clientIp}</p>
                            <p><strong>客户端网段:</strong> ${result.client_network || 'N/A'}</p>
                            <p>状态: <span class="status-success">${result.same_network ? '同一网段 - 可连通' : '不同网段但可连通'}</span></p>
                            <p>延迟: ${result.latency || 'N/A'}</p>
                            <p>✅ 网络配置正常，无需添加路由</p>
                        </div>
                    `;
                } else {
                    pingResult.innerHTML = `
                        <div class="ping-failure">
                            <h4>❌ 连通性测试失败</h4>
                            <p><strong>测试主机:</strong> ${result.test_host_ip || testHostIp}</p>
                            <p><strong>测试主机网段:</strong> ${result.test_network || 'N/A'}</p>
                            <p><strong>客户端:</strong> ${result.client_ip || clientIp}</p>
                            <p><strong>客户端网段:</strong> ${result.client_network || 'N/A'}</p>
                            <p>状态: <span class="status-error">不同网段 - 不可连通</span></p>
                            <p><strong>可能原因:</strong></p>
                            <ul>
                                <li>客户端和测试主机不在同一网段</li>
                                <li>缺少必要的路由配置</li>
                                <li>防火墙阻止了连接</li>
                            </ul>
                            <p><strong>⚠️ 重要提示 - 请仔细阅读:</strong></p>
                            <div class="route-warning">
                                <p>✅ 以下命令应该在您的<strong>测试主机</strong>（${testHostIp}）上执行</p>
                                <p>❌ 不要在客户端主机（当前浏览器所在电脑）上执行这些命令</p>
                                <p><strong>🎯 路由目的：</strong>让测试主机能够访问客户端主机网段</p>
                            </div>
                            <p><strong>建议添加的路由命令:</strong></p>
                            <div class="route-commands">
                                <h5>Linux:</h5>
                                <pre id="linux-route-command">${result.route_commands?.linux?.[2] || '无'}</pre>
                                <h5>Windows:</h5>
                                <pre id="windows-route-command">${result.route_commands?.windows?.[2] || '无'}</pre>
                            </div>
                            <div class="route-check-terminal-actions">
                                <button id="open-terminal-btn" class="btn-terminal" data-command="${result.route_commands?.linux?.[2] || ''}">
                                    🖥️ 打开主机终端添加路由
                                </button>
                            </div>
                        </div>
                    `;

                    // 绑定打开终端按钮事件
                    const openTerminalBtn = document.getElementById('open-terminal-btn');
                    if (openTerminalBtn) {
                        openTerminalBtn.addEventListener('click', async () => {
                            const command = openTerminalBtn.dataset.command;
                            if (!command || command === '无') {
                                addLogEntry('没有可用的路由命令', 'warning');
                                return;
                            }

                            try {
                                // 保存命令到 sessionStorage，供终端页面使用
                                sessionStorage.setItem('pending_terminal_command', command);
                                sessionStorage.setItem('command_source', 'route_check');

                                // 关闭路由检查弹框
                                document.body.removeChild(dialog);

                                // 切换到终端页面
                                if (typeof switchPage === 'function') {
                                    switchPage('terminal');
                                } else {
                                    // 如果 switchPage 不在全局作用域，使用 DOM 操作
                                    const event = new Event('click');
                                    const terminalLink = document.querySelector('[data-page="terminal"]');
                                    if (terminalLink) {
                                        terminalLink.dispatchEvent(event);
                                    }
                                }

                                addLogEntry(`✅ 已切换到终端页面，命令已准备: ${command}`, 'success');

                            } catch (error) {
                                addLogEntry('打开终端失败: ' + error.message, 'error');
                                console.error('Error opening terminal:', error);
                            }
                        });
                    }
                }
            } else {
                pingResult.innerHTML = `<div class="ping-error">测试失败: ${result.error}</div>`;
            }
        } catch (error) {
            pingResult.innerHTML = `<div class="ping-error">测试失败: ${error.message}</div>`;
        }
    });

    // 点击背景关闭
    dialog.addEventListener('click', (e) => {
        if (e.target === dialog) {
            document.body.removeChild(dialog);
        }
    });
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
        xhr.open('POST', '/api/files/upload');
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
        await stopTest();
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
            link.href = '/api/test/logs/current';
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
            const status = await apiCall(hasRealtimeConnection ? '/api/test/status?logs=false' : '/api/test/status');

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
        const status = await apiCall('/api/test/status');
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
                <td style="padding: 12px; text-align: center; font-family: monospace; font-size: 11px;">
                    ${displayClient}
                </td>
                <td style="padding: 12px; text-align: center; font-weight: 700; font-size: 12px; ${typeStyle}">
                    ${testType}
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
                    <button class="btn-xxs" onclick="event.stopPropagation(); claudeAnalyzeReport('${report.timestamp}')" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; font-weight: 600;">🤖 Claude分析</button>
                    <button class="btn-xxs" onclick="event.stopPropagation(); analyzeReport('${report.timestamp}')">📈 分析报告</button>
                    <button class="btn-xxs" onclick="event.stopPropagation(); viewReportDetails('${report.timestamp}')">📄 查看报告</button>
                    <button class="btn-xxs" onclick="event.stopPropagation(); retryReportWithSuite('${report.timestamp}', '${report.test_type || ''}', '${(report.suite_path || '').replace(/'/g, "\\'")}')" style="background: var(--primary-color);">🔄 retry报告</button>
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

/**
 * 使用Claude分析测试报告
 */
async function claudeAnalyzeReport(timestamp) {
    console.log(`[Claude Analyze] 分析报告: ${timestamp}`);

    // 显示分析中提示
    showToast('🤖 正在使用Claude分析报告...', 'info');

    try {
        // 询问是否使用Claude API
        const useClaudeApi = confirm('是否使用Claude API进行深度分析？\n\n确定 = 使用Claude API (需要API密钥)\n取消 = 仅基础分析 (免费)');

        let apiUrl = `/api/reports/claude-analyze/${encodeURIComponent(timestamp)}`;

        if (useClaudeApi) {
            // 获取Claude API密钥
            const apiKey = prompt('请输入Claude API密钥 (sk-ant-xxxxx):');
            if (!apiKey) {
                showToast('已取消分析', 'info');
                return;
            }
            apiUrl += `?use_claude_api=true&claude_api_key=${encodeURIComponent(apiKey)}`;
        }

        const response = await fetch(apiUrl);
        const result = await response.json();

        if (!result.success) {
            showToast('分析失败: ' + (result.error || '未知错误'), 'error');
            return;
        }

        // 显示分析结果弹窗
        showClaudeAnalysisResult(result, timestamp);

    } catch (error) {
        console.error('[Claude Analyze] Error:', error);
        showToast('分析失败: ' + error.message, 'error');
    }
}

/**
 * 显示Claude分析结果弹窗
 */
function showClaudeAnalysisResult(result, timestamp) {
    const basic = result.basic_analysis;
    const claude = result.claude_analysis;

    // 创建弹窗HTML
    const modalHtml = `
        <div id="claude-analysis-modal" class="modal" style="display: flex;">
            <div class="modal-content" style="max-width: 900px; max-height: 90vh;">
                <div class="modal-header">
                    <span class="modal-title">🤖 Claude分析报告 - ${timestamp}</span>
                    <span class="modal-close" onclick="closeClaudeAnalysisModal()">&times;</span>
                </div>
                <div class="modal-body" style="padding: 20px; overflow-y: auto;">
                    <!-- 基础分析 -->
                    <div style="margin-bottom: 24px;">
                        <h3 style="color: var(--primary-color); margin-bottom: 12px;">📊 测试概要</h3>
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px;">
                            <div style="background: var(--light-bg); padding: 16px; border-radius: 8px; text-align: center;">
                                <div style="font-size: 24px; font-weight: bold; color: ${basic.summary.failed === 0 ? 'var(--success-color)' : 'var(--danger-color)'};">
                                    ${basic.summary.status}
                                </div>
                                <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">状态</div>
                            </div>
                            <div style="background: var(--light-bg); padding: 16px; border-radius: 8px; text-align: center;">
                                <div style="font-size: 24px; font-weight: bold; color: var(--primary-color);">
                                    ${basic.summary.total_tests}
                                </div>
                                <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">总测试数</div>
                            </div>
                            <div style="background: var(--light-bg); padding: 16px; border-radius: 8px; text-align: center;">
                                <div style="font-size: 24px; font-weight: bold; color: var(--success-color);">
                                    ${basic.summary.passed}
                                </div>
                                <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">通过</div>
                            </div>
                            <div style="background: var(--light-bg); padding: 16px; border-radius: 8px; text-align: center;">
                                <div style="font-size: 24px; font-weight: bold; color: var(--danger-color);">
                                    ${basic.summary.failed}
                                </div>
                                <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">失败</div>
                            </div>
                            <div style="background: var(--light-bg); padding: 16px; border-radius: 8px; text-align: center;">
                                <div style="font-size: 20px; font-weight: bold; color: var(--text-primary);">
                                    ${basic.summary.duration || '-'}
                                </div>
                                <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">耗时</div>
                            </div>
                            <div style="background: var(--light-bg); padding: 16px; border-radius: 8px; text-align: center;">
                                <div style="font-size: 20px; font-weight: bold; color: var(--warning-color);">
                                    ${basic.summary.retry_success}/${basic.summary.retry_failure}
                                </div>
                                <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">重试</div>
                            </div>
                        </div>
                    </div>

                    <!-- 测试信息 -->
                    <div style="margin-bottom: 24px;">
                        <h3 style="color: var(--primary-color); margin-bottom: 12px;">📋 测试信息</h3>
                        <div style="background: var(--light-bg); padding: 16px; border-radius: 8px;">
                            <div style="margin-bottom: 8px;">
                                <strong>设备:</strong> <code style="background: var(--darker-bg); padding: 4px 8px; border-radius: 4px;">${basic.test_info.device}</code>
                            </div>
                            <div style="margin-bottom: 8px;">
                                <strong>模块:</strong> <code style="background: var(--darker-bg); padding: 4px 8px; border-radius: 4px;">${basic.test_info.module}</code>
                            </div>
                            <div>
                                <strong>测试用例:</strong> <code style="background: var(--darker-bg); padding: 4px 8px; border-radius: 4px; word-break: break-all;">${basic.test_info.test_case}</code>
                            </div>
                        </div>
                    </div>

                    <!-- 智能洞察 -->
                    ${basic.insights.length > 0 ? `
                    <div style="margin-bottom: 24px;">
                        <h3 style="color: var(--primary-color); margin-bottom: 12px;">💡 智能洞察</h3>
                        ${basic.insights.map(insight => `
                            <div style="background: ${insight.type === 'error' ? 'rgba(239, 68, 68, 0.1)' : insight.type === 'warning' ? 'rgba(245, 158, 11, 0.1)' : insight.type === 'success' ? 'rgba(16, 185, 129, 0.1)' : 'rgba(59, 130, 246, 0.1)'}; padding: 12px; border-radius: 8px; border-left: 4px solid ${insight.type === 'error' ? 'var(--danger-color)' : insight.type === 'warning' ? 'var(--warning-color)' : insight.type === 'success' ? 'var(--success-color)' : 'var(--primary-color)'}; margin-bottom: 12px;">
                                <div style="display: flex; gap: 12px; align-items: start;">
                                    <span style="font-size: 20px;">${insight.icon}</span>
                                    <div style="flex: 1;">
                                        <div style="font-weight: 600; margin-bottom: 4px; color: var(--text-primary);">${insight.title}</div>
                                        <div style="font-size: 13px; color: var(--text-secondary);">${insight.message}</div>
                                    </div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                    ` : ''}

                    <!-- Claude深度分析 -->
                    ${claude && claude.success ? `
                    <div style="margin-bottom: 24px;">
                        <h3 style="color: var(--primary-color); margin-bottom: 12px;">🤖 Claude深度分析</h3>
                        <div style="background: linear-gradient(135deg, rgba(102, 126, 234, 0.1) 0%, rgba(118, 75, 162, 0.1) 100%); padding: 16px; border-radius: 8px; border: 1px solid rgba(102, 126, 234, 0.2);">
                            <div style="font-size: 13px; line-height: 1.6; color: var(--text-primary); white-space: pre-wrap;">${claude.analysis}</div>
                        </div>
                    </div>
                    ` : ''}

                    <!-- 路径信息 -->
                    <div>
                        <h3 style="color: var(--primary-color); margin-bottom: 12px;">📁 文件路径</h3>
                        <div style="background: var(--light-bg); padding: 16px; border-radius: 8px;">
                            ${basic.paths.log_dir ? `<div style="margin-bottom: 8px;"><strong>日志目录:</strong> <code style="background: var(--darker-bg); padding: 4px 8px; border-radius: 4px; font-size: 11px; word-break: break-all;">${basic.paths.log_dir}</code></div>` : ''}
                            ${basic.paths.result_dir ? `<div><strong>结果目录:</strong> <code style="background: var(--darker-bg); padding: 4px 8px; border-radius: 4px; font-size: 11px; word-break: break-all;">${basic.paths.result_dir}</code></div>` : ''}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    // 添加到页面
    const modalContainer = document.createElement('div');
    modalContainer.innerHTML = modalHtml;
    document.body.appendChild(modalContainer);

    // 显示弹窗
    const modal = document.getElementById('claude-analysis-modal');
    if (modal) {
        modal.style.display = 'flex';
    }
}

/**
 * 关闭Claude分析弹窗
 */
function closeClaudeAnalysisModal() {
    const modal = document.getElementById('claude-analysis-modal');
    if (modal) {
        modal.style.display = 'none';
        setTimeout(() => {
            modal.remove();
        }, 300);
    }
}

async function retryReport(timestamp, testType) {
    try {
        // 先切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            console.log(`[Retry] 开始填充数据, timestamp=${timestamp}, testType=${testType}`);

            // 填入测试报告名称（字段ID是 retry-result）
            const reportNameInput = document.getElementById('retry-result');
            if (reportNameInput) {
                reportNameInput.value = timestamp;
                console.log(`[Retry] 已填入报告名称: ${timestamp}`);
            } else {
                console.error('[Retry] 未找到 retry-result 元素');
            }

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect) {
                if (testType) {
                    testTypeSelect.value = testType;
                    console.log(`[Retry] 已设置测试类型: ${testType}, 当前值: ${testTypeSelect.value}`);
                } else {
                    console.warn('[Retry] testType 为空');
                }
            } else {
                console.error('[Retry] 未找到 test-type 元素');
            }

            // 根据测试类型填入测试套件路径
            const suitePathInput = document.getElementById('suite-path');
            if (suitePathInput) {
                // 根据测试类型设置默认路径
                const suitePaths = {
                    'CTS': 'android-cts',
                    'GSI': 'android-gsi',
                    'GTS': 'android-gts',
                    'STS': 'android-sts',
                    'VTS': 'android-vts',
                    'APTS': 'android-apts'
                };

                // 如果有匹配的测试类型，使用对应的路径
                if (testType && suitePaths[testType]) {
                    suitePathInput.value = suitePaths[testType];
                    console.log(`[Retry] 已设置测试套件路径: ${suitePaths[testType]}, 当前值: ${suitePathInput.value}`);
                } else {
                    console.warn(`[Retry] testType=${testType} 没有对应的套件路径`);
                }
            } else {
                console.error('[Retry] 未找到 suite-path 元素');
            }

            // 打印所有相关元素的值以便调试
            console.log('[Retry] 当前字段值:', {
                reportName: document.getElementById('retry-result')?.value,
                testType: document.getElementById('test-type')?.value,
                suitePath: document.getElementById('suite-path')?.value
            });
        }, 200);

        showToast(`已填入报告名称: ${timestamp}${testType ? ' (类型: ' + testType + ')' : ''}`, 'success');

        // 可选：自动开始测试（如果需要的话，取消下面的注释）
        // setTimeout(() => {
        //     startTest();
        // }, 500);
    } catch (error) {
        console.error('Retry report error:', error);
        showToast('操作失败: ' + error.message, 'error');
    }
}

async function retryReportWithSuite(timestamp, testType, suitePath) {
    try {
        // 先切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            console.log(`[Retry] 开始填充数据, timestamp=${timestamp}, testType=${testType}, suitePath=${suitePath}`);

            // 填入测试报告名称（字段ID是 retry-result）
            const reportNameInput = document.getElementById('retry-result');
            if (reportNameInput) {
                reportNameInput.value = timestamp;
                console.log(`[Retry] 已填入报告名称: ${timestamp}`);
            } else {
                console.error('[Retry] 未找到 retry-result 元素');
            }

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect) {
                if (testType) {
                    testTypeSelect.value = testType;
                    console.log(`[Retry] 已设置测试类型: ${testType}, 当前值: ${testTypeSelect.value}`);
                } else {
                    console.warn('[Retry] testType 为空');
                }
            } else {
                console.error('[Retry] 未找到 test-type 元素');
            }

            // 填入测试套件路径（优先使用原始路径，否则使用默认路径）
            const suitePathInput = document.getElementById('suite-path');
            if (suitePathInput) {
                if (suitePath && suitePath !== 'null' && suitePath !== '') {
                    // 使用报告中的原始测试套件路径
                    suitePathInput.value = suitePath;
                    console.log(`[Retry] 已设置测试套件路径(原始): ${suitePath}, 当前值: ${suitePathInput.value}`);
                } else {
                    // 根据测试类型设置默认路径
                    const suitePaths = {
                        'CTS': 'android-cts',
                        'GSI': 'android-gsi',
                        'GTS': 'android-gts',
                        'STS': 'android-sts',
                        'VTS': 'android-vts',
                        'APTS': 'android-apts'
                    };

                    if (testType && suitePaths[testType]) {
                        suitePathInput.value = suitePaths[testType];
                        console.log(`[Retry] 已设置测试套件路径(默认): ${suitePaths[testType]}, 当前值: ${suitePathInput.value}`);
                    } else {
                        console.warn(`[Retry] testType=${testType} 没有对应的套件路径`);
                    }
                }
            } else {
                console.error('[Retry] 未找到 suite-path 元素');
            }

            // 打印所有相关元素的值以便调试
            console.log('[Retry] 当前字段值:', {
                reportName: document.getElementById('retry-result')?.value,
                testType: document.getElementById('test-type')?.value,
                suitePath: document.getElementById('suite-path')?.value
            });
        }, 200);

        showToast(`已填入报告名称: ${timestamp}${testType ? ' (类型: ' + testType + ')' : ''}`, 'success');

        // 可选：自动开始测试（如果需要的话，取消下面的注释）
        // setTimeout(() => {
        //     startTest();
        // }, 500);
    } catch (error) {
        console.error('Retry report error:', error);
        showToast('操作失败: ' + error.message, 'error');
    }
}

async function downloadReport(timestamp) {
    try {
        showToast('正在下载报告...', 'info');

        const response = await fetch(`/api/reports/download/${timestamp}`);

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

            const resp = await fetch(`/api/reports/analyze/${timestamp}`);
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
        const resp = await fetch(`/api/reports/files/${timestamp}`);
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
// 模块级状态管理，避免使用 this
const _modalState = {
    installGuide: { escListenerAdded: false },
    sshdInstallGuide: { escListenerAdded: false }
};

function showInstallGuide(title, guide) {
    const modal = document.getElementById('install-guide-modal');
    if (modal) {
        modal.classList.add('show');
        // 添加 ESC 键监听
        if (!_modalState.installGuide.escListenerAdded) {
            document.addEventListener('keydown', handleInstallGuideEsc);
            _modalState.installGuide.escListenerAdded = true;
        }
    }
}

function closeInstallGuide() {
    const modal = document.getElementById('install-guide-modal');
    if (modal) {
        modal.classList.remove('show');
        // 隐藏进度条
        const progressDiv = document.getElementById('install-progress');
        if (progressDiv) {
            progressDiv.style.display = 'none';
        }
    }
    if (_modalState.installGuide.escListenerAdded) {
        document.removeEventListener('keydown', handleInstallGuideEsc);
        _modalState.installGuide.escListenerAdded = false;
    }
}

function handleInstallGuideEsc(event) {
    if (event.key === 'Escape') {
        closeInstallGuide();
    }
}

async function autoInstallUsbipd() {
    const progressDiv = document.getElementById('install-progress');
    const progressBar = document.getElementById('install-progress-bar');
    const statusText = document.getElementById('install-status');

    // 显示进度条
    progressDiv.style.display = 'block';

    try {
        // 更新状态：准备安装
        progressBar.style.width = '10%';
        statusText.textContent = '📡 正在连接 Windows 主机...';

        // 调用后端自动安装 API
        const result = await apiCall('/api/usbip/auto-install', 'POST', {});

        // 更新状态：安装中
        progressBar.style.width = '50%';
        statusText.textContent = '⏳ 正在安装 usbipd，请稍候...';

        if (result.success) {
            // 安装成功
            progressBar.style.width = '100%';
            progressBar.style.background = 'var(--success-color, #28a745)';
            statusText.innerHTML = '✅ 安装成功！usbipd 已就绪';
            statusText.style.color = 'var(--success-color, #28a745)';

            addLogEntry('usbipd 自动安装成功', 'success');

            // 3秒后关闭弹窗并刷新设备
            setTimeout(() => {
                closeInstallGuide();
                // 直接调用 refreshDevices 而不是 debouncedRefreshDevices，避免防抖延迟
                refreshDevices();
            }, 3000);
        } else {
            // 安装失败
            progressBar.style.width = '100%';
            progressBar.style.background = 'var(--danger-color, #dc3545)';
            statusText.innerHTML = '❌ 安装失败: ' + (result.error || '未知错误');
            statusText.style.color = 'var(--danger-color, #dc3545)';

            addLogEntry('usbipd 自动安装失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (error) {
        // 异常处理
        progressBar.style.width = '100%';
        progressBar.style.background = 'var(--danger-color, #dc3545)';
        statusText.innerHTML = '❌ 安装失败: ' + error.message;
        statusText.style.color = 'var(--danger-color, #dc3545)';

        addLogEntry('usbipd 自动安装失败: ' + error.message, 'error');
    }
}

// ==================== SSHD 安装指南弹窗 ====================
function showSshdInstallGuide(guide) {
    const modal = document.getElementById('sshd-install-guide-modal');
    if (modal) {
        // 设置指南内容
        const guideContent = document.getElementById('sshd-guide-content');
        if (guideContent) {
            guideContent.textContent = guide;
        }
        modal.classList.add('show');
        // 添加 ESC 键监听（防止重复添加）
        if (!_modalState.sshdInstallGuide.escListenerAdded) {
            document.addEventListener('keydown', handleSshdInstallGuideEsc);
            _modalState.sshdInstallGuide.escListenerAdded = true;
        }
    }
}

function closeSshdInstallGuide() {
    const modal = document.getElementById('sshd-install-guide-modal');
    if (modal) {
        modal.classList.remove('show');
    }
    if (_modalState.sshdInstallGuide.escListenerAdded) {
        document.removeEventListener('keydown', handleSshdInstallGuideEsc);
        _modalState.sshdInstallGuide.escListenerAdded = false;
    }
}

function handleSshdInstallGuideEsc(event) {
    if (event.key === 'Escape') {
        closeSshdInstallGuide();
    }
}

async function autoInstallSshd() {
    // SSHD 需要手动安装，直接显示提示
    addLogEntry('⚠️ SSHD 需要在 Windows 客户端上手动安装，请按照安装指南操作', 'warning');
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

        const response = await fetch('/api/reports/analyze', {
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

        const response = await fetch('/api/reports/analyze', {
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
        const response = await fetch('/api/reports/analyze-ai', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                test_name: testName,
                error_message: errorMessage,
                stack_trace: errorMessage
            })
        });

        const result = await response.json();

        // 检查HTTP状态码
        if (!response.ok) {
            // 处理HTTP错误（FastAPI的HTTPException返回 {detail: "error message"}）
            const errorDetail = result.detail || result.error || '未知错误';
            modal.querySelector('.modal-title').textContent = '❌ 分析失败';
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">分析失败: ${errorDetail}</div>`;
            return;
        }

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

            // OpenGrok源码搜索结果
            if (data.opengrok_results && data.opengrok_results.length > 0) {
                content += '<div style="margin-top: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid #9c27b0;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: #9c27b0;">🔍 相关源码 (OpenGrok)</div>';
                content += '<div style="max-height: 300px; overflow-y: auto;">';

                data.opengrok_results.forEach(item => {
                    const opengrokUrl = `http://10.10.10.203:8080/source/xref/${item.file}#${item.line}`;
                    content += `
                        <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 4px; padding: 8px; margin-bottom: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                                <div style="font-family: monospace; font-size: 11px; color: #1976d2; font-weight: 600;">
                                    ${item.class_name}
                                </div>
                                <a href="${opengrokUrl}" target="_blank" style="font-size: 10px; color: #9c27b0; text-decoration: none; white-space: nowrap;">
                                    查看源码 ↗
                                </a>
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: var(--text-secondary); margin-bottom: 4px;">
                                ${item.file}:${item.line}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: #424242; background: white; padding: 4px; border-radius: 3px; overflow-x: auto;">
                                ${escapeHtml(item.context)}
                            </div>
                        </div>
                    `;
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
            // 处理业务逻辑错误（success: false）
            const errorDetail = result.error || result.detail || '未知错误';
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">分析失败: ${errorDetail}</div>`;
        }

    } catch (error) {
        modal.querySelector('.modal-title').textContent = '❌ 分析失败';
        modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">请求失败: ${error.message}</div>`;
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
        const response = await fetch('/api/reports/analyze-source', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ test_name: testName, error_message: errorMessage })
        });

        const result = await response.json();

        // 检查HTTP状态码
        if (!response.ok) {
            // 处理HTTP错误（FastAPI的HTTPException返回 {detail: "error message"}）
            const errorDetail = result.detail || result.error || '未知错误';
            modal.querySelector('.modal-title').textContent = '❌ 分析失败';
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">分析失败: ${errorDetail}</div>`;
            return;
        }

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
            // 处理业务逻辑错误（success: false）
            const errorDetail = result.error || result.detail || '未知错误';
            modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">分析失败: ${errorDetail}</div>`;
        }

    } catch (error) {
        modal.querySelector('.modal-title').textContent = '❌ 分析失败';
        modal.querySelector('.modal-body').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">请求失败: ${error.message}</div>`;
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

        const response = await fetch('/api/reports/analyze-source', {
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
            showToast('源码分析失败: ' + (result.error || result.detail || '未知错误'), 'error');
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

        const response = await fetch('/api/reports/analyze-ai', {
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
            showToast('AI分析失败: ' + (result.error || result.detail || '未知错误'), 'error');
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

// ==================== OpenGrok源码分析 ====================

/**
 * 打开源码分析弹框
 */
function openSourceAnalysisModal() {
    const modal = document.getElementById('source-analysis-modal');
    if (modal) {
        modal.classList.add('show');
        // 清空之前的搜索结果
        document.getElementById('opengrok-results').style.display = 'none';
        document.getElementById('opengrok-results-list').innerHTML = '<div style="text-align: center; color: var(--text-secondary); padding: 20px;">请输入关键词进行搜索</div>';
    }
}

/**
 * 关闭源码分析弹框
 */
function closeSourceAnalysisModal() {
    const modal = document.getElementById('source-analysis-modal');
    if (modal) {
        modal.classList.remove('show');
    }
}

/**
 * 执行源码搜索
 */
async function searchSourceCode() {
    const query = document.getElementById('opengrok-query').value.trim();
    const searchField = document.getElementById('opengrok-search-field').value;
    const project = document.getElementById('opengrok-project').value;
    const fileType = document.getElementById('opengrok-type').value;
    const resultsDiv = document.getElementById('opengrok-results');
    const resultsList = document.getElementById('opengrok-results-list');

    if (!query) {
        showToast('请输入搜索关键词', 'warning');
        return;
    }

    // 显示加载状态
    resultsDiv.style.display = 'block';
    resultsList.innerHTML = '<div style="text-align: center; color: var(--text-secondary); padding: 20px;">搜索中...</div>';

    try {
        const response = await fetch('/api/opengrok/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query: query,
                search_field: searchField,
                project: project,
                type: fileType,
                limit: 15
            })
        });

        const data = await response.json();

        if (data.success && data.results && data.results.length > 0) {
            // 渲染搜索结果
            resultsList.innerHTML = data.results.map(item => {
                const opengrokUrl = `http://10.10.10.203:8080/source/xref/${item.file}#${item.line}`;
                return `
                    <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 4px; padding: 10px; margin-bottom: 10px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
                            <div style="font-family: monospace; font-size: 11px; color: #1976d2; font-weight: 600;">
                                ${item.file}
                            </div>
                            <a href="${opengrokUrl}" target="_blank" style="font-size: 10px; color: #9c27b0; text-decoration: none; white-space: nowrap;">
                                查看源码 ↗
                            </a>
                        </div>
                        <div style="font-family: monospace; font-size: 10px; color: var(--text-secondary); margin-bottom: 5px;">
                            Line ${item.line}
                        </div>
                        <div style="font-family: monospace; font-size: 11px; color: #424242; background: white; padding: 8px; border-radius: 3px; overflow-x: auto; white-space: pre-wrap;">
                            ${escapeHtml(item.context)}
                        </div>
                    </div>
                `;
            }).join('');

            showToast(`找到 ${data.count} 条结果`, 'success');
        } else {
            resultsList.innerHTML = '<div style="text-align: center; color: var(--text-secondary); padding: 20px;">未找到匹配的结果</div>';
            showToast('未找到匹配的结果', 'info');
        }
    } catch (error) {
        console.error('[OpenGrok] Search error:', error);
        resultsList.innerHTML = '<div style="text-align: center; color: var(--danger-color); padding: 20px;">搜索失败: ' + error.message + '</div>';
        showToast('搜索失败: ' + error.message, 'error');
    }
}

/**
 * 在OpenGrok网站打开搜索结果
 */
function openOpenGrokLink() {
    const query = document.getElementById('opengrok-query').value.trim();
    const searchField = document.getElementById('opengrok-search-field').value;
    const project = document.getElementById('opengrok-project').value;

    if (!query) {
        showToast('请输入搜索关键词', 'warning');
        return;
    }

    // 构建OpenGrok URL
    let url = 'http://10.10.10.203:8080/source/search?';

    const params = new URLSearchParams();
    params.append('q', query);

    // 根据搜索字段设置参数
    if (searchField === 'full') {
        params.append('full', query);
    } else if (searchField === 'def') {
        params.append('defs', query);
    } else if (searchField === 'symbol') {
        params.append('refs', query);
    } else if (searchField === 'path') {
        params.append('path', query);
    }

    // 添加项目过滤
    if (project) {
        params.append('project', project);
    }

    url += params.toString();

    // 在新标签页打开
    window.open(url, '_blank');
    showToast('已在OpenGrok网站打开搜索', 'success');
}

// HTML实体映射（模块级常量，避免重复创建）
const HTML_ENTITIES = Object.freeze({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
});

// Escape HTML to prevent XSS (efficient regex-based implementation)
function escapeHtml(text) {
    return text.replace(/[&<>"']/g, char => HTML_ENTITIES[char]);
}

// ==================== 全局函数暴露 ====================
// 将 HTML onclick 需要的函数暴露到 window 对象
window.refreshDevices = refreshDevices;
window.selectAllDevices = selectAllDevices;
window.rebootDevices = rebootDevices;
window.remountDevices = remountDevices;
window.connectWifi = connectWifi;
window.setupUsbipForward = setupUsbipForward;
window.checkSshd = checkSshd;
window.checkRouting = checkRouting;
window.connectVpn = connectVpn;
window.checkVpnStatus = checkVpnStatus;
window.startTest = startTest;
window.stopTest = stopTest;
window.selectReportSource = selectReportSource;
window.deleteReport = deleteReport;
window.viewReportDetails = viewReportDetails;  // 使用实际存在的函数名
window.downloadReport = downloadReport;
window.retryReportWithSuite = retryReportWithSuite;
window.analyzeReport = analyzeReport;
window.openOpenGrokLink = openOpenGrokLink;
window.showSshdInstallGuide = showSshdInstallGuide;
window.closeSshdInstallGuide = closeSshdInstallGuide;
window.autoInstallUsbipd = autoInstallUsbipd;
window.resetReportAnalysis = resetReportAnalysis;

// ==================== API文档相关函数 ====================

/**
 * API分类定义
 */
const API_CATEGORIES = {
    '/api/system/health': 'health',
    '/api/config/read': 'config',
    '/api/config/update': 'config',
    '/api/config/validate': 'config',
    '/api/config/values': 'config',
    '/api/users': 'users',
    '/api/devices/list': 'device',
    '/api/devices/bootloader-lock': 'device',
    '/api/devices/bootloader-unlock': 'device',
    '/api/devices/bootloader-status': 'device',
    '/api/devices/info': 'device',
    '/api/devices/management': 'device',
    '/api/devices/user-locked': 'device',
    '/api/devices/reboot': 'device',
    '/api/devices/remount': 'device',
    '/api/devices/connect-wifi': 'device',
    '/api/devices/shell': 'device',
    '/api/devices/screen': 'device',
    '/api/desktop': 'desktop',
    '/api/test': 'test',
    '/api/reports': 'report',
    '/api/vpn': 'vpn',
    '/api/ssh': 'ssh',
    '/api/adb-forward': 'usbip',
    '/api/usbip': 'usbip',
    '/api/files': 'file',
    '/api/burn': 'burn',
    '/api/files': 'file',
    '/api/system/websocket/': 'health'
};

/**
 * 获取API分类
 */
function getApiCategory(path) {
    for (const [prefix, category] of Object.entries(API_CATEGORIES)) {
        if (path.startsWith(prefix)) {
            return category;
        }
    }
    return 'other';
}

/**
 * 获取分类显示名称
 */
function getCategoryName(category) {
    const names = {
        'test': '🧪 测试管理',
        'config': '⚙️ 配置管理',
        'device': '📱 设备管理',
        'users': '👥 用户管理',
        'client': '👤 用户管理',
        'report': '📊 报告管理',
        'vpn': '🔐 VPN管理',
        'ssh': '🔑 SSH管理',
        'desktop': '🖥️ 主机桌面',
        'usbip': '📡 USB/IP',
        'burn': '🔥 固件烧写',
        'file': '📁 文件管理',
        'health': '💚 系统管理',
        'other': '📋 其他'
    };
    return names[category] || '📋 其他';
}

/**
 * 获取分类排序权重
 */
function getCategoryOrder(category) {
    const order = {
        'test': 1,
        'config': 2,
        'device': 3,
        'users': 4,
        'client': 4,
        'report': 5,
        'vpn': 6,
        'ssh': 7,
        'desktop': 8,
        'usbip': 9,
        'burn': 10,
        'file': 11,
        'health': 13,
        'other': 999
    };
    return order[category] || 999;
}

/**
 * 按分类排序API列表
 */
function sortApisByCategory(apis) {
    // 全部分类时，直接按路径字母顺序排序
    return apis.sort((a, b) => a.path.localeCompare(b.path));
}

// 当前筛选状态
let currentCategoryFilter = 'all';
let currentMethodFilter = 'all';

/**
 * 按分类筛选
 */
function filterByCategory(category) {
    currentCategoryFilter = category;

    // 更新按钮状态
    document.querySelectorAll('[data-category]').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.category === category) {
            btn.classList.add('active');
        }
    });

    applyFilters();
}

/**
 * 按方法筛选
 */
function filterByMethod(method) {
    currentMethodFilter = method;

    // 更新按钮状态
    document.querySelectorAll('[data-method]').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.method === method) {
            btn.classList.add('active');
        }
    });

    applyFilters();
}

/**
 * Debounce wrapper for search input
 */
let debounceTimer;
function debounceFilterApiDocs() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        filterApiDocs();
    }, 300);
}

/**
 * 应用筛选
 */
function applyFilters() {
    const searchInput = $('api-search-input');
    const searchTerm = searchInput ? searchInput.value.toLowerCase() : '';

    // 筛选API
    const filteredApis = allApiDocs.filter(api => {
        // 搜索关键词匹配
        const matchesSearch = !searchTerm ||
            api.path.toLowerCase().includes(searchTerm) ||
            api.description.toLowerCase().includes(searchTerm);

        // 分类匹配
        const matchesCategory = currentCategoryFilter === 'all' || api.category === currentCategoryFilter;

        // 方法匹配
        const matchesMethod = currentMethodFilter === 'all' || api.method === currentMethodFilter;

        return matchesSearch && matchesCategory && matchesMethod;
    });

    // 筛选结果保持原有顺序（allApiDocs已排序），无需重新排序
    displayApiDocs(filteredApis);

    // 更新筛选结果数量
    const filteredCountEl = $('filtered-apis-count');
    if (filteredCountEl) {
        filteredCountEl.textContent = filteredApis.length;
    }
}

/**
 * 筛选API文档（搜索框使用）
 */
function filterApiDocs() {
    applyFilters();
}

/**
 * 加载API文档列表（带缓存优化）
 */
async function loadApiDocs() {
    debugLog('[API Docs] ===== loadApiDocs called =====');
    try {
        // 检查DOM元素是否存在
        const tbody = $('api-docs-table-body');
        if (!tbody) {
            return;
        }

        // 检查缓存
        const now = Date.now();
        if (apiDocsCache && (now - apiDocsCacheTime) < API_DOCS_CACHE_DURATION) {
            displayApiDocs(apiDocsCache);
            updateApiStats(apiDocsCache);
            return;
        }

        const resp = await fetch('/api/system/docs');

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        }

        const data = await resp.json();

        if (data.apis && Array.isArray(data.apis)) {
            // 过滤掉根路径（返回HTML，不是真正的API接口）
            const filteredApis = data.apis.filter(api => api.path !== '/');

            // 为每个API添加分类信息
            const apisWithCategory = filteredApis.map(api => ({
                ...api,
                category: getApiCategory(api.path)
            }));

            // 按分类排序
            const sortedApis = sortApisByCategory(apisWithCategory);

            // 更新缓存
            apiDocsCache = sortedApis;
            allApiDocs = sortedApis;
            apiDocsCacheTime = now;

            displayApiDocs(sortedApis);
            updateApiStats(sortedApis);
        } else {
            throw new Error('Invalid response format: missing or invalid apis field');
        }
    } catch (e) {
        showToast('加载API文档失败: ' + e.message, 'error');

        // 显示错误状态
        const tbody = $('api-docs-table-body');
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="4" style="padding: 40px; text-align: center; color: var(--danger-color);">
                        ❌ 加载失败: ${escapeHtml(e.message)}
                    </td>
                </tr>
            `;
        }
    }
}

/**
 * 更新API统计数据
 */
function updateApiStats(apis) {
    const totalCount = apis.length;
    const getCount = apis.filter(api => api.method === 'GET').length;
    const postCount = apis.filter(api => api.method === 'POST').length;

    const totalEl = $('total-apis-count');
    const getEl = $('get-apis-count');
    const postEl = $('post-apis-count');
    const filteredEl = $('filtered-apis-count');

    if (totalEl) totalEl.textContent = totalCount;
    if (getEl) getEl.textContent = getCount;
    if (postEl) postEl.textContent = postCount;
    if (filteredEl) filteredEl.textContent = totalCount;
}

// ==================== 常量定义 ====================
// API表格列宽配置 (与HTML模板保持一致: 25%, 18%, 17%, 40%)
const API_TABLE_COLUMNS = {
    INTERFACE: 25,    // 百分比 - API接口
    DESCRIPTION: 18,  // 百分比 - 接口说明
    SKILL: 17,        // 百分比 - skill使用
    USAGE: 40         // 百分比 - 使用方法
};

// HTTP方法类型
const HTTP_METHODS = {
    GET: 'GET',
    POST: 'POST',
    WEBSOCKET: 'WebSocket'
};

// CURL特殊参数
const CURL_SPECIAL_PARAMS = ['force_refresh', 'log_type', 'report_timestamp'];

// 视口高度偏移量（用于表格高度计算）
const VIEWPORT_HEIGHT_OFFSET = 150; // 像素

// ==================== API Documentation Constants ====================

/**
 * Parameter type constants for type safety
 */
const PARAM_TYPES = {
    STRING: 'string',
    NUMBER: 'number',
    ARRAY: 'array',
    BOOLEAN: 'boolean',
    FILE: 'file',
    OBJECT: 'object'
};

/**
 * Curl placeholder values for different parameter types (immutable)
 */
const CURL_PLACEHOLDERS = Object.freeze({
    [PARAM_TYPES.STRING]: 'VALUE',
    [PARAM_TYPES.NUMBER]: 123,
    [PARAM_TYPES.ARRAY]: ['Serial'],
    [PARAM_TYPES.BOOLEAN]: true,
    [PARAM_TYPES.FILE]: '/path/to/file.img',
    [PARAM_TYPES.OBJECT]: {}
});

/**
 * Path parameter normalization patterns
 */
const PATH_PATTERNS = [
    { pattern: /^\/api\/reports\/files\//, template: '/api/reports/files/{report_timestamp}' },
    { pattern: /^\/api\/reports\/analyze\//, template: '/api/reports/analyze/{report_timestamp}' },
    { pattern: /^\/api\/reports\/download\//, template: '/api/reports/download/{report_timestamp}' }
];

/**
 * API details cache to avoid repeated lookups
 */
const apiDetailsCache = new Map();

/**
 * Default API details for unknown endpoints
 */
const DEFAULT_API_DETAILS = Object.freeze({
    title: 'API接口',
    description: '执行API操作',
    params: Object.freeze([]),
    response: '{ "success": true }',
    usage: '使用该接口完成相关操作'
});

/**
 * Badge size and padding constants
 */
const BADGE_SIZES = { xs: '9px', sm: '10px', md: '11px', lg: '12px' };
const BADGE_PADDINGS = { xs: '1px 4px', sm: '2px 6px', md: '3px 8px', lg: '4px 10px' };

/**
 * Badge HTML generation utility
 */
function createBadge(text, colorVar, size = 'xs') {
    return `<span style="background: var(--${colorVar}); color: white; padding: ${BADGE_PADDINGS[size]}; border-radius: 3px; font-size: ${BADGE_SIZES[size]};">${escapeHtml(text)}</span>`;
}

/**
 * Get example value for parameter type
 */
function getExampleValue(type) {
    const examples = {
        'string': '"VALUE"',
        'number': '123',
        'array': '[]',
        'boolean': 'true',
        'file': '"/path/to/file"',
        'object': '{}'
    };
    return examples[type] || '"VALUE"';
}

/**
 * Format JSON response for display
 */
function formatJsonResponse(response) {
    try {
        // Try to parse as JSON
        const parsed = JSON.parse(response);
        // Format with 2-space indentation
        return JSON.stringify(parsed, null, 2);
    } catch (e) {
        // If not valid JSON, return as-is
        return response;
    }
}

/**
 * API详细说明映射表
 */
const API_DETAILS_MAP = {
    '/api/test/start': {
        title: '启动测试',
        description: '启动兼容性测试(CTS/VTS/GTS等)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' },
            { name: 'test_type', type: 'string', required: true, desc: '测试类型: CTS|VTS|STS|GTS|CTS_VERIFIER' },
            { name: 'test_module', type: 'string', required: true, desc: '测试模块名称' },
            { name: 'test_case', type: 'string', required: false, desc: '具体测试用例(可选)' },
            { name: 'retry_dir', type: 'string', required: false, desc: '重试目录(可选)' },
            { name: 'test_suite', type: 'string', required: false, desc: '测试套件路径(可选)' }
        ],
        response: '{ "success": true, "message": "测试已启动" }',
        usage: '⭐核心接口 - 启动CTS/VTS/GTS等兼容性测试'
    },
    '/api/test/stop': {
        title: '停止测试',
        description: '停止当前正在运行的测试',
        params: [],
        response: '{ "success": true, "message": "测试已停止" }',
        usage: '紧急停止正在运行的测试'
    },
    '/api/test/clean': {
        title: '清理测试环境',
        description: '清理测试环境并释放资源',
        params: [],
        response: '{ "success": true, "message": "测试环境已清理" }',
        usage: '测试完成后清理临时文件和进程'
    },
    '/api/reports/analyze-source': {
        title: '分析源码',
        description: '分析测试用例的源代码',
        params: [
            { name: 'test_name', type: 'string', required: true, desc: '测试用例名称' },
            { name: 'error_message', type: 'string', required: true, desc: '错误信息' }
        ],
        response: '{ "source_code": "...", "analysis": "..." }',
        usage: '测试失败时分析源代码找出原因'
    },
    '/api/test/logs/current': {
        title: '下载当前日志',
        description: '下载当前单个测试日志文件',
        method: 'GET',
        params: [],
        response: '日志文件下载 (.log格式)',
        usage: '快速下载当前正在运行的测试日志'
    },
    '/api/test/logs/batch': {
        title: '批量下载日志',
        description: '批量下载多个测试日志文件（ZIP压缩包）',
        method: 'POST',
        params: [
            { name: 'files', type: 'array', required: true, desc: '日志文件路径数组' }
        ],
        response: 'ZIP压缩包下载',
        usage: '批量下载和归档多个日志文件'
    },
    '/api/test/logs/save-current': {
        title: '保存当前日志',
        description: '保存当前正在运行的日志',
        params: [],
        response: '{ "success": true, "log_path": "/logs/saved_20260326_110000.log" }',
        usage: '测试运行中保存当前日志快照'
    },
    '/api/test/logs/list': {
        title: '获取日志列表',
        description: '获取所有保存的日志文件列表',
        params: [],
        response: '{ "logs": [{ "filename": "console_20260326_100000.log" }] }',
        usage: '查看历史日志文件'
    },
    '/api/test/status': {
        title: '获取测试状态',
        description: '获取当前测试运行状态',
        params: [],
        response: '{ "running": false, "test_type": "CTS", "devices": ["RF8TC2W4JNH"] }',
        usage: '⭐核心接口 - 查看测试是否正在运行及进度'
    },
    '/api/system/health': {
        title: '系统管理',
        description: '检查服务器运行状态',
        params: [],
        response: '{ "status": "healthy", "timestamp": "2026-03-26T10:30:00" }',
        usage: '用于监控服务器健康状态,建议每分钟调用一次'
    },
    '/api/config/validate': {
        title: '验证配置',
        description: '验证系统配置文件的正确性（检查必要字段和路径）',
        method: 'GET',
        params: [],
        response: '{ "valid": true, "errors": [], "warnings": [] }',
        usage: '在修改配置后验证配置是否正确'
    },
    '/api/config/values': {
        title: '获取前端配置',
        description: '获取前端页面需要的配置（不含敏感信息）',
        method: 'GET',
        params: [],
        response: '{ "success": true, "data": {"script_path": "...", "ubuntu_user": "..."}}',
        usage: '前端页面初始化使用，不暴露密码'
    },
    '/api/config/read': {
        title: '获取完整配置',
        description: '获取完整系统配置（包含所有字段和敏感信息）',
        method: 'GET',
        params: [],
        response: '{ "ubuntu_user": "hcq", "ubuntu_host": "172.16.14.233", "ubuntu_pswd": "..."}',
        usage: '管理员查看完整配置，包含密码等敏感信息'
    },
    '/api/config/update': {
        title: '更新配置',
        description: '更新系统配置（修改动态配置字段）',
        method: 'POST',
        params: [
            { name: 'ubuntu_user', type: 'string', required: false, desc: 'Ubuntu用户名' },
            { name: 'ubuntu_host', type: 'string', required: false, desc: 'Ubuntu主机地址' },
            { name: 'device_host', type: 'string', required: false, desc: '设备主机地址' },
            { name: 'local_server', type: 'string', required: false, desc: '本地服务器地址' }
        ],
        response: '{ "success": true, "message": "配置已保存" }',
        usage: '修改服务器连接配置'
    },
    '/api/users/current': {
        title: '获取客户端信息',
        description: '获取当前客户端ID和主机名',
        params: [],
        response: '{ "client_id": "172.16.14.248_1234567890", "hostname": "172.16.14.248" }',
        usage: '初始化客户端身份,用于多用户隔离'
    },
    '/api/users/detect': {
        title: '检测客户端信息',
        description: '自动检测客户端用户名和身份（通过SSH）',
        params: [
            { name: 'ip', type: 'string', required: false, desc: '客户端IP地址(可选)' },
            { name: 'username', type: 'string', required: false, desc: '用户名(可选)' },
            { name: 'password', type: 'string', required: false, desc: '密码(可选)' }
        ],
        response: '{ "success": true, "username": "hcq" }',
        usage: '自动识别当前登录用户,首次使用需要提供SSH凭据'
    },
    '/api/users/set-username': {
        title: '设置客户端用户名',
        description: '手动设置客户端用户名（无需SSH密码）',
        params: [
            { name: 'username', type: 'string', required: true, desc: '用户名（不能为unknown）' },
            { name: 'ip', type: 'string', required: false, desc: '客户端IP地址（可选，默认自动获取）' }
        ],
        response: '{ "success": true, "username": "hjf", "ip": "10.10.10.206", "client_id": "hjf@10.10.10.206" }',
        usage: '手动设置当前用户的用户名，保存后自动识别'
    },
    '/api/users/list': {
        title: '获取在线用户',
        description: '获取所有在线用户列表',
        params: [],
        response: '{ "users": [{ "client_id": "xxx", "username": "admin", "running": false }] }',
        usage: '查看当前在线用户及其设备使用情况'
    },
    '/api/devices/list': {
        title: '获取设备列表',
        description: '获取所有已连接的Android设备',
        params: [
            { name: 'force_refresh', type: 'number', required: false, desc: '是否强制刷新,默认0' }
        ],
        response: '[{ "device_id": "RF8TC2W4JNH", "serial": "RF8TC2W4JNH", "status": "device" }]',
        usage: '查看可用设备列表,包括设备锁定状态'
    },
    '/api/devices/bootloader-lock': {
        title: '锁定Bootloader',
        description: '锁定设备的Bootloader(使用run_Device_Lock.sh脚本)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "results": [{ "device": "RF8TC2W4JNH", "success": true }] }',
        usage: '⚠️危险操作 - 锁定设备Bootloader,启用安全启动'
    },
    '/api/devices/bootloader-unlock': {
        title: '解锁Bootloader',
        description: '解锁设备的Bootloader(快捷方式,等同于bootloader-lock的unlock操作)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "results": [{ "device": "RF8TC2W4JNH", "success": true }] }',
        usage: '⚠️危险操作 - 解锁设备Bootloader,将允许刷入自定义系统'
    },
    '/api/devices/bootloader-status': {
        title: '检查Bootloader锁状态',
        description: '检查设备的Verified Boot锁定状态(GREEN=锁定, ORANGE=未锁定)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '[{ "device": "RF8TC2W4JNH", "locked": true, "state": "GREEN", "status": "已锁定" }]',
        usage: '检查设备Bootloader是否被锁定,通过ro.boot.verifiedbootstate属性判断'
    },
    '/api/devices/info': {
        title: '获取设备详细信息',
        description: '获取设备的详细硬件和软件信息',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "serial": "RF8TC2W4JNH", "product": "takku", "android_version": "14" }',
        usage: '查看设备详细配置信息,包括Android版本、安全补丁等'
    },
    '/api/devices/management': {
        title: '设备管理信息',
        description: '获取所有设备的详细管理信息(设备列表、电池、来源等)',
        params: [],
        response: '[{ "device_id": "xxx", "serial_no": "xxx", "model": "xxx", "android_version": "14", "battery_level": "85", "source_type": "usbip", "source_host": "172.16.14.68", "status": "online", "locked_by": "", "locked_by_self": false }]',
        usage: '查看设备详细信息，包括电池电量、设备型号、Android版本、设备来源(本地/USB/IP)、锁定状态等'
    },
    '/api/devices/user-locked': {
        title: '列出用户锁定设备',
        description: '列出所有被用户锁定的设备(多用户环境下的设备占用状态)',
        params: [],
        response: '{ "success": true, "data": { "RF8TC2W4JNH": { "client_id": "hcq@172.16.14.68", "username": "hcq", "timestamp": "2026-04-04T15:30:00" } } }',
        usage: '查看哪些设备被其他用户占用,避免多用户冲突'
    },
    '/api/devices/reboot': {
        title: '重启设备',
        description: '重启指定的Android设备',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "message": "设备正在重启" }',
        usage: '设备无响应或需要清理状态时重启'
    },
    '/api/devices/remount': {
        title: '重新挂载设备',
        description: '将设备重新挂载为读写模式',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "message": "设备已重新挂载为读写模式" }',
        usage: '需要修改系统文件时使用'
    },
    '/api/devices/connect-wifi': {
        title: '连接WiFi',
        description: '让设备连接到指定的WiFi网络',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' },
            { name: 'ssid', type: 'string', required: false, desc: 'WiFi名称，默认AndroidWifi' },
            { name: 'password', type: 'string', required: false, desc: 'WiFi密码，默认1234567890' }
        ],
        response: '{ "success": true, "message": "WiFi连接成功" }',
        usage: '配置设备连接到WiFi网络'
    },
    '/api/devices/shell': {
        title: '执行Shell命令',
        description: '在设备上执行ADB Shell命令',
        params: [
            { name: 'serial_no', type: 'string', required: true, desc: '设备序列号' }
        ],
        response: '{ "success": true, "output": "命令输出..." }',
        usage: '为终端页面准备设备连接,建立ADB Shell会话'
    },
    '/api/devices/screen': {
        title: '显示设备屏幕',
        description: '启动设备屏幕显示(VNC)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "screens": [{ "device_id": "RF8TC2W4JNH", "port": 5900 }] }',
        usage: '批量查看多个设备屏幕,用于远程监控'
    },
    '/api/reports/list': {
        title: '获取报告列表',
        description: '获取所有历史测试报告',
        params: [],
        response: '{ "reports": [{ "timestamp": "20260326_100000", "test_type": "CTS" }] }',
        usage: '查看所有历史测试报告'
    },
    '/api/reports/files/{report_timestamp}': {
        title: '获取报告文件',
        description: '下载指定时间戳的报告文件',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳,如20260326_100000' }
        ],
        response: '报告文件下载',
        usage: '下载完整测试报告'
    },
    '/api/reports/analyze/{report_timestamp}': {
        title: '分析报告',
        description: '分析测试报告并给出统计信息',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳' }
        ],
        response: '{ "summary": { "passed": 150, "failed": 5 }, "failed_tests": [] }',
        usage: '快速查看测试结果统计和失败用例'
    },
    '/api/reports/view': {
        title: '查看报告',
        description: '在浏览器中查看HTML报告',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳' }
        ],
        response: 'HTML报告页面',
        usage: '在浏览器中查看详细测试报告'
    },
    '/api/reports/download/{report_timestamp}': {
        title: '下载报告ZIP',
        description: '下载测试报告的ZIP压缩包',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳' }
        ],
        response: 'ZIP文件下载',
        usage: '下载完整报告ZIP包,包含所有测试结果'
    },
    '/api/reports/delete': {
        title: '删除报告',
        description: '删除指定的测试报告',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳（如20260330-120000）' }
        ],
        response: '{ "success": true, "message": "报告已删除" }',
        usage: '⚠️ 危险操作 - 删除后无法恢复',
        curl_example: 'curl -X DELETE "http://server:5001/api/reports/delete" -G -d "report_timestamp=20260330-120000"'
    },
    '/api/reports/analyze': {
        title: 'AI分析报告',
        description: '使用AI分析测试失败原因',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳' },
            { name: 'use_ai', type: 'boolean', required: false, desc: '是否使用AI分析' }
        ],
        response: '{ "analysis": "基于AI分析...", "suggestions": [] }',
        usage: 'AI智能分析测试失败原因并给出修复建议'
    },
    '/api/reports/analyze-ai': {
        title: 'AI深度分析',
        description: '使用AI进行深度分析',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳' }
        ],
        response: '{ "ai_analysis": "...", "root_cause": "...", "fix_suggestions": [] }',
        usage: 'AI深度分析,找出根本原因'
    },
    '/api/vnc/status': {
        title: '获取VNC状态',
        description: '检查VNC服务运行状态',
        params: [],
        response: '{ "running": false, "port": 5900 }',
        usage: '检查VNC服务是否正在运行'
    },
    '/api/vnc/start': {
        title: '启动VNC',
        description: '启动VNC服务',
        params: [],
        response: '{ "success": true, "port": 5900 }',
        usage: '启动VNC服务以远程查看主机桌面'
    },
    '/api/vnc/stop': {
        title: '停止VNC',
        description: '停止VNC服务',
        params: [],
        response: '{ "success": true, "message": "VNC已停止" }',
        usage: '停止VNC服务释放资源'
    },
    '/api/desktop/validate': {
        title: '验证桌面主机',
        description: '验证Ubuntu主机SSH连接并检查VNC服务可用性',
        params: [
            { name: 'host', type: 'string', required: true, desc: '主机地址（格式：user@ip，如hcq@172.16.14.233）' },
            { name: 'password', type: 'string', required: false, desc: 'SSH登录密码（可选）' }
        ],
        response: '{ "success": true, "message": "SSH连接成功，VNC服务可用" }',
        usage: '连接Ubuntu桌面主机前验证SSH连接和VNC服务状态'
    },
    '/api/desktop/vnc/status': {
        title: '查询桌面VNC状态',
        description: '查询Ubuntu主机桌面VNC服务状态',
        params: [],
        response: '{ "success": true, "running": true, "url": "http://172.16.14.233:6080/vnc.html" }',
        usage: '检查Ubuntu桌面VNC服务是否正在运行，获取远程访问URL'
    },
    '/api/desktop/vnc/start': {
        title: '启动桌面VNC',
        description: '启动Ubuntu主机桌面VNC服务',
        params: [
            { name: 'host', type: 'string', required: false, desc: '桌面主机地址，格式：user@ip' },
            { name: 'password', type: 'string', required: false, desc: 'SSH登录密码' },
            { name: 'vnc_password', type: 'string', required: false, desc: 'VNC访问密码（可选）' }
        ],
        response: '{ "success": true, "url": "http://172.16.14.233:6080/vnc.html" }',
        usage: '启动Ubuntu桌面的VNC服务，通过浏览器远程访问图形化桌面'
    },
    '/api/desktop/vnc/stop': {
        title: '停止桌面VNC',
        description: '停止Ubuntu主机桌面VNC服务',
        params: [],
        response: '{ "success": true, "message": "桌面VNC已停止" }',
        usage: '停止Ubuntu桌面VNC服务，释放系统资源'
    },
    '/api/adb-forward/start': {
        title: '启动ADB端口转发',
        description: '通过USB/IP启动ADB端口转发',
        params: [
            { name: 'device_host', type: 'string', required: true, desc: '设备主机地址' },
            { name: 'device_password', type: 'string', required: true, desc: '设备SSH密码' }
        ],
        response: '{ "success": true, "forwarding": [] }',
        usage: '通过USB/IP连接远程设备进行ADB调试'
    },
    '/api/adb-forward/stop': {
        title: '停止ADB端口转发',
        description: '停止ADB端口转发',
        params: [
            { name: 'device_id', type: 'string', required: true, desc: '设备序列号' }
        ],
        response: '{ "success": true, "message": "ADB端口转发已停止" }',
        usage: '停止ADB端口转发'
    },
    '/api/usbip/status': {
        title: '获取USB/IP状态',
        description: '检查USB/IP服务状态',
        params: [],
        response: '{ "installed": true, "running": false }',
        usage: '检查USB/IP服务是否已安装和运行'
    },
    '/api/usbip/start': {
        title: '启动USB/IP',
        description: '启动USB/IP设备共享',
        params: [
            { name: 'device_host', type: 'string', required: false, desc: '设备主机地址，如172.16.14.233' },
            { name: 'device_password', type: 'string', required: false, desc: '设备主机SSH密码（可选）' }
        ],
        response: '{ "success": true, "message": "USB/IP已启动" }',
        usage: '通过IP网络共享USB设备，连接远程测试主机的USB设备'
    },
    '/api/usbip/stop': {
        title: '停止USB/IP',
        description: '停止USB/IP服务',
        params: [],
        response: '{ "success": true, "message": "USB/IP已停止" }',
        usage: '停止USB/IP服务'
    },
    '/api/usbip/auto-install': {
        title: '自动安装USB/IP',
        description: '自动安装USB/IP服务',
        params: [],
        response: '{ "success": true, "message": "USB/IP已自动安装" }',
        usage: '一键安装USB/IP服务'
    },
    '/api/ssh/sshd-check': {
        title: '检查SSHD状态',
        description: '检查SSH服务状态',
        params: [],
        response: '{ "installed": true, "running": true }',
        usage: '检查SSH服务是否正常运行'
    },
    '/api/ssh/sshd-install': {
        title: '安装SSHD',
        description: '获取SSHD安装指南',
        params: [],
        response: '{ "success": false, "error": "SSHD需要在Windows客户端手动安装", "install_guide": "安装步骤...", "manual_install": true }',
        usage: '💡 提示：使用 jq -r \'.install_guide\' 查看换行内容\n命令：curl -sX POST "..." | jq -r \'.install_guide\''
    },
    '/api/ssh/route': {
        title: '检查路由',
        description: '检查系统路由表',
        params: [],
        response: '{ "routing_table": [] }',
        usage: '查看系统路由配置'
    },
    '/api/vpn/status': {
        title: '获取VPN状态',
        description: '检查VPN连接状态',
        params: [],
        response: '{ "success": true, "connected": true }',
        usage: '检查VPN是否已连接'
    },
    '/api/vpn/connect': {
        title: '连接VPN',
        description: '连接到默认VPN服务器（无需参数）',
        params: [],
        response: '{ "success": true, "message": "VPN已连接" }',
        usage: '连接到默认VPN服务器'
    },
    '/api/vpn/disconnect': {
        title: '断开VPN',
        description: '断开VPN连接',
        params: [],
        response: '{ "success": true, "message": "VPN已断开" }',
        usage: '断开当前VPN连接'
    },
    '/api/files/upload': {
        title: '上传文件',
        description: '上传文件到服务器',
        method: 'POST',
        params: [
            { name: 'file', type: 'file', required: true, desc: '要上传的文件' },
            { name: 'path', type: 'string', required: false, desc: '目标路径' }
        ],
        response: '{ "success": true, "filename": "test.apk" }',
        usage: '上传任意文件到服务器'
    },
    '/api/files/install': {
        title: '上传并安装',
        description: '上传APK并安装到设备',
        method: 'POST',
        params: [
            { name: 'file', type: 'file', required: true, desc: 'APK文件' },
            { name: 'device_id', type: 'string', required: true, desc: '目标设备序列号' }
        ],
        response: '{ "success": true, "message": "应用已安装" }',
        usage: '上传并安装APK到指定设备'
    },
    '/api/files/progress': {
        title: '获取上传进度',
        description: '获取当前文件上传进度',
        method: 'GET',
        params: [
            { name: 'upload_id', type: 'string', required: false, desc: '上传任务ID' }
        ],
        response: '{ "uploading": false, "progress": 0 }',
        usage: '查看文件上传进度'
    },
    '/api/burn/firmware': {
        title: '刷入固件',
        description: '上传固件文件并刷入设备',
        params: [
            { name: 'firmware_file', type: 'file', required: true, desc: '固件文件（.img格式）' },
            { name: 'devices', type: 'string', required: true, desc: '设备序列号（多个用逗号分隔）' },
            { name: 'wipe_data', type: 'boolean', required: false, desc: '是否清除数据（默认true）' }
        ],
        response: '{ "success": true, "message": "固件刷入成功" }',
        usage: '⚠️危险操作 - 刷入固件会重启设备',
        curl_example: 'curl -X POST "http://server:5001/api/burn/firmware" -F "devices=rk3572cai" -F "firmware_file=@/path/to/firmware.img" -F "wipe_data=true"'
    },
    '/api/burn/gsi': {
        title: '刷入GSI',
        description: '刷入GSI镜像',
        params: [
            { name: 'gsi_image', type: 'file', required: true, desc: 'GSI镜像文件（.img格式）' },
            { name: 'devices', type: 'string', required: true, desc: '设备序列号（多个用逗号分隔）' },
            { name: 'wipe_data', type: 'boolean', required: false, desc: '是否清除数据（默认true）' }
        ],
        response: '{ "success": true, "message": "GSI刷入成功" }',
        usage: '⚠️危险操作 - 刷入GSI镜像'
    },
    '/api/burn/serial': {
        title: '修改序列号',
        description: '修改设备序列号',
        params: [
            { name: 'device_id', type: 'string', required: true, desc: '当前设备序列号' },
            { name: 'new_serial', type: 'string', required: true, desc: '新的序列号' }
        ],
        response: '{ "success": true, "message": "序列号已修改" }',
        usage: '⚠️危险操作 - 修改设备序列号'
    },
    '/api/files/list': {
        title: '列出文件',
        description: '列出设备指定目录的文件',
        params: [
            { name: 'path', type: 'string', required: true, desc: '目录路径,如/sdcard' }
        ],
        response: '{ "files": [{ "name": "DCIM", "type": "directory" }] }',
        usage: '浏览设备文件系统'
    },
    '/api/terminal/push': {
        title: '终端推送命令',
        description: '向终端推送命令执行',
        params: [
            { name: 'command', type: 'string', required: true, desc: '要执行的命令' }
        ],
        response: '{ "success": true, "output": "命令输出..." }',
        usage: '在Web终端中执行命令'
    },
    '/api/opengrok/search': {
        title: 'OpenGrok搜索',
        description: '在源码中搜索代码',
        params: [
            { name: 'query', type: 'string', required: true, desc: '搜索关键词' },
            { name: 'full', type: 'boolean', required: false, desc: '是否全文搜索' }
        ],
        response: '{ "results": [{ "file": "/path/to/Test.java", "line": 10 }] }',
        usage: '在Android源码中搜索代码'
    }
};

/**
 * Normalize API path to handle path parameters
 */
function normalizeApiPath(apiPath) {
    const matched = PATH_PATTERNS.find(p => p.pattern.test(apiPath));
    return matched ? matched.template : apiPath;
}

/**
 * Get API details with caching
 */
function getApiDetails(apiPath) {
    // Single cache lookup (more efficient than has() + get())
    const cached = apiDetailsCache.get(apiPath);
    if (cached !== undefined) {
        return cached;
    }

    // Normalize path for path parameters
    const detailPath = normalizeApiPath(apiPath);

    // Get details or use default (frozen constant)
    const details = API_DETAILS_MAP[detailPath] || DEFAULT_API_DETAILS;

    // Cache the result
    apiDetailsCache.set(apiPath, details);
    return details;
}

// Module-level constants for server info (never change during page lifetime)
const SERVER_HOST = window.location.hostname;
const SERVER_PORT = window.location.port || '5001';
const BASE_URL = `http://${SERVER_HOST}:${SERVER_PORT}`;

/**
 * Generate curl command for an API endpoint
 * Moved to module level to avoid recreating on every render
 */
function generateCurlCommand(api, details) {
    if (api.method === 'GET') {
        // 特殊处理stream端点：使用 -N 而不是 -s
        const isStreamEndpoint = api.path.includes('/api/test/logs/stream');
        const curlOptions = isStreamEndpoint ? 'curl -N' : 'curl -s';

        let cmd = `${curlOptions} "${BASE_URL}${api.path}"`;
        // Add query parameter example
        if (details.params && details.params.length > 0) {
            const queryParams = details.params.filter(p =>
                !p.required || p.name === 'force_refresh' || p.name === 'log_type' || p.name === 'report_timestamp'
            );
            if (queryParams.length > 0) {
                cmd += ` \\\n  -G \\\n  -d "${queryParams[0].name}=VALUE"`;
            }
        }
        // For GET requests, add continuation if there are params
        const displayCmd = cmd.includes('\\') ? cmd.split('\n')[0] : cmd;
        return { display: displayCmd, full: cmd };
    } else if (api.method === 'POST') {
        // Check if any parameter is of type FILE - if so, use FormData format
        const hasFileParam = details.params && details.params.some(p => p.type === PARAM_TYPES.FILE);

        if (hasFileParam) {
            // Generate FormData format for file uploads
            let multiLineCmd = `curl -sX POST "${BASE_URL}${api.path}"`;

            if (details.params && details.params.length > 0) {
                details.params.forEach(p => {
                    const placeholder = CURL_PLACEHOLDERS[p.type] || CURL_PLACEHOLDERS[PARAM_TYPES.STRING];

                    if (p.type === PARAM_TYPES.FILE) {
                        // File parameter: -F "name=@path"
                        multiLineCmd += ` \\\n  -F "${p.name}=@${placeholder}"`;
                    } else if (p.type === PARAM_TYPES.BOOLEAN) {
                        // Boolean parameter: -F "name=true"
                        multiLineCmd += ` \\\n  -F "${p.name}=${placeholder}"`;
                    } else {
                        // Other parameters: -F "name=value"
                        multiLineCmd += ` \\\n  -F "${p.name}=${placeholder}"`;
                    }
                });
            }

            const displayCmd = multiLineCmd.split('\n')[0];
            return { display: displayCmd, full: multiLineCmd };
        } else {
            // Generate JSON format for non-file uploads
            let multiLineCmd = `curl -sX POST "${BASE_URL}${api.path}"`;

            // Generate request body example
            if (details.params && details.params.length > 0) {
                multiLineCmd += ` \\\n  -H "Content-Type: application/json"`;
                const bodyLines = ['{'];

                // Include all parameters including FILE type for documentation
                details.params.forEach((p, index) => {
                    // Include all parameters (both required and optional)
                    const placeholder = CURL_PLACEHOLDERS[p.type] || CURL_PLACEHOLDERS[PARAM_TYPES.STRING];

                    // Format the value based on type
                    let valueStr;
                    if (p.type === PARAM_TYPES.STRING) {
                        valueStr = `"${placeholder}"`;
                    } else if (p.type === PARAM_TYPES.NUMBER) {
                        valueStr = placeholder;
                    } else if (p.type === PARAM_TYPES.BOOLEAN) {
                        valueStr = placeholder;
                    } else if (p.type === PARAM_TYPES.ARRAY) {
                        valueStr = JSON.stringify(placeholder);
                    } else if (p.type === PARAM_TYPES.FILE) {
                        // For file type, still show in JSON format as placeholder
                        valueStr = `"${placeholder}"`;
                    } else {
                        valueStr = placeholder;
                    }

                    // Add comma if not last item
                    const comma = (index < details.params.length - 1) ? ',' : '';
                    bodyLines.push(`    "${p.name}": ${valueStr}${comma}`);
                });
                bodyLines.push('  }');

                if (bodyLines.length > 2) { // More than just '{' and '}'
                    multiLineCmd += ' \\\n  -d \'' + bodyLines.join('\n') + '\'';
                } else {
                    multiLineCmd += ` \\\n  -d '{}'`;
                }
            } else {
                // No parameters - don't add -d '{}' or Content-Type header
                // Just return the basic curl command
            }

            // Display version: only first line with continuation
            const displayCmd = multiLineCmd.split('\n')[0];

            return { display: displayCmd, full: multiLineCmd };
        }
    } else if (api.method === 'DELETE') {
        // Generate DELETE request
        let cmd = `curl -X DELETE "${BASE_URL}${api.path}"`;

        // Add query parameters or request body
        if (details.params && details.params.length > 0) {
            const queryParams = details.params.filter(p => p.required || p.name === 'report_timestamp');
            if (queryParams.length > 0) {
                // Use query parameters for DELETE
                cmd += ` \\\n  -G \\\n  -d "${queryParams[0].name}=VALUE"`;
            }
        }

        const displayCmd = cmd.includes('\\') ? cmd.split('\n')[0] : cmd;
        return { display: displayCmd, full: cmd };
    } else if (api.method === 'WebSocket') {
        const wsBaseUrl = `${SERVER_HOST}:${SERVER_PORT}`;
        return { display: `wscat -c ws://${wsBaseUrl}${api.path.replace('{client_id}', 'YOUR_CLIENT_ID')}`, full: `wscat -c ws://${wsBaseUrl}${api.path.replace('{client_id}', 'YOUR_CLIENT_ID')}` };
    }
    return { display: `curl -s ${BASE_URL}${api.path}`, full: `curl -s ${BASE_URL}${api.path}` };
}

/**
 * Generate parameter descriptions HTML
 * Moved to module level to avoid recreating on every render
 */
function generateParamsHtml(details) {
    if (!details.params || details.params.length === 0) {
        return '<span style="color: var(--text-secondary);">无参数</span>';
    }

    // Use array.join() instead of string concatenation
    const parts = ['<div style="margin-top: 8px;">'];
    details.params.forEach(param => {
        const requiredBadge = createBadge(
            param.required ? '必需' : '可选',
            param.required ? 'danger-color' : 'info-color'
        );
        const typeBadge = createBadge(param.type, 'primary-color');

        parts.push(`
            <div style="margin-bottom: 4px; font-size: 10px;">
                <span style="font-family: monospace; font-weight: 600; color: var(--primary-color);">${escapeHtml(param.name)}</span>
                ${typeBadge} ${requiredBadge}
                <span style="color: var(--text-secondary); margin-left: 4px;">${escapeHtml(param.desc)}</span>
            </div>
        `);
    });
    parts.push('</div>');
    return parts.join('');
}

/**
 * Display API documentation list with collapsible details
 */
function displayApiDocs(apis) {
    const tbody = document.getElementById('api-docs-table-body');
    if (!tbody) return;

    // Use array.join() instead of string concatenation for better performance
    const htmlParts = [];
    apis.forEach((api, index) => {
        const methodClass = api.method === 'GET' ? 'color: var(--success-color);' :
                           api.method === 'POST' ? 'color: var(--warning-color);' :
                           api.method === 'WebSocket' ? 'color: var(--primary-color);' :
                           'color: var(--text-secondary);';

        const categoryBadge = getCategoryName(api.category);

        // 获取API详细信息
        const details = getApiDetails(api.path);
        const curlCmdObj = generateCurlCommand(api, details);
        const paramsHtml = generateParamsHtml(details);

        // 将curl命令存储到data属性中,避免在onclick中直接传递复杂字符串
        const escapedCurlCmd = curlCmdObj.full.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const displayCurlCmd = curlCmdObj.display;

        htmlParts.push(`
            <tr style="border-bottom: 1px solid var(--border-color); ${index % 2 === 0 ? 'background: var(--bg-color);' : 'background: var(--light-bg);'}">
                <!-- Column 1: API Interface -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 25%;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span style="${methodClass} font-weight: 700; font-size: 13px; min-width: 90px; display: inline-block;">${api.method}</span>
                        <span style="font-family: monospace; font-size: 12px; color: var(--text-primary); word-break: break-all;">${escapeHtml(api.path)}</span>
                    </div>
                </td>

                <!-- Column 2: Description -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 20%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-size: 11px; color: var(--text-primary); font-weight: 600; line-height: 1.3;">
                            ${escapeHtml(details.title)}(${escapeHtml(details.description)})
                        </div>
                    </div>
                </td>

                <!-- Column 3: Skill Usage -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 20%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-size: 11px; color: var(--primary-color); font-weight: 600; line-height: 1.3;">
                            ${api.skill ? escapeHtml(api.skill) : '<span style="color: var(--text-secondary);">-</span>'}
                        </div>
                    </div>
                </td>

                <!-- Column 4: Usage Method -->
                <td style="padding: 4px 8px; text-align: left; vertical-align: middle; width: 35%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <!-- Curl Command Row -->
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <pre
                                 data-cmd="${escapedCurlCmd}"
                                 style="margin: 0; padding: 2px 6px; font-family: 'Monaco', 'Menlo', monospace; font-size: 11px; color: var(--success-color); overflow-x: auto; white-space: nowrap; cursor: pointer; transition: all 0.2s; line-height: 1.3; display: block; flex: 1; background: transparent; border: none; text-overflow: ellipsis;"
                                 onclick="copyCurlCommandFromData(this)"
                                 onmouseover="this.style.color='var(--primary-color)';"
                                 onmouseout="this.style.color='var(--success-color)';"
                                 title="点击复制curl命令">${escapeHtml(displayCurlCmd)}</pre>
                            <button
                                id="expand-btn-${index}"
                                onclick="toggleApiDetails('${index}')"
                                style="background: var(--primary-color); color: white; border: none; padding: 2px 6px; border-radius: 3px; cursor: pointer; font-size: 12px; font-weight: 600; min-width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; transition: all 0.2s; flex-shrink: 0;"
                                title="点击展开/收起详情">
                                <span id="expand-icon-${index}">▶</span>
                            </button>
                        </div>

                        <!-- Expandable Details (Hidden by Default) -->
                        <div id="api-details-${index}" style="display: none;">
                            <div style="border-top: 1px solid var(--border-color); padding-top: 8px; margin-top: 4px;">
                                <!-- Full Curl Command -->
                                <div style="font-size: 11px; font-weight: 600; margin-bottom: 4px; color: var(--text-primary);">📜 完整curl命令:</div>
                                <pre style="font-family: 'Monaco', 'Menlo', monospace; font-size: 10px; color: var(--success-color); background: var(--darker-bg); padding: 6px; border-radius: 4px; margin-bottom: 8px; white-space: pre-wrap; word-break: break-all; cursor: pointer;" onclick="navigator.clipboard.writeText(this.textContent); this.style.background='var(--success-color)'; this.style.color='white'; setTimeout(() => { this.style.background='var(--darker-bg)'; this.style.color='var(--success-color)'; }, 200);" title="点击复制">${escapeHtml(curlCmdObj.full)}</pre>

                                <!-- Title with star if core API -->
                                <div style="font-size: 12px; font-weight: 700; color: var(--primary-color); margin-bottom: 6px;">
                                    ${details.usage.includes('⭐核心接口') ? '### ' : ''}${escapeHtml(details.title)} ${details.usage.includes('⭐核心接口') ? '⭐核心接口' : ''}
                                </div>

                                <!-- HTTP Method and Path -->
                                <div style="font-family: monospace; font-size: 11px; color: var(--text-primary); background: var(--darker-bg); padding: 6px; border-radius: 4px; margin-bottom: 8px; font-weight: 600;">
${api.method} ${api.path}
${api.method === 'POST' ? 'Content-Type: application/json' : ''}
                                </div>

                                <!-- Parameters -->
                                ${details.params && details.params.length > 0 ? `
                                <div style="font-size: 11px; font-weight: 600; margin-bottom: 6px; color: var(--text-primary);">📋 请求参数说明:</div>
                                ${paramsHtml}
                                ` : ''}

                                <!-- Response Example -->
                                <div style="margin-top: 12px; font-size: 11px; font-weight: 600; margin-bottom: 4px; color: var(--text-secondary);">📤 响应示例:</div>
                                <div style="font-family: monospace; font-size: 10px; color: var(--success-color); background: var(--darker-bg); padding: 6px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;">${escapeHtml(formatJsonResponse(details.response))}</div>
                            </div>
                        </div>
                    </div>
                </td>
            </tr>
        `);
    });

    tbody.innerHTML = htmlParts.join('');
}

/**
 * Toggle API details visibility
 */
window.toggleApiDetails = function(index) {
    const detailsDiv = document.getElementById(`api-details-${index}`);
    const iconSpan = document.getElementById(`expand-icon-${index}`);
    const button = document.getElementById(`expand-btn-${index}`);

    if (detailsDiv.style.display === 'none') {
        // Expand
        detailsDiv.style.display = 'block';
        iconSpan.textContent = '▼';
        button.style.background = 'var(--warning-color)';
    } else {
        // Collapse
        detailsDiv.style.display = 'none';
        iconSpan.textContent = '▶';
        button.style.background = 'var(--primary-color)';
    }
};

/**
 * 从data属性复制curl命令到剪贴板（自动添加jq格式化，但跳过纯文本端点）
 */
window.copyCurlCommandFromData = function(element) {
    const text = element.getAttribute('data-cmd');
    if (!text) {
        debugLog('[Copy] No data-cmd attribute found');
        showToast('✗ 复制失败: 未找到命令', 'error');
        return;
    }
    console.log('[Copy] Attempting to copy:', text);

    let commandToCopy = text;
    let successMessage = '✓ curl命令已复制';

    // 检查是否为WebSocket端点（不需要jq格式化）
    const isWebSocketEndpoint = text.startsWith('wscat -c');

    // 检查是否为纯文本端点（不需要jq格式化）
    const isPlainTextEndpoint = text.includes('/api/test/logs/stream') ||
                                text.includes('/api/terminal/ws') ||
                                text.includes('/api/screen/ws') ||
                                // 匹配根路径（如 "http://localhost:5001/" 或 "http://172.16.14.233:5001/"）
                                (text.match(/http:\/\/[^\/]+:\d+\/"$/) !== null);

    // 检查是否为需要特殊jq处理的端点
    const isSshdInstall = text.includes('/api/ssh/sshd-install');

    if (isWebSocketEndpoint) {
        // WebSocket端点，不添加jq
        commandToCopy = text;
        successMessage = '✓ WebSocket命令已复制';
    } else if (isPlainTextEndpoint) {
        // 纯文本端点，不添加jq
        commandToCopy = text;
        successMessage = '✓ curl命令已复制';
    } else if (isSshdInstall) {
        // sshd-install API，使用 jq -r '.install_guide'
        commandToCopy = text + ' | jq -r \'.install_guide\'';
        successMessage = '✓ curl命令已复制 (含jq -r查看指南)';
    } else {
        // 其他JSON端点，使用 jq "."
        commandToCopy = text + ' | jq "."';
        successMessage = '✓ curl命令已复制 (含jq格式化)';
    }

    // 方法1: 使用现代Clipboard API
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(commandToCopy).then(() => {
            console.log('[Copy] Success with Clipboard API');
            showToast(successMessage, 'success');
        }).catch(err => {
            console.error('[Copy] Clipboard API failed:', err);
            // 尝试备用方法
            fallbackCopyTextToClipboard(commandToCopy);
        });
    } else {
        console.log('[Copy] Clipboard API not available, using fallback');
        fallbackCopyTextToClipboard(commandToCopy);
    }
};

/**
 * 显示使用实例弹窗
 */
function showUsageExamples() {
    const modal = document.getElementById('usage-examples-modal');
    if (modal) {
        // 获取当前服务器地址
        const serverUrl = window.location.origin || 'http://172.16.14.233:5001';

        // 替换弹框中的硬编码IP为动态服务器地址
        const currentContent = modal.innerHTML;
        const updatedContent = currentContent.replace(/172\.16\.14\.233:5001/g, serverUrl);
        modal.innerHTML = updatedContent;
        modal.style.display = 'flex';
    }
}

/**
 * 关闭使用实例弹窗
 */
function closeUsageExamplesModal() {
    const modal = document.getElementById('usage-examples-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

/**
 * 复制文本到剪贴板（统一函数）
 * @param {string} text - 要复制的文本
 * @param {Object} options - 配置选项 { addJq: boolean, successMsg: string }
 */
function copyText(text, options = {}) {
    const { addJq = false, successMsg = '✓ 命令已复制到剪贴板' } = options;
    const textToCopy = addJq ? text + ' | jq "."' : text;

    console.log('[Copy] Copying text:', textToCopy);

    // 使用现代Clipboard API
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(textToCopy).then(() => {
            console.log('[Copy] Success with Clipboard API');
            showToast(successMsg, 'success');
        }).catch(err => {
            console.error('[Copy] Clipboard API failed:', err);
            fallbackCopyTextToClipboard(textToCopy, successMsg);
        });
    } else {
        console.log('[Copy] Clipboard API not available, using fallback');
        fallbackCopyTextToClipboard(textToCopy, successMsg);
    }
}

/**
 * 复制curl命令到剪贴板（自动添加jq格式化）- 兼容旧代码
 */
window.copyCurlCommand = function(text) {
    copyText(text, { addJq: true, successMsg: '✓ curl命令已复制 (含jq格式化)' });
};

/**
 * 备用复制方法（使用传统textarea方法）
 * @param {string} text - 要复制的文本
 * @param {string} successMsg - 成功提示消息
 */
function fallbackCopyTextToClipboard(text, successMsg = '✓ 已复制到剪贴板') {
    console.log('[Copy] Using fallback method');
    try {
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        textArea.style.top = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();

        const successful = document.execCommand('copy');
        document.body.removeChild(textArea);

        if (successful) {
            console.log('[Copy] Fallback method successful');
            showToast(successMsg, 'success');
        } else {
            console.error('[Copy] Fallback method failed');
            showToast('✗ 复制失败，请手动复制', 'error');
        }
    } catch (err) {
        console.error('[Copy] Fallback method error:', err);
        showToast('✗ 复制失败: ' + err.message, 'error');
    }
}

/**
 * 复制命令（使用示例专用）
 */
window.copyCommand = function(elementId) {
    const element = document.getElementById(elementId);
    if (!element) {
        console.error('[CopyCommand] Element not found:', elementId);
        showToast('✗ 找不到命令内容', 'error');
        return;
    }

    const text = element.textContent || element.innerText;
    console.log('[CopyCommand] Copying from element:', elementId, text);

    copyText(text);
};

// 将API文档函数暴露到window对象
window.loadApiDocs = loadApiDocs;
window.filterApiDocs = filterApiDocs;
window.autoInstallSshd = autoInstallSshd;
