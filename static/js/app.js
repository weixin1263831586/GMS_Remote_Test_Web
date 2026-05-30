// 全局状态
const state = {
    connected: false,
    testing: false,
    devices: [],
    selectedDevices: new Set(),
    socket: null,
    sshConnected: false,
    vpnConnected: null,
    adbForwardRunning: false,
    usbipConnected: false,
    config: null,
    fileBrowser: { currentPath: '', selectedFile: null, targetInputId: null, mode: null },
    suiteBrowser: { selectedSuitePath: '', currentPath: '', highlightPath: '' },
    // 性能优化
    domCache: {},
    lastLogCount: 0,
    pendingDeviceRefresh: null,
    deviceRefreshPromise: null,
    isRefreshingDevices: false,
    notifications: [],
    unreadNotifications: 0,
    browserNotificationsEnabled: localStorage.getItem('gms_browser_notifications') === 'true'
};

// Debug flag - set to false in production to disable console logs
const DEBUG = false;

// OpenGrok配置 - 从后端API获取（异步加载，不阻塞启动）
// Redmine配置缓存（减少重复API调用）
let cachedRedmineConfig = null;
let redmineConfigFetchTime = 0;
const REDMINE_CONFIG_CACHE_TTL = 300000; // 5分钟缓存

const OPENGROK_CONFIG = {
    _loaded: false,
    _baseUrl: '',
    _defaultProject: '',
    _projectMapping: {},

    get isValid() {
        return !!(this._loaded && this._baseUrl && this._defaultProject);
    },

    getProjectForAndroidVersion(androidVersion) {
        // 根据Android版本获取对应的项目（使用预编译的正则表达式）
        if (!androidVersion || !this._projectMapping) {
            return this._defaultProject;
        }

        // 提取主版本号（复用现有逻辑）
        const versionMatch = androidVersion.match(/^(\d+)/);
        if (versionMatch && versionMatch[1] && this._projectMapping[versionMatch[1]]) {
            return this._projectMapping[versionMatch[1]];
        }

        return this._defaultProject;
    },

    init() {
        debugLog('[OpenGrok] 开始加载配置...');
        fetch('/api/config/opengrok')
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json();
            })
            .then(result => {
                if (result.success && result.data) {
                    this._baseUrl = result.data.base_url;
                    this._defaultProject = result.data.default_project;
                    this._projectMapping = result.data.project_mapping || {};
                    this._loaded = true;
                    debugLog('[OpenGrok] ✅ 配置已加载:', { baseUrl: this._baseUrl, defaultProject: this._defaultProject, projectMapping: this._projectMapping });
                } else {
                    debugLog('[OpenGrok] 配置响应格式异常:', result);
                }
            })
            .catch(error => {
                debugLog('[OpenGrok] 配置加载失败:', error.message);
            });
    }
};

// API文档缓存（全局变量，避免重复请求）
let apiDocsCache = null;
let apiDocsCacheTime = 0;
let allApiDocs = []; // 所有API文档数据（已排序）
const API_DOCS_CACHE_DURATION = 5 * 60 * 1000; // 5分钟缓存（生产环境）
const FIRMWARE_UPLOAD_TIMEOUT = 10 * 60 * 1000; // 10分钟上传超时

// ==================== 轮询间隔配置 ====================
// GSI 固件烧写进度轮询间隔（毫秒）
const GSI_PROGRESS_POLL_INTERVAL = 2000; // 2 秒
// 状态轮询间隔（毫秒）
const STATUS_POLL_INTERVAL = 2000; // 2 秒
// 报告列表刷新间隔（毫秒）
const REPORTS_REFRESH_INTERVAL = 15000; // 15 秒
// 最大进度轮询错误次数
const MAX_PROGRESS_ERRORS = 3;

// 辅助函数
function validateDeviceSelection() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return false;
    }
    return true
}

// 获取Redmine配置（带缓存）
async function getRedmineConfig() {
    const now = Date.now();

    // 返回缓存配置（如果仍在有效期内）
    if (cachedRedmineConfig && (now - redmineConfigFetchTime) < REDMINE_CONFIG_CACHE_TTL) {
        return cachedRedmineConfig;
    }

    // 获取新配置
    const configResponse = await fetch('/api/config/redmine');
    const configResult = await configResponse.json();

    if (!configResult.success || !configResult.data || !configResult.data.domain) {
        throw new Error(configResult.error || 'Redmine 未配置或配置不完整');
    }

    // 更新缓存
    cachedRedmineConfig = configResult.data;
    redmineConfigFetchTime = now;

    return cachedRedmineConfig;
}

// 性能优化工具 - DOM element caching with null-check and stale detection
function $(id) {
    const cached = state.domCache[id];
    if (cached) {
        // Verify element is still in the DOM (handles page switches that remove elements)
        if (cached.isConnected) return cached;
        // Remove stale cache entry
        delete state.domCache[id];
    }
    const el = document.getElementById(id);
    if (el) state.domCache[id] = el;
    return el;
}

// Clear DOM cache (call when switching pages to avoid stale references)
function clearDomCache() {
    state.domCache = {};
}

// Debug logger wrapper (only logs when DEBUG is true)
function debugLog(...args) {
    if (DEBUG) {
        console.log(...args);
    }
}

// Modal error display utility
function showModalError(modal, message) {
    modal.querySelector('.modal-title').textContent = '❌ 分析失败';
    modal.querySelector('.modal-body').textContent = message;
    modal.querySelector('.modal-body').style.cssText = 'color: var(--danger-color); padding: 20px; text-align: center;';
}

// Modal factory utility
function createAnalysisModal(type, title, loadingMessage) {
    const modalId = `${type}-modal-${Date.now()}`;
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 900px; max-height: 90vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">${title}</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔍</div>
                    <div style="color: var(--text-secondary); margin-bottom: 12px;">${loadingMessage}</div>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    ModalManager.open(modalId);

    return { modal, modalId };
}

// OpenGrok URL builder utility
function buildOpenGrokUrl(path, line = null) {
    if (!OPENGROK_CONFIG.isValid) return '';

    const url = `${OPENGROK_CONFIG._baseUrl}/xref/${OPENGROK_CONFIG._defaultProject}/${path}`;
    return line ? `${url}#${line}` : url;
}

function debounce(func, wait) {
    let timer = null;
    return function(...args) {
        clearTimeout(timer);
        timer = setTimeout(() => func.apply(this, args), wait);
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
    _escListener: null,
    _activeModals: [],
    _dynamicModals: new Set(),

    open(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.add('show');
            this._addActiveModal(modalId);
            this._ensureEscListener();
        }
    },

    close(modalId) {
        // 动态弹窗走 unregisterDynamic 路径（会 remove DOM）
        if (this._dynamicModals.has(modalId)) {
            this.unregisterDynamic(modalId);
            return;
        }
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.remove('show');
            if (modal.style.display === 'flex') {
                modal.style.display = 'none';
            }
            this._removeActiveModal(modalId);
            this._cleanupEscListener();
        }
    },

    closeAll() {
        document.querySelectorAll('.modal.show').forEach(m => {
            m.classList.remove('show');
            if (m.style.display === 'flex') {
                m.style.display = 'none';
            }
            this._removeActiveModal(m.id);
        });
        this._cleanupEscListener();
    },

    toggle(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.toggle('show');
            if (modal.classList.contains('show')) {
                this._addActiveModal(modalId);
                this._ensureEscListener();
            } else {
                if (modal.style.display === 'flex') {
                    modal.style.display = 'none';
                }
                this._removeActiveModal(modalId);
                this._cleanupEscListener();
            }
        }
    },

    isOpen(modalId) {
        const modal = document.getElementById(modalId);
        return modal ? modal.classList.contains('show') : false;
    },

    // Register a dynamically created modal
    registerDynamic(modalElement) {
        document.body.appendChild(modalElement);
        this._addActiveModal(modalElement.id);
        this._dynamicModals.add(modalElement.id);
        this._ensureEscListener();
        return modalElement;
    },

    // Unregister and remove a dynamically created modal
    unregisterDynamic(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.remove();
        }
        this._dynamicModals.delete(modalId);
        this._removeActiveModal(modalId);
    },

    _addActiveModal(modalId) {
        if (!this._activeModals.includes(modalId)) {
            this._activeModals.push(modalId);
        }
    },

    _removeActiveModal(modalId) {
        this._activeModals = this._activeModals.filter(id => id !== modalId);
        if (this._activeModals.length === 0) {
            this._cleanupEscListener();
        }
    },

    _ensureEscListener() {
        if (!this._escListener) {
            this._escListener = (event) => {
                if (event.key === 'Escape' && this._activeModals.length > 0) {
                    // 关闭最上层（最后打开）的弹框
                    const topModalId = this._activeModals[this._activeModals.length - 1];
                    this.close(topModalId);
                }
            };
            document.addEventListener('keydown', this._escListener);
        }
    },

    _cleanupEscListener() {
        if (this._escListener && this._activeModals.length === 0) {
            document.removeEventListener('keydown', this._escListener);
            this._escListener = null;
        }
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
    initEventListeners();
    initDragDrop();
    renderNotificationList();

    // 非阻塞加载OpenGrok配置（不等待，让它在后台加载）
    OPENGROK_CONFIG.init();

    // 🚀 优先加载客户端信息，确保所有API调用都有正确的clientId
    try {
        const currentUserResponse = await fetch('/api/users/current');
        if (currentUserResponse.ok) {
            const userData = await currentUserResponse.json();
            if (userData.client_id) {
                state.clientId = userData.client_id;
                debugLog('[Init] ✅ Set state.clientId from /api/users/current:', state.clientId);

                // 检查是否是 unknown 用户（apiCall 中会统一处理弹框）
                if (userData.client_id.startsWith('unknown@')) {
                    debugLog('[Init] Detected unknown client, will show username modal via apiCall');
                } else {
                    loadNotifications();
                    // 已获取到正确的用户名，延迟检查 USB/IP 和 VPN 状态（避免阻塞关键请求）
                    setTimeout(() => {
                        Promise.all([
                            checkUsbipStatus(),
                            checkVpnStatus()
                        ]).catch(error => {
                            debugLog('[Init] Background status check failed:', error);
                        });
                    }, 3000);  // 3秒后再检查
                }
            }
        } else {
            debugLog('[Init] Failed to call /api/users/current');
        }
    } catch (error) {
        debugLog('[Init] Error getting current user:', error);
    }

    // 🔌 现在初始化WebSocket（需要clientId）
    initWebSocket();

    // ⚙️ 延迟加载非关键数据（避免阻塞关键请求）
    setTimeout(() => {
        loadConfig().catch(error => {
            console.warn('[Init] Config load failed, using defaults:', error);
        });
        loadDevices();
        loadTestSuites();
        checkInitialTestStatus().catch(error => {
            console.warn('[Init] Test status check failed:', error);
        });
    }, 1000);

    startStatusPolling();

    // 检查是否有未完成的固件上传
    checkPendingFirmwareUpload();

    // 检查URL参数，如果refresh=true则强制刷新API文档
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('refresh') === 'true') {
        debugLog('[Init] Force refresh API docs due to URL parameter');
        if (window.loadApiDocs) {
            await window.loadApiDocs(true);
        }
    }

    // 延迟执行耗时操作，不阻塞页面加载
    setTimeout(async () => {

        // 自动启动 VNC 服务
        try {
            await initAndStartVnc();
        } catch (error) {
            console.warn('[Init] Failed to auto-start VNC:', error);
        }

        // 加载用户列表
        try {
            await loadUsers();
        } catch (error) {
            console.warn('[Init] Failed to load users:', error);
        }
    }, 100);  // 减少延迟时间，更快获取客户端信息
});

// ==================== Firmware Upload Recovery ====================
/**
 * 检查是否有未完成的固件上传
 */
function checkPendingFirmwareUpload() {
    const uploadInProgress = sessionStorage.getItem('firmwareUploadInProgress');
    if (uploadInProgress === 'true') {
        const fileName = sessionStorage.getItem('firmwareUploadFileName');
        const fileSize = sessionStorage.getItem('firmwareUploadFileSize');
        const startTime = parseInt(sessionStorage.getItem('firmwareUploadStartTime') || '0');
        const elapsed = Date.now() - startTime;

        const progress = parseFloat(sessionStorage.getItem('firmwareUploadProgress') || '0');
        const uploadedSize = parseInt(sessionStorage.getItem('firmwareUploadedSize') || '0');
        const totalSize = parseInt(sessionStorage.getItem('firmwareTotalSize') || '0');

        // 如果超过超时时间，认为上传已失败/过期
        if (elapsed > FIRMWARE_UPLOAD_TIMEOUT) {
            clearFirmwareUploadState();
            return;
        }

        // 显示警告：上传已中断
        const message = `⚠️ 固件上传已中断: ${fileName}\n` +
                       `上次进度: ${progress.toFixed(1)}% (${formatBytes(uploadedSize)}/${formatBytes(totalSize)})\n` +
                       `中断时间: ${Math.floor(elapsed / 1000)}秒前\n\n` +
                       `请重新开始上传。\n\n` +
                       `💡 提示：上传过程中请勿刷新页面。`;

        addLogEntry(message, 'warning');
        showToast('固件上传已中断，请重新上传', 'warning');
        createLocalNotification('固件上传中断', `${fileName} 上传中断于 ${progress.toFixed(1)}%`, 'warning', 'firmware-upload', {
            filename: fileName,
            progress
        });

        // 显示进度条为警告状态（黄色）
        if (progress > 0 && totalSize > 0) {
            const progressFill = document.getElementById('upload-progress-fill');
            const progressInfo = document.getElementById('progress-info');

            if (progressFill && progressInfo) {
                progressFill.style.width = progress + '%';
                progressFill.style.background = 'linear-gradient(135deg, #f59e0b 0%, #fbbf24 100%)'; // 黄色
                progressInfo.textContent = `⚠️ ${fileName} 上传中断于 ${progress.toFixed(1)}%`;

                // 10秒后重置进度条
                setTimeout(() => {
                    progressFill.style.width = '0%';
                    progressFill.style.background = ''; // 恢复默认颜色
                    progressInfo.textContent = '';
                }, 10000);
            }
        }

        // 清理状态
        clearFirmwareUploadState();
    }
}

// ==================== Configuration ====================
async function loadConfig() {
    try {
        const config = await apiCall('/api/config/read', 'GET');
        state.config = config;
    } catch (error) {
        debugLog('Failed to load config:', error);
        state.config = { ubuntu_user: 'gms' };  // Fallback
    }
}

function getDefaultUbuntuUser() {
    return state.config?.ubuntu_user || 'gms';
}

// ==================== WebSocket Connection (FastAPI) ====================
function initWebSocket() {
    // 获取客户端ID
    apiCall('/api/users/current', 'GET').then(data => {
        const clientId = data.client_id || 'unknown';
        state.clientId = clientId;

        // 建立WebSocket连接
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/api/system/websocket/${encodeURIComponent(clientId)}`;

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
                    debugLog('[WebSocket] Attempting to reconnect...');
                    initWebSocket();
                }
            }, 5000);
        };

        state.websocket.onerror = (error) => {
            debugLog('[WebSocket] Error:', error);
        };

        state.websocket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const messageType = data.type;

                switch (messageType) {
                    case 'log_update':
                        debugLog('[WebSocket] log_update:', data.log);
                        // 所有日志都添加到日志区域
                        addLogEntry(data.log, data.log_type || 'info');
                        break;

                    case 'test_complete':
                        state.testing = false;
                        state.currentBurningProgress = 0;  // 重置进度
                        updateTestToggleButton(false);
                        addLogEntry('测试完成', 'success');
                        if (data.notification) {
                            handleRealtimeNotification(data.notification, { toast: false });
                        }
                        showToast('测试完成', 'success');
                        break;

                    case 'devices_updated':
                        state.devices = data.devices;
                        renderDevices();
                        break;

                    case 'device_lock_update':
                        // 快速更新设备锁定状态（不需要重新查询设备列表）
                        debugLog('[WebSocket] device_lock_update:', data);
                        if (data.devices && Array.isArray(data.devices)) {
                            let updated = false;
                            data.devices.forEach(update => {
                                const deviceId = update.device_id;
                                debugLog(`[Device Lock] Updating ${deviceId}: locked=${update.locked}, by=${update.locked_by}`);
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
                                        debugLog(`[Device Lock] Converted to object:`, state.devices[idx]);
                                    } else {
                                        // 更新现有对象
                                        device.locked = update.locked;
                                        device.locked_by = update.locked_by || '';
                                        device.locked_at = update.locked_at || '';
                                        debugLog(`[Device Lock] Updated device:`, device);
                                    }
                                } else {
                                    console.warn(`[Device Lock] Device ${deviceId} not found in state.devices`);
                                }
                            });

                            // 重新渲染设备列表
                            if (updated) {
                                debugLog('[Device Lock] Re-rendering devices...');
                                try {
                                    renderDevices();
                                    debugLog('[Device Lock] Render completed successfully');
                                } catch (error) {
                                    console.error('[Device Lock] Render failed:', error);
                                }
                            } else {
                                console.warn('[Device Lock] No devices were updated, skipping render');
                            }
                        }
                        break;

                    case 'devices_changed':
                        // USB 设备插拔事件，自动刷新设备列表
                        debugLog('[WebSocket] devices_changed:', data.devices);
                        if (data.notification) {
                            handleRealtimeNotification(data.notification, { toast: false });
                        }

                        // 优先使用后端提供的 connected/disconnected 信息（更准确、更快）
                        let connected = data.connected || [];
                        let disconnected = data.disconnected || [];

                        // 如果没有提供 connected/disconnected，则通过比较计算（向后兼容）
                        if (connected.length === 0 && disconnected.length === 0) {
                            const oldDevices = new Set(state.devices.map(d => typeof d === 'string' ? d : d.device_id));
                            const newDevicesSet = new Set(data.devices || []);
                            connected = [...newDevicesSet].filter(d => !oldDevices.has(d));
                            disconnected = [...oldDevices].filter(d => !newDevicesSet.has(d));
                        }

                        // 刷新设备列表
                        loadDevices(true).then(() => {
                            // 构建设备变化消息
                            let changeMessage = '检测到 USB 设备变化';
                            if (connected.length > 0) {
                                changeMessage += `，连接：${connected.join(' ')}`;
                            }
                            if (disconnected.length > 0) {
                                changeMessage += `，断开：${disconnected.join(' ')}`;
                            }
                            addLogEntry(changeMessage, 'info');

                            let message = '设备列表已更新';
                            if (connected.length > 0) {
                                message += `，连接：${connected.join(' ')}`;
                            }
                            if (disconnected.length > 0) {
                                message += `，断开：${disconnected.join(' ')}`;
                            }
                            showToast(message, 'success');

                            // 检查 USB/IP 设备是否断开，如果是则重置按钮状态
                            if (state.usbipConnected && disconnected.length > 0) {
                                const btn = $('usbip-btn');
                                if (btn) {
                                    btn.textContent = '📱 本地设备';
                                    btn.disabled = false;
                                    state.usbipConnected = false;
                                    debugLog('[USB/IP] Button reset due to device disconnect');
                                }
                            }
                        }).catch(err => {
                            console.error('Failed to refresh devices:', err);
                        });
                        break;

                    case 'notification':
                        handleRealtimeNotification(data.notification);
                        break;

                    case 'firmware_progress':
                        // 固件烧写进度更新
                        debugLog('[WebSocket] firmware_progress:', data.percentage);
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
                        updateUploadProgress(data.percentage, data.filename, data.uploaded_size, data.total_size);
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

                    case 'heartbeat':
                        // 服务器端心跳包，不需要响应
                        break;

                    case 'pong':
                        // 心跳响应，不需要处理
                        break;

                    default:
                        debugLog('[WebSocket] Unknown message type:', messageType, data);
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

    // Initialize report analysis drag and drop
    initReportAnalysis();
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

    // 清空测试报告输入框
    const retryResultInput = $('retry-result');
    if (retryResultInput) {
        retryResultInput.value = '';
        addLogEntry('测试类型已更改，清空测试报告', 'info');
    }

    renderTestSuitesDropdown();
}

// 自动选择测试套件的函数
function autoSelectTestSuite(testType) {
    // 获取所有匹配的测试套件
    // 特殊处理：GSI使用CTS的测试套件，GTS-ROOT使用GTS的测试套件
    let matchingSuites;
    const testTypeLower = testType.toLowerCase();

    if (testTypeLower === 'gsi') {
        // GSI使用CTS套件
        matchingSuites = testSuitesCache.filter(suite =>
            suite.test_type.toLowerCase() === 'cts'
        );
        addLogEntry('GSI使用CTS测试套件', 'info');
    } else if (testTypeLower === 'gts-root') {
        // GTS-ROOT使用GTS套件
        matchingSuites = testSuitesCache.filter(suite =>
            suite.test_type.toLowerCase() === 'gts'
        );
        addLogEntry('GTS-ROOT使用GTS测试套件', 'info');
    } else {
        matchingSuites = testSuitesCache.filter(suite =>
            suite.test_type.toLowerCase() === testTypeLower
        );
    }

    debugLog(`[autoSelectTestSuite] 测试类型: ${testType}, 找到 ${matchingSuites.length} 个匹配套件`);

    if (matchingSuites.length > 0) {
        // 按版本号排序，选择版本号最大的
        matchingSuites.sort((a, b) => {
            // 更精确的版本号提取和比较
            // 支持多种格式:
            // android-cts-16.1_r2 -> 主版本: 16.1, 修订版: 2
            // android-gts-13.1-R1 -> 主版本: 13.1, 修订版: 1
            const extractVersion = (version) => {
                // 移除前缀，保留版本部分
                let versionStr = (version || '').replace(/^[^-]+-[^-]+-/, '');

                let mainVersion = versionStr;
                let revision = 0;

                // 分离主版本和修订版 (支持 _r 和 -R 格式)
                // 先尝试 _r 格式 (CTS格式)
                if (versionStr.includes('_r')) {
                    const parts = versionStr.split('_r');
                    mainVersion = parts[0];
                    revision = parseInt(parts[1]) || 0;
                }
                // 再尝试 -R 格式 (GTS格式)
                else if (versionStr.includes('-R')) {
                    const parts = versionStr.split('-R');
                    mainVersion = parts[0];
                    revision = parseInt(parts[1]) || 0;
                }

                // 解析主版本号 (支持 "16.1", "16" 等格式)
                let mainParts;
                if (mainVersion.includes('.')) {
                    mainParts = mainVersion.split('.').map(Number);
                } else {
                    const num = parseInt(mainVersion);
                    mainParts = isNaN(num) ? [0] : [num];
                }

                return {
                    main: mainParts,
                    revision: revision
                };
            };

            const versionA = extractVersion(a.version);
            const versionB = extractVersion(b.version);

            debugLog(`[版本比较] ${a.version} ->`, versionA, `vs ${b.version} ->`, versionB);

            // 先比较主版本号
            const maxMainLength = Math.max(versionA.main.length, versionB.main.length);
            for (let i = 0; i < maxMainLength; i++) {
                const numA = versionA.main[i] || 0;
                const numB = versionB.main[i] || 0;
                if (numA !== numB) {
                    return numB - numA; // 降序排列
                }
            }

            // 主版本相同，比较修订版
            return versionB.revision - versionA.revision; // 降序排列
        });

        // 选择版本号最大的
        const latestSuite = matchingSuites[0];
        $('test-suite').value = latestSuite.tools_path;
        addLogEntry(`自动选择最新测试套件: ${latestSuite.version}`, 'info');

        debugLog(`[autoSelectTestSuite] 已选择套件:`, {
            version: latestSuite.version,
            path: latestSuite.tools_path,
            all_suites: matchingSuites.map(s => ({ version: s.version, path: s.tools_path }))
        });
    } else {
        addLogEntry(`未找到 ${testType} 类型的测试套件`, 'warning');
        // 清空测试套件选择
        $('test-suite').value = '';
    }
}

function onDeviceHostConfirm() {
    const deviceHost = $('device-host').value.trim();
    addLogEntry(`设备主机地址暂不支持动态更新: ${deviceHost}`, 'warning');
    showToast('设备主机地址需要直接编辑config.json文件', 'warning');
    // 注意：device_host不是动态配置字段，无法通过API更新
    // 如需修改，请直接编辑configs/config.json文件
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
// Helper function to create FormData for API calls
// Analysis mode constants (matching backend Enum)
const AnalysisMode = {
    UPLOAD: 'upload',
    SAVED: 'saved',
    AI: 'ai'
};

// Helper function to create FormData for API calls
function createFormData(mode, params = {}, files = {}) {
    const formData = new FormData();
    formData.append('mode', mode);

    // Add regular parameters
    for (const [key, value] of Object.entries(params)) {
        if (value !== undefined && value !== null) {
            formData.append(key, value);
        }
    }

    // Add files
    for (const [key, file] of Object.entries(files)) {
        if (file instanceof File) {
            formData.append(key, file);
        }
    }

    return formData;
}

async function apiCall(url, method = 'GET', data = null) {
    try {
        const headers = {
            'Content-Type': 'application/json',
            ...getClientIdentityHeaders()
        };

        const options = {
            method,
            headers
        };

        // Only add body for POST/PUT/PATCH/DELETE methods (not GET/HEAD)
        if (data && !['GET', 'HEAD'].includes(method.toUpperCase())) {
            options.body = JSON.stringify(data);
        }

        const response = await fetch(url, options);
        const contentType = response.headers.get('content-type') || '';
        let result = null;

        if (contentType.includes('application/json')) {
            try {
                result = await response.json();
            } catch (jsonError) {
                result = { success: response.ok, error: '响应 JSON 解析失败' };
            }
        } else {
            const text = await response.text();
            result = text ? { success: response.ok, message: normalizeApiTextError(text) } : { success: response.ok };
        }

        if (!result || typeof result !== 'object') {
            result = { success: response.ok };
        }

        // 如果API返回了client_id，更新state.clientId
        if (result.client_id) {
            const oldClientId = state.clientId;
            state.clientId = result.client_id;

            // 检查是否是unknown用户
            if (result.client_id.startsWith('unknown@')) {
                debugLog(`[apiCall] Detected unknown client: ${result.client_id}`);

                // 只在第一次检测到unknown时显示弹框（避免重复弹窗）
                if (!state.usernameDetectShown) {
                    state.usernameDetectShown = true;
                    debugLog('[apiCall] Showing username detect modal for:', result.ip);

                    // 延迟显示弹框，确保页面已加载完成
                    setTimeout(() => {
                        showUsernameDetectModal(result.ip);
                    }, 500);
                }
            } else if (oldClientId !== result.client_id) {
                debugLog(`[apiCall] Updated state.clientId: ${oldClientId} → ${result.client_id}`);
            }
        }

        if (!response.ok) {
            const error = new Error(result.error || result.message || 'Request failed');
            // Attach additional fields from error response
            if (result.need_password) {
                error.needPassword = true;
                error.suppressToast = true; // Don't show toast for password prompt
            }
            if (result.device_host) error.deviceHost = result.device_host;
            if (result.install_guide) error.installGuide = result.install_guide;
            if (result.public_client_required) {
                error.publicClientRequired = true;
                error.agentInstallUrl = result.agent_install_url;
                error.suppressToast = true;
            }
            throw error;
        }

        return result;
    } catch (error) {
        debugLog('API Error:', error);
        // Only show toast if not suppressed (e.g., for password prompt)
        if (!error.suppressToast) {
            showToast(error.message, 'error');
        }
        throw error;
    }
}

function normalizeApiTextError(text) {
    const message = String(text || '').trim();
    const lower = message.toLowerCase();
    if (lower.includes('ngrok gateway error') || lower.includes('err_ngrok_')) {
        return '公网隧道临时不可用或请求被中断，请确认服务端未重启、agent 在线后重试';
    }
    if (lower.startsWith('<!doctype html') || lower.startsWith('<html')) {
        return '服务器返回了 HTML 错误页，请稍后重试或查看服务端日志';
    }
    return message;
}

function safeHeaderPercentEncode(value) {
    const text = String(value ?? '');
    try {
        return encodeURIComponent(text);
    } catch (error) {
        // Drop invalid surrogate code units before encoding; header values must stay ASCII.
        return encodeURIComponent(text.replace(/[\uD800-\uDFFF]/g, ''));
    }
}

function getClientIdentityHeaders() {
    if (!state.clientId || state.clientId === 'unknown') {
        return {};
    }

    const separatorIndex = state.clientId.lastIndexOf('@');
    const username = separatorIndex >= 0
        ? state.clientId.slice(0, separatorIndex)
        : state.clientId;

    return {
        'X-Client-Username': safeHeaderPercentEncode(username),
        'X-Client-Username-Encoding': 'percent'
    };
}

function applyClientIdentityHeadersToXhr(xhr) {
    Object.entries(getClientIdentityHeaders()).forEach(([key, value]) => {
        xhr.setRequestHeader(key, value);
    });
}

// ==================== Device Management ====================
async function loadDevices(forceRefresh = false) {
    if (state.isRefreshingDevices) {
        state.pendingDeviceRefresh = Boolean(state.pendingDeviceRefresh || forceRefresh);
        return state.deviceRefreshPromise || Promise.resolve(state.devices);
    }

    state.isRefreshingDevices = true;
    state.pendingDeviceRefresh = null;

    state.deviceRefreshPromise = (async () => {
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
        return devices;
    })();

    try {
        return await state.deviceRefreshPromise;
    } catch (error) {
        addLogEntry('加载设备列表失败: ' + error.message, 'error');
        throw error;
    } finally {
        state.isRefreshingDevices = false;
        state.deviceRefreshPromise = null;

        if (state.pendingDeviceRefresh !== null) {
            const pendingForceRefresh = state.pendingDeviceRefresh;
            state.pendingDeviceRefresh = null;
            setTimeout(() => {
                loadDevices(pendingForceRefresh).catch((error) => {
                    debugLog('[Devices] Pending refresh failed:', error);
                });
            }, 100);
        }
    }
}

// 测试套件管理
let testSuitesCache = [];
let _loadSuitesPromise = null;

async function loadTestSuites(forceRefresh = false) {
    // 如果有正在进行的请求，等待它完成
    if (_loadSuitesPromise) {
        return _loadSuitesPromise;
    }

    if (!forceRefresh && testSuitesCache.length > 0) {
        return testSuitesCache;
    }

    _loadSuitesPromise = (async () => {
        try {
            const response = await apiCall('/api/test/suites');

            if (response.success && response.suites) {
                testSuitesCache = response.suites;
                renderTestSuitesDropdown();
                if (typeof renderTestSuiteBrowserList === 'function') {
                    renderTestSuiteBrowserList();
                }
                debugLog('[loadTestSuites] 已加载测试套件:', response.count, '个');
                return testSuitesCache;
            } else {
                showToast('加载测试套件失败', 'error');
            }
        } catch (error) {
            console.error('[loadTestSuites] 错误:', error);
            showToast('加载测试套件失败: ' + error.message, 'error');
        } finally {
            _loadSuitesPromise = null;
        }
        return testSuitesCache;
    })();

    return _loadSuitesPromise;
}

function renderTestSuitesDropdown() {
    const selectElement = document.getElementById('test-suite');

    // 清空现有选项
    selectElement.innerHTML = '';

    // 添加空选项作为默认值
    const emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = '';
    emptyOption.disabled = true;
    emptyOption.selected = true;
    selectElement.appendChild(emptyOption);

    // 按测试类型分组
    const groupedSuites = {};
    testSuitesCache.forEach(suite => {
        // GTS-ROOT 使用 GTS 分组
        const groupType = suite.test_type.toLowerCase() === 'gts-root' ? 'gts' : suite.test_type.toLowerCase();
        if (!groupedSuites[groupType]) {
            groupedSuites[groupType] = [];
        }
        groupedSuites[groupType].push(suite);
    });

    // 添加分组选项
    Object.keys(groupedSuites).sort().forEach(testType => {
        const group = document.createElement('optgroup');
        group.label = testType.toUpperCase();

        groupedSuites[testType].forEach(suite => {
            const option = document.createElement('option');
            option.value = suite.tools_path;
            option.textContent = suite.tools_path;
            group.appendChild(option);
        });

        selectElement.appendChild(group);
    });

    // 渲染完成后，自动根据当前选择的测试类型来选择合适的测试套件
    const currentTestType = $('test-type')?.value;
    if (currentTestType) {
        autoSelectTestSuite(currentTestType);
    }
}

// ==================== Test Suite Browser ====================
function getSuiteDisplayName(suite) {
    if (!suite) return '-';
    return suite.version || suite.binary || (suite.tools_path || '').split('/').filter(Boolean).slice(-2).join('/') || suite.tools_path || '-';
}

function getSuiteRootFromToolsPath(toolsPath) {
    if (!toolsPath) return '';
    return toolsPath.endsWith('/tools') ? toolsPath.slice(0, -'/tools'.length) : toolsPath;
}

function getSuiteReleasePath(suite) {
    const toolsPath = suite?.tools_path || '';
    const version = suite?.version || '';

    if (toolsPath && version) {
        const marker = `/${version}`;
        const markerIndex = toolsPath.indexOf(marker);
        if (markerIndex !== -1) {
            return toolsPath.slice(0, markerIndex + marker.length);
        }
    }

    const rootPath = getSuiteRootFromToolsPath(toolsPath);
    const parts = rootPath.split('/').filter(Boolean);
    if (parts.length >= 1 && /^android-[^/]+$/.test(parts[parts.length - 1])) {
        parts.pop();
        return `/${parts.join('/')}`;
    }
    return rootPath || toolsPath;
}

function getSuiteBrowserRouteParams() {
    const rawHash = window.location.hash.substring(1);
    const [page, query = ''] = rawHash.split('?');
    if (page !== 'test-suites' || !query) {
        return null;
    }

    const params = new URLSearchParams(query);
    const suitePath = params.get('suite_path') || params.get('suite') || '';
    const filePath = params.get('file') || '';
    const directoryPath = params.get('path') || (filePath ? getParentSuitePath(filePath) : '');

    if (!suitePath) {
        return null;
    }

    return {
        suitePath,
        directoryPath,
        filePath
    };
}

function buildSuiteBrowserLink(path = '', type = 'file') {
    const params = new URLSearchParams();
    params.set('suite_path', state.suiteBrowser.selectedSuitePath);
    if (type === 'directory') {
        params.set('path', path || '');
    } else {
        params.set('file', path || '');
    }

    return `${window.location.origin}${window.location.pathname}${window.location.search}#test-suites?${params.toString()}`;
}

async function initTestSuiteBrowserPage() {
    const listEl = $('suite-browser-list');
    if (listEl) {
        listEl.innerHTML = '<div class="suite-empty">正在加载...</div>';
    }

    await loadTestSuites();
    renderTestSuiteBrowserList();

    const routeParams = getSuiteBrowserRouteParams();
    if (routeParams) {
        state.suiteBrowser.highlightPath = routeParams.filePath || '';
        await selectTestSuiteForBrowser(
            routeParams.suitePath,
            routeParams.directoryPath || '',
            { preserveHighlight: true }
        );
        return;
    }

    if (state.suiteBrowser.selectedSuitePath) {
        const selectedSuite = testSuitesCache.find(s => s.tools_path === state.suiteBrowser.selectedSuitePath);
        if (selectedSuite) {
            await selectTestSuiteForBrowser(selectedSuite.tools_path, state.suiteBrowser.currentPath || '');
            return;
        }
    }

    clearSuiteBrowserSelection('请选择左侧测试套件');
}

async function refreshTestSuiteBrowser() {
    await loadTestSuites(true);
    renderTestSuiteBrowserList();
    const suitePath = state.suiteBrowser.selectedSuitePath || '';
    if (!suitePath) {
        clearSuiteBrowserSelection('请选择左侧测试套件');
        return;
    }

    const selectedSuite = testSuitesCache.find(s => s.tools_path === suitePath);
    if (selectedSuite) {
        await selectTestSuiteForBrowser(suitePath, state.suiteBrowser.currentPath || '');
    } else {
        clearSuiteBrowserSelection('已选择的测试套件不存在');
    }
}

function filterTestSuiteBrowserList() {
    renderTestSuiteBrowserList();
}

// ==================== 测试套件下载和解压 ====================

// 暴露到全局作用域
window.downloadTestSuite = async function downloadTestSuite() {
    const urlInput = $('suite-download-url');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');
    const logDiv = $('suite-download-log');

    console.log('[downloadTestSuite] urlInput:', urlInput);
    console.log('[downloadTestSuite] downloadBtn:', downloadBtn);

    if (!urlInput || !urlInput.value) {
        showToast('请输入下载地址', 'error');
        return;
    }

    const url = urlInput.value.trim();

    console.log('[downloadTestSuite] URL:', url);

    // 禁用按钮
    if (downloadBtn) {
        downloadBtn.disabled = true;
        downloadBtn.textContent = '⬇️ 下载中...';
    }
    if (extractBtn) extractBtn.disabled = true;

    // 显示进度
    if (progressDiv) progressDiv.style.display = 'block';
    if (logDiv) {
        logDiv.style.display = 'block';
        logDiv.innerHTML = '';
    }

    const log = (msg) => {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ${msg}\n`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        console.log('[downloadTestSuite] ' + msg);
    };

    log(`开始下载：${url}`);
    console.log('[downloadTestSuite] 开始 fetch 请求...');

    try {
        const response = await fetch('/api/test/suites/download-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: url,
                save_dir: `/home/${getDefaultUbuntuUser()}/GMS-Suite`
            })
        });
        console.log('[downloadTestSuite] fetch 响应状态:', response.status);

        const result = await response.json();
        console.log('[downloadTestSuite] 响应结果:', result);

        if (result.success) {
            log(`✅ 下载完成：${result.archive_path}`);
            log(`📦 文件大小：${(result.file_size / 1024 / 1024).toFixed(2)} MB`);

            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';

            showToast(`下载完成：${result.message}`, 'success');

            // 刷新测试套件列表
            await refreshTestSuiteBrowser();
        } else {
            log(`❌ 下载失败：${result.error}`);
            if (progressStatus) progressStatus.textContent = '❌ 下载失败';
            showToast(`下载失败：${result.error}`, 'error');
        }
    } catch (error) {
        console.error('[downloadTestSuite] 异常:', error);
        log(`❌ 错误：${error.message}`);
        if (progressStatus) progressStatus.textContent = '❌ 错误';
        showToast(`下载失败：${error.message}`, 'error');
    } finally {
        console.log('[downloadTestSuite] finally - 恢复按钮');
        // 恢复按钮
        if (downloadBtn) {
            downloadBtn.disabled = false;
            downloadBtn.textContent = '⬇️ 下载套件';
        }
        if (extractBtn) extractBtn.disabled = false;
    }
};

// 页面加载完成后验证函数是否暴露成功
document.addEventListener('DOMContentLoaded', () => {
    console.log('[Init] downloadTestSuite exposed:', typeof window.downloadTestSuite === 'function');
    console.log('[Init] extractTestSuite exposed:', typeof window.extractTestSuite === 'function');
    console.log('[Init] addLocalTestSuite exposed:', typeof window.addLocalTestSuite === 'function');
    console.log('[Init] showAddLocalSuiteDialog exposed:', typeof window.showAddLocalSuiteDialog === 'function');
});

// 显示添加本地测试套件路径弹框
window.showAddLocalSuiteDialog = function showAddLocalSuiteDialog() {
    const modal = $('add-local-suite-modal');
    if (modal) {
        modal.style.display = 'flex';
        const input = $('local-suite-path-input');
        if (input) {
            input.value = '';
            input.focus();
        }
    }
};

// 关闭弹框
window.closeAddLocalSuiteModal = function closeAddLocalSuiteModal() {
    const modal = $('add-local-suite-modal');
    if (modal) {
        modal.style.display = 'none';
    }
};

// 处理 Esc 键关闭弹框
window.handleAddLocalSuiteKeydown = function handleAddLocalSuiteKeydown(event) {
    if (event.key === 'Escape') {
        closeAddLocalSuiteModal();
    }
    // 回车键提交
    if (event.key === 'Enter') {
        submitAddLocalSuite();
    }
};

// 提交添加本地测试套件
window.submitAddLocalSuite = async function submitAddLocalSuite() {
    const pathInput = $('local-suite-path-input');
    if (!pathInput || !pathInput.value) {
        showToast('请输入本地路径', 'error');
        return;
    }

    const localPath = pathInput.value.trim();
    console.log('[submitAddLocalSuite] 本地路径:', localPath);

    try {
        const response = await fetch('/api/test/suites/add-local', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: localPath })
        });

        const result = await response.json();
        console.log('[submitAddLocalSuite] 响应结果:', result);

        if (result.success) {
            showToast(`添加成功：${result.message}`, 'success');
            closeAddLocalSuiteModal();
            // 刷新测试套件列表
            await refreshTestSuiteBrowser();
        } else {
            showToast(`添加失败：${result.error}`, 'error');
        }
    } catch (error) {
        console.error('[submitAddLocalSuite] 异常:', error);
        showToast(`添加失败：${error.message}`, 'error');
    }
};

// 暴露到全局作用域 - 添加本地测试套件路径（保留向后兼容）
window.addLocalTestSuite = async function addLocalTestSuite() {
    // 已废弃，改用弹框方式
    showAddLocalSuiteDialog();
};

// 暴露到全局作用域
window.extractTestSuite = async function extractTestSuite() {
    const urlInput = $('suite-download-url');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const logDiv = $('suite-download-log');

    // 获取最近下载的文件
    try {
        const response = await fetch('/api/test/suites');
        const result = await response.json();

        // 查找最新的压缩包文件
        const archiveExtensions = ['.zip', '.tar.gz', '.tgz', '.tar.bz2', '.tar'];
        let archivePath = '';

        if (result.suites && result.suites.length > 0) {
            // 从套件列表中查找压缩包
            for (const suite of result.suites) {
                for (const ext of archiveExtensions) {
                    if (suite.tools_path.endsWith(ext)) {
                        archivePath = suite.tools_path;
                        break;
                    }
                }
                if (archivePath) break;
            }
        }

        // 如果没有找到，使用 URL 输入框的值
        if (!archivePath && urlInput && urlInput.value) {
            const filename = urlInput.value.split('/').pop();
            archivePath = `/home/${getDefaultUbuntuUser()}/GMS-Suite/${filename}`;
        }

        if (!archivePath) {
            showToast('未找到可解压的压缩包', 'error');
            return;
        }

        // 禁用按钮
        if (extractBtn) {
            extractBtn.disabled = true;
            extractBtn.textContent = '📦 解压中...';
        }
        if (downloadBtn) downloadBtn.disabled = true;

        if (logDiv) {
            logDiv.style.display = 'block';
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] 开始解压：${archivePath}\n`;
        }

        const response2 = await fetch('/api/test/suites/extract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                archive_path: archivePath,
                extract_dir: `/home/${getDefaultUbuntuUser()}/GMS-Suite`
            })
        });

        const result2 = await response2.json();

        if (result2.success) {
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ✅ 解压完成：${result2.extracted_path}\n`;
            }
            showToast(`解压完成：${result2.message}`, 'success');

            // 刷新测试套件列表
            await refreshTestSuiteBrowser();
        } else {
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ❌ 解压失败：${result2.error}\n`;
            }
            showToast(`解压失败：${result2.error}`, 'error');
        }
    } catch (error) {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ❌ 错误：${error.message}\n`;
        }
        showToast(`解压失败：${error.message}`, 'error');
    } finally {
        // 恢复按钮
        if (extractBtn) {
            extractBtn.disabled = false;
            extractBtn.textContent = '📦 解压套件';
        }
        if (downloadBtn) downloadBtn.disabled = false;
    }
}

function clearSuiteBrowserSelection(message) {
    state.suiteBrowser.selectedSuitePath = '';
    state.suiteBrowser.currentPath = '';
    state.suiteBrowser.highlightPath = '';

    const titleEl = $('suite-browser-title');
    const pathEl = $('suite-browser-path');
    const breadcrumb = $('suite-browser-breadcrumb');
    if (titleEl) titleEl.textContent = '未选择测试套件';
    if (pathEl) pathEl.textContent = '';
    if (breadcrumb) breadcrumb.innerHTML = '';

    renderTestSuiteBrowserList();
    renderSuiteFileEmpty(message || '请选择左侧测试套件');
}

function setSuiteBrowserHighlightedPath(path) {
    state.suiteBrowser.highlightPath = path || '';
    const rows = document.querySelectorAll('#suite-file-list .suite-file-row');
    rows.forEach(row => {
        const isTarget = row.dataset.path === path;
        row.classList.toggle('active', isTarget);
    });
}

function renderTestSuiteBrowserList() {
    const listEl = $('suite-browser-list');
    const countEl = $('suite-browser-count');
    if (!listEl) return;

    const filterText = ($('suite-browser-filter')?.value || '').trim().toLowerCase();
    const suites = testSuitesCache.filter(suite => {
        const haystack = [
            suite.test_type,
            suite.version,
            suite.tools_path,
            suite.binary
        ].join(' ').toLowerCase();
        return !filterText || haystack.includes(filterText);
    });

    if (countEl) {
        countEl.textContent = `${testSuitesCache.length} 个套件`;
    }

    if (suites.length === 0) {
        listEl.innerHTML = '<div class="suite-empty">没有匹配的测试套件</div>';
        return;
    }

    listEl.innerHTML = '';
    suites.forEach(suite => {
        const row = document.createElement('div');
        row.className = `suite-suite-item ${suite.tools_path === state.suiteBrowser.selectedSuitePath ? 'active' : ''}`;
        row.dataset.suitePath = suite.tools_path;

        const badge = document.createElement('span');
        badge.className = 'suite-type-badge';
        let displayType = suite.test_type || '-';
        // 将 cts-verifier 显示为 CTS-V
        if (displayType === 'cts-verifier') displayType = 'cts-v';
        badge.textContent = displayType.toUpperCase();

        const main = document.createElement('div');
        main.className = 'suite-suite-main';
        main.innerHTML = `
            <div class="suite-suite-name">${escapeHtml(getSuiteDisplayName(suite))}</div>
            <div class="suite-suite-path">${escapeHtml(getSuiteReleasePath(suite))}</div>
        `;

        row.append(badge, main);
        row.addEventListener('click', () => selectTestSuiteForBrowser(suite.tools_path));
        listEl.appendChild(row);
    });
}

async function selectTestSuiteForBrowser(suitePath, path = '', options = {}) {
    const suite = testSuitesCache.find(s => s.tools_path === suitePath);
    if (!suite) {
        renderSuiteFileEmpty('测试套件不存在');
        return;
    }

    state.suiteBrowser.selectedSuitePath = suite.tools_path;
    state.suiteBrowser.currentPath = path || '';
    if (!options.preserveHighlight) {
        state.suiteBrowser.highlightPath = '';
    }

    const suiteSelect = document.getElementById('test-suite');
    if (suiteSelect && suiteSelect.value !== suite.tools_path) {
        suiteSelect.value = suite.tools_path;
    }

    const titleEl = $('suite-browser-title');
    const pathEl = $('suite-browser-path');
    let displayType = suite.test_type || '';
    // 将 cts-verifier 显示为 CTS-V
    if (displayType === 'cts-verifier') displayType = 'cts-v';
    if (titleEl) titleEl.textContent = `${displayType.toUpperCase()} ${getSuiteDisplayName(suite)}`;
    if (pathEl) pathEl.textContent = getSuiteRootFromToolsPath(suite.tools_path);

    renderTestSuiteBrowserList();
    await loadSuiteBrowserDirectory(path || '');
}

async function loadSuiteBrowserDirectory(path = '') {
    if (!state.suiteBrowser.selectedSuitePath) {
        renderSuiteFileEmpty('请先选择测试套件');
        return;
    }

    const fileList = $('suite-file-list');
    if (fileList) {
        fileList.innerHTML = '<div class="suite-empty">正在加载目录...</div>';
    }

    try {
        const params = new URLSearchParams({
            suite_path: state.suiteBrowser.selectedSuitePath,
            path: path || ''
        });
        const result = await apiCall(`/api/test/suites/files?${params.toString()}`);
        const data = result.data || {};
        state.suiteBrowser.currentPath = data.path || '';
        renderSuiteBreadcrumb(state.suiteBrowser.currentPath);
        renderSuiteFiles(data.items || []);
    } catch (error) {
        renderSuiteFileEmpty(`加载失败: ${error.message}`);
    }
}

function renderSuiteBreadcrumb(path) {
    const breadcrumb = $('suite-browser-breadcrumb');
    if (!breadcrumb) return;

    const parts = (path || '').split('/').filter(Boolean);
    breadcrumb.innerHTML = '';

    const rootBtn = document.createElement('button');
    rootBtn.className = 'btn-xs';
    rootBtn.textContent = '根目录';
    rootBtn.addEventListener('click', () => loadSuiteBrowserDirectory(''));
    breadcrumb.appendChild(rootBtn);

    if (parts.length === 0) return;

    let current = '';
    parts.forEach(part => {
        current = current ? `${current}/${part}` : part;
        const separator = document.createTextNode(' / ');
        const btn = document.createElement('button');
        btn.className = 'btn-xs';
        btn.textContent = part;
        const targetPath = current;
        btn.addEventListener('click', () => loadSuiteBrowserDirectory(targetPath));
        breadcrumb.append(separator, btn);
    });
}

function renderSuiteFiles(items) {
    const fileList = $('suite-file-list');
    if (!fileList) return;

    fileList.innerHTML = '';

    if (state.suiteBrowser.currentPath) {
        const parentRow = createSuiteFileRow({
            name: '..',
            path: getParentSuitePath(state.suiteBrowser.currentPath),
            type: 'directory',
            size: 0,
            isParent: true
        });
        fileList.appendChild(parentRow);
    }

    if (!items.length) {
        if (!state.suiteBrowser.currentPath) {
            renderSuiteFileEmpty('目录为空');
        }
        return;
    }

    items.forEach(item => {
        fileList.appendChild(createSuiteFileRow(item));
    });

    const activeRow = fileList.querySelector('.suite-file-row.active');
    if (activeRow) {
        activeRow.scrollIntoView({ block: 'center' });
    }
}

function createSuiteFileRow(item) {
    const row = document.createElement('div');
    row.className = 'suite-file-row';
    row.dataset.path = item.path || '';
    if (item.path && item.path === state.suiteBrowser.highlightPath) {
        row.classList.add('active');
    }
    row.addEventListener('click', () => {
        if (!item.isParent) {
            setSuiteBrowserHighlightedPath(item.path || '');
        }
    });

    const icon = document.createElement('span');
    icon.textContent = item.type === 'directory' ? '📁' : (item.is_apk ? '📦' : (item.is_jar ? '🫙' : '📄'));

    const main = document.createElement('div');
    main.className = 'suite-file-main';

    const name = document.createElement('div');
    name.className = 'suite-file-name';
    name.textContent = item.name;

    main.appendChild(name);

    if (item.type !== 'directory') {
        const meta = document.createElement('div');
        meta.className = 'suite-file-meta';
        meta.textContent = `${formatBytes(item.size || 0, true)}${item.is_apk ? ' · APK' : (item.is_jar ? ' · JAR' : '')}`;
        main.appendChild(meta);
    }

    const actions = document.createElement('div');
    actions.className = 'suite-file-actions';

    if (item.type === 'directory') {
        const openBtn = document.createElement('button');
        openBtn.className = 'btn-xs';
        openBtn.textContent = item.isParent ? '返回' : '打开';
        openBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            if (!item.isParent) {
                setSuiteBrowserHighlightedPath(item.path || '');
            }
            loadSuiteBrowserDirectory(item.path || '');
        });
        actions.appendChild(openBtn);

        if (!item.isParent) {
            const copyBtn = document.createElement('button');
            copyBtn.className = 'btn-xs';
            copyBtn.textContent = '分享链接';
            copyBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                setSuiteBrowserHighlightedPath(item.path || '');
                copySuiteBrowserLink(item.path || '', 'directory');
            });
            actions.appendChild(copyBtn);
        }

        row.addEventListener('dblclick', () => loadSuiteBrowserDirectory(item.path || ''));
    } else {
        if (item.is_apk || item.is_jar) {
            const analyzeBtn = document.createElement('button');
            analyzeBtn.className = 'btn-xs';
            analyzeBtn.textContent = '反编译';
            analyzeBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                analyzeSuiteApk(item.path);
            });
            actions.appendChild(analyzeBtn);
        }

        const downloadBtn = document.createElement('button');
        downloadBtn.className = 'btn-xs';
        downloadBtn.textContent = '下载';
        downloadBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            downloadSuiteFile(item.path, item.name);
        });
        actions.appendChild(downloadBtn);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn-xs';
        copyBtn.textContent = '分享链接';
        copyBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            setSuiteBrowserHighlightedPath(item.path || '');
            copySuiteBrowserLink(item.path || '', 'file');
        });
        actions.appendChild(copyBtn);

        row.addEventListener('dblclick', () => downloadSuiteFile(item.path, item.name));
    }

    row.append(icon, main, actions);
    return row;
}

function copySuiteBrowserLink(path, type = 'file') {
    if (!state.suiteBrowser.selectedSuitePath) return;
    copyText(buildSuiteBrowserLink(path, type), { successMsg: '链接已复制' });
}

if (!window.__suiteBrowserHashListenerInstalled) {
    window.__suiteBrowserHashListenerInstalled = true;
    window.addEventListener('hashchange', () => {
        if (!getSuiteBrowserRouteParams()) {
            return;
        }

        if (typeof window.switchPage === 'function') {
            window.switchPage('test-suites', null);
        } else {
            initTestSuiteBrowserPage();
        }
    });
}

function getParentSuitePath(path) {
    const parts = (path || '').split('/').filter(Boolean);
    parts.pop();
    return parts.join('/');
}

function renderSuiteFileEmpty(message) {
    const fileList = $('suite-file-list');
    if (fileList) {
        fileList.innerHTML = `<div class="suite-empty">${escapeHtml(message)}</div>`;
    }
}

function downloadSuiteFile(path, filename = '') {
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    const params = new URLSearchParams({
        suite_path: state.suiteBrowser.selectedSuitePath,
        path
    });
    let frame = document.getElementById('suite-download-frame');
    if (!frame) {
        frame = document.createElement('iframe');
        frame.id = 'suite-download-frame';
        frame.name = 'suite-download-frame';
        frame.style.display = 'none';
        document.body.appendChild(frame);
    }

    const link = document.createElement('a');
    link.href = `/api/test/suites/download?${params.toString()}`;
    link.download = filename || path.split('/').pop() || 'download';
    link.target = frame.name;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
}

async function analyzeSuiteApk(path) {
    if (!state.suiteBrowser.selectedSuitePath || !path) return;

    try {
        showToast('正在准备反编译任务...', 'info');
        const result = await apiCall('/api/test/suites/apk/analyze', 'POST', {
            suite_path: state.suiteBrowser.selectedSuitePath,
            path
        });
        const task = result.data || {};
        if (!task.task_id) {
            showToast('创建反编译任务失败', 'error');
            return;
        }

        switchPage('apk-analysis', null);
        initApkAnalysisPage();
        stopApkPolling();
        window.apkNotifiedTaskId = null;

        window.apkCurrentTaskId = task.task_id;
        setApkUploadEmpty(false);

        const fileSizeMB = task.size ? (task.size / (1024 * 1024)).toFixed(1) : '-';
        $('apk-analysis-status').style.display = 'block';
        $('apk-file-name').textContent = `${task.filename || path} (${fileSizeMB}MB)`;
        $('apk-analysis-state').textContent = '已从测试套件导入，正在启动反编译';
        $('apk-btn-download').style.display = 'none';
        $('apk-analysis-result').style.display = 'none';
        $('apk-analysis-progress-container').style.display = 'none';
        $('apk-analysis-progress-bar').style.width = '0%';

        const sourceTree = $('apk-source-tree');
        if (sourceTree) {
            sourceTree.dataset.loaded = '';
            sourceTree.innerHTML = '';
        }
        const permList = $('apk-permissions-list');
        if (permList) {
            permList.dataset.loaded = '';
            permList.innerHTML = '';
        }
        const manifestInfo = $('apk-manifest-info');
        if (manifestInfo) manifestInfo.innerHTML = '';
        const rawXml = $('apk-raw-xml');
        if (rawXml) rawXml.textContent = '';
        closeApkFileViewer();
        switchApkTab('manifest');

        await startApkAnalysis();
    } catch (error) {
        showToast(`准备反编译失败: ${error.message}`, 'error');
    }
}

// 用户列表管理
async function loadUsers(forceRefresh = false) {
    if (state.isRefreshingUsers) {
        return;
    }

    state.isRefreshingUsers = true;

    try {
        const url = forceRefresh ? '/api/users/list?force_refresh=1' : '/api/users/list';
        const response = await apiCall(url);

        debugLog('[loadUsers] API response:', response);

        // 处理不同的响应格式
        let users = [];
        if (Array.isArray(response)) {
            users = response;
            debugLog('[loadUsers] Response is array, length:', users.length);
        } else if (response && response.users && Array.isArray(response.users)) {
            users = response.users;
            debugLog('[loadUsers] Response has users array, length:', users.length);
        } else if (response && response.data && Array.isArray(response.data)) {
            users = response.data;
            debugLog('[loadUsers] Response has data array, length:', users.length);
        } else {
            console.warn('[loadUsers] Unexpected user list format:', response);
        }

        state.users = users;
        debugLog('[loadUsers] state.users set to:', state.users);
        // renderUsers() 已移除，使用 HTML 中的 displayUsersList() 避免重复渲染
    } catch (error) {
        console.error('加载用户列表失败:', error);
    } finally {
        state.isRefreshingUsers = false;
    }
}


function formatTime(timestamp) {
    if (!timestamp) return '-';
    const date = new Date(timestamp);
    const now = new Date();
    const diff = Math.floor((now - date) / 1000); // 秒

    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
    return `${Math.floor(diff / 86400)}天前`;
}

// 防抖版本的刷新函数
const debouncedRefreshDevices = debounce(() => loadDevices(false), 500);
const debouncedRefreshUsers = debounce(() => loadUsers(false), 500);

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
        deviceCanvas.style.minHeight = '150px'; // 确保无设备时有默认高度
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
    // Event delegation is used on the containers (setup below), so no individual onclick needed
    const renderDeviceItem = ({ deviceId, isLocked, lockedBy }) => {
        const div = document.createElement('div');
        const isSelected = state.selectedDevices.has(deviceId);
        div.className = `device-item ${isSelected ? 'selected' : ''} ${isLocked ? 'locked' : ''}`;
        div.dataset.deviceId = deviceId;
        if (isLocked) div.dataset.locked = 'true';

        div.title = isLocked ? `已被 ${lockedBy} 占用` : '点击选择设备';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'device-checkbox';
        checkbox.checked = isSelected;
        if (isLocked) checkbox.disabled = true;

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

    // Setup event delegation on containers (only once, using data attributes)
    const setupDeviceDelegation = (container) => {
        if (container._delegated) return;
        container._delegated = true;
        container.addEventListener('click', (e) => {
            if (e.target.classList.contains('device-checkbox') && !e.target.disabled) {
                e.stopPropagation();
            }
            const item = e.target.closest('.device-item');
            if (!item || item.dataset.locked === 'true') return;
            const deviceId = item.dataset.deviceId;
            if (deviceId) toggleDevice(deviceId);
        });
    };
    setupDeviceDelegation(leftContainer);
    setupDeviceDelegation(rightContainer);
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
        // Select all - skip devices locked by other users
        let selectedCount = 0;
        let skippedLocked = 0;

        state.devices.forEach(device => {
            // Extract device_id from object or use string directly
            const deviceId = typeof device === 'string' ? device : device.device_id;
            const deviceObj = typeof device === 'string' ?
                state.devices.find(d => d.device_id === deviceId) : device;

            // 检查设备是否被锁定
            if (deviceObj && deviceObj.locked && !deviceObj.locked_by_self) {
                // 设备被其他用户锁定，跳过
                skippedLocked++;
                debugLog(`[SelectAll] Skipping locked device: ${deviceId} (locked by: ${deviceObj.locked_by})`);
            } else {
                // 设备未被锁定或被自己锁定，可以选择
                state.selectedDevices.add(deviceId);
                selectedCount++;
            }
        });

        if (skippedLocked > 0) {
            showToast(`跳过 ${skippedLocked} 台被其他用户锁定的设备`, 'warning');
            addLogEntry(`全选设备：已选择 ${selectedCount} 台，跳过 ${skippedLocked} 台被锁定的设备`, 'warning');
        }
    }
    renderDevices();
    addLogEntry(`已选择 ${state.selectedDevices.size} 台设备`, 'info');
}

async function rebootDevices() {
    if (!validateDeviceSelection()) return;

    // 获取选中设备的序列号
    const selectedDeviceSerials = Array.from(state.selectedDevices).map(deviceId => {
        const device = state.devices.find(d =>
            (d.device_id && d.device_id === deviceId) ||
            (d.serial && d.serial === deviceId) ||
            d === deviceId
        );
        return device ? (device.device_id || device.serial || deviceId) : deviceId;
    });

    const confirmed = await showConfirmDialog(
        '重启设备',
        `确定要重启以下 ${state.selectedDevices.size} 台设备吗？\n\n${selectedDeviceSerials.join('\n')}`
    );

    if (!confirmed) return;

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
    const button = document.getElementById('btn-remount-devices');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        addLogEntry('正在执行 remount...', 'info');
        await callDeviceApi('/api/devices/remount');
    } catch (error) {
        addLogEntry('Remount失败: ' + error.message, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

async function connectWifi() {
    if (!validateDeviceSelection()) return;
    ModalManager.open('wifi-modal');
}

function closeWifiModal() {
    ModalManager.close('wifi-modal');
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

        await apiCall('/api/devices/wifi', 'POST', {
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
    if (!validateDeviceSelection()) return;

    const buttonId = action === 'lock' ? 'btn-lock-device' : 'btn-unlock-device';
    const button = document.getElementById(buttonId);
    const actionText = action === 'lock' ? '锁定' : '解锁';

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        addLogEntry(`正在${actionText}设备...`, 'info');
        await callDeviceApi(`/api/devices/bootloader-${action}`, {});
        addLogEntry(`设备${actionText}完成`, 'info');
    } catch (error) {
        addLogEntry(`设备${actionText}失败: ${error.message}`, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

async function checkDeviceLockStatus() {
    if (!validateDeviceSelection()) return;

    const button = document.getElementById('btn-check-lock-status');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        const result = await apiCall('/api/devices/bootloader-status', 'POST', {
            devices: Array.from(state.selectedDevices)
        });
        addLogEntry('设备锁定状态: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('获取锁定状态失败: ' + error.message, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

async function collectDeviceInfo() {
    if (!validateDeviceSelection()) return;

    const button = document.getElementById('btn-device-info');

    // 禁用按钮，防止重复点击
    if (button) {
        button.disabled = true;
        button.style.opacity = '0.5';
        button.style.cursor = 'not-allowed';
    }

    try {
        const result = await apiCall('/api/devices/info', 'POST', {
            devices: Array.from(state.selectedDevices)
        });
        addLogEntry('设备信息: ' + JSON.stringify(result, null, 2), 'info');
    } catch (error) {
        addLogEntry('获取设备信息失败: ' + error.message, 'error');
    } finally {
        // 恢复按钮状态
        if (button) {
            button.disabled = false;
            button.style.opacity = '1';
            button.style.cursor = 'pointer';
        }
    }
}

// ==================== VNC & Remote Control ====================
async function burnFirmware() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择要烧写固件的设备', 'warning');
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
                showToast(`已选择固件文件: ${file.name}`, 'info');
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

    const defaultUser = getDefaultUbuntuUser();
    await loadFileDirectory(`/home/${defaultUser}/GMS-Suite`);
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
    try {
        closeFirmwareModal();
        showToast('正在烧写固件...', 'info');
        addLogEntry(`开始烧写固件: ${firmwarePath}`, 'info');

        // 立即在UI上标记设备为锁定状态
        lockDevicesInUI(devices);

        const warnBeforeRefresh = (e) => {
            e.preventDefault();
            e.returnValue = '固件上传中，刷新将中断上传！确定要离开吗？';
            return e.returnValue;
        };
        const cleanupUploadState = () => {
            if (selectedFirmwareFile) {
                window.removeEventListener('beforeunload', warnBeforeRefresh);
                clearFirmwareUploadState();
            }
        };

        if (selectedFirmwareFile) {
            // 设置上传状态标记，防止刷新导致进度丢失
            saveFirmwareUploadState(
                selectedFirmwareFile.name,
                selectedFirmwareFile.size,
                Date.now()
            );

            // 添加beforeunload事件监听，警告用户不要刷新
            window.addEventListener('beforeunload', warnBeforeRefresh);
        } else {
            addLogEntry(`使用服务器固件路径，跳过本机上传: ${firmwarePath}`, 'info');
        }

        // 准备FormData
        const formData = new FormData();
        formData.append('firmware_path', firmwarePath);
        if (selectedFirmwareFile) {
            formData.append('firmware_file', selectedFirmwareFile);
        }

        // 使用XMLHttpRequest以显示上传进度
        const uploadResult = await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();

            // 监听上传进度
            xhr.upload.addEventListener('progress', (e) => {
                if (selectedFirmwareFile && e.lengthComputable) {
                    const progress = (e.loaded / e.total) * 100;
                    // 保存进度到sessionStorage（用于刷新后恢复）
                    // 使用统一的状态管理函数
                    const startTime = parseInt(sessionStorage.getItem('firmwareUploadStartTime') || Date.now());
                    saveFirmwareUploadState(
                        selectedFirmwareFile.name,
                        selectedFirmwareFile.size,
                        startTime,
                        progress,
                        e.loaded,
                        e.total
                    );

                    // 复用现有的上传进度条显示
                    updateUploadProgress(progress, selectedFirmwareFile.name, e.loaded, e.total);
                }
            });

            xhr.addEventListener('load', () => {
                cleanupUploadState();

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
                cleanupUploadState();
                reject(new Error('Network error'));
            });

            xhr.addEventListener('abort', () => {
                cleanupUploadState();
                reject(new Error('Upload aborted'));
            });

            xhr.open('POST', `/api/burn/firmware?devices=${encodeURIComponent(devices.join(','))}`);
            applyClientIdentityHeadersToXhr(xhr);
            xhr.send(formData);
        });

        const result = uploadResult;
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
    const defaultUser = getDefaultUbuntuUser();
    const scriptInput = document.getElementById('gsi-script');
    if (scriptInput && !scriptInput.value) {
        scriptInput.value = `/home/${defaultUser}/GMS-Suite/run_GSI_Burn.sh`;
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
    const defaultUser = getDefaultUbuntuUser();
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
    ModalManager.open('file-browser-modal');

    // Load initial directory (GMS-Suite)
    const defaultUser = getDefaultUbuntuUser();
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
            debugLog('[Desktop] Default host VNC started');
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

    debugLog('[setupUsbipForward] Called, state.usbipConnected =', state.usbipConnected);

    if (state.usbipConnected) {
        // 断开连接
        debugLog('[setupUsbipForward] Disconnecting...');
        try {
            btn.textContent = '📱 断开中...';
            btn.disabled = true;

            const result = await apiCall('/api/usbip/disconnect', 'POST', {});
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
        debugLog('[setupUsbipForward] Connecting...');
        try {
            btn.textContent = '📱 连接中...';
            btn.disabled = true;

            const result = await apiCall('/api/usbip/connect', 'POST', {});

            // 检查是否成功（支持多种响应格式）
            if (result.success || result.devices || (result.message && result.message.includes('成功连接'))) {
                state.usbipConnected = true;
                btn.textContent = '📱 断开设备';
                btn.disabled = false;
                addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
                setTimeout(() => debouncedRefreshDevices(), 3500);
            } else {
                btn.textContent = '📱 本地设备';
                btn.disabled = false;

                // 检查是否需要SSH密码
                if (result.public_client_required) {
                    handlePublicClientRequired(result.agent_install_url);
                } else if (result.need_password && result.device_host) {
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
            if (error.publicClientRequired) {
                handlePublicClientRequired(error.agentInstallUrl);
            } else if (error.needPassword && error.deviceHost) {
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

function handlePublicClientRequired(agentInstallUrl) {
    const installUrl = new URL(agentInstallUrl || '/api/public-client/install.ps1', window.location.origin);
    installUrl.search = '';
    const username = state.clientId && state.clientId !== 'unknown'
        ? state.clientId.split('@').slice(0, -1).join('@')
        : '';
    const safeUsername = username.replace(/'/g, "''");

    addLogEntry('⚠️ 公网访问需要先在 Windows 客户端运行 GMS 公网客户端 agent。', 'warning');
    addLogEntry('以管理员身份运行【PowerShell】执行命令:', 'info', false);
    addLogEntry('[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12', 'info', false);
    addLogEntry('$Headers = @{"ngrok-skip-browser-warning" = "true"}', 'info', false);
    if (username) {
        addLogEntry(`$Username = '${safeUsername}'`, 'info', false);
        addLogEntry('$EncodedUsername = [uri]::EscapeDataString($Username)', 'info', false);
        addLogEntry(`iwr "${installUrl.toString()}?username=$EncodedUsername" -Headers $Headers -OutFile "$env:TEMP\\gms-public-client.ps1"`, 'info', false);
    } else {
        addLogEntry(`iwr "${installUrl.toString()}" -Headers $Headers -OutFile "$env:TEMP\\gms-public-client.ps1"`, 'info', false);
    }
    addLogEntry('powershell -ExecutionPolicy Bypass -File "$env:TEMP\\gms-public-client.ps1"', 'info', false);
    showToast('请先运行公网客户端 agent，并保持 PowerShell 窗口打开', 'warning');
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
}

async function saveUsernameManually(clientIp, username) {
    const response = await postJson('/api/users/set-username', {
        ip: clientIp,
        username
    });

    const savedUsername = response.username || username;
    const clientId = response.client_id || `${savedUsername}@${clientIp}`;
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

        const result = await apiCall('/api/usbip/connect', 'POST', {
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
        const result = await apiCall('/api/ssh/sshd', 'GET');

        if (result.public_client_required) {
            handlePublicClientRequired(result.agent_install_url);
            return;
        }

        if (!result.installed && result.install_guide) {
            // SSHD 未安装，显示安装指南（已包含在 API 响应中）
            showSshdInstallGuide(result.install_guide);
        } else if (result.running) {
            addLogEntry(`SSHD 状态: 运行中`, 'success');
        } else if (!result.installed) {
            addLogEntry(`SSHD 状态: 无法确认是否已安装`, 'warning');
        } else {
            addLogEntry(`SSHD 状态: 已安装但未运行`, 'warning');
        }

        // 如果有错误信息，显示警告
        if (result.error) {
            addLogEntry(`⚠️ ${result.error}`, 'warning');
        }
    } catch (error) {
        if (error.publicClientRequired) {
            handlePublicClientRequired(error.agentInstallUrl);
            return;
        }
        addLogEntry('检查 SSHD 失败: ' + error.message, 'error');
        // 即使检查失败，也尝试从服务器获取安装指南
        try {
            const result = await apiCall('/api/ssh/sshd', 'GET');
            if (result.public_client_required) {
                handlePublicClientRequired(result.agent_install_url);
            } else if (result.install_guide) {
                showSshdInstallGuide(result.install_guide);
            } else {
                addLogEntry('无法加载安装指南', 'error');
            }
        } catch (guideError) {
            if (guideError.publicClientRequired) {
                handlePublicClientRequired(guideError.agentInstallUrl);
                return;
            }
            addLogEntry('无法加载安装指南', 'error');
        }
    }
}

async function checkRouting() {
    // 创建弹框
    const dialog = document.createElement('div');
    dialog.id = 'route-check-dialog';
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

    ModalManager.registerDynamic(dialog);

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

    const closeDialog = () => {
        ModalManager.unregisterDynamic('route-check-dialog');
    };

    // X 按钮关闭
    closeXBtn.addEventListener('click', closeDialog);

    closeDialogBtn.addEventListener('click', closeDialog);

    pingTestBtn.addEventListener('click', async () => {
        const testHostIp = document.getElementById('test-host-ip').value.trim();
        const clientIp = document.getElementById('client-ip').value.trim();

        if (!testHostIp || !clientIp) {
            pingResult.textContent = '请填写测试主机IP和客户端IP';
            pingResult.className = 'ping-error';
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
            pingResult.textContent = 'IP地址格式不正确，请输入有效的IPv4地址 (例如: 192.168.1.100)';
            pingResult.className = 'ping-error';
            return;
        }

        pingResult.innerHTML = '<div class="ping-testing">🔄 正在测试连通性，请稍候...</div>';

        try {
            // 首先尝试使用SSH ping API
            let result;
            try {
                result = await apiCall('/api/ssh/ping', 'POST', {
                    test_host_ip: testHostIp,
                    client_ip: clientIp
                });
            } catch (postError) {
                // 如果POST API不可用（服务器未重启），使用GET API作为后备
                debugLog('POST API不可用，使用GET API作为后备');
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
                                    🐧 打开主机终端添加路由
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
                pingResult.textContent = `测试失败: ${result.error}`;
                pingResult.className = 'ping-error';
            }
        } catch (error) {
            pingResult.textContent = `测试失败: ${error.message}`;
            pingResult.className = 'ping-error';
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
        await checkVpnStatus();
        return;
    }

    try {
        await apiCall('/api/vpn/connect', 'POST');
        updateVpnStatus(true);
        addLogEntry('VPN 已连接', 'success');
    } catch (error) {
        addLogEntry('连接 VPN 失败: ' + error.message, 'error');
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
    const previous = state.vpnConnected;

    if (connected) {
        label.textContent = '状态: 已连接';
        label.className = 'vpn-status-label connected';
        btn.textContent = '📡 检查VPN';
        state.vpnConnected = true;
    } else {
        label.textContent = '状态: 未连接';
        label.className = 'vpn-status-label disconnected';
        btn.textContent = '🔌 连接VPN';
        state.vpnConnected = false;
    }

    if (previous === true && connected === false) {
        createLocalNotification('VPN已断开', 'VPN 连接状态变为未连接', 'warning', 'vpn');
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
                progressInfo.textContent = `上传中... ${percentage.toFixed(1)}% (${transferred}/${total}) ${speed}`;
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
        xhr.open('POST', '/api/terminal/push');
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

// 导出到全局，供其他模块使用
window.formatBytes = formatBytes;

// ==================== Firmware Upload State Management ====================

/**
 * 保存固件上传状态到 sessionStorage
 */
function saveFirmwareUploadState(fileName, fileSize, startTime, progress = 0, uploadedSize = 0, totalSize = 0) {
    sessionStorage.setItem('firmwareUploadInProgress', 'true');
    sessionStorage.setItem('firmwareUploadFileName', fileName);
    sessionStorage.setItem('firmwareUploadFileSize', fileSize);
    sessionStorage.setItem('firmwareUploadStartTime', startTime.toString());
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
    sessionStorage.removeItem('firmwareUploadStartTime');
    sessionStorage.removeItem('firmwareUploadProgress');
    sessionStorage.removeItem('firmwareUploadedSize');
    sessionStorage.removeItem('firmwareTotalSize');
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

    // Update modal title
    document.getElementById('file-browser-title').textContent = title;

    // Show modal
    ModalManager.open('file-browser-modal');

    // Load initial directory - use test suite results directory
    const defaultUser = getDefaultUbuntuUser();
    let defaultPath = `/home/${defaultUser}/GMS-Suite`;

    // Get current test suite selection
    const testSuiteSelect = document.getElementById('test-suite');
    const toolsPath = testSuiteSelect?.value || '';

    if (!toolsPath) {
        addLogEntry(`未选择测试套件，使用默认路径: ${defaultPath}`, 'info');
        await loadFileDirectory(defaultPath);
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
    state.fileBrowser.selectedFile = { name, type };

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
    ModalManager.close('file-browser-modal');
    state.fileBrowser.selectedFile = null;
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
            // For directory selection in retry mode, use current path (already the directory)
            fullPath = state.fileBrowser.currentPath;
        } else {
            // For file selection, include the filename
            fullPath = `${state.fileBrowser.currentPath}/${selectedItem.name}`;
        }

        if (targetInput) {
            targetInput.value = fullPath;
            addLogEntry(`已选择测试报告: ${fullPath}`, 'info');
        }

        // Clear test module and test case inputs when retry report is selected
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
            addLogEntry(`已选择System镜像: ${fullPath}`, 'info');
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
    if (currentPath === '/' || !currentPath.includes('/')) {
        showToast('已到达根目录', 'info');
        return;  // Already at root
    }

    const parentPath = currentPath.substring(0, currentPath.lastIndexOf('/')) || '/';
    loadFileDirectory(parentPath);
}

// Navigate to root directory
function navigateToRoot() {
    const defaultUser = getDefaultUbuntuUser();
    const rootPath = `/home/${defaultUser}/GMS-Suite`;

    // Always navigate to GMS-Suite root directory
    loadFileDirectory(rootPath);
    addLogEntry(`导航到根目录: ${rootPath}`, 'info');
}

// Refresh current directory
function refreshCurrentDirectory() {
    const currentPath = state.fileBrowser.currentPath;
    if (currentPath) {
        loadFileDirectory(currentPath);
        addLogEntry(`刷新目录: ${currentPath}`, 'info');
    } else {
        showToast('没有可刷新的目录', 'warning');
    }
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
    const suitePath = document.getElementById('test-suite')?.value?.trim() || '';

    if (!suitePath) {
        showToast('请先选择测试套件', 'warning');
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
            local_server: state.config?.local_server || state.clientId || ''
        });

        debugLog('[startTest] API call successful, setting testing = true');
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

    // 禁用/启用测试相关输入框
    const testInputs = [
        'test-type',      // 测试类型
        'test-module',    // 测试模块
        'test-case',      // 测试用例
        'test-suite',     // 测试套件
        'retry-result'    // 测试报告
    ];

    testInputs.forEach(id => {
        const element = document.getElementById(id);
        if (element) {
            element.disabled = isTesting;
        }
    });

    // 禁用/启用浏览按钮
    const browseButtons = document.querySelectorAll('button[onclick*="browseRemoteFile"]');
    browseButtons.forEach(btn => {
        if (btn.getAttribute('onclick').includes('suite') || btn.getAttribute('onclick').includes('retry')) {
            btn.disabled = isTesting;
        }
    });
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

// ==================== 工具函数 ====================

/**
 * 触发文件下载
 * @param {string} url - 下载URL
 * @param {string} filename - 下载的文件名
 * @param {boolean} isBlobUrl - 是否为Blob URL（需要清理）
 */
function triggerDownload(url, filename, isBlobUrl = false) {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();

    if (isBlobUrl) {
        // Blob URL 需要延迟清理和释放
        setTimeout(() => {
            document.body.removeChild(link);
            window.URL.revokeObjectURL(url);
        }, 100);
    } else {
        document.body.removeChild(link);
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
        const saveResult = await apiCall('/api/test/logs/save', 'POST', {
            content: logContent,
            test_type: state.testType || 'unknown'
        });

        if (saveResult.success) {
            addLogEntry(`✅ 日志已保存: ${saveResult.filename}`, 'success');
            triggerDownload('/api/test/logs/get', saveResult.filename);
            showToast(`日志已保存并下载: ${saveResult.filename}`, 'success');
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
        config = await apiCall('/api/config/read', 'GET');
    } catch (error) {
        addLogEntry('获取配置失败: ' + error.message, 'error');
        return;
    }

    // Generate config form with actual values
    modalBody.innerHTML = `
        <form onsubmit="event.preventDefault(); saveConfig();" autocomplete="off">
        <div class="modal-form-row">
            <label>测试主机用户:</label>
            <input type="text" id="config-ubuntu-user" value="${config.ubuntu_user || ''}" autocomplete="username" />
        </div>
        <div class="modal-form-row">
            <label>测试主机地址:</label>
            <input type="text" id="config-ubuntu-host" value="${config.ubuntu_host || ''}" />
        </div>
        <div class="modal-form-row">
            <label>测试主机密码:</label>
            <input type="password" id="config-ubuntu-pswd" placeholder="输入测试主机SSH密码(留空保持不变)" autocomplete="current-password" />
        </div>
        <div class="modal-form-row">
            <label>设备主机地址:</label>
            <input type="text" id="config-device-host" value="${config.device_host || ''}" />
        </div>
        <div class="modal-form-row">
            <label>设备主机密码:</label>
            <input type="password" id="config-device-pswd" placeholder="输入设备主机SSH密码(留空保持不变)" autocomplete="current-password" />
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
        </form>
        <div class="modal-buttons">
            <button class="btn-xxs" onclick="closeModal()">取消</button>
            <button class="btn-xxs btn-primary" onclick="saveConfig()">保存</button>
        </div>
    `;

    ModalManager.open('config-modal');
}

function closeModal(modalId) {
    const id = modalId || 'config-modal';
    const modal = document.getElementById(id);
    if (modal) {
        // 对于动态创建的模态框（直接移除）
        if (id.startsWith('source-analysis-modal-') || id.startsWith('ai-analysis-modal-')) {
            // 先从 ModalManager 移除（清理 Esc 监听器）
            ModalManager.close(id);

            modal.style.display = 'none';
            // 延迟删除，确保动画完成
            setTimeout(() => {
                if (modal && modal.parentNode) {
                    modal.parentNode.removeChild(modal);
                }
            }, 300);
        } else {
            // 对于静态模态框（使用class控制）
            ModalManager.close(id);
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

        await apiCall('/api/config/update', 'POST', config);
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
// Log batching queue for performance - coalesces multiple log entries into a single DOM update
const _logQueue = [];
let _logFlushScheduled = false;

function addLogEntry(message, type = 'info', showTimestamp = true) {
    // Queue the log entry
    _logQueue.push({
        message,
        type,
        showTimestamp,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false })
    });

    // Cap queue size to prevent memory spikes during rapid WebSocket bursts
    if (_logQueue.length > 500) _logQueue.splice(0, _logQueue.length - 500);

    // Schedule a flush if not already scheduled
    if (!_logFlushScheduled) {
        _logFlushScheduled = true;
        requestAnimationFrame(flushLogQueue);
    }
}

function flushLogQueue() {
    _logFlushScheduled = false;

    const logOutput = document.getElementById('log-output');
    if (!logOutput) return;

    // Take all queued entries
    const entries = _logQueue.splice(0, _logQueue.length);
    if (entries.length === 0) return;

    // Use DocumentFragment for batch DOM insertion
    const fragment = document.createDocumentFragment();
    entries.forEach(({ message, type, timestamp, showTimestamp }) => {
        const logEntry = document.createElement('div');
        logEntry.className = `log-entry log-${type}`;
        logEntry.textContent = showTimestamp ? `[${timestamp}] ${message}` : message;
        fragment.appendChild(logEntry);
    });

    logOutput.appendChild(fragment);
    logOutput.scrollTop = logOutput.scrollHeight;

    // Batch trim old log entries (keep max 500)
    const maxLogs = 500;
    if (logOutput.children.length > maxLogs) {
        const removeCount = logOutput.children.length - maxLogs;
        // Remove in bulk using range
        const range = document.createRange();
        range.setStartBefore(logOutput.firstChild);
        range.setEndBefore(logOutput.children[removeCount]);
        range.deleteContents();
    }
}

// 更新进度条 - 使用固件上传的进度条
function updateProgressBar(percentage, message = '', title = '进度') {
    debugLog('[Progress] updateProgressBar called:', percentage, message, title);

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
    progressInfo.textContent = `${title} ${percentage.toFixed(1)}%`;

    // 如果有消息，显示在日志中
    if (message) {
        addLogEntry(message, 'info');
    }

    debugLog('[Progress] Updated to:', percentage);

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
// ==================== Status Polling ====================
function startStatusPolling() {
    // 轮询状态和日志
    let shownPyudevWarning = false;  // 标记是否已显示过 pyudev 警告
    let pollInterval = 2000;  // 初始轮询间隔：2秒
    const maxPollInterval = 30000;  // 最大轮询间隔：30秒
    let pollTimer = null;

    const pollStatus = async () => {
        try {
            // 检查是否有 WebSocket 连接
            const hasRealtimeConnection = state.websocket && state.websocket.readyState === WebSocket.OPEN;

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

            // 动态调整轮询间隔：如果测试正在运行，使用快速轮询；否则退避
            // Use exponential backoff when no changes detected
            if (status.running) {
                pollInterval = 2000;  // 测试运行时：2秒
            } else {
                // If nothing changed since last poll, increase backoff faster
                const stateChanged = (status.running !== state.testing) ||
                                     (status.vpn_connected !== undefined && status.vpn_connected !== state.vpnConnected);
                if (stateChanged) {
                    pollInterval = 2000;  // Reset to fast polling on state change
                } else {
                    pollInterval = Math.min(pollInterval * 1.5, maxPollInterval);  // 测试未运行时：逐渐增加到30秒
                }
            }

        } catch (error) {
            console.error('Status polling error:', error);
        }

        // 使用动态间隔重新调度
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(pollStatus, pollInterval);
    };

    // 启动轮询
    pollStatus();
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

// 统一确认对话框
function showConfirmDialog(title, message, onConfirm, onCancel) {
    return new Promise((resolve) => {
        const modal = document.getElementById('confirm-modal');
        const titleEl = document.getElementById('confirm-title');
        const messageEl = document.getElementById('confirm-message');
        const okBtn = document.getElementById('confirm-ok-btn');
        const cancelBtn = document.getElementById('confirm-cancel-btn');

        // 设置标题和消息
        titleEl.textContent = title;
        messageEl.textContent = message;

        // 显示模态框
        ModalManager.open('confirm-modal');

        // 确定按钮事件
        const handleOk = () => {
            ModalManager.close('confirm-modal');
            cleanup();
            resolve(true);
            if (onConfirm) onConfirm();
        };

        // 取消按钮事件
        const handleCancel = () => {
            ModalManager.close('confirm-modal');
            cleanup();
            resolve(false);
            if (onCancel) onCancel();
        };

        // 清理事件监听器
        const cleanup = () => {
            okBtn.removeEventListener('click', handleOk);
            cancelBtn.removeEventListener('click', handleCancel);
        };

        // 绑定事件
        okBtn.addEventListener('click', handleOk);
        cancelBtn.addEventListener('click', handleCancel);
    });
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

// ==================== Snackbar 右下角通知 ====================

// 暴露到全局作用域，确保模板中的函数可以调用
window.showSnackbar = function showSnackbar(title, message, level = 'info', duration = 5000) {
    console.log('[showSnackbar] 被调用:', { title, message, level });

    const container = document.getElementById('snackbar-container');
    console.log('[showSnackbar] container:', container);

    if (!container) {
        console.error('[Snackbar] Container not found! 无法显示通知');
        return;
    }

    const icons = {
        'success': '✅',
        'error': '❌',
        'warning': '⚠️',
        'info': '📢'
    };

    const snackbar = document.createElement('div');
    snackbar.className = `snackbar ${level}`;
    snackbar.innerHTML = `
        <span class="snackbar-icon">${icons[level] || icons.info}</span>
        <div class="snackbar-content">
            <div class="snackbar-title">${escapeHtml(title)}</div>
            <div class="snackbar-message">${escapeHtml(message || '')}</div>
        </div>
        <button class="snackbar-close" onclick="this.parentElement.remove()">×</button>
    `;

    console.log('[showSnackbar] 创建 snackbar 元素:', snackbar);
    container.appendChild(snackbar);
    console.log('[showSnackbar] 已添加到容器');

    // 自动关闭
    setTimeout(() => {
        if (snackbar.parentElement) {
            snackbar.classList.add('snackbar-exit');
            setTimeout(() => {
                if (snackbar.parentElement) {
                    snackbar.remove();
                    console.log('[showSnackbar] 已移除 snackbar');
                }
            }, 300);
        }
    }, duration);
};

// ==================== Notification Center ====================
function normalizeNotification(notification) {
    const now = new Date().toISOString();
    return {
        id: notification?.id || `local-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        timestamp: notification?.timestamp || now,
        title: notification?.title || '通知',
        message: notification?.message || '',
        level: ['success', 'warning', 'error', 'info'].includes(notification?.level) ? notification.level : 'info',
        category: notification?.category || 'system',
        read: Boolean(notification?.read),
        data: notification?.data || {}
    };
}

function formatNotificationTime(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return date.toLocaleString('zh-CN', { hour12: false });
}

function updateNotificationBadge() {
    const badge = $('notification-badge');
    if (!badge) return;
    const count = state.unreadNotifications || 0;
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.style.display = count > 0 ? 'inline-block' : 'none';
}

function renderNotificationList() {
    const list = $('notification-list');
    if (!list) return;

    if (!state.notifications.length) {
        list.innerHTML = '<div class="notification-empty">暂无通知</div>';
        updateNotificationBadge();
        return;
    }

    list.innerHTML = state.notifications.map(item => `
        <div class="notification-item ${escapeHtml(item.level)} ${item.read ? '' : 'unread'}"
             data-notification-id="${escapeHtml(item.id)}"
             onclick="markNotificationRead('${escapeHtml(item.id)}')">
            <div class="notification-level-dot"></div>
            <div>
                <div class="notification-title">${escapeHtml(item.title)}</div>
                <div class="notification-message">${escapeHtml(item.message || '')}</div>
                <div class="notification-time">${escapeHtml(formatNotificationTime(item.timestamp))}</div>
            </div>
        </div>
    `).join('');
    updateNotificationBadge();
}

function mergeNotification(notification) {
    const normalized = normalizeNotification(notification);
    const existingIndex = state.notifications.findIndex(item => item.id === normalized.id);
    if (existingIndex >= 0) {
        state.notifications[existingIndex] = normalized;
    } else {
        state.notifications.unshift(normalized);
        state.notifications = state.notifications.slice(0, 200);
    }
    state.unreadNotifications = state.notifications.filter(item => !item.read).length;
    renderNotificationList();
    return normalized;
}

function shouldShowBrowserNotification() {
    return state.browserNotificationsEnabled &&
        'Notification' in window &&
        Notification.permission === 'granted' &&
        document.visibilityState !== 'visible';
}

function showBrowserNotification(notification) {
    if (!shouldShowBrowserNotification()) return;
    try {
        const browserNotification = new Notification(notification.title, {
            body: notification.message || '',
            tag: notification.id,
            silent: false
        });
        browserNotification.onclick = () => {
            window.focus();
            closeNotificationPanel();
            toggleNotificationPanel();
        };
    } catch (error) {
        debugLog('[Notification] Browser notification failed:', error);
    }
}

function handleRealtimeNotification(notification, options = {}) {
    if (!notification) return;
    const item = mergeNotification(notification);
    if (options.toast !== false) {
        showToast(`${item.title}${item.message ? ': ' + item.message : ''}`, item.level);
    }
    if (options.browser !== false) {
        showBrowserNotification(item);
    }
}

async function loadNotifications() {
    try {
        const result = await apiCall('/api/notifications?limit=100', 'GET');
        const payload = result.data || {};
        state.notifications = (payload.records || []).map(normalizeNotification);
        state.unreadNotifications = payload.unread_count ?? state.notifications.filter(item => !item.read).length;
        renderNotificationList();
    } catch (error) {
        debugLog('[Notification] Load failed:', error);
    }
}

function toggleNotificationPanel() {
    const panel = $('notification-panel');
    if (!panel) return;
    const isShowing = panel.classList.contains('show');
    panel.classList.toggle('show');
    if (!isShowing) {
        loadNotifications();
        // 添加 Esc 键关闭监听
        const escHandler = (e) => {
            if (e.key === 'Escape') {
                closeNotificationPanel();
                document.removeEventListener('keydown', escHandler);
            }
        };
        document.addEventListener('keydown', escHandler);
    }
}

function closeNotificationPanel() {
    const panel = $('notification-panel');
    if (panel) panel.classList.remove('show');
}

async function requestBrowserNotificationPermission() {
    if (!('Notification' in window)) {
        showToast('当前浏览器不支持系统通知', 'warning');
        return;
    }
    if (!window.isSecureContext && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
        showToast('浏览器通知需要 HTTPS 或 localhost', 'warning');
        return;
    }

    const permission = Notification.permission === 'default'
        ? await Notification.requestPermission()
        : Notification.permission;

    if (permission === 'granted') {
        state.browserNotificationsEnabled = true;
        localStorage.setItem('gms_browser_notifications', 'true');
        showToast('浏览器通知已开启', 'success');
    } else {
        state.browserNotificationsEnabled = false;
        localStorage.setItem('gms_browser_notifications', 'false');
        showToast('浏览器通知未授权', 'warning');
    }
}

async function markNotificationRead(id) {
    const item = state.notifications.find(notification => notification.id === id);
    if (item && !item.read) {
        item.read = true;
        state.unreadNotifications = Math.max(0, state.unreadNotifications - 1);
        renderNotificationList();
    }
    try {
        await apiCall('/api/notifications/mark-read', 'POST', { ids: [id] });
    } catch (error) {
        debugLog('[Notification] Mark read failed:', error);
    }
}

async function markAllNotificationsRead() {
    state.notifications.forEach(item => { item.read = true; });
    state.unreadNotifications = 0;
    renderNotificationList();
    try {
        await apiCall('/api/notifications/mark-read', 'POST', {});
    } catch (error) {
        debugLog('[Notification] Mark all read failed:', error);
    }
}

async function clearNotifications() {
    state.notifications = [];
    state.unreadNotifications = 0;
    renderNotificationList();
    try {
        await apiCall('/api/notifications/clear', 'POST', {});
    } catch (error) {
        debugLog('[Notification] Clear failed:', error);
    }
}

async function createLocalNotification(title, message = '', level = 'info', category = 'system', data = {}) {
    try {
        const result = await apiCall('/api/notifications', 'POST', { title, message, level, category, data });
        const notification = result.data?.notification;
        handleRealtimeNotification(notification || { title, message, level, category, data });
    } catch (error) {
        handleRealtimeNotification({ title, message, level, category, data });
    }
}

// Close modal when clicking outside - optimized with mapping to avoid repeated DOM lookups
const _modalCloseHandlers = {
    'config-modal': closeModal,
    'firmware-modal': closeFirmwareModal,
    'file-browser-modal': closeFileBrowserModal,
    'gsi-modal': closeGsiModal,
    'sn-modal': closeSnModal
};

document.addEventListener('click', function(event) {
    const target = event.target;
    if (target.classList && target.classList.contains('modal') && _modalCloseHandlers[target.id]) {
        _modalCloseHandlers[target.id]();
    }
});

// ==================== Test Reports ====================
let reportsRefreshInterval = null;
let currentUserFilter = false;  // 当前是否只显示本用户报告

// Cleanup reports interval when leaving page (memory leak prevention)
function cleanupReportsPolling() {
    if (reportsRefreshInterval) {
        clearInterval(reportsRefreshInterval);
        reportsRefreshInterval = null;
    }
}

async function loadTestReports(userOnly = false) {
    try {
        const url = userOnly ? '/api/reports/list?user_only=true' : '/api/reports/list';
        const resp = await fetch(url);
        const data = await resp.json();

        if (data.reports) {
            displayTestReports(data.reports);
        }

        // 启动自动刷新（每15秒）带变更检测
        if (!reportsRefreshInterval) {
            let lastReportsHash = null;

            reportsRefreshInterval = setInterval(async () => {
                if (currentPage === 'reports') {
                    try {
                        const url = currentUserFilter ? '/api/reports/list?user_only=true' : '/api/reports/list';
                        const response = await fetch(url);
                        const data = await response.json();

                        // 计算报告列表的哈希值以检测变更
                        const reportsHash = JSON.stringify(data.reports);

                        // 只有在报告列表发生变化时才更新DOM
                        if (reportsHash !== lastReportsHash) {
                            lastReportsHash = reportsHash;
                            displayTestReports(data.reports);
                        }
                    } catch (error) {
                        console.error('[Reports] Error refreshing reports:', error);
                    }
                }
            }, REPORTS_REFRESH_INTERVAL);
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
        // 调整容器高度
        const container = document.querySelector('#page-reports > div:last-child');
        if (container) {
            container.style.height = 'auto';
            container.style.minHeight = '100px';
        }

        tbody.innerHTML = `
            <tr>
                <td colspan="8" style="padding: 60px 40px; text-align: center; color: var(--text-secondary);">
                    暂无测试报告
                </td>
            </tr>
        `;
        return;
    }

    // 恢复容器高度
    const container = document.querySelector('#page-reports > div:last-child');
    if (container) {
        container.style.height = 'calc(100vh - 85px)';
        container.style.minHeight = '';
    }

    // 使用 DocumentFragment 提高渲染性能
    const fragment = document.createDocumentFragment();

    // 测试类型颜色映射（定义在循环外，避免重复创建）
    const typeColors = {
        'CTS': '#3B82F6',
        'GTS': '#10B981',
        'STS': '#F59E0B',
        'VTS': '#8B5CF6',
        'XTS': '#EC4899',
    };

    reports.forEach(report => {
        const testType = report.test_type || '-';
        const displayClient = report.client_id || report.user || '-';
        const passCount = report.pass !== undefined ? report.pass : '-';
        const failCount = report.fail !== undefined ? report.fail : '-';
        const totalCount = report.total !== undefined ? report.total : '-';
        const passRate = report.total > 0 ? ((report.pass / report.total) * 100).toFixed(1) + '%' : '-';

        const passRateStyle = report.total > 0 ? (report.pass / report.total >= 0.9 ? 'color: var(--success-color);' : 'color: var(--warning-color);') : '';

        const typeColor = typeColors[testType] || 'var(--text-secondary)';

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-color)';
        tr.dataset.timestamp = report.timestamp;
        tr.dataset.testType = report.test_type || '';
        tr.dataset.suitePath = report.suite_path || '';

        tr.innerHTML = `
            <td style="padding: 12px; text-align: center; font-family: monospace; font-size: 11px;">${displayClient}</td>
            <td style="padding: 12px; text-align: center; font-weight: 700; font-size: 12px; color: ${typeColor};">${testType}</td>
            <td style="padding: 12px; text-align: center; font-family: monospace; font-size: 11px;">${report.timestamp}</td>
            <td style="padding: 12px; text-align: center; color: var(--success-color); font-weight: 600; font-size: 12px;">${passCount}</td>
            <td style="padding: 12px; text-align: center; color: var(--danger-color); font-weight: 600; font-size: 12px;">${failCount}</td>
            <td style="padding: 12px; text-align: center; font-weight: 600; font-size: 12px;">${totalCount}</td>
            <td style="padding: 12px; text-align: center; font-weight: 600; font-size: 12px; ${passRateStyle}">${passRate}</td>
            <td style="padding: 12px; text-align: center;">
                <button class="btn-xxs" data-action="analyze" style="margin: 2px;">📈 分析报告</button>
                <button class="btn-xxs" data-action="retry" style="background: var(--primary-color); margin: 2px;">🔄 retry报告</button>
                <button class="btn-xxs" data-action="download" style="background: var(--success-color); margin: 2px;">⬇️ 下载报告</button>
                <button class="btn-xxs" data-action="delete" style="background: var(--danger-color); margin: 2px;">🗑️ 删除报告</button>
            </td>
        `;

        fragment.appendChild(tr);
    });

    tbody.innerHTML = '';
    tbody.appendChild(fragment);

    // 使用事件委托处理按钮点击（提高性能）
    tbody.removeEventListener('click', handleReportAction);
    tbody.addEventListener('click', handleReportAction);
}

// 事件委托处理函数
function handleReportAction(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;

    const action = button.dataset.action;
    const tr = button.closest('tr');
    if (!tr) return;

    const timestamp = tr.dataset.timestamp;
    const testType = tr.dataset.testType;
    const suitePath = tr.dataset.suitePath;

    event.stopPropagation();

    switch (action) {
        case 'analyze':
            analyzeReport(timestamp);
            break;
        case 'retry':
            retryReportWithSuite(timestamp, testType, suitePath);
            break;
        case 'download':
            downloadReport(timestamp);
            break;
        case 'delete':
            deleteReport(timestamp);
            break;
    }
}

async function deleteReport(timestamp) {
    const confirmed = await showConfirmDialog(
        '删除报告',
        `确定要删除报告 ${timestamp} 吗？此操作不可恢复。`
    );

    if (!confirmed) return;

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


async function retryReport(timestamp, testType) {
    try {
        // 先切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            debugLog(`[Retry] 开始填充数据, timestamp=${timestamp}, testType=${testType}`);

            // 填入测试报告名称（字段ID是 retry-result）
            const reportNameInput = document.getElementById('retry-result');
            if (reportNameInput) {
                reportNameInput.value = timestamp;
                debugLog(`[Retry] 已填入报告名称: ${timestamp}`);
            } else {
                console.error('[Retry] 未找到 retry-result 元素');
            }

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect) {
                if (testType) {
                    testTypeSelect.value = testType;
                    debugLog(`[Retry] 已设置测试类型: ${testType}, 当前值: ${testTypeSelect.value}`);
                } else {
                    console.warn('[Retry] testType 为空');
                }
            } else {
                console.error('[Retry] 未找到 test-type 元素');
            }

            // 根据测试类型填入测试套件路径
            const suitePathInput = document.getElementById('test-suite');
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
                    debugLog(`[Retry] 已设置测试套件路径: ${suitePaths[testType]}, 当前值: ${suitePathInput.value}`);
                } else {
                    console.warn(`[Retry] testType=${testType} 没有对应的套件路径`);
                }
            } else {
                console.error('[Retry] 未找到 test-suite 元素');
            }

            // 打印所有相关元素的值以便调试
            debugLog('[Retry] 当前字段值:', {
                reportName: document.getElementById('retry-result')?.value,
                testType: document.getElementById('test-type')?.value,
                suitePath: document.getElementById('test-suite')?.value
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
            debugLog(`[Retry] 开始填充数据, timestamp=${timestamp}, testType=${testType}, suitePath=${suitePath}`);

            // 填入测试报告名称（字段ID是 retry-result）
            const reportNameInput = document.getElementById('retry-result');
            if (reportNameInput) {
                reportNameInput.value = timestamp;
                debugLog(`[Retry] 已填入报告名称: ${timestamp}`);
            } else {
                console.error('[Retry] 未找到 retry-result 元素');
            }

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect) {
                if (testType) {
                    testTypeSelect.value = testType;
                    debugLog(`[Retry] 已设置测试类型: ${testType}, 当前值: ${testTypeSelect.value}`);
                } else {
                    console.warn('[Retry] testType 为空');
                }
            } else {
                console.error('[Retry] 未找到 test-type 元素');
            }

            // 填入测试套件路径（优先使用原始路径，否则使用默认路径）
            const suitePathInput = document.getElementById('test-suite');
            if (suitePathInput) {
                if (suitePath && suitePath !== 'null' && suitePath !== '') {
                    // 使用报告中的原始测试套件路径
                    suitePathInput.value = suitePath;
                    debugLog(`[Retry] 已设置测试套件路径(原始): ${suitePath}, 当前值: ${suitePathInput.value}`);
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
                        debugLog(`[Retry] 已设置测试套件路径(默认): ${suitePaths[testType]}, 当前值: ${suitePathInput.value}`);
                    } else {
                        console.warn(`[Retry] testType=${testType} 没有对应的套件路径`);
                    }
                }
            } else {
                console.error('[Retry] 未找到 test-suite 元素');
            }

            // 打印所有相关元素的值以便调试
            debugLog('[Retry] 当前字段值:', {
                reportName: document.getElementById('retry-result')?.value,
                testType: document.getElementById('test-type')?.value,
                suitePath: document.getElementById('test-suite')?.value
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
        debugLog('[downloadReport] Starting download for timestamp:', timestamp);
        showToast('正在下载测试报告文件夹...', 'info');

        // 获取文件列表
        const listUrl = `/api/reports/download?report_timestamp=${timestamp}`;
        debugLog('[downloadReport] Fetching file list from:', listUrl);
        const listResponse = await fetch(listUrl);

        if (!listResponse.ok) {
            let errorMsg = `HTTP ${listResponse.status}`;
            try {
                const errorData = await listResponse.json();
                errorMsg = errorData.error || errorMsg;
            } catch (e) {
                // 如果无法解析 JSON，使用默认错误消息
            }
            console.error('Download failed:', listResponse.status, errorMsg);
            showToast('下载失败：' + errorMsg, 'error');
            return;
        }

        const listData = await listResponse.json();
        debugLog('[downloadReport] File list data:', listData);

        if (!listData.success || !listData.files || listData.files.length === 0) {
            showToast('下载失败：' + (listData.error || '没有找到文件'), 'error');
            return;
        }

        debugLog('[downloadReport] Found', listData.files.length, 'files');

        // 检查浏览器是否支持文件系统访问 API（需要 HTTPS 或 localhost 环境）
        if ('showDirectoryPicker' in window) {
            debugLog('[downloadReport] Using File System Access API');
            await downloadReportWithFileSystemAPI(timestamp, listData.files);
        } else {
            debugLog('[downloadReport] File System Access API not supported, falling back to ZIP');
            // 回退到 ZIP 下载
            await downloadReportAsZip(timestamp);
        }
    } catch (error) {
        console.error('Download report error:', error);
        showToast('下载失败：' + error.message, 'error');
    }
}

async function downloadReportWithFileSystemAPI(timestamp, files) {
    try {
        // 让用户选择保存目录
        const dirHandle = await window.showDirectoryPicker({
            startIn: 'downloads',
            suggestedName: timestamp
        });

        let successCount = 0;
        let failCount = 0;

        // 下载每个文件
        for (const file of files) {
            try {
                // 创建目录结构
                const pathParts = file.relative_path.split('/');
                let currentHandle = dirHandle;

                // 创建子目录（除了最后的部分，那是文件名）
                for (let i = 0; i < pathParts.length - 1; i++) {
                    const part = pathParts[i];
                    currentHandle = await currentHandle.getDirectoryHandle(part, { create: true });
                }

                // 获取文件内容
                const fileResponse = await fetch(`/api/reports/download?path=${encodeURIComponent(file.path)}`);
                if (!fileResponse.ok) {
                    console.error(`Failed to download ${file.relative_path}`);
                    failCount++;
                    continue;
                }

                const fileData = await fileResponse.json();
                if (!fileData.success) {
                    console.error(`Failed to download ${file.relative_path}: ${fileData.error}`);
                    failCount++;
                    continue;
                }

                // 解码base64内容
                const binaryString = atob(fileData.content);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }

                // 创建文件
                const fileName = pathParts[pathParts.length - 1];
                const fileHandle = await currentHandle.getFileHandle(fileName, { create: true });
                const writable = await fileHandle.createWritable();
                await writable.write(bytes);
                await writable.close();

                successCount++;
                debugLog(`Downloaded: ${file.relative_path}`);
            } catch (error) {
                console.error(`Error downloading ${file.relative_path}:`, error);
                failCount++;
            }
        }

        showToast(`报告下载成功：${successCount}个文件成功，${failCount}个失败`, successCount > 0 ? 'success' : 'error');
    } catch (error) {
        console.error('File System Access API error:', error);
        // 如果用户取消或 API 失败，回退到 ZIP 下载
        showToast('文件夹下载失败，正在切换到 ZIP 下载...', 'info');
        await downloadReportAsZip(timestamp);
    }
}

// 回退方案：下载为 ZIP
async function downloadReportAsZip(timestamp) {
    try {
        const response = await fetch(`/api/reports/download?report_timestamp=${timestamp}&download=true`);

        if (!response.ok) {
            let errorMsg = `HTTP ${response.status}`;
            try {
                const errorData = await response.json();
                errorMsg = errorData.error || errorMsg;
            } catch (e) {
                // 如果无法解析 JSON，使用默认错误消息
            }
            console.error('Download failed:', response.status, errorMsg);
            showToast('下载失败：' + errorMsg, 'error');
            return;
        }

        // 检查 Content-Type
        const contentType = response.headers.get('Content-Type');
        debugLog('Response Content-Type:', contentType);

        if (contentType && contentType.includes('application/json')) {
            // 如果返回的是 JSON 而不是文件，说明有错误
            const errorData = await response.json();
            console.error('Server returned error:', errorData);
            showToast('下载失败：' + (errorData.error || '服务器错误'), 'error');
            return;
        }

        // 获取文件名
        const contentDisposition = response.headers.get('Content-Disposition');
        let filename = `${timestamp}.zip`;

        if (contentDisposition) {
            const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
            if (filenameMatch && filenameMatch[1] && typeof filenameMatch[1] === 'string') {
                filename = filenameMatch[1].replace(/['"]/g, '');
            }
        }
        debugLog('Downloading file as:', filename);

        // 下载文件
        const blob = await response.blob();
        debugLog('Blob size:', blob.size, 'bytes');

        if (blob.size === 0) {
            showToast('下载失败：文件为空', 'error');
            return;
        }

        const url = window.URL.createObjectURL(blob);
        triggerDownload(url, filename, true);

        showToast('报告 ZIP 下载成功', 'success');
    } catch (error) {
        console.error('Download report as ZIP error:', error);
        showToast('ZIP 下载失败：' + error.message, 'error');
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

            const formData = createFormData(AnalysisMode.SAVED, { report_timestamp: timestamp });
            const resp = await fetch('/api/reports/analyze', {
                method: 'POST',
                body: formData
            });
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


// ==================== 安装指南弹窗 ====================

function showInstallGuide(title, guide) {
    ModalManager.open('install-guide-modal');
}

function closeInstallGuide() {
    const modal = document.getElementById('install-guide-modal');
    if (modal) {
        // 隐藏进度条
        const progressDiv = document.getElementById('install-progress');
        if (progressDiv) {
            progressDiv.style.display = 'none';
        }
    }
    ModalManager.close('install-guide-modal');
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

        // 调用后端安装 API
        const result = await apiCall('/api/usbip/install', 'POST', {});

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
            statusText.textContent = '❌ 安装失败: ' + (result.error || '未知错误');
            statusText.style.color = 'var(--danger-color, #dc3545)';

            if (result.install_guide) {
                showInstallGuide('usbipd 安装指南', result.install_guide);
            }
            addLogEntry('usbipd 自动安装失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (error) {
        // 异常处理
        progressBar.style.width = '100%';
        progressBar.style.background = 'var(--danger-color, #dc3545)';
        statusText.textContent = '❌ 安装失败: ' + error.message;
        statusText.style.color = 'var(--danger-color, #dc3545)';

        if (error.installGuide) {
            showInstallGuide('usbipd 安装指南', error.installGuide);
        }
        addLogEntry('usbipd 自动安装失败: ' + error.message, 'error');
    }
}

// ==================== SSHD 安装指南弹窗 ====================
function showSshdInstallGuide(guide) {
    if (!guide) {
        addLogEntry('SSHD 安装指南为空，未打开弹框', 'warning');
        return;
    }
    const modal = document.getElementById('sshd-install-guide-modal');
    if (modal) {
        // 设置指南内容
        const guideContent = document.getElementById('sshd-guide-content');
        if (guideContent) {
            guideContent.textContent = guide;
        }
        ModalManager.open('sshd-install-guide-modal');
    }
}

function closeSshdInstallGuide() {
    const modal = document.getElementById('sshd-install-guide-modal');
    if (modal) {
        modal.classList.remove('show');
    }
    ModalManager.close('sshd-install-guide-modal');
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

    // 注册到 ModalManager
    ModalManager.registerDynamic(modal);

    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeReportSourceModal();
        }
    });
}

function closeReportSourceModal() {
    ModalManager.unregisterDynamic('report-source-modal');
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

        // 检查是否有 URL（从网页拖拽，如 Redmine 附件）
        const url = e.dataTransfer.getData('URL') || e.dataTransfer.getData('text/uri-list');
        if (url) {
            debugLog('[Report Analysis] Detected URL drop:', url);
            await handleRedmineAttachment(url);
            return;
        }

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
                                    value: (entry.fullPath || '').replace(/^\//, ''),
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

// 用于取消正在进行的请求
let currentRedmineRequest = null;

async function handleRedmineAttachment(url) {
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (!progress || !progressFill) return;

    // 取消之前的请求
    if (currentRedmineRequest) {
        currentRedmineRequest.abort();
        currentRedmineRequest = null;
    }

    // 显示进度
    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '10%';

    try {
        // 首先获取 Redmine 配置（带缓存，减少API调用）
        let redmineDomain;

        try {
            const redmineConfig = await getRedmineConfig();
            redmineDomain = redmineConfig.domain;
        } catch (configError) {
            console.error('[Redmine] 配置获取失败:', configError);
            showToast('❌ Redmine 配置错误，请联系管理员', 'error');
            return; // 终止处理
        }

        // 检测是否为 Redmine URL
        if (url.includes(redmineDomain)) {
            const issueMatch = url.match(/\/issues\/(\d+)/);
            if (issueMatch) {
                // 是问题页面，尝试获取第一个附件
                showToast('📋 检测到 Redmine 问题页面，正在提取附件...', 'info');
                progressFill.style.width = '15%';

                try {
                    // 调用后端 API 提取附件
                    const extractResponse = await fetch('/api/reports/extract-redmine-attachment', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ issue_url: url })
                    });

                    const extractResult = await extractResponse.json();

                    if (extractResult.success && extractResult.attachment_url) {
                        showToast(`📎 找到附件: ${extractResult.filename || '未知'}`, 'info');
                        // 不替换URL，保持原始问题页面URL用于报告命名
                        debugLog('[Report Analysis] Found attachment:', extractResult.filename);
                    } else {
                        throw new Error(extractResult.error || '无法提取附件');
                    }
                } catch (extractError) {
                    showToast(`❌ ${extractError.message}`, 'error');
                    setTimeout(() => {
                        if (progress) progress.style.opacity = '0';
                        if (content) content.style.opacity = '1';
                    }, 2000);
                    return;
                }
            }

            showToast('🔐 检测到 Redmine URL，使用服务器端处理...', 'info');
            progressFill.style.width = '20%';

            // 创建 AbortController 用于取消请求
            const controller = new AbortController();
            currentRedmineRequest = controller;

            // 调用后端 API（使用服务器端存储的凭证）
            const response = await fetch('/api/reports/analyze-url', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    url: url,
                    use_redmine_auth: true  // 使用存储的 Redmine 凭证
                }),
                signal: controller.signal
            });

            progressFill.style.width = '70%';

            const result = await response.json();

            progressFill.style.width = '100%';

            if (result.success) {
                currentRedmineRequest = null;  // 重置请求控制器
                setTimeout(() => {
                    if (progress) progress.style.opacity = '0';
                    if (content) content.style.opacity = '1';
                    displayReportAnalysis(result.data);
                    showToast(`✅ 成功分析: ${result.filename || '附件'}`, 'success');
                }, 300);
            } else {
                currentRedmineRequest = null;  // 重置请求控制器
                // 如果需要凭证，显示凭证输入框
                if (result.requires_auth) {
                    showRedmineAuthDialog(url, uploadZone, content, progress, progressFill);
                } else {
                    showToast('❌ 分析失败: ' + (result.error || '未知错误'), 'error');
                    setTimeout(() => {
                        if (progress) progress.style.opacity = '0';
                        if (content) content.style.opacity = '1';
                    }, 2000);
                }
            }
            return;
        }

        // 非 Redmine URL，使用服务器端下载
        showToast('正在从 URL 下载附件...', 'info');

        progressFill.style.width = '30%';

        // 创建 AbortController 用于取消请求
        const controller = new AbortController();
        currentRedmineRequest = controller;

        const response = await fetch('/api/reports/analyze-url', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ url: url }),
            signal: controller.signal
        });

        progressFill.style.width = '80%';

        const result = await response.json();

        progressFill.style.width = '100%';

        if (result.success) {
            currentRedmineRequest = null;  // 重置请求控制器
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
                showToast(`✅ 成功分析: ${result.filename || '附件'}`, 'success');
            }, 300);
        } else {
            currentRedmineRequest = null;  // 重置请求控制器
            showToast('❌ 分析失败: ' + (result.error || '未知错误'), 'error');
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 2000);
        }
    } catch (error) {
        currentRedmineRequest = null;  // 重置请求控制器
        if (error.name === 'AbortError') {
            debugLog('请求被取消');
            return;
        }
        console.error('URL attachment analysis error:', error);
        showToast('❌ 分析失败: ' + error.message, 'error');
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function showRedmineAuthDialog(url, uploadZone, content, progress, progressFill) {
    // 显示 Redmine 凭证输入对话框
    const modal = document.createElement('div');
    modal.id = 'redmine-auth-modal';
    modal.className = 'modal show';
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 400px;">
            <div class="modal-header">
                <span class="modal-title">🔐 Redmine 认证</span>
                <span class="modal-close" onclick="ModalManager.unregisterDynamic('redmine-auth-modal'); resetReportUploadProgress();">&times;</span>
            </div>
            <div class="modal-body">
                <p style="margin-bottom: 15px;">请输入 Redmine 账号密码以自动下载附件：</p>
                <form onsubmit="event.preventDefault(); submitRedmineAuth('${url}');" autocomplete="off">
                <div class="modal-form-row">
                    <label>用户名</label>
                    <input type="text" id="redmine-username" placeholder="输入 Redmine 用户名" autocomplete="username">
                </div>
                <div class="modal-form-row">
                    <label>密码</label>
                    <input type="password" id="redmine-password" placeholder="输入 Redmine 密码" autocomplete="current-password"
                           onkeypress="if(event.key === 'Enter') submitRedmineAuth('${url}')">
                </div>
                </form>
                <div class="modal-buttons">
                    <button class="btn-xs" onclick="ModalManager.unregisterDynamic('redmine-auth-modal'); resetReportUploadProgress();">取消</button>
                    <button class="btn-xs btn-primary" onclick="submitRedmineAuth('${url}')">确定</button>
                </div>
                <p style="font-size: 11px; color: var(--text-secondary); margin-top: 15px; text-align: center;">
                    💾 凭证将被加密存储，下次无需重新输入
                </p>
            </div>
        </div>
    `;
    ModalManager.registerDynamic(modal);

    // 聚焦到用户名输入框
    setTimeout(() => {
        const usernameInput = document.getElementById('redmine-username');
        if (usernameInput) usernameInput.focus();
    }, 100);
}

function resetReportUploadProgress() {
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (progress) progress.style.opacity = '0';
    if (progressFill) progressFill.style.width = '0%';
    if (content) content.style.opacity = '1';
}

async function submitRedmineAuth(url) {
    const username = document.getElementById('redmine-username')?.value;
    const password = document.getElementById('redmine-password')?.value;

    if (!username || !password) {
        showToast('请输入用户名和密码', 'warning');
        return;
    }

    // 关闭对话框
    ModalManager.unregisterDynamic('redmine-auth-modal');

    // 显示进度
    const uploadZone = $('report-upload-zone');
    const content = uploadZone?.querySelector('.report-upload-content');
    const progress = $('report-upload-progress');
    const progressFill = $('report-progress-fill');

    if (content) content.style.opacity = '0.5';
    progress.style.opacity = '1';
    progressFill.style.width = '30%';

    try {
        showToast('⬇️ 正在从 Redmine 下载附件...', 'info');

        const response = await fetch('/api/reports/analyze-url', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                url: url,
                redmine_username: username,
                redmine_password: password
            })
        });

        progressFill.style.width = '80%';

        const result = await response.json();

        progressFill.style.width = '100%';

        if (result.success) {
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
                displayReportAnalysis(result.data);
                showToast(`✅ 成功分析: ${result.filename || '附件'}`, 'success');
            }, 300);
        } else {
            showToast('❌ 分析失败: ' + (result.error || '未知错误'), 'error');
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 2000);
        }
    } catch (error) {
        console.error('Redmine auth error:', error);
        showToast('❌ 分析失败: ' + error.message, 'error');
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
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
        const formData = createFormData(AnalysisMode.UPLOAD, { file: file });

        const result = await postFormDataWithProgress('/api/reports/analyze', formData, (percent) => {
            progressFill.style.width = `${Math.min(95, Math.max(5, percent * 0.95))}%`;
        });

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

function postFormDataWithProgress(url, formData, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        xhr.upload.addEventListener('progress', (event) => {
            if (event.lengthComputable && onProgress) {
                onProgress((event.loaded / event.total) * 100, event.loaded, event.total);
            }
        });

        xhr.addEventListener('load', () => {
            let result = null;
            try {
                result = JSON.parse(xhr.responseText || '{}');
            } catch (error) {
                reject(new Error('服务器返回无效JSON'));
                return;
            }

            if (xhr.status >= 200 && xhr.status < 300) {
                resolve(result);
                return;
            }

            reject(new Error(result.error || result.detail || `HTTP ${xhr.status}`));
        });

        xhr.addEventListener('error', () => reject(new Error('网络错误')));
        xhr.addEventListener('abort', () => reject(new Error('上传已取消')));

        xhr.open('POST', url);
        applyClientIdentityHeadersToXhr(xhr);
        xhr.send(formData);
    });
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
        formData.append('mode', 'upload');

        // 添加所有文件到 FormData，保持文件夹结构
        let fileCount = 0;
        for (let i = 0; i < files.length; i++) {
            const file = files[i];

            // 使用 webkitRelativePath 或文件名
            const filename = file.webkitRelativePath || file.name;

            formData.append('files[]', file, filename);
            fileCount++;
        }

        debugLog(`Uploading ${fileCount} files...`);
        const result = await postFormDataWithProgress('/api/reports/analyze', formData, (percent) => {
            progressFill.style.width = `${Math.min(95, Math.max(5, percent * 0.95))}%`;
        });

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
    if (DEBUG) debugLog('[displayReportAnalysis] Called with data:', data);

    // 保存当前报告名称到全局变量，供失败用例卡片使用（使用一次性状态）
    window.currentReportName = data.report_name || '';
    if (DEBUG) debugLog('[displayReportAnalysis] Current report name:', window.currentReportName);

    const resultDiv = $('report-analysis-result');
    const uploadZone = $('report-upload-zone');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    // 清空之前的内容
    if (summaryDiv) summaryDiv.innerHTML = '';
    if (detailsDiv) detailsDiv.innerHTML = '';
    if (failureList) failureList.innerHTML = '';
    if (failuresDiv) failuresDiv.style.display = 'none';

    // 移除上传空状态类（缩小到固定高度）
    if (uploadZone) uploadZone.classList.remove('upload-empty');

    if (DEBUG) debugLog('[displayReportAnalysis] Elements:', {
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

    // 生成摘要
    if (summaryDiv && data.summary) {
        const summary = data.summary;

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
                    <span class="summary-value">${data.details.suite_version}</span>
                </div>
            ` : ''}
            ${data.details && data.details.android_version ? `
                <div>
                    <span class="summary-label">Android版本：</span>
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
            <div>
                <span class="summary-label">测试报告：</span>
                <span class="summary-value">${data.report_name || data.test_result?.test_name || 'N/A'}</span>
            </div>
        `;

        summaryDiv.innerHTML = summaryHTML;
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

        const failuresHTML = data.failures.map((failure, idx) => {
            // 解析失败信息
            const reasonText = failure.reason || '无失败原因';

            // 使用后端返回的模块名，如果没有则使用默认值
            const moduleName = failure.module || '未知模块';

            // 使用后端返回的测试用例名
            const testCaseName = failure.name || '未知用例';

            // 格式化完整堆栈信息，保留换行和缩进
            // 每行开头添加 4 个空格缩进
            const formattedStackTrace = (reasonText || '无失败原因')
                .split('\n')
                .map(line => '&nbsp;&nbsp;&nbsp;&nbsp;' + line
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/ /g, '&nbsp;')
                )
                .join('<br>');

            // 从 report_name 中提取 Redmine issue ID（使用预编译的正则表达式）
            const reportName = window.currentReportName || '';
            const redmineIssueMatch = reportName.match(/^Redmine-(\d+)-/);
            const issueIdFromReport = redmineIssueMatch ? redmineIssueMatch[1] : '';

            return `
                <div style="background: var(--darker-bg); border-left: 3px solid var(--danger-color); border-radius: 4px; padding: 12px; margin-bottom: 12px; position: relative;">
                    <!-- 右上角按钮 -->
                    <div style="position: absolute; top: 8px; right: 8px; display: flex; gap: 6px;">
                        <button onclick="aiAnalyzeFailureReport('${testCaseName}', \`${reasonText.substring(0, 500).replace(/`/g, '\\`')}\`)" style="font-size: 11px; padding: 4px 10px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; font-weight: 500; box-shadow: 0 2px 4px rgba(102, 126, 234, 0.3);">🤖 报错分析</button>
                        ${issueIdFromReport ? `<button onclick="openRedmineReplyModal('${moduleName}', '${testCaseName}', '${idx}', '${issueIdFromReport}')" data-reason="${encodeURIComponent(reasonText)}" style="font-size: 11px; padding: 4px 10px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; font-weight: 500; box-shadow: 0 2px 4px rgba(245, 87, 108, 0.3);">📝 Redmine回复</button>` : ''}
                    </div>

                    <div style="margin-bottom: 8px; padding-right: 240px;">
                        <div style="font-size: 12px; color: var(--text-secondary);">测试模块: <span style="font-weight: 600; color: var(--text-primary);">${moduleName}</span></div>
                    </div>
                    <div style="margin-bottom: 8px; padding-right: 240px;">
                        <div style="font-size: 12px; color: var(--text-secondary);">测试用例: <span style="font-family: 'Courier New', monospace; color: var(--primary-color); word-break: break-all;">${testCaseName}</span></div>
                    </div>
                    <div style="padding-right: 240px;">
                        <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 4px;">报错信息: </div>
                        <div class="failure-reason" id="failure-reason-${idx}" style="font-size: 11px; font-family: 'Courier New', monospace; white-space: pre-wrap; word-wrap: break-word;">${formattedStackTrace}</div>
                        <div class="failure-reason-raw" id="failure-reason-raw-${idx}" style="display: none;">${reasonText.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>
                    </div>
                </div>
            `;
        }).join('');

        failureList.innerHTML = failuresHTML;
    } else if (failuresDiv) {
        failuresDiv.style.display = 'none';
        const actionsDiv = $('report-failure-actions');
        if (actionsDiv) actionsDiv.innerHTML = '';
    }
}

// 提取类名的辅助函数
function extractClassNames(testName, errorMessage) {
    const classNames = new Set();

    // 1. 从测试名称中提取类名（格式：com.android.test.ClassName#methodName）
    const testClassMatch = testName.match(/^([\w.]+)#/);
    if (testClassMatch) {
        classNames.add(testClassMatch[1]);
    }

    // 2. 从错误消息中提取实际的测试类（格式：ClassName#methodName）
    const errorTestMatch = errorMessage.match(/([\w.]+Test)#(\w+)/);
    if (errorTestMatch) {
        const actualTestClass = errorTestMatch[1];
        classNames.add(actualTestClass);
        debugLog(`[源码搜索] 从错误消息提取实际测试类: ${actualTestClass}`);
    }

    // 3. 从堆栈跟踪中提取实际失败的类（优先级最高）
    // 匹配格式: at com.example.ClassName.method(ClassName.kt:294)
    const stackTraceFilePattern = /at\s+[\w.$]+\.run\(([\w.]+)\.(kt|java):(\d+)\)/;
    const stackFileMatch = errorMessage.match(stackTraceFilePattern);
    if (stackFileMatch) {
        const actualFile = stackFileMatch[1]; // 如: AppFunctionManagerTest
        const extension = stackFileMatch[2];  // kt 或 java
        const lineNumber = stackFileMatch[3]; // 行号

        // 从文件名提取类名（去掉内部类后缀）
        const actualClass = actualFile.split('$')[0];
        classNames.add(actualClass);
        debugLog(`[源码搜索] 从堆栈跟踪提取实际失败位置: ${actualClass}.${extension}:${lineNumber}`);
    }

    // 4. 从堆栈跟踪中提取所有相关类（at com.example.Class.method）
    const stackTracePattern = /at\s+([\w.]+)\./g;
    let match;
    while ((match = stackTracePattern.exec(errorMessage)) !== null) {
        const className = match[1];
        // 过滤掉常见的Java/Android框架类
        if (!className.startsWith('java.') &&
            !className.startsWith('javax.') &&
            !className.startsWith('android.') &&
            !className.startsWith('androidx.') &&
            !className.startsWith('com.google.')) {
            // 去掉内部类后缀（$1$2等）
            const cleanClassName = className.split('$')[0];
            classNames.add(cleanClassName);
        }
    }

    // 5. 从错误消息中提取其他类名（Java类名模式）
    const javaClassPattern = /(?:\s|^|at\s)([a-z][\w.]*\.[A-Z][\w\$]*)/g;
    while ((match = javaClassPattern.exec(errorMessage)) !== null) {
        const className = match[1];
        if (!className.startsWith('java.') &&
            !className.startsWith('javax.') &&
            !className.startsWith('android.') &&
            !className.startsWith('androidx.') &&
            !className.startsWith('com.google.')) {
            classNames.add(className);
        }
    }

    const result = Array.from(classNames).slice(0, 5);
    debugLog(`[源码搜索] 最终提取的类名列表: ${result.join(', ')}`);
    return result;
}

// 从堆栈跟踪中提取实际的失败位置信息
function extractFailureLocation(errorMessage) {
    // 匹配格式: at com.example.ClassName.method(ClassName.kt:294)
    // 或者: at com.example.ClassName.method(Class.java:100)
    const patterns = [
        /at\s+[\w.$]+\.run\(([\w.]+)\.(kt|java):(\d+)\)/,  // .kt:294 或 .java:100
        /at\s+[\w.$]+\.(\w+)\(([\w.]+)\.(kt|java):(\d+)\)/,  // 备用模式
    ];

    for (const pattern of patterns) {
        const match = errorMessage.match(pattern);
        if (match) {
            // 根据匹配组提取信息
            let fileName, fileType, lineNumber;

            if (match.length === 4) {
                // 第一个模式: match[1]=文件名, match[2]=扩展名, match[3]=行号
                fileName = match[1];
                fileType = match[2];
                lineNumber = match[3];
            } else if (match.length === 5) {
                // 第二个模式: match[2]=文件名, match[3]=扩展名, match[4]=行号
                fileName = match[2];
                fileType = match[3];
                lineNumber = match[4];
            }

            if (fileName && fileType && lineNumber) {
                const location = {
                    file_name: fileName,
                    file_type: fileType,  // 'kt' 或 'java'
                    line_number: lineNumber,
                    extension: fileType  // 兼容字段
                };

                debugLog(`[源码搜索] 📍 从堆栈跟踪提取失败位置:`, location);
                return location;
            }
        }
    }

    debugLog(`[源码搜索] ⚠️ 堆栈跟踪中未找到文件位置信息`);
    return null;
}

// 从错误信息中提取搜索关键词（优化版）
function extractKeywordsFromError(testCaseName, errorMessage) {
    debugLog(`[源码分析] 开始提取关键词，测试用例: ${testCaseName}`);

    // 1. 优先从测试用例名中提取核心功能名
    const functionMatch = testCaseName.match(/test(?:Atom|Statsd)_([A-Z][a-zA-Z0-9_]*)/);
    if (functionMatch) {
        const functionName = functionMatch[1];
        debugLog(`[源码分析] 提取到功能名: ${functionName}`);
        return functionName;
    }

    // 2. 从测试用例名中提取类名
    const classMatch = testCaseName.match(/([A-Z][a-zA-Z0-9_]*)Test/);
    if (classMatch) {
        const className = classMatch[1];
        debugLog(`[源码分析] 提取到类名: ${className}`);
        return className;
    }

    // 3. 从堆栈信息中提取失败的类名（排除工具类）
    const stackLines = errorMessage.split('\n');
    for (const line of stackLines) {
        const stackMatch = line.match(/at\s+([\w.$]+)\(([\w.]+):(\d+)\)/);
        if (stackMatch) {
            const fullClassName = stackMatch[1];
            const fileName = stackMatch[2];

            if (!fileName.includes('TestUtil') &&
                !fileName.includes('TestRunner') &&
                !fileName.includes('Assert') &&
                !fileName.includes('Mock')) {

                const classNameParts = fullClassName.split('.');
                const mainClassName = classNameParts[classNameParts.length - 1];
                const cleanClassName = mainClassName.split('$')[0];

                if (cleanClassName.length > 3 &&
                    !cleanClassName.includes('Util') &&
                    !cleanClassName.includes('Helper')) {

                    debugLog(`[源码分析] 从堆栈提取类名: ${cleanClassName}`);
                    return cleanClassName;
                }
            }
        }
    }

    // 4. 默认返回测试用例名的前部分
    const parts = testCaseName.split(/[.#_]/);
    const fallback = parts[parts.length - 1] || testCaseName;
    debugLog(`[源码分析] 使用默认关键词: ${fallback}`);
    return fallback;
}

// 源码分析失败用例（根据堆栈信息定位）
async function analyzeFailureWithSource(testName, errorMessage) {
    const modalId = 'source-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 900px; max-height: 90vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">🔍 源码分析 - 正在定位失败位置...</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🔍</div>
                    <div style="color: var(--text-secondary); margin-bottom: 12px;">正在分析堆栈信息...</div>
                    <div style="font-size: 12px; color: var(--text-secondary);">自动提取文件位置并搜索源码</div>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    ModalManager.open(modalId);

    try {
        // 从堆栈跟踪提取失败位置
        const failureLocation = extractFailureLocation(errorMessage);

        // 提取搜索关键词
        const classNames = extractClassNames(testName, errorMessage);
        const keywords = classNames.length > 0 ? classNames[0] : extractKeywordsFromError(testName, errorMessage);

        // 构建快速访问卡片（等后端返回后再构建，使用实际路径）
        let quickLinksHtml = '';

        // 调用 AI 分析获取源码搜索结果
        const formData = createFormData(AnalysisMode.AI, {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: errorMessage,
            class_names: JSON.stringify(classNames),
            failure_location: failureLocation ? JSON.stringify(failureLocation) : '',
            include_source_search: 'true'
        });

        const response = await fetch('/api/reports/analyze', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (!response.ok) {
            const errorDetail = result.detail || result.error || '未知错误';
            showModalError(modal, `分析失败: ${errorDetail}`);
            return;
        }

        modal.querySelector('.modal-title').textContent = '🔍 源码分析结果';

        if (result.success) {
            const data = result.data;
            let content = '';

            // 如果有失败位置，构建快速访问卡片（使用后端返回的实际路径）
            if (failureLocation && data.source_search_results && data.source_search_results.length > 0) {
                // 找到匹配失败位置的搜索结果
                const exactMatch = data.source_search_results.find(item =>
                    item.path.includes(failureLocation.file_name) &&
                    item.file_type === failureLocation.file_type
                );

                if (exactMatch) {
                    let openGrokUrl = exactMatch.url || buildOpenGrokUrl(exactMatch.path, exactMatch.line);

                    if (openGrokUrl) {
                        content += `
                            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                                <div style="color: white; font-size: 14px; font-weight: 600; margin-bottom: 12px;">🎯 快速访问 - 失败位置</div>
                                <div style="background: rgba(255, 255, 255, 0.1); border-radius: 6px; padding: 12px; margin-bottom: 10px;">
                                    <div style="color: rgba(255, 255, 255, 0.8); font-size: 11px; margin-bottom: 4px;">📁 失败位置</div>
                                    <div style="color: white; font-family: 'Courier New', monospace; font-size: 13px; margin-bottom: 8px;">
                                        ${exactMatch.path.split('/').pop()} :${failureLocation.line_number}
                                    </div>
                                    <a href="${openGrokUrl}" target="_blank" style="display: inline-block; padding: 6px 12px; background: white; color: #667eea; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: 600;">
                                        🚀 直接跳转到源码 ↗
                                    </a>
                                </div>
                            </div>
                        `;
                    }
                }
            }

            // 显示源码搜索结果
            if (data.source_search_results && data.source_search_results.length > 0) {
                content += '<div style="margin-top: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid #9c27b0;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: #9c27b0;">🔍 AI 智能源码搜索</div>';
                content += '<div style="max-height: 400px; overflow-y: auto;">';

                data.source_search_results.forEach(item => {
                    const fileIcon = item.file_type === 'kt' ? '🔷' : (item.file_type === 'java' ? '☕' : '📄');
                    // 优先使用 item.url，如果没有则根据配置生成
                    let itemUrl = item.url;
                    if (!itemUrl) {
                        itemUrl = buildOpenGrokUrl(item.path, item.line);
                    }

                    const linkHtml = itemUrl ?
                        `<a href="${itemUrl}" target="_blank" style="font-size: 11px; color: #667eea; text-decoration: none; white-space: nowrap; font-weight: 600;">
                            在 OpenGrok 中查看 →
                        </a>` :
                        '<span style="font-size: 10px; color: #999;">无链接</span>';

                    content += `
                        <div style="background: white; border-radius: 4px; padding: 10px; margin-bottom: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div style="display: flex; align-items: center; gap: 6px;">
                                    <span style="font-size: 14px;">${fileIcon}</span>
                                    <span style="font-family: monospace; font-size: 12px; color: #1976d2; font-weight: 600;">
                                        ${item.type}
                                    </span>
                                </div>
                                ${linkHtml}
                            </div>
                            <div style="font-family: monospace; font-size: 11px; color: #616161; margin-bottom: 4px;">
                                📁 ${item.path}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: #424242; background: #f5f5f5; padding: 6px; border-radius: 3px;">
                                行 ${item.line || 'N/A'} ${item.project ? '· 项目：' + item.project : ''}
                            </div>
                        </div>
                    `;
                });

                content += '</div></div>';
            }

            modal.querySelector('.modal-body').innerHTML = content || '<div style="padding: 20px; text-align: center;">未找到源码搜索结果</div>';
        }
    } catch (error) {
        showModalError(modal, `分析失败: ${error.message}`);
    }
}

// AI分析失败用例（自动搜索源码）
async function aiAnalyzeFailureReport(testName, errorMessage) {
    const modalId = 'ai-analysis-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';  // 不直接添加 show 类
    modal.style.cssText = 'z-index: 10000;';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 800px; max-height: 85vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">🤖 正在分析报错并搜索源码...</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; margin-bottom: 20px;">🤖</div>
                    <div style="color: var(--text-secondary); margin-bottom: 12px;">正在分析失败原因，请稍候...</div>
                    <div style="font-size: 12px; color: var(--text-secondary);">自动提取类名并搜索相关源码</div>
                </div>
            </div>
        </div>
    `;

    // 添加到 DOM
    document.body.appendChild(modal);

    // 使用 ModalManager 打开（这样 Esc 键才会生效）
    ModalManager.open(modalId);

    try {
        // 自动提取类名
        const classNames = extractClassNames(testName, errorMessage);

        // 从堆栈跟踪提取失败位置
        const failureLocation = extractFailureLocation(errorMessage);

        // 更新模态框显示正在搜索源码
        // 将类名列表格式化为多行显示
        const classNamesList = classNames.map((name, index) => {
            const prefix = index === 0 ? '' : '├── ';
            return `${prefix}${name}`;
        }).join('<br>');

        modal.querySelector('.modal-body').innerHTML = `
            <div style="text-align: center; padding: 40px;">
                <div style="font-size: 30px; margin-bottom: 20px;">🔍</div>
                <div style="color: var(--text-secondary); margin-bottom: 12px;">正在搜索相关源码...</div>
                <div style="font-size: 16px; color: var(--text-secondary); margin-bottom: 8px;">找到 ${classNames.length} 个相关类</div>
                <div style="font-size: 16px; font-family: 'Courier New', monospace; color: var(--primary-color); text-align: left; display: inline-block; max-width: 90%;">${classNamesList}</div>
                ${failureLocation ? `<div style="font-size: 16px; color: var(--success-color); margin-top: 8px;">📍 失败位置: ${failureLocation.file_name}.${failureLocation.file_type}:${failureLocation.line_number}</div>` : ''}
            </div>
        `;

        const formData = createFormData(AnalysisMode.AI, {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: errorMessage,
            class_names: JSON.stringify(classNames),
            failure_location: failureLocation ? JSON.stringify(failureLocation) : '',
            include_source_search: 'true'  // 启用源码搜索
        });

        const response = await fetch('/api/reports/analyze', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        debugLog('[AI Analysis] API响应:', result);

        // 检查HTTP状态码
        if (!response.ok) {
            // 处理HTTP错误（FastAPI的HTTPException返回 {detail: "error message"}）
            const errorDetail = result.detail || result.error || '未知错误';
            console.error('[AI Analysis] HTTP错误:', response.status, errorDetail);
            showModalError(modal, `分析失败: ${errorDetail}`);
            return;
        }

        // 更新模态框内容
        modal.querySelector('.modal-title').textContent = '🤖 报错分析结果';

        if (result.success) {
            const data = result.data;

            // 验证必需字段
            if (!data.root_cause && !data.analysis && !data.suggestions) {
                console.error('[AI Analysis] 返回数据缺少必需字段:', data);
                showModalError(modal, 'AI分析结果格式异常，缺少必需字段。请查看后端日志了解详情。');
                return;
            }

            let content = '';

            // 根本原因
            if (data.root_cause) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px; border-left: 3px solid var(--warning-color);">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--warning-color);">🎯 根本原因</div>';
                content += `<div style="font-size: 13px; line-height: 1.6;">${escapeHtml(data.root_cause)}</div>`;
                content += '</div>';
            }

            // 详细分析
            if (data.analysis) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--primary-color);">📊 详细分析</div>';
                content += `<div style="font-size: 13px; line-height: 1.6; white-space: pre-wrap;">${escapeHtml(data.analysis)}</div>`;
                content += '</div>';
            }

            // 解决建议
            if (data.suggestions && data.suggestions.length > 0) {
                content += '<div style="margin-bottom: 16px; padding: 12px; background: var(--darker-bg); border-radius: 6px;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: var(--success-color);">✅ 解决建议</div>';
                content += '<ol style="margin: 4px 0; padding-left: 20px; font-size: 13px; line-height: 1.8;">';
                data.suggestions.forEach((suggestion, index) => {
                    content += `<li style="margin-bottom: 6px;">${escapeHtml(suggestion)}</li>`;
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
                    let opengrokUrl = '';
                    if (OPENGROK_CONFIG.isValid) {
                        opengrokUrl = `${OPENGROK_CONFIG._baseUrl}/xref/${item.file}#${item.line}`;
                    }

                    content += `
                        <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 4px; padding: 8px; margin-bottom: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                                <div style="font-family: monospace; font-size: 11px; color: #1976d2; font-weight: 600;">
                                    ${item.class_name}
                                </div>
                                ${opengrokUrl ? `<a href="${opengrokUrl}" target="_blank" style="font-size: 10px; color: #9c27b0; text-decoration: none; white-space: nowrap;">
                                    查看源码 ↗
                                </a>` : '<span style="font-size: 10px; color: #999;">无链接</span>'}
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

            // OpenGrok源码搜索结果
            if (data.source_search_results && data.source_search_results.length > 0) {
                content += '<div style="margin-top: 16px; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 6px; border-left: 3px solid #9c27b0;">';
                content += '<div style="font-weight: 600; margin-bottom: 8px; color: white;">🔍 OpenGrok源码搜索</div>';
                content += '<div style="max-height: 400px; overflow-y: auto;">';

                data.source_search_results.forEach(item => {
                    // 优先使用 item.url，如果没有则根据配置生成
                    let itemUrl = item.url;
                    if (!itemUrl) {
                        itemUrl = buildOpenGrokUrl(item.path, item.line);
                    }

                    // 调试信息
                    if (!itemUrl && DEBUG) {
                        console.debug('[OpenGrok] No URL for item:', {
                            hasItemUrl: !!item.url,
                            configValid: OPENGROK_CONFIG.isValid,
                            path: item.path
                        });
                    }

                    // 使用 display_path（如果有），否则使用 path
                    const displayPath = item.display_path || item.path;
                    content += `
                        <div style="background: white; border-radius: 4px; padding: 10px; margin-bottom: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <div style="font-family: monospace; font-size: 12px; color: #1976d2; font-weight: 600;">
                                    ${item.type}
                                </div>
                                ${itemUrl ? `<a href="${itemUrl}" target="_blank" style="font-size: 11px; color: #667eea; text-decoration: none; white-space: nowrap; font-weight: 600;">
                                    在 OpenGrok 中查看 →
                                </a>` : '<span style="font-size: 10px; color: #999;">无链接</span>'}
                            </div>
                            <div style="font-family: monospace; font-size: 11px; color: #616161; margin-bottom: 4px;">
                                📁 ${displayPath}
                            </div>
                            <div style="font-family: monospace; font-size: 10px; color: #424242; background: #f5f5f5; padding: 6px; border-radius: 3px; overflow-x: auto;">
                                行 ${item.line} ${item.project ? '· 项目: ' + item.project : ''}
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
        showModalError(modal, `请求失败: ${error.message}`);
    }
}

/**
 * 使用 AI 分析测试失败
 * @param {string} testName - 测试用例名称
 * @param {string} errorMessage - 错误消息
 * @param {string} module - 测试模块
 */

async function aiAnalyzeFailure(testName, errorMessage, module = '') {
    try {
        // 显示加载提示
        showToast('🤖 报错分析...', 'info');

        // 提取类名和堆栈信息
        const classNames = extractClassNames(testName, errorMessage);
        const stackTrace = errorMessage; // errorMessage 包含完整的错误信息

        const formData = createFormData(AnalysisMode.AI, {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: stackTrace,
            module: module,
            class_names: JSON.stringify(classNames)
        });

        const response = await fetch('/api/reports/analyze', {
            method: 'POST',
            body: formData
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
                <h2 style="margin: 0; font-size: 18px; font-weight: 600;">🤖 报错分析</h2>
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


    html += `
            <div style="display: flex; gap: 10px; margin-top: 20px;">
                <button onclick="closeAIAnalysisModal('${modalId}')" class="btn-xs">关闭</button>
                <button onclick="copyAIAnalysis('${modalId}')" class="btn-xs" style="background: var(--success-color);">📋 复制分析报告</button>
            </div>
        </div>
    `;

    modal.innerHTML = html;
    document.body.appendChild(modal);

    // 注册到 ModalManager
    ModalManager.registerDynamic(modal);

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
    ModalManager.unregisterDynamic(modalId);
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
    if (text === null || text === undefined) return '';
    return String(text).replace(/[&<>"']/g, char => HTML_ENTITIES[char]);
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
window.downloadReport = downloadReport;
window.retryReportWithSuite = retryReportWithSuite;
window.analyzeReport = analyzeReport;
window.loadTestReports = loadTestReports;
window.showSshdInstallGuide = showSshdInstallGuide;
window.closeSshdInstallGuide = closeSshdInstallGuide;
window.autoInstallUsbipd = autoInstallUsbipd;
window.resetReportAnalysis = resetReportAnalysis;
window.openRedmineReplyModal = openRedmineReplyModal;
window.copyNgrokUrl = copyNgrokUrl;
window.copyDeployCommand = copyDeployCommand;
window.loadNgrokPublicUrl = loadNgrokPublicUrl;

// ==================== ngrok 公网地址 ====================

/**
 * 复制部署脚本命令
 */
function copyDeployCommand() {
    const protocol = window.location.protocol;
    const host = window.location.hostname;
    const port = window.location.port || (protocol === 'https:' ? '443' : '80');

    // 构建 curl 命令（直接执行安装）
    const deployCommand = `curl -s ${protocol}//${host}:${port}/api/system/install-sh | bash`;

    const clipboardWrite = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(deployCommand)
        : Promise.reject(new Error('Clipboard API unavailable'));

    clipboardWrite.then(() => {
        showToast('✓ 部署命令已复制', 'success');
    }).catch(() => {
        // 备用复制方案
        const textArea = document.createElement('textarea');
        textArea.value = deployCommand;
        textArea.style.position = 'fixed';
        textArea.style.left = '-9999px';
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showToast('✓ 部署命令已复制', 'success');
        } catch (e) {
            showToast('复制失败', 'error');
        }
        document.body.removeChild(textArea);
    });
}

/**
 * 加载 ngrok 公网地址
 */
function loadNgrokPublicUrl() {
    const label = document.getElementById('ngrok-url-label');

    fetch('/api/ngrok/public-url')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.public_url) {
                window.ngrokPublicUrl = data.public_url;
                if (label) {
                    label.textContent = data.public_url;
                    label.style.color = 'var(--success-color)';
                }
            } else {
                window.ngrokPublicUrl = '';
                if (label) {
                    label.textContent = '未运行 ngrok';
                    label.style.color = 'var(--text-secondary)';
                }
            }
        })
        .catch(error => {
            console.error('[ngrok] 加载失败:', error);
            window.ngrokPublicUrl = '';
            if (label) {
                label.textContent = '获取失败';
                label.style.color = 'var(--danger-color)';
            }
        });
}

/**
 * 复制 ngrok 公网地址
 */
async function copyNgrokUrl() {
    const label = document.getElementById('ngrok-url-label');

    if (window.ngrokEnsureInProgress) {
        showToast('正在准备公网地址...', 'info');
        return;
    }

    window.ngrokEnsureInProgress = true;
    let url = '';

    try {
        showToast('正在检查 ngrok...', 'info');
        const response = await fetch('/api/ngrok/ensure-public-url', { method: 'POST' });
        const data = await response.json();

        if (!data.success || !data.public_url) {
            const detail = data.detail && data.detail.stderr ? `：${data.detail.stderr.trim()}` : '';
            throw new Error((data.error || '无法获取公网地址') + detail);
        }

        url = data.public_url;
        window.ngrokPublicUrl = url;
        if (label) {
            label.textContent = url;
            label.style.color = 'var(--success-color)';
        }
        if (data.started) {
            showToast('ngrok 已启动，正在复制公网地址...', 'success');
        }
    } catch (error) {
        window.ngrokPublicUrl = '';
        if (label) {
            label.textContent = '获取失败';
            label.style.color = 'var(--danger-color)';
        }
        showToast(`获取公网地址失败：${error.message}`, 'error');
        window.ngrokEnsureInProgress = false;
        return;
    }

    const clipboardWrite = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(url)
        : Promise.reject(new Error('Clipboard API unavailable'));

    clipboardWrite.then(() => {
        showToast('✓ 公网地址已复制：' + url, 'success');
    }).catch(() => {
        // 备用复制方案
        const textArea = document.createElement('textarea');
        textArea.value = url;
        textArea.style.position = 'fixed';
        textArea.style.left = '-9999px';
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showToast('✓ 公网地址已复制：' + url, 'success');
        } catch (e) {
            showToast('复制失败', 'error');
        }
        document.body.removeChild(textArea);
    }).finally(() => {
        window.ngrokEnsureInProgress = false;
    });
}


// Redmine 回复对话框
function openRedmineReplyModal(moduleName, testCaseName, failureIndex, issueIdFromReport) {
    const modalId = 'redmine-reply-modal-' + Date.now();
    const modal = document.createElement('div');
    modal.id = modalId;
    modal.className = 'modal';
    modal.style.cssText = 'z-index: 10001;';

    // 从隐藏的原始数据元素中获取完整的错误信息（保留换行和格式）
    const failureReasonElement = document.getElementById(`failure-reason-raw-${failureIndex}`);
    const failureReason = failureReasonElement ? failureReasonElement.textContent.trim() : '';

    // 生成默认回复模板
    const defaultReply = '**测试模块**: ' + moduleName + '\n\n' +
        '**测试用例**: ' + testCaseName + '\n\n' +
        '**报错信息**:\n' +
        '<pre>\n' + failureReason + '\n</pre>';

    modal.innerHTML = `
        <div class="modal-content" style="max-width: 700px; max-height: 85vh; overflow-y: auto;">
            <div class="modal-header">
                <span class="modal-title">📝 Redmine回复</span>
                <span class="modal-close" onclick="ModalManager.close('${modalId}')">&times;</span>
            </div>
            <div class="modal-body">
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">Redmine Issue ID</label>
                    <input type="text" id="redmine-issue-id-input" value="${issueIdFromReport}" placeholder="输入 Redmine Issue ID"
                           style="width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--darker-bg); color: var(--text-primary); font-size: 14px; font-family: 'Courier New', monospace;">
                </div>
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">回复内容</label>
                    <textarea id="redmine-reply-text" rows="12" placeholder="输入回复内容..."
                              style="width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--darker-bg); color: var(--text-primary); font-size: 13px; font-family: 'Courier New', monospace; white-space: pre-wrap; resize: vertical;">${defaultReply}</textarea>
                </div>
                <div style="display: flex; gap: 10px; justify-content: flex-end;">
                    <button onclick="ModalManager.close('${modalId}')"
                            style="padding: 8px 16px; background: var(--secondary-bg); color: var(--text-primary); border: none; border-radius: 6px; cursor: pointer; font-size: 13px;">取消</button>
                    <button onclick="confirmAndSendRedmineReply('${modalId}')"
                            style="padding: 8px 16px; background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; box-shadow: 0 2px 4px rgba(245, 87, 108, 0.3);">确认并发送</button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    ModalManager.open(modalId);
}

// 确认并发送 Redmine 回复
async function confirmAndSendRedmineReply(modalId) {
    const issueId = document.getElementById('redmine-issue-id-input')?.value?.trim();
    const replyText = document.getElementById('redmine-reply-text')?.value?.trim();

    if (!issueId) {
        showToast('❌ 请输入 Redmine Issue ID', 'error');
        return;
    }

    if (!replyText) {
        showToast('❌ 回复内容不能为空', 'error');
        return;
    }

    // 立即关闭弹窗，提升响应速度
    ModalManager.close(modalId);
    showToast('📤 正在发送回复...', 'info');

    // 异步发送请求，不阻塞 UI
    fetch('/api/redmine/reply', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            issue_id: issueId,
            reply_text: replyText
        })
    })
    .then(response => response.json())
    .then(result => {
        if (result.success) {
            showToast(`✅ 回复已成功发送到 Redmine #${issueId}`, 'success');
            // 可选：打开 Redmine 页面查看
            setTimeout(() => {
                window.open(`https://redmine.rock-chips.com/issues/${issueId}`, '_blank');
            }, 800);
        } else {
            showToast('❌ 发送失败：' + (result.error || result.detail || '未知错误'), 'error');
        }
    })
    .catch(error => {
        console.error('[Redmine Reply] Error:', error);
        showToast('❌ 发送失败：' + error.message, 'error');
    });
}

function resetReportAnalysis() {
    const resultDiv = $('report-analysis-result');
    const uploadZone = $('report-upload-zone');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    // Clear all analysis results
    if (resultDiv) resultDiv.innerHTML = '';
    if (summaryDiv) summaryDiv.innerHTML = '';
    if (detailsDiv) detailsDiv.innerHTML = '';
    if (failuresDiv) failuresDiv.innerHTML = '';
    if (failureList) failureList.innerHTML = '';

    // Reset upload zone to empty state
    if (uploadZone) {
        uploadZone.classList.add('upload-empty');
        const content = uploadZone.querySelector('.report-upload-content');
        if (content) content.style.opacity = '1';
    }

    debugLog('[resetReportAnalysis] Report analysis reset complete');
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
            (api.path && api.path.toLowerCase().includes(searchTerm)) ||
            (api.description && api.description.toLowerCase().includes(searchTerm));

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
 * @param {boolean} forceRefresh - 强制刷新，绕过缓存
 */
async function loadApiDocs(forceRefresh = false) {
    debugLog('[API Docs] ===== loadApiDocs called =====');
    try {
        // 检查DOM元素是否存在
        const tbody = $('api-docs-table-body');
        if (!tbody) {
            return;
        }

        // 检查缓存（除非强制刷新）
        const now = Date.now();
        if (!forceRefresh && apiDocsCache && (now - apiDocsCacheTime) < API_DOCS_CACHE_DURATION) {
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
            const filteredApis = data.apis.filter(api => api.path !== '/');

            // 为每个API添加分类信息
            const apisWithCategory = filteredApis.map(api => ({
                ...api,
                category: getApiCategory(api.path || '')
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

    // 统计唯一的技能数量
    const uniqueSkills = new Set();
    apis.forEach(api => {
        if (api.skill && api.skill.trim()) {
            uniqueSkills.add(api.skill.trim());
        }
    });
    const skillsCount = uniqueSkills.size;

    const totalEl = $('total-apis-count');
    const getEl = $('get-apis-count');
    const postEl = $('post-apis-count');
    const filteredEl = $('filtered-apis-count');
    const skillsCountEl = $('skills-count');

    if (totalEl) totalEl.textContent = totalCount;
    if (getEl) getEl.textContent = getCount;
    if (postEl) postEl.textContent = postCount;
    if (filteredEl) filteredEl.textContent = totalCount;
    if (skillsCountEl) skillsCountEl.textContent = skillsCount;
}

// ==================== 常量定义 ====================
// API表格列宽配置 (与HTML模板保持一致: 25%, 18%, 17%, 40%)
const API_TABLE_COLUMNS = {
    INTERFACE: 25,    // 百分比 - API接口
    DESCRIPTION: 20,  // 百分比 - 接口说明
    SKILL: 20,        // 百分比 - skill使用
    USAGE: 35         // 百分比 - 使用方法
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
    // No more path patterns needed with unified API
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
const BASE_URL = window.location.origin;
const WS_BASE_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;

/**
 * Generate curl command for an API endpoint
 * Moved to module level to avoid recreating on every render
 */
function generateCurlCommand(api, details) {
    const apiPath = api.path || '';
    if (api.method === 'GET') {
        // 特殊处理stream端点：使用 -N 而不是 -s
        const isStreamEndpoint = apiPath.includes('/api/test/logs/stream');
        // 特殊处理文件下载端点：使用 -OJ
        const isDownloadEndpoint = apiPath.includes('/api/system/skills');

        let curlOptions = 'curl -s';
        if (isStreamEndpoint) {
            curlOptions = 'curl -N';
        } else if (isDownloadEndpoint) {
            curlOptions = 'curl -s -OJ';
        }

        let cmd = `${curlOptions} "${BASE_URL}${apiPath}"`;
        // Add query parameter example
        if (details.params && details.params.length > 0) {
            const queryParams = details.params.filter(p =>
                p.required && p.name !== 'force_refresh' || p.name === 'log_type' || p.name === 'report_timestamp'
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
            let multiLineCmd = `curl -sX POST "${BASE_URL}${api.path || ''}"`;

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
            let multiLineCmd = `curl -sX POST "${BASE_URL}${api.path || ''}"`;

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
        let cmd = `curl -X DELETE "${BASE_URL}${api.path || ''}"`;

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
        const wsPath = apiPath.replace('{client_id}', 'YOUR_CLIENT_ID');
        return { display: `wscat -c ${WS_BASE_URL}${wsPath}`, full: `wscat -c ${WS_BASE_URL}${wsPath}` };
    }
    return { display: `curl -s ${BASE_URL}${apiPath}`, full: `curl -s ${BASE_URL}${apiPath}` };
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
        const details = getApiDetails(api.path || '');
        const curlCmdObj = generateCurlCommand(api, details);
        const paramsHtml = generateParamsHtml(details);

        // 将curl命令存储到data属性中,避免在onclick中直接传递复杂字符串
        const escapedCurlCmd = (curlCmdObj.full || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const displayCurlCmd = curlCmdObj.display;

        htmlParts.push(`
            <tr style="border-bottom: 1px solid var(--border-color); ${index % 2 === 0 ? 'background: var(--bg-color);' : 'background: var(--light-bg);'}">
                <!-- Column 1: API Interface -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 25%;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span style="${methodClass} font-weight: 700; font-size: 13px; min-width: 90px; display: inline-block;">${api.method}</span>
                        <span style="font-family: monospace; font-size: 12px; color: var(--text-primary); word-break: break-all;">${escapeHtml(api.path || '')}</span>
                    </div>
                </td>

                <!-- Column 2: Description -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 20%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-size: 11px; color: var(--text-primary); font-weight: 600; line-height: 1.3;">
                            ${escapeHtml(details.title)}
                        </div>
                    </div>
                </td>

                <!-- Column 3: Skill Usage -->
                <td style="padding: 4px 8px; border-right: 1px solid var(--border-color); text-align: left; vertical-align: middle; width: 20%;">
                    <div style="display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-size: 11px; color: var(--primary-color); font-weight: 600; line-height: 1.3; cursor: pointer; transition: all 0.2s;"
                             onclick="copySkillCommand(this)"
                             onmouseover="this.style.color='var(--success-color)';"
                             onmouseout="this.style.color='var(--primary-color)';"
                             title="点击复制 skill 命令">
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
                                 title="点击复制 curl 命令">${escapeHtml(displayCurlCmd)}</pre>
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
${api.method} ${api.path || ''}
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
    debugLog('[Copy] Attempting to copy:', text);

    let commandToCopy = text;
    let successMessage = '✓ curl命令已复制';

    // 检查是否为WebSocket端点（不需要jq格式化）
    const isWebSocketEndpoint = text.startsWith('wscat -c');

    // 检查是否为纯文本端点（不需要jq格式化）
    const isPlainTextEndpoint = text.includes('/api/test/logs/stream') ||
                                text.includes('/api/terminal/ws') ||
                                text.includes('/api/screen/ws') ||
                                // 匹配根路径（如 "http://localhost:5001/" 或 "http://192.168.1.10:5001/"）
                                (text.match(/http:\/\/[^\/]+:\d+\/"$/) !== null);

    if (isWebSocketEndpoint) {
        // WebSocket端点，不添加jq
        commandToCopy = text;
        successMessage = '✓ WebSocket命令已复制';
    } else if (isPlainTextEndpoint) {
        // 纯文本端点，不添加jq
        commandToCopy = text;
        successMessage = '✓ curl命令已复制';
    } else {
        // 其他JSON端点，使用 jq "."
        commandToCopy = text + ' | jq "."';
        successMessage = '✓ curl命令已复制 (含jq格式化)';
    }

    copyText(commandToCopy, { successMsg: successMessage });
};

/**
 * 显示使用实例弹窗
 */
function showUsageExamples() {
    ModalManager.open('usage-examples-modal');
}


/**
 * 关闭使用实例弹窗
 */
function closeUsageExamplesModal() {
    ModalManager.close('usage-examples-modal');
}

/**
 * 下载 skills zip 文件（直接下载，不跳转）
 */
async function downloadSkillsZip() {
    try {
        const response = await fetch('/api/system/skills');
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || '下载失败');
        }
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        triggerDownload(url, 'gms-remote-test-skills.zip', true);
    } catch (e) {
        console.error('[downloadSkillsZip] Error:', e);
        alert('下载失败：' + e.message);
    }
}

/**
 * 复制文本到剪贴板（统一函数）
 * @param {string} text - 要复制的文本
 * @param {Object} options - 配置选项 { addJq: boolean, successMsg: string, element: HTMLElement }
 */
function copyText(text, options = {}) {
    const {
        addJq = false,
        successMsg = '✓ 命令已复制到剪贴板',
        element = null
    } = options;
    const textToCopy = addJq ? text + ' | jq "."' : text;

    debugLog('[Copy] Copying text:', textToCopy);

    const onSuccess = () => {
        debugLog('[Copy] Success');
        showToast(successMsg, 'success');
        if (element) {
            const originalColor = element.style.color;
            element.style.color = 'var(--success-color)';
            setTimeout(() => {
                if (element) {
                    element.style.color = originalColor || 'var(--primary-color)';
                }
            }, 500);
        }
    };

    const doFallback = () => {
        try {
            const textArea = document.createElement('textarea');
            textArea.value = textToCopy;
            textArea.style.position = 'fixed';
            textArea.style.left = '-999999px';
            document.body.appendChild(textArea);
            textArea.select();
            const successful = document.execCommand('copy');
            document.body.removeChild(textArea);
            if (successful) {
                onSuccess();
            } else {
                showToast('✗ 复制失败，请手动复制', 'error');
            }
        } catch (err) {
            console.error('[Copy] Fallback error:', err);
            showToast('✗ 复制失败：' + err.message, 'error');
        }
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(textToCopy).then(() => {
            onSuccess();
        }).catch(err => {
            console.error('[Copy] Clipboard API failed:', err);
            doFallback();
        });
    } else {
        doFallback();
    }
}

/**
 * 复制curl命令到剪贴板（自动添加jq格式化）
 */
window.copyCurlCommand = function(text) {
    copyText(text, { addJq: true, successMsg: '✓ curl命令已复制 (含jq格式化)' });
};

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
    debugLog('[CopyCommand] Copying from element:', elementId, text);

    copyText(text);
};

// 将API文档函数暴露到window对象
window.loadApiDocs = loadApiDocs;
window.filterApiDocs = filterApiDocs;
window.autoInstallSshd = autoInstallSshd;

/**
 * 复制 skill 命令到剪贴板
 */
window.copySkillCommand = function(element) {
    const text = element.textContent.trim();
    if (!text || text === '-') {
        showToast('✗ 无内容可复制', 'error');
        return;
    }
    copyText(text, {
        successMsg: '✓ 已复制：' + text,
        element: element
    });
};

/**
 * 复制文本到剪贴板（通用方法，用于 skill 命令等）
 * @param {string} text - 要复制的文本
 * @param {HTMLElement} element - 触发复制的元素
 */
window.copyToClipboard = function(text, element) {
    if (!text || text === '-') {
        showToast('✗ 无内容可复制', 'error');
        return;
    }
    copyText(text, {
        successMsg: '✓ 已复制：' + text,
        element: element
    });
};

// ==================== APK Analysis ====================

window.apkCurrentTaskId = null;
window.apkPollInterval = null;
window.apkStatusPollInFlight = false;
window.apkNotifiedTaskId = null;
window.apkOpenFiles = new Map();
window.apkActiveFilePath = null;

function stopApkPolling() {
    clearInterval(window.apkPollInterval);
    window.apkPollInterval = null;
    window.apkStatusPollInFlight = false;
}

function setApkUploadEmpty(empty) {
    const uploadZone = $('apk-upload-zone');
    if (uploadZone) {
        uploadZone.classList.toggle('upload-empty', empty);
    }
}

function initApkAnalysisPage() {
    const uploadZone = $('apk-upload-zone');
    const fileInput = $('apk-file-input');

    if (!uploadZone || !fileInput) return;

    setApkUploadEmpty(!window.apkCurrentTaskId);
    initApkSourceResizer();

    // 绑定拖拽事件
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('drag-over');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
        if (e.dataTransfer.files.length > 0) {
            handleApkFile(e.dataTransfer.files[0]);
        }
    });

    // 绑定文件选择事件
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleApkFile(e.target.files[0]);
        }
    });
}

function initApkSourceResizer() {
    const layout = $('apk-tab-source')?.querySelector('.apk-source-layout');
    const resizer = $('apk-source-resizer');
    if (!layout || !resizer || resizer.dataset.initialized === 'true') return;

    resizer.dataset.initialized = 'true';
    const savedWidth = Number(localStorage.getItem('apk_source_tree_width') || 0);
    if (savedWidth) {
        layout.style.setProperty('--apk-source-tree-width', `${Math.min(620, Math.max(180, savedWidth))}px`);
    }

    let dragging = false;
    const stopDrag = () => {
        if (!dragging) return;
        dragging = false;
        document.body.classList.remove('apk-resizing');
    };

    resizer.addEventListener('mousedown', (event) => {
        if (window.matchMedia('(max-width: 980px)').matches) return;
        event.preventDefault();
        dragging = true;
        document.body.classList.add('apk-resizing');
    });

    document.addEventListener('mousemove', (event) => {
        if (!dragging) return;
        const rect = layout.getBoundingClientRect();
        const width = Math.min(620, Math.max(180, event.clientX - rect.left));
        layout.style.setProperty('--apk-source-tree-width', `${width}px`);
        localStorage.setItem('apk_source_tree_width', String(Math.round(width)));
    });
    document.addEventListener('mouseup', stopDrag);
    document.addEventListener('mouseleave', stopDrag);
}

// APK/JAR 文件扩展名常量
const SUPPORTED_APK_EXTENSIONS = ['.apk', '.jar'];

function isSupportedApkFile(filename) {
    const nameLower = filename.toLowerCase();
    return SUPPORTED_APK_EXTENSIONS.some(ext => nameLower.endsWith(ext));
}

async function handleApkFile(file) {
    if (!isSupportedApkFile(file.name)) {
        showToast('仅支持 .apk 和 .jar 文件', 'error');
        return;
    }

    const fileSizeMB = (file.size / (1024 * 1024)).toFixed(1);
    showToast(`正在上传 ${file.name} (${fileSizeMB}MB)...`, 'info');

    const uploadProgress = $('apk-upload-progress');
    const uploadProgressFill = $('apk-progress-fill');
    if (uploadProgress) uploadProgress.style.display = 'block';
    if (uploadProgressFill) uploadProgressFill.style.width = '0%';

    try {
        const data = await window.uploadFileWithProgress(file, '/api/apk/upload', {
            useChunkUpload: true,
            chunkSize: 32 * 1024 * 1024,
            onProgress: (percent) => {
                if (uploadProgressFill) {
                    uploadProgressFill.style.width = `${Math.min(100, Math.max(1, percent))}%`;
                }
            }
        });
        if (uploadProgressFill) uploadProgressFill.style.width = '100%';

        if (data.success && data.data) {
            stopApkPolling();
            window.apkCurrentTaskId = data.data.task_id;
            window.apkNotifiedTaskId = null;
            showToast(`上传成功: ${file.name}`, 'success');
            setApkUploadEmpty(false);

            $('apk-analysis-status').style.display = 'block';
            $('apk-file-name').textContent = `${file.name} (${fileSizeMB}MB)`;
            $('apk-analysis-state').textContent = '已上传，正在启动反编译';
            $('apk-btn-download').style.display = 'none';
            $('apk-analysis-result').style.display = 'none';
            $('apk-analysis-progress-container').style.display = 'none';

            const sourceTree = $('apk-source-tree');
            if (sourceTree) {
                sourceTree.dataset.loaded = '';
                sourceTree.innerHTML = '';
            }
            const permList = $('apk-permissions-list');
            if (permList) {
                permList.dataset.loaded = '';
                permList.innerHTML = '';
            }
            const manifestInfo = $('apk-manifest-info');
            if (manifestInfo) manifestInfo.innerHTML = '';
            const rawXml = $('apk-raw-xml');
            if (rawXml) rawXml.textContent = '';
            closeApkFileViewer();
            switchApkTab('manifest');
            await startApkAnalysis();
        } else {
            showToast(`上传失败: ${data.error}`, 'error');
        }
    } catch (e) {
        showToast(`上传失败: ${e.message}`, 'error');
    } finally {
        setTimeout(() => {
            if (uploadProgress) uploadProgress.style.display = 'none';
            if (uploadProgressFill) uploadProgressFill.style.width = '0%';
        }, 500);
    }
}

async function startApkAnalysis() {
    if (!window.apkCurrentTaskId) {
        showToast('请先上传 APK 文件', 'error');
        return;
    }

    const btn = $('apk-btn-analyze');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ 分析中...';
    }
    $('apk-analysis-state').textContent = '正在反编译 APK...';
    $('apk-analysis-progress-container').style.display = 'block';
    $('apk-analysis-progress-bar').style.width = '5%';

    try {
        const data = await apiCall(`/api/apk/analyze/${window.apkCurrentTaskId}`, 'POST');

        if (data.success) {
            window.apkPollInterval = setInterval(pollApkStatus, STATUS_POLL_INTERVAL);
            await pollApkStatus();
        } else {
            showToast(`分析失败: ${data.error}`, 'error');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔬 开始分析';
            }
        }
    } catch (e) {
        showToast(`分析失败: ${e.message}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔬 开始分析';
        }
    }
}

async function pollApkStatus() {
    if (!window.apkCurrentTaskId) return;
    if (window.apkStatusPollInFlight) return;
    window.apkStatusPollInFlight = true;

    try {
        const data = await apiCall(`/api/apk/status/${window.apkCurrentTaskId}`);

        if (!data.success) {
            stopApkPolling();
            $('apk-analysis-state').textContent = `状态查询失败: ${data.error || data.message || '未知错误'}`;
            const btn = $('apk-btn-analyze');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔬 重新分析';
            }
            return;
        }

        const status = data.data;
        if (!status || typeof status !== 'object') {
            stopApkPolling();
            $('apk-analysis-state').textContent = '状态查询失败: 响应数据为空';
            const btn = $('apk-btn-analyze');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔬 重新分析';
            }
            return;
        }
        $('apk-analysis-progress-bar').style.width = status.progress + '%';
        $('apk-analysis-state').textContent =
            status.status === 'analyzing' ? `正在反编译... (${status.progress}%)` :
            status.status === 'completed' ? '反编译完成' :
            status.status === 'error' ? `错误: ${status.error}` : status.status;

        if (status.status === 'completed') {
            stopApkPolling();

            $('apk-btn-download').style.display = 'inline-block';
            $('apk-analysis-state').textContent = '反编译完成 - 可查看结果';
            $('apk-analysis-result').style.display = 'block';

            loadApkManifest();
            showToast('APK 分析完成', 'success');
            if (window.apkNotifiedTaskId !== window.apkCurrentTaskId) {
                window.apkNotifiedTaskId = window.apkCurrentTaskId;
                createLocalNotification('APK分析完成', status.filename || '反编译完成，可查看结果', 'success', 'apk', {
                    task_id: window.apkCurrentTaskId
                });
            }
        } else if (status.status === 'error') {
            stopApkPolling();

            showToast(`分析失败: ${status.error}`, 'error');
            if (window.apkNotifiedTaskId !== window.apkCurrentTaskId) {
                window.apkNotifiedTaskId = window.apkCurrentTaskId;
                createLocalNotification('APK分析失败', status.error || '反编译失败', 'error', 'apk', {
                    task_id: window.apkCurrentTaskId
                });
            }
        }
    } catch (e) {
        stopApkPolling();
        $('apk-analysis-state').textContent = `状态查询失败: ${e.message}`;
        const btn = $('apk-btn-analyze');
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔬 重新分析';
        }
    } finally {
        window.apkStatusPollInFlight = false;
    }
}

async function loadApkManifest() {
    if (!window.apkCurrentTaskId) return;

    try {
        const data = await apiCall(`/api/apk/manifest/${window.apkCurrentTaskId}`);

        if (!data.success) {
            $('apk-manifest-info').innerHTML = `<div style="color: var(--danger-color);">加载失败: ${escapeHtml(data.error)}</div>`;
            return;
        }

        const manifest = data.data.manifest;
        const rawXml = data.data.raw_xml;

        const version = [
            manifest.versionName ? `版本名 ${manifest.versionName}` : '',
            manifest.versionCode ? `版本号 ${manifest.versionCode}` : ''
        ].filter(Boolean).join(' / ') || '-';
        const sdk = [
            manifest.minSdkVersion ? `min ${manifest.minSdkVersion}` : '',
            manifest.targetSdkVersion ? `target ${manifest.targetSdkVersion}` : ''
        ].filter(Boolean).join(' / ') || '-';
        const fields = [
            { label: '包名', value: manifest.package || '-', icon: '📦' },
            { label: '版本', value: version, icon: '🏷️' },
            { label: 'SDK', value: sdk, icon: '📱' },
        ];

        if (manifest.launchActivity) {
            fields.push({ label: '启动 Activity', value: manifest.launchActivity, icon: '🚀' });
        }

        $('apk-manifest-info').innerHTML = `<div class="apk-manifest-row">
            <div class="apk-manifest-label">📦 包名</div>
            <div class="apk-manifest-value">${escapeHtml(manifest.package || '-')}</div>
            <div class="apk-manifest-label">🏷️ 版本</div>
            <div class="apk-manifest-value">${escapeHtml(version)}</div>
            <div class="apk-manifest-label">📱 SDK</div>
            <div class="apk-manifest-value">${escapeHtml(sdk)}</div>
        </div>`;

        $('apk-raw-xml').textContent = rawXml;
    } catch (e) {
        $('apk-manifest-info').innerHTML = `<div style="color: var(--danger-color);">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadApkPermissions() {
    if (!window.apkCurrentTaskId) return;

    try {
        const data = await apiCall(`/api/apk/permissions/${window.apkCurrentTaskId}`);

        if (!data.success) {
            $('apk-permissions-list').innerHTML = `<div style="color: var(--danger-color); padding: 20px; text-align: center;">加载失败: ${escapeHtml(data.error)}</div>`;
            return;
        }

        const permissions = data.data.permissions;
        $('apk-perm-count').textContent = permissions.length;

        if (permissions.length === 0) {
            $('apk-permissions-list').innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-secondary);">未发现权限声明</div>';
            return;
        }

        $('apk-permissions-list').innerHTML = permissions.map((p, i) =>
            `<div class="apk-permission-item">
                <div class="apk-perm-left">
                    <span class="apk-perm-index">${i + 1}.</span>
                    <span class="apk-perm-name">${escapeHtml(p.name)}</span>
                </div>
                <span class="apk-perm-short">${escapeHtml(p.short_name)}</span>
            </div>`
        ).join('');
    } catch (e) {
        $('apk-permissions-list').innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

async function loadApkSourceTree(path = '') {
    if (!window.apkCurrentTaskId) return;

    try {
        const data = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=${encodeURIComponent(path)}`);

        if (!data.success) {
            $('apk-source-tree').innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(data.error)}</div>`;
            return;
        }

        const items = data.data.items;

        // 不再在加载时构建索引，改为首次搜索时构建

        if (items.length === 0) {
            $('apk-source-tree').innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-secondary);">目录为空</div>';
            return;
        }

        if (!path) {
            $('apk-source-tree').innerHTML = '';
            renderApkSourceItems(items, $('apk-source-tree'), '');
        } else {
            const container = document.querySelector(`[data-apk-path="${path}"]`);
            if (container) {
                const childContainer = container.nextElementSibling;
                if (childContainer && childContainer.classList.contains('apk-tree-children')) {
                    childContainer.innerHTML = '';
                    renderApkSourceItems(items, childContainer, path);
                }
            }
        }
    } catch (e) {
        $('apk-source-tree').innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

function renderApkSourceItems(items, container, parentPath) {
    items.forEach(item => {
        const itemDiv = document.createElement('div');

        const itemHeader = document.createElement('div');
        itemHeader.className = `apk-tree-item ${item.type}`;
        itemHeader.setAttribute('data-apk-path', item.path);

        const nameSpan = document.createElement('span');
        nameSpan.textContent = item.name;
        itemHeader.appendChild(nameSpan);

        if (item.type === 'dir') {
            const childContainer = document.createElement('div');
            childContainer.className = 'apk-tree-children';

            itemHeader.addEventListener('click', async () => {
                if (childContainer.classList.contains('expanded')) {
                    childContainer.classList.remove('expanded');
                    return;
                }

                if (childContainer.children.length === 0) {
                    await loadApkSourceTree(item.path);
                }

                childContainer.classList.add('expanded');
            });

            itemDiv.appendChild(itemHeader);
            itemDiv.appendChild(childContainer);
        } else {
            itemHeader.addEventListener('click', () => viewApkFile(item.path));
            itemDiv.appendChild(itemHeader);
        }

        container.appendChild(itemDiv);
    });
}

function getApkFileLabel(filePath) {
    const parts = String(filePath || '').split(/[\\/]/);
    return parts[parts.length - 1] || filePath || '-';
}

function renderApkFileTabs() {
    const tabsEl = $('apk-file-tabs');
    const viewer = $('apk-file-viewer');
    if (!tabsEl || !viewer) return;

    tabsEl.innerHTML = '';
    window.apkOpenFiles.forEach((file, path) => {
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = `apk-file-tab${path === window.apkActiveFilePath ? ' active' : ''}`;
        tab.title = path;

        const label = document.createElement('span');
        label.className = 'apk-file-tab-label';
        label.textContent = getApkFileLabel(path);
        tab.appendChild(label);

        const closeBtn = document.createElement('span');
        closeBtn.className = 'apk-file-tab-close';
        closeBtn.textContent = '×';
        closeBtn.title = '关闭文件';
        closeBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            closeApkFileTab(path);
        });
        tab.appendChild(closeBtn);

        tab.addEventListener('click', () => activateApkFileTab(path));
        tabsEl.appendChild(tab);
    });

    viewer.style.display = window.apkOpenFiles.size ? 'block' : 'none';
}

function activateApkFileTab(filePath, targetLine = null) {
    const file = window.apkOpenFiles.get(filePath);
    if (!file) return;

    const contentEl = $('apk-file-content');
    const pathEl = $('apk-file-path');
    window.apkActiveFilePath = filePath;
    pathEl.textContent = filePath;
    contentEl.dataset.currentPath = filePath;

    if (file.error) {
        contentEl.textContent = file.error;
    } else if (file.contentHtml) {
        contentEl.innerHTML = file.contentHtml;
        bindApkCodeNavigation(contentEl);
    } else {
        contentEl.textContent = '加载中...';
    }

    renderApkFileTabs();
    if (targetLine) {
        requestAnimationFrame(() => scrollApkCodeToLine(targetLine));
    }
}

function closeApkFileTab(filePath) {
    if (!window.apkOpenFiles.has(filePath)) return;

    const paths = Array.from(window.apkOpenFiles.keys());
    const closedIndex = paths.indexOf(filePath);
    window.apkOpenFiles.delete(filePath);

    if (window.apkActiveFilePath === filePath) {
        const remaining = Array.from(window.apkOpenFiles.keys());
        window.apkActiveFilePath = remaining[Math.max(0, Math.min(closedIndex, remaining.length - 1))] || null;
        if (window.apkActiveFilePath) {
            activateApkFileTab(window.apkActiveFilePath);
        } else {
            const contentEl = $('apk-file-content');
            const pathEl = $('apk-file-path');
            if (contentEl) contentEl.textContent = '';
            if (pathEl) pathEl.textContent = '';
        }
    }

    renderApkFileTabs();
}

async function viewApkFile(filePath) {
    return viewApkFileAt(filePath, null);
}

function renderApkCodeContent(content, filePath) {
    const javaKeywords = new Set([
        'abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch', 'char', 'class',
        'const', 'continue', 'default', 'do', 'double', 'else', 'enum', 'extends', 'final',
        'finally', 'float', 'for', 'goto', 'if', 'implements', 'import', 'instanceof', 'int',
        'interface', 'long', 'native', 'new', 'package', 'private', 'protected', 'public',
        'return', 'short', 'static', 'strictfp', 'super', 'switch', 'synchronized', 'this',
        'throw', 'throws', 'transient', 'try', 'void', 'volatile', 'while', 'true', 'false',
        'null'
    ]);
    const identifierRe = /[A-Za-z_$][A-Za-z0-9_$]*/g;
    const lines = String(content || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');

    return lines.map((line, index) => {
        let html = '';
        let lastIndex = 0;
        identifierRe.lastIndex = 0;
        let match;
        while ((match = identifierRe.exec(line)) !== null) {
            html += escapeHtml(line.slice(lastIndex, match.index));
            const token = match[0];
            if (javaKeywords.has(token)) {
                html += `<span class="apk-code-keyword">${escapeHtml(token)}</span>`;
            } else {
                html += `<span class="apk-code-symbol" data-symbol="${escapeHtml(token)}">${escapeHtml(token)}</span>`;
            }
            lastIndex = match.index + token.length;
        }
        html += escapeHtml(line.slice(lastIndex));

        const lineNo = index + 1;
        return `<div class="apk-code-line" id="apk-code-line-${lineNo}" data-line="${lineNo}">
            <span class="apk-code-line-no">${lineNo}</span><span class="apk-code-text">${html || ' '}</span>
        </div>`;
    }).join('');
}

async function jumpToApkDefinition(symbol, currentPath, currentLine) {
    if (!window.apkCurrentTaskId) return;

    if (!symbol) return;
    try {
        const params = new URLSearchParams({
            symbol,
            path: currentPath || '',
            line: String(currentLine || 0)
        });
        const data = await apiCall(`/api/apk/definition/${window.apkCurrentTaskId}?${params.toString()}`);

        if (!data.success || !data.data?.definition) {
            showToast(data.error || `未找到定义: ${symbol}`, 'warning');
            return;
        }

        const definition = data.data.definition;
        await viewApkFileAt(definition.path, definition.line);
    } catch (e) {
        showToast(`跳转失败: ${e.message}`, 'error');
    }
}

async function viewApkFileAt(filePath, targetLine = null) {
    if (!window.apkCurrentTaskId) return;

    const existingFile = window.apkOpenFiles.get(filePath);
    if (existingFile && (existingFile.contentHtml || existingFile.error)) {
        activateApkFileTab(filePath, targetLine);
        return;
    }

    window.apkOpenFiles.set(filePath, { loading: true });
    activateApkFileTab(filePath);

    try {
        const data = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=${encodeURIComponent(filePath)}&view=true`);

        if (data.success) {
            window.apkOpenFiles.set(filePath, {
                loading: false,
                contentHtml: renderApkCodeContent(data.data.content, filePath)
            });
        } else {
            window.apkOpenFiles.set(filePath, {
                loading: false,
                error: `加载失败: ${data.error}`
            });
        }
    } catch (e) {
        window.apkOpenFiles.set(filePath, {
            loading: false,
            error: `加载失败: ${e.message}`
        });
    }

    activateApkFileTab(filePath, targetLine);
}

function bindApkCodeNavigation(contentEl) {
    if (!contentEl || contentEl.dataset.navigationBound === 'true') return;
    contentEl.dataset.navigationBound = 'true';
    contentEl.addEventListener('click', async (event) => {
        const symbolEl = event.target.closest('.apk-code-symbol');
        if (!symbolEl || !event.ctrlKey) return;

        event.preventDefault();
        const lineEl = symbolEl.closest('.apk-code-line');
        const symbol = symbolEl.dataset.symbol;
        const currentPath = contentEl.dataset.currentPath || '';
        const currentLine = Number(lineEl?.dataset.line || 0);
        await jumpToApkDefinition(symbol, currentPath, currentLine);
    });
}

function scrollApkCodeToLine(line) {
    const contentEl = $('apk-file-content');
    const target = contentEl?.querySelector(`#apk-code-line-${line}`);
    if (!target) return;

    target.scrollIntoView({ block: 'center' });
    target.classList.add('apk-code-line-target');
    setTimeout(() => target.classList.remove('apk-code-line-target'), 1800);
}

function closeApkFileViewer() {
    window.apkOpenFiles.clear();
    window.apkActiveFilePath = null;
    const contentEl = $('apk-file-content');
    const pathEl = $('apk-file-path');
    if (contentEl) contentEl.textContent = '';
    if (pathEl) pathEl.textContent = '';
    renderApkFileTabs();
}

function switchApkTab(tabName) {
    document.querySelectorAll('[data-apk-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.apkTab === tabName);
    });

    $('apk-tab-manifest').style.display = tabName === 'manifest' ? 'block' : 'none';
    $('apk-tab-permissions').style.display = tabName === 'permissions' ? 'block' : 'none';
    $('apk-tab-source').style.display = tabName === 'source' ? 'block' : 'none';

    if (tabName === 'permissions' && !$('apk-permissions-list').dataset.loaded) {
        $('apk-permissions-list').dataset.loaded = 'true';
        loadApkPermissions();
    }
    if (tabName === 'source' && !$('apk-source-tree').dataset.loaded) {
        initApkSourceResizer();
        $('apk-source-tree').dataset.loaded = 'true';
        loadApkSourceTree('');
    } else if (tabName === 'source') {
        initApkSourceResizer();
    }
}

function downloadApkSource() {
    if (!window.apkCurrentTaskId) return;
    const link = document.createElement('a');
    link.href = `/api/apk/download/${window.apkCurrentTaskId}`;
    link.download = '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

function resetApkAnalysis() {
    stopApkPolling();
    window.apkCurrentTaskId = null;
    window.apkNotifiedTaskId = null;
    resetApkFileIndex();

    setApkUploadEmpty(true);
    $('apk-analysis-status').style.display = 'none';
    $('apk-analysis-result').style.display = 'none';
    $('apk-file-input').value = '';
    $('apk-upload-progress').style.display = 'none';
    $('apk-progress-fill').style.width = '0%';
    $('apk-analysis-progress-container').style.display = 'none';
    $('apk-analysis-progress-bar').style.width = '0%';

    const sourceTree = $('apk-source-tree');
    if (sourceTree) {
        sourceTree.dataset.loaded = '';
        sourceTree.innerHTML = '';
    }
    const permList = $('apk-permissions-list');
    if (permList) {
        permList.dataset.loaded = '';
        permList.innerHTML = '';
    }
    const manifestInfo = $('apk-manifest-info');
    if (manifestInfo) manifestInfo.innerHTML = '';
    const rawXml = $('apk-raw-xml');
    if (rawXml) rawXml.textContent = '';
    closeApkFileViewer();
}

// ==================== Security Audit ====================

function recordSecurityPageView(pageName) {
    if (!pageName) return;
    fetch('/api/security-audit/page-view', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...getClientIdentityHeaders()
        },
        body: JSON.stringify({
            page: pageName,
            title: document.title || '',
            hash: window.location.hash || ''
        })
    }).catch(error => debugLog('[SecurityAudit] page view record failed:', error));
}

function getSecurityAuditFilterParams() {
    const params = new URLSearchParams();
    params.set('limit', '300');

    const source = $('audit-source-filter')?.value || '';
    const actionType = $('audit-type-filter')?.value || '';
    const query = $('audit-search-input')?.value?.trim() || '';

    if (source) params.set('source', source);
    if (actionType) params.set('action_type', actionType);
    if (query) params.set('q', query);
    return params;
}

async function loadSecurityAudit() {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    tbody.innerHTML = `
        <tr>
            <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                加载中...
            </td>
        </tr>
    `;

    try {
        const params = getSecurityAuditFilterParams();
        const result = await apiCall(`/api/security-audit/logs?${params.toString()}`);
        const payload = result.data || {};
        updateSecurityAuditStats(payload.stats || {});
        renderSecurityAuditRows(payload.records || []);
    } catch (error) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--danger-color);">
                    加载失败: ${escapeHtml(error.message)}
                </td>
            </tr>
        `;
    }
}

function updateSecurityAuditStats(stats) {
    const setText = (id, value) => {
        const el = $(id);
        if (el) el.textContent = value ?? 0;
    };
    setText('audit-total-count', stats.total);
    setText('audit-web-count', stats.web);
    setText('audit-cli-count', stats.cli);
    setText('audit-error-count', stats.errors);
}

function getAuditSourceLabel(source) {
    if (source === 'cli') {
        return '<span style="color: var(--warning-color); font-weight: 600;">CLI</span>';
    }
    if (source === 'web') {
        return '<span style="color: var(--success-color); font-weight: 600;">Web</span>';
    }
    return `<span style="color: var(--text-secondary);">${escapeHtml(source || '-')}</span>`;
}

function getAuditStatusLabel(statusCode) {
    const code = Number(statusCode || 0);
    const color = code >= 500 ? 'var(--danger-color)' : code >= 400 ? 'var(--warning-color)' : 'var(--success-color)';
    return `<span style="color: ${color}; font-weight: 600;">${code || '-'}</span>`;
}

function formatAuditTime(timestamp) {
    if (!timestamp) return '-';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return date.toLocaleString('zh-CN', { hour12: false });
}

function renderSecurityAuditRows(records) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (!records.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    暂无审计记录
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = records.map(record => {
        const userIpText = `${record.username || 'unknown'} / ${record.client_ip || '-'}`;
        const path = record.page ? `#${record.page}` : (record.path || '');
        const detail = [
            record.method ? `${record.method}` : '',
            path,
            record.query && Object.keys(record.query).length ? JSON.stringify(record.query) : ''
        ].filter(Boolean).join(' ');
        const operation = record.operation || detail || '-';
        const operationLine = [operation, detail && detail !== operation ? detail : ''].filter(Boolean).join('  |  ');
        const rowTitle = [
            '点击查看审计详情',
            `时间: ${formatAuditTime(record.timestamp)}`,
            `用户/IP: ${userIpText}`,
            `操作: ${operationLine}`,
        ].join('\n');

        return `
            <tr data-audit-id="${escapeHtml(record.id || '')}" style="border-bottom: 1px solid var(--border-color); cursor: pointer; height: 34px;" title="${escapeHtml(rowTitle)}">
                <td style="padding: 7px 8px; font-size: 12px; color: var(--text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(formatAuditTime(record.timestamp))}</td>
                <td style="padding: 7px 8px; font-size: 12px; text-align: center; white-space: nowrap;">${getAuditSourceLabel(record.source)}</td>
                <td style="padding: 7px 8px; font-size: 12px; text-align: center; white-space: nowrap;">${getAuditStatusLabel(record.status_code)}</td>
                <td style="padding: 7px 8px; font-size: 12px; color: var(--text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(userIpText)}</td>
                <td style="padding: 7px 8px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                    <span style="color: var(--text-primary); font-weight: 600;">${escapeHtml(operationLine)}</span>
                </td>
                <td style="padding: 7px 8px; font-size: 12px; color: var(--text-secondary); text-align: center; white-space: nowrap;">${escapeHtml(String(record.duration_ms ?? 0))} ms</td>
            </tr>
        `;
    }).join('');

    tbody.querySelectorAll('[data-audit-id]').forEach(row => {
        row.addEventListener('click', () => showSecurityAuditDetail(row.dataset.auditId));
    });
}

function formatAuditJson(value) {
    if (value === undefined || value === null || value === '') return '-';
    try {
        return JSON.stringify(value, null, 2);
    } catch (error) {
        return String(value);
    }
}

function renderAuditDetailBlock(title, content, options = {}) {
    const isJson = options.json !== false;
    const text = isJson ? formatAuditJson(content) : String(content || '-');
    return `
        <div style="background: var(--light-bg); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px; margin-bottom: 10px;">
            <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text-primary);">${escapeHtml(title)}</div>
            <pre style="margin: 0; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: 11px; line-height: 1.45; color: var(--text-secondary);">${escapeHtml(text)}</pre>
        </div>
    `;
}

function ensureSecurityAuditDetailModal() {
    let modal = $('security-audit-detail-modal');
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = 'security-audit-detail-modal';
    modal.className = 'modal';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: min(980px, 92vw); max-height: 88vh; overflow: hidden; display: flex; flex-direction: column;">
            <div class="modal-header">
                <span class="modal-title">安全审计详情</span>
                <span class="modal-close" onclick="closeSecurityAuditDetailModal()">&times;</span>
            </div>
            <div class="modal-body" id="security-audit-detail-body" style="overflow: auto; padding-right: 4px;">
                加载中...
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (event) => {
        if (event.target === modal) closeSecurityAuditDetailModal();
    });
    // 支持 Esc 键关闭
    modal.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeSecurityAuditDetailModal();
    });
    return modal;
}

function closeSecurityAuditDetailModal() {
    const modal = $('security-audit-detail-modal');
    if (modal) {
        modal.classList.remove('show');
        modal.style.display = '';
    }
    // 移除全局 Esc 监听器
    if (window._securityAuditEscHandler) {
        document.removeEventListener('keydown', window._securityAuditEscHandler);
        window._securityAuditEscHandler = null;
    }
}

function renderRelatedAuditLogs(relatedLogs) {
    const recentLogs = relatedLogs?.recent_client_logs || [];
    const savedTail = relatedLogs?.saved_log_tail || [];
    const blocks = [];

    if (recentLogs.length) {
        blocks.push(renderAuditDetailBlock('最近页面操作日志', recentLogs));
    }

    if (relatedLogs?.saved_log_file) {
        blocks.push(renderAuditDetailBlock('已保存日志文件', relatedLogs.saved_log_file, { json: false }));
    }

    if (savedTail.length) {
        blocks.push(renderAuditDetailBlock('已保存日志尾部', savedTail.join(''), { json: false }));
    }

    return blocks.join('') || renderAuditDetailBlock('关联日志', '暂无关联日志', { json: false });
}

async function showSecurityAuditDetail(auditId) {
    if (!auditId) return;
    const modal = ensureSecurityAuditDetailModal();
    const body = $('security-audit-detail-body');
    modal.style.display = '';
    modal.classList.add('show');
    body.innerHTML = '加载中...';

    // 添加全局 Esc 监听器
    if (window._securityAuditEscHandler) {
        document.removeEventListener('keydown', window._securityAuditEscHandler);
    }
    window._securityAuditEscHandler = (event) => {
        if (event.key === 'Escape') {
            closeSecurityAuditDetailModal();
        }
    };
    document.addEventListener('keydown', window._securityAuditEscHandler);

    try {
        const result = await apiCall(`/api/security-audit/detail/${encodeURIComponent(auditId)}`);
        const payload = result.data || {};
        const record = payload.record || {};
        const relatedLogs = payload.related_logs || {};
        const metadata = {
            id: record.id,
            timestamp: record.timestamp,
            source: record.source,
            action_type: record.action_type,
            operation: record.operation,
            method: record.method,
            path: record.path,
            page: record.page,
            status_code: record.status_code,
            duration_ms: record.duration_ms,
            username: record.username,
            client_ip: record.client_ip,
            client_id: record.client_id,
            user_agent: record.user_agent,
            error: record.error || ''
        };

        body.innerHTML = `
            ${renderAuditDetailBlock('基本信息', metadata)}
            ${renderAuditDetailBlock('请求参数摘要', record.request_summary || record.query || {})}
            ${renderAuditDetailBlock('执行结果摘要', record.response_summary || {})}
            ${renderRelatedAuditLogs(relatedLogs)}
        `;
    } catch (error) {
        body.innerHTML = `<div style="color: var(--danger-color); padding: 20px;">加载失败: ${escapeHtml(error.message)}</div>`;
    }
}

function exportSecurityAudit() {
    window.open('/api/security-audit/export', '_blank');
}

window.recordSecurityPageView = recordSecurityPageView;
window.loadSecurityAudit = loadSecurityAudit;
window.showSecurityAuditDetail = showSecurityAuditDetail;
window.closeSecurityAuditDetailModal = closeSecurityAuditDetailModal;
window.exportSecurityAudit = exportSecurityAudit;
window.toggleNotificationPanel = toggleNotificationPanel;
window.closeNotificationPanel = closeNotificationPanel;
window.requestBrowserNotificationPermission = requestBrowserNotificationPermission;
window.markNotificationRead = markNotificationRead;
window.markAllNotificationsRead = markAllNotificationsRead;
window.clearNotifications = clearNotifications;

// ==================== APK 文件搜索功能 ====================

let apkSearchDebounceTimer = null;
let apkFileIndex = new Map(); // 惰性缓存：path -> { name, type, children? }
let apkIndexBuilt = false; // 索引是否已构建
let apkIndexBuilding = false; // 是否正在构建索引

function buildApkFileIndex(items, parentPath) {
    // 缓存当前加载的目录层
    for (const item of items) {
        apkFileIndex.set(item.path, {
            name: item.name,
            type: item.type,
            children: item.children?.map(c => c.path) || []
        });
    }
}

async function buildFullApkIndex() {
    // 构建完整索引（首次搜索时调用）
    if (apkIndexBuilt || apkIndexBuilding) return;
    apkIndexBuilding = true;

    try {
        const rootData = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=`);
        if (rootData.success && rootData.data.items) {
            buildApkFileIndex(rootData.data.items, '');
            // 递归缓存所有子目录（不管 children 是否有数据）
            for (const item of rootData.data.items) {
                if (item.type === 'dir') {
                    await cacheApkDirectory(item.path);
                }
            }
        }
    } catch (e) {
        console.error('[APK Index] Build failed:', e);
    } finally {
        apkIndexBuilding = false;
        apkIndexBuilt = true;
    }
}

async function cacheApkDirectory(path) {
    // 递归缓存单个目录
    try {
        const data = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=${encodeURIComponent(path)}`);
        if (data.success && data.data.items) {
            buildApkFileIndex(data.data.items, path);
            // 继续缓存子目录（不管 children 是否有数据）
            for (const item of data.data.items) {
                if (item.type === 'dir') {
                    await cacheApkDirectory(item.path);
                }
            }
        }
    } catch (e) {
        console.error('[APK Index] Cache dir failed:', path, e);
    }
}

function resetApkFileIndex() {
    apkFileIndex.clear();
    apkIndexBuilt = false;
    apkIndexBuilding = false;
}

function getApkFilesByQuery(query) {
    // 惰性搜索：遍历索引找到匹配的文件
    const matches = [];
    const lowerQuery = query.toLowerCase();
    for (const [path, info] of apkFileIndex) {
        if (info.type === 'file' && info.name.toLowerCase().includes(lowerQuery)) {
            matches.push({ path, name: info.name });
            if (matches.length >= 20) break; // 提前退出
        }
    }
    return matches;
}

async function filterApkFiles() {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');

    if (!query || query.length < 2) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    // 首次搜索时构建完整索引，并切换到源码预览标签
    if (!apkIndexBuilt && !apkIndexBuilding) {
        // 自动切换到源码预览标签
        if (typeof switchApkTab === 'function') {
            switchApkTab('source');
        }
        showToast('正在构建文件索引...', 'info');
        await buildFullApkIndex();
    }

    // 等待索引构建完成
    if (apkIndexBuilding) {
        await new Promise(resolve => {
            const check = setInterval(() => {
                if (!apkIndexBuilding) {
                    clearInterval(check);
                    resolve();
                }
            }, 100);
        });
    }

    // 过滤匹配的文件（最多 20 项）
    const matches = getApkFilesByQuery(query);

    if (!resultsEl || matches.length === 0) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    // 显示搜索结果
    resultsEl.innerHTML = '';
    for (const file of matches) {
        const item = document.createElement('div');
        item.className = 'apk-search-result-item';
        item.onclick = () => jumpToApkFile(file.path);
        item.innerHTML = `<span style="font-family: monospace;">${escapeHtml(file.name)}</span><span style="color: var(--text-secondary); font-size: 11px; margin-left: 8px;">${escapeHtml(file.path)}</span>`;
        resultsEl.appendChild(item);
    }
    resultsEl.style.display = 'block';

    // 定位搜索结果到搜索框下方，宽度与输入框一致
    const searchEl = $('apk-file-search');
    if (searchEl && resultsEl) {
        const rect = searchEl.getBoundingClientRect();
        resultsEl.style.position = 'absolute';
        resultsEl.style.top = (rect.bottom + window.scrollY) + 'px';
        resultsEl.style.left = (rect.left + window.scrollY) + 'px';
        resultsEl.style.width = rect.width + 'px';
    }
}

// Use generic debounce utility for APK search
const debounceFilterApkFiles = debounce(filterApkFiles, 300);

function jumpToApkFile(selectedPath) {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');

    // 如果没有指定路径，从搜索结果或缓存中查找
    let path = selectedPath;
    if (!path && query) {
        const matches = getApkFilesByQuery(query);
        if (matches.length > 0) {
            path = matches[0].path;
        }
    }

    if (!path) {
        showToast('未找到匹配的文件', 'warning');
        return;
    }

    // 关闭搜索结果
    if (resultsEl) resultsEl.style.display = 'none';

    // 打开文件
    viewApkFile(path);

    // 展开文件树到该文件
    expandApkTreeToPath(path);
}

function clearApkSearch() {
    const searchEl = $('apk-file-search');
    const resultsEl = $('apk-search-results');
    if (searchEl) searchEl.value = '';
    if (resultsEl) resultsEl.style.display = 'none';
}

function expandApkTreeToPath(filePath) {
    const parts = filePath.split('/');
    let currentPath = '';

    for (let i = 0; i < parts.length - 1; i++) {
        currentPath = (currentPath ? currentPath + '/' : '') + parts[i];
        const container = document.querySelector(`[data-apk-path="${CSS.escape(currentPath)}"]`);
        if (container) {
            const childContainer = container.querySelector('.apk-tree-children');
            if (childContainer && childContainer.classList.contains('apk-tree-children')) {
                childContainer.classList.add('expanded');
            }
        }
    }
}

// 点击搜索结果外部时关闭
document.addEventListener('click', (e) => {
    const resultsEl = $('apk-search-results');
    const searchEl = $('apk-file-search');
    if (resultsEl && searchEl && !resultsEl.contains(e.target) && e.target !== searchEl) {
        resultsEl.style.display = 'none';
    }
});

// Export APK search functions to window
window.filterApkFiles = filterApkFiles;
window.jumpToApkFile = jumpToApkFile;
window.clearApkSearch = clearApkSearch;
window.expandApkTreeToPath = expandApkTreeToPath;
window.debounceFilterApkFiles = debounceFilterApkFiles;
window.handleApkFile = handleApkFile;
window.initApkAnalysisPage = initApkAnalysisPage;
