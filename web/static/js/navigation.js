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
let currentCategoryFilter = 'all';
let currentMethodFilter = 'all';
const API_DOCS_CACHE_DURATION = 5 * 60 * 1000; // 5分钟缓存（生产环境）
const FIRMWARE_UPLOAD_TIMEOUT = 24 * 60 * 60 * 1000; // 服务端分片保留24小时
const apiDetailsCache = new Map();

// API表格列宽配置 (与HTML模板保持一致: 25%, 18%, 17%, 40%)
const API_TABLE_COLUMNS = {
    INTERFACE: 25,
    DESCRIPTION: 20,
    SKILL: 20,
    USAGE: 35
};

const HTTP_METHODS = {
    GET: 'GET',
    POST: 'POST',
    WEBSOCKET: 'WebSocket'
};

const CURL_SPECIAL_PARAMS = ['force_refresh', 'log_type', 'report_timestamp'];
const VIEWPORT_HEIGHT_OFFSET = 150;
let pendingUsbipDeviceHost = '';
let activeUsbipSelection = null;
let usbipSourceLoadPromise = null;
const usbipSourceDeviceCache = new Map();
const usbipAssignedBusidsBySource = new Map();
let pendingDevicePasswordAction = 'usbip';
let pendingDevicePasswordRetry = null;
let usbipReconnectTimer = null;
let usbipReconnectAttempts = 0;
let usbipManualDisconnectUntil = 0;
let usbipReconnectWaiting = false;
let usbipOperationGeneration = 0;
let adbProxyStatus = null;
let adbProxyOperationRunning = false;
let adbProxyDeviceRefreshTimer = null;
let adbProxyDeviceRefreshRunning = false;
let usbipSourceRefreshTimer = null;
let usbipSourceRefreshRunning = false;
let usbipRoutingOperationRunning = false;
const usbipPendingAssignmentKeys = new Set();
const USBIP_RECONNECT_MAX_ATTEMPTS = 30;
const USBIP_RECONNECT_INTERVAL_MS = 5000;
const USBIP_RECONNECT_INITIAL_DELAY_MS = 1500;
const USBIP_MANUAL_DISCONNECT_SUPPRESS_MS = 5 * 60 * 1000;
const DEVICE_ROUTING_REFRESH_INTERVAL_MS = 3000;

const PARAM_TYPES = {
    STRING: 'string',
    NUMBER: 'number',
    ARRAY: 'array',
    BOOLEAN: 'boolean',
    FILE: 'file',
    OBJECT: 'object'
};

const CURL_PLACEHOLDERS = Object.freeze({
    [PARAM_TYPES.STRING]: 'VALUE',
    [PARAM_TYPES.NUMBER]: 123,
    [PARAM_TYPES.ARRAY]: ['Serial'],
    [PARAM_TYPES.BOOLEAN]: true,
    [PARAM_TYPES.FILE]: '/path/to/file.img',
    [PARAM_TYPES.OBJECT]: {}
});

const PATH_PATTERNS = [];

const DEFAULT_API_DETAILS = Object.freeze({
    title: 'API接口',
    description: '执行API操作',
    params: Object.freeze([]),
    response: '{ "success": true }',
    usage: '使用该接口完成相关操作'
});

// 页面生命周期内不变的服务端信息。
const BASE_URL = window.location.origin;
const WS_BASE_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;

const BADGE_SIZES = { xs: '9px', sm: '10px', md: '11px', lg: '12px' };
const BADGE_PADDINGS = { xs: '1px 4px', sm: '2px 6px', md: '3px 8px', lg: '4px 10px' };

// ==================== 轮询间隔配置 ====================
// GSI 固件烧写进度轮询间隔（毫秒）
const GSI_PROGRESS_POLL_INTERVAL = 2000; // 2 秒
// 状态轮询间隔（毫秒）
const STATUS_POLL_INTERVAL = 2000; // 2 秒
// 报告列表刷新间隔（毫秒）
const REPORTS_REFRESH_INTERVAL = 15000; // 15 秒
// 最大进度轮询错误次数
const MAX_PROGRESS_ERRORS = 3;
let wakeTestStatusPolling = () => {};
let stopTestStatusPolling = () => {};

// 辅助函数
function validateDeviceSelection() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return false;
    }
    const unavailable = Array.from(state.selectedDevices).filter(deviceId => {
        const device = state.devices.find(item => {
            const id = typeof item === 'string' ? item : item.device_id;
            return id === deviceId;
        });
        return device && !isSelectableTestDevice(device);
    });
    if (unavailable.length > 0) {
        showToast(`所选设备当前不可执行 ADB/测试操作: ${unavailable.join(', ')}`, 'warning');
        return false;
    }
    return true;
}

function validateBootloaderDeviceSelection() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return false;
    }
    const unavailable = Array.from(state.selectedDevices).filter(deviceId => {
        const device = state.devices.find(item => {
            const id = typeof item === 'string' ? item : item.device_id;
            return id === deviceId;
        });
        return device && !isSelectableBootloaderDevice(device);
    });
    if (unavailable.length > 0) {
        const proxyDevices = selectedAdbProxyDeviceIds();
        showToast(proxyDevices.length
            ? `ADB Proxy远程设备没有本地USB/Fastboot通道，不能锁定或解锁: ${proxyDevices.join(', ')}`
            : `所选设备当前不可执行 Bootloader 锁定/解锁: ${unavailable.join(', ')}`, 'warning');
        return false;
    }
    return true;
}

function selectedAdbProxyDeviceIds() {
    return Array.from(state.selectedDevices).filter(deviceId => {
        const device = state.devices.find(item => (
            (typeof item === 'string' ? item : item.device_id) === deviceId
        ));
        return typeof device === 'object' && device?.transport === 'adb_proxy';
    });
}

function validateLocalUsbDeviceSelection(operationName) {
    const proxyDevices = selectedAdbProxyDeviceIds();
    if (!proxyDevices.length) return true;
    showToast(
        `ADB Proxy远程设备不支持${operationName}，请在设备来源主机操作: ${proxyDevices.join(', ')}`,
        'warning'
    );
    return false;
}

function syncLocalUsbActionButtons() {
    const blocked = selectedAdbProxyDeviceIds().length > 0;
    [
        ['btn-lock-device', '锁定设备'],
        ['btn-unlock-device', '解锁设备'],
        ['btn-burn-firmware', '烧写固件'],
        ['btn-burn-gsi', '烧写GSI']
    ].forEach(([buttonId, operationName]) => {
        const button = document.getElementById(buttonId);
        if (!button) return;
        if (blocked && button.dataset.adbProxyBlocked !== 'true') {
            button.dataset.adbProxyBlocked = 'true';
            button.dataset.adbProxyPreviousDisabled = String(button.disabled);
            button.dataset.adbProxyPreviousTitle = button.title || '';
            button.disabled = true;
            button.title = `ADB Proxy远程设备不支持${operationName}，请在设备来源主机操作`;
        } else if (!blocked && button.dataset.adbProxyBlocked === 'true') {
            button.disabled = button.dataset.adbProxyPreviousDisabled === 'true';
            button.title = button.dataset.adbProxyPreviousTitle || '';
            delete button.dataset.adbProxyBlocked;
            delete button.dataset.adbProxyPreviousDisabled;
            delete button.dataset.adbProxyPreviousTitle;
        }
    });
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

// OpenGrok URL builder utility
function buildOpenGrokUrl(path, line = null) {
    if (!OPENGROK_CONFIG.isValid) return '';

    const url = `${OPENGROK_CONFIG._baseUrl}/xref/${OPENGROK_CONFIG._defaultProject}/${path}`;
    return line ? `${url}#${line}` : url;
}

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
        const workerId = selectedClusterWorker();
        const clusterActions = {
            '/api/devices/reboot': 'reboot',
            '/api/devices/remount': 'remount',
            '/api/devices/info': 'get_properties'
        };
        if (workerId && clusterActions[endpoint]) {
            return await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId,
                devices: Array.from(state.selectedDevices),
                action: clusterActions[endpoint]
            });
        }
        if (workerId) {
            throw new Error(`远端 Worker 暂不支持此结构化设备操作: ${endpoint}`);
        }
        await apiCall(endpoint, 'POST', {
            devices: Array.from(state.selectedDevices),
            ...additionalData
        });
    } catch (error) {
        addLogEntry(`操作失败: ${error.message}`, 'error');
    }
}

function selectedClusterWorker() {
    const selected = Array.from(state.selectedDevices);
    if (!selected.length) return '';
    const workspaceWorker = workspaceWorkerId();
    if (workspaceWorker && !isLocalWorkspaceWorker(workspaceWorker)) {
        return workspaceWorker;
    }
    const workers = new Set(selected.map(deviceId => {
        const device = state.devices.find(item => typeof item !== 'string' &&
            (item.device_id === deviceId || item.serial === deviceId));
        return device && device.cluster_worker_id;
    }).filter(Boolean));
    return workers.size === 1 ? Array.from(workers)[0] : '';
}

// ==================== Initialization ====================
// 认证完成或匿名进入后执行一次应用初始化。
let _appInitStarted = false;
async function continueAppInitialization() {
    if (_appInitStarted) return;
    _appInitStarted = true;
    // 文件浏览和传输功能依赖服务端路径配置。
    await loadConfig();
    // 通知页面恢复逻辑认证状态已就绪。
    window.dispatchEvent(new CustomEvent('gms:auth-ready'));

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
                state.clientDisplayId = userData.display_client_id || userData.client_id;
                debugLog('[Init] ✅ Set state.clientId from /api/users/current:', state.clientId);

                // 检查是否是 unknown 用户（apiCall 中会统一处理弹框）
                if (userData.client_id.startsWith('unknown@')) {
                    debugLog('[Init] Detected unknown client, will show username modal via apiCall');
                } else {
                    loadNotifications();
                    // 已获取到正确的用户名，延迟检查 USB/IP 和 VPN 状态（避免阻塞关键请求）
                    setTimeout(() => {
                        const statusChecks = [checkUsbipStatus()];
                        if (isPlatformAdmin()) statusChecks.push(checkVpnStatus());
                        Promise.all(statusChecks).catch(error => {
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

    await initializeClusterMode();
    await loadClusterWorkers().catch(error => debugLog('[Cluster] Worker list unavailable:', error));

    // 刷新非测试页面时不要启动 ADB、套件和用户列表等全局扫描。各页面会由
    // runPageInitializers() 按需加载自己的数据，避免多个慢请求争抢后端资源。
    const initialPage = window.__targetPage || 'test';
    const needsTestWorkspace = initialPage === 'test';

    // 📱 测试页优先加载设备列表，缩短首屏等待时间。
    // 必须先等待 workspace 上下文加载完成，否则 workspaceWorkerId() 会返回
    // 默认的 ats-worker-controller，先加载本机设备再跳到实际主机，造成闪屏。
    if (needsTestWorkspace) {
        await (window.GmsWorkspace?.ready || Promise.resolve());
        loadDevices();
    }

    // ⚙️ 延迟加载非关键数据（避免阻塞关键请求）
    setTimeout(() => {
        if (needsTestWorkspace) {
            loadTestSuites();
            checkInitialTestStatus().catch(error => {
                console.warn('[Init] Test status check failed:', error);
            });
        }
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
    if (needsTestWorkspace) setTimeout(async () => {

        // 加载用户列表
        if (isPlatformAdmin()) {
            try {
                await loadUsers();
            } catch (error) {
                console.warn('[Init] Failed to load users:', error);
            }
        }
    }, 100);  // 减少延迟时间，更快获取客户端信息
}

document.addEventListener('DOMContentLoaded', async () => {
    const authenticated = await ensureAuthenticatedBeforeAppStart().catch(error => {
        console.error('[Auth] Failed to check auth status:', error);
        showAuthGate(false);
        return false;
    });
    if (!authenticated) {
        return;
    }
    await continueAppInitialization();
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
        const uploadId = sessionStorage.getItem('firmwareUploadId') || `${fileName || ''}:${fileSize || ''}`;
        const warningKey = `firmwareUploadWarningShown:${uploadId}`;
        const warningShown = sessionStorage.getItem(warningKey) === 'true';
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

        if (!warningShown) {
            const message = `⚠️ 固件上传已中断: ${fileName}\n` +
                           `上次进度: ${progress.toFixed(1)}% (${formatBytes(uploadedSize)}/${formatBytes(totalSize)})\n` +
                           `中断时间: ${Math.floor(elapsed / 1000)}秒前\n\n` +
                           `重新选择同一文件后会自动从已上传分片继续。`;

            addLogEntry(message, 'warning');
            showToast('固件上传已暂停，请重新选择同一文件续传', 'warning');
            createLocalNotification('固件上传中断', `${fileName} 上传中断于 ${progress.toFixed(1)}%`, 'warning', 'firmware-upload', {
                filename: fileName,
                progress
            });
            sessionStorage.setItem(warningKey, 'true');
        }

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

        sessionStorage.setItem('firmwareUploadInterrupted', 'true');
    }
}

// ==================== Configuration ====================
async function loadConfig() {
    try {
        const config = await apiCall('/api/config/read', 'GET');
        state.config = config;
    } catch (error) {
        debugLog('Failed to load config:', error);
        state.config = {};
    }
}

function getDefaultUbuntuUser() {
    return state.config?.effective_ubuntu_user || state.config?.ubuntu_user || 'gms';
}

function getDefaultSuitesPath() {
    const configured = String(
        state.config?.effective_suites_path || state.config?.suites_path || ''
    ).trim();
    if (configured === '~') return `/home/${getDefaultUbuntuUser()}`;
    if (configured.startsWith('~/')) {
        return `/home/${getDefaultUbuntuUser()}/${configured.slice(2)}`;
    }
    const resolved = configured || `/home/${getDefaultUbuntuUser()}/GMS-Suite`;
    return resolved === '/' ? resolved : resolved.replace(/\/+$/, '');
}

// FastAPI WebSocket 连接。
function initWebSocket() {
    if (state.websocket && (
        state.websocket.readyState === WebSocket.OPEN
        || state.websocket.readyState === WebSocket.CONNECTING
    )) {
        return;
    }
    if (state.websocketReconnectTimer) {
        clearTimeout(state.websocketReconnectTimer);
        state.websocketReconnectTimer = null;
    }

    // 获取客户端ID
    apiCall('/api/users/current', 'GET').then(data => {
        const clientId = data.client_id || 'unknown';
        state.clientId = clientId;
        state.clientDisplayId = data.display_client_id || clientId;

        // 建立WebSocket连接
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/api/system/websocket/${encodeURIComponent(clientId)}`;

        debugLog(`[WebSocket] Connecting to: ${wsUrl}`);
        const websocket = new WebSocket(wsUrl);
        state.websocket = websocket;

        websocket.onopen = () => {
            if (state.websocket !== websocket) {
                return;
            }
            debugLog('[WebSocket] Connected');
            updateConnectionStatus(true);
            // 显示可读的 username@ip，而非平台用户安全边界（裸 ID）。
            const displayId = data.display_client_id || clientId;
            addLogEntry(`WebSocket已连接 (${displayId})`, 'success');
            // WebSocket 成为实时主通道后，把增量游标对齐到服务端当前日志总数，
            // 避免断连期间的轮询已显示的日志在重连后被再次补发。
            // F5 刷新时 state.testing 可能尚未置位（checkInitialTestStatus 有延迟），
            // 因此不依赖 state.testing，只要服务端有日志就对齐游标。
            apiCall('/api/test/status?logs=false').then(s => {
                if (typeof s.log_count === 'number') {
                    state.lastLogCount = Math.max(state.lastLogCount || 0, s.log_count);
                }
                if (
                    typeof s.running === 'boolean'
                    && isLocalWorkspaceWorker(workspaceWorkerId())
                    && !state.clusterJobId
                ) {
                    state.testing = s.running;
                    updateTestToggleButton(s.running);
                }
                // 重连后重置停滞计数，避免上一连接的残留计数误触发增量兜底。
                state.wsLogStallTicks = 0;
            }).catch(() => {});
        };

        websocket.onclose = () => {
            if (state.websocket !== websocket) {
                return;
            }
            debugLog('[WebSocket] Disconnected');
            state.websocket = null;
            updateConnectionStatus(false);
            addLogEntry('WebSocket连接已断开', 'warning');
            wakeTestStatusPolling();
            // 5秒后重连
            state.websocketReconnectTimer = setTimeout(() => {
                state.websocketReconnectTimer = null;
                debugLog('[WebSocket] Attempting to reconnect...');
                initWebSocket();
            }, 5000);
        };

        websocket.onerror = (error) => {
            if (state.websocket !== websocket) {
                return;
            }
            debugLog('[WebSocket] Error:', error);
        };

        websocket.onmessage = (event) => {
            if (state.websocket !== websocket) {
                return;
            }
            try {
                const data = JSON.parse(event.data);
                const messageType = data.type;

                switch (messageType) {
                    case 'log_update':
                        debugLog('[WebSocket] log_update:', data.log);
                        addNormalizedLogEntry(data);
                        state.lastLogCount = (state.lastLogCount || 0) + 1;
                        // WebSocket 正常投递日志，清除停滞计数。
                        state.wsLogStallTicks = 0;
                        break;

                    case 'test_complete':
                        if (!isLocalWorkspaceWorker(workspaceWorkerId()) || state.clusterJobId) break;
                        state.testing = false;
                        state.currentBurningProgress = 0;  // 重置进度
                        updateTestToggleButton(false);
                        addLogEntry('测试完成', 'success');
                        if (data.notification) {
                            handleRealtimeNotification(data.notification, { toast: false, browser: true, forceBrowser: true });
                        } else {
                            handleRealtimeNotification({
                                title: '测试完成',
                                message: '测试已完成',
                                level: 'success',
                                category: 'test'
                            }, { toast: false, browser: true, forceBrowser: true });
                        }
                        break;

                    case 'devices_updated':
                        if (!isLocalWorkspaceWorker(workspaceWorkerId())) break;
                        state.devices = data.devices;
                        renderDevices();
                        break;

                    case 'device_lock_update':
                        if (!isLocalWorkspaceWorker(workspaceWorkerId())) break;
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
                        if (!isLocalWorkspaceWorker(workspaceWorkerId())) break;
                        // USB 设备插拔事件，自动刷新设备列表
                        debugLog('[WebSocket] devices_changed received:', data);
                        debugLog('[WebSocket] devices_changed:', data.devices);
                        if (data.notification) {
                            handleRealtimeNotification(data.notification, { toast: false });
                        }

                        const connected = data.connected || [];
                        const disconnected = data.disconnected || [];

                        // 刷新设备列表（静默：避免再打印一条泛泛的"[自动刷新]"日志，
                        // 下方的"检测到 USB 设备变化"信息量更高，作为 USB 事件的唯一日志）
                        loadDevices(true, {silent: true}).then(() => {
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

                            // USB/IP 设备重启时优先自动重连。
                            if (
                                data.source !== 'usbip_disconnect'
                                && state.usbipConnected
                                && disconnected.length > 0
                                && Date.now() > usbipManualDisconnectUntil
                            ) {
                                scheduleUsbipReconnect('检测到 USB/IP 设备断开: ' + disconnected.join(' '));
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

                    case 'firmware_burn_complete':
                        if (!isLocalWorkspaceWorker(workspaceWorkerId())) break;
                        // 固件/GSI 烧写完成且设备锁已释放：自动刷新 ADB 设备状态，
                        // 避免界面仍显示"锁定/Allocated"需手动点刷新。
                        debugLog('[WebSocket] firmware_burn_complete:', data);
                        loadDevices(true).catch(err => {
                            console.error('[WebSocket] refresh after firmware burn failed:', err);
                        });
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
            initWebSocket();
        }, 3000);
    });
}

// ==================== Event Listeners ====================
function initEventListeners() {
    // Test type change
    $('test-type').addEventListener('change', onTestTypeChange);
    $('test-suite')?.addEventListener('change', event => {
        const suitePath = String(event.target.value || '');
        window.GmsWorkspace?.update({suite_path: suitePath, suite_key: suitePath}, {source: 'test'});
    });

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
/**
 * 转义字符串用于双引号 HTML 属性里的单引号 JS 字符串上下文
 */
function escapeJsAttr(str) {
    return String(str ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/\r/g, '\\r')
        .replace(/\n/g, '\\n')
        .replace(/\u2028/g, '\\u2028')
        .replace(/\u2029/g, '\\u2029')
        .replace(/'/g, "\\'")
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/**
 * 测试模块/用例 与 测试报告 互斥处理
 * @param {'module_case' | 'retry'} mode - 'module_case' 表示填入了模块/用例，清空报告；'retry' 表示填入了报告，清空模块/用例
 */
function enforceFieldExclusion(mode) {
    if (mode === 'module_case') {
        // 填入模块/用例时，清空测试报告
        const retryInput = $('retry-result');
        if (retryInput) retryInput.value = '';
    } else if (mode === 'retry') {
        // 填入测试报告时，清空模块和用例
        const moduleInput = $('test-module');
        const caseInput = $('test-case');
        if (moduleInput) moduleInput.value = '';
        if (caseInput) caseInput.value = '';
    }
}

function onInputChange() {
    // 测试模块、用例和重试报告互斥。
    const testModule = $('test-module').value.trim();
    const testCase = $('test-case').value.trim();
    const retryResult = $('retry-result').value.trim();

    // If typing in retry-result, clear module and case
    if (document.activeElement.id === 'retry-result' && retryResult) {
        enforceFieldExclusion('retry');
    }
    // If typing in module or case, clear retry-result
    else if ((document.activeElement.id === 'test-module' || document.activeElement.id === 'test-case') && (testModule || testCase)) {
        enforceFieldExclusion('module_case');
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
        document.getElementById('test-suite').value = '';
    }
}

function onDeviceHostConfirm() {
    const deviceHost = document.getElementById('device-host').value.trim();
    addLogEntry(`设备主机地址暂不支持动态更新: ${deviceHost}`, 'warning');
    showToast('设备主机地址需要直接编辑config.json文件', 'warning');
    // device_host 不支持通过 API 动态更新。
    // 如需修改，请直接编辑configs/config.json文件
}

async function onLocalServerConfirm() {
    const localServer = document.getElementById('local-server').value.trim();
    try {
        await apiCall('/api/config/update', 'POST', {
            local_server: localServer
        });
        addLogEntry(`本地主机地址已更新: ${localServer}`, 'info');
        showToast('本地主机地址已更新', 'success');
    } catch (error) {
        addLogEntry(`本地主机地址更新失败: ${error.message}`, 'error');
    }
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

// ==================== Device Management ====================
let _loadClusterWorkersInFlight = null;
async function loadClusterWorkers() {
    const select = document.getElementById('cluster-worker');
    if (!select) return;
    if (typeof initializeClusterMode === 'function') {
        await initializeClusterMode();
    }
    if (_loadClusterWorkersInFlight) return _loadClusterWorkersInFlight;
    _loadClusterWorkersInFlight = (async () => {
        try {
            const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
            if (!response.ok) return;
            const data = await response.json();
            await (window.GmsWorkspace?.ready || Promise.resolve());
            const localWorkerId = workspaceLocalWorkerId();
            const workers = (data.workers || []).filter(worker =>
                worker.status !== 'offline'
                && (state.clusterStatus?.enabled || worker.id === localWorkerId)
            );
            const context = window.GmsWorkspace?.get?.() || {};
            const optionData = [];
            if (!workers.some(worker => worker.id === localWorkerId)) {
                optionData.push({value: localWorkerId, label: localWorkerId});
            }
            for (const worker of workers) {
                optionData.push({
                    value: worker.id,
                    label: worker.id,
                });
            }

            const availableValues = new Set(optionData.map(option => option.value));
            const contextWorkerId = context.worker_id || localWorkerId;
            const selectedWorkerId = availableValues.has(contextWorkerId)
                ? contextWorkerId
                : (availableValues.has(select.value)
                    ? select.value
                    : (availableValues.has(localWorkerId) ? localWorkerId : optionData[0]?.value || ''));
            const optionsUnchanged = select.options.length === optionData.length
                && optionData.every((option, index) => {
                    const currentOption = select.options[index];
                    return currentOption.value === option.value
                        && currentOption.textContent === option.label;
                });

            // 列表没有变化时保留现有 DOM，避免自动刷新导致原生 select 重新绘制。
            if (!optionsUnchanged) {
                const fragment = document.createDocumentFragment();
                for (const item of optionData) {
                    const option = document.createElement('option');
                    option.value = item.value;
                    option.textContent = item.label;
                    option.selected = item.value === selectedWorkerId;
                    fragment.appendChild(option);
                }
                select.replaceChildren(fragment);
            } else if (select.value !== selectedWorkerId) {
                select.value = selectedWorkerId;
            }

            select.dataset.workersLoaded = 'true';
            select.setAttribute('aria-busy', 'false');
            const clusterModeEnabled = Boolean(
                state.clusterStatus?.enabled && context.scope_mode === 'cluster'
            );
            select.disabled = !clusterModeEnabled;
        } finally {
            _loadClusterWorkersInFlight = null;
        }
    })();
    return _loadClusterWorkersInFlight;
}

async function resolveClusterHost(workerId) {
    const response = await fetch('/api/cluster/hosts', {cache: 'no-store'});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.success === false) {
        throw new Error(payload.error || `Worker ${workerId} 主机信息加载失败`);
    }
    const host = (payload.hosts || []).find(item => item.worker_id === workerId);
    if (!host || !host.address || !host.ssh_user) {
        throw new Error(`Worker ${workerId} 缺少 SSH 主机信息`);
    }
    if (host.status === 'offline') {
        throw new Error(`Worker ${workerId} 当前离线`);
    }
    return host;
}

function updateTestHostScopedControls(workerId = workspaceWorkerId()) {
    const remoteSelected = Boolean(workerId && !isLocalWorkspaceWorker(workerId));
    const controllerOnly = {
        'check-sshd-btn': 'SSHD 检查面向 Controller 连接的设备主机',
        'check-routing-btn': '路由检查面向 Controller 与浏览器客户端',
        'vpn-connect-btn': 'VPN 连接由 Controller 测试主机管理',
    };
    Object.entries(controllerOnly).forEach(([id, message]) => {
        const control = document.getElementById(id);
        if (!control) return;
        control.disabled = remoteSelected;
        control.title = remoteSelected ? `${message}；当前已选择远端 Worker` : '';
    });
    const usbip = document.getElementById('usbip-btn');
    if (usbip) {
        usbip.disabled = false;
        usbip.title = remoteSelected
            ? '选择设备来源和接入主机；远端 Worker 接入将在后端分发启用后开放'
            : '选择设备来源、接入主机和 USB 设备';
    }
    const adbForward = document.getElementById('adb-forward-btn');
    if (adbForward) {
        adbForward.disabled = adbProxyOperationRunning;
        adbForward.title = '选择ADB设备来源和接入主机（不依赖USB/IP内核模块）';
    }
}

function requireControllerHostAction(actionName) {
    if (isLocalWorkspaceWorker(workspaceWorkerId())) return true;
    showToast(`${actionName}只作用于 Controller 本机，请先切换测试主机`, 'warning');
    return false;
}

function workspaceLocalWorkerId() {
    return window.GmsWorkspace?.localWorkerId?.()
        || state.clusterStatus?.local_worker_id
        || 'ats-worker-controller';
}

function isLocalWorkspaceWorker(workerId) {
    return !workerId || workerId === 'ats-worker-controller' || workerId === workspaceLocalWorkerId();
}

function workspaceWorkerId() {
    const context = window.GmsWorkspace?.get?.() || {};
    if (!state.clusterStatus?.enabled || context.scope_mode !== 'cluster') return workspaceLocalWorkerId();
    return context.worker_id || workspaceLocalWorkerId();
}

function syncWorkspaceWorkerSelectors(workerId) {
    for (const id of ['cluster-worker', 'suite-worker-select', 'reports-worker-filter']) {
        const select = document.getElementById(id);
        if (!select) continue;
        if (Array.from(select.options).some(option => option.value === workerId)) {
            select.value = workerId;
        }
    }
}

function updateClusterToggleUI(enabled) {
    const row = document.getElementById('cluster-mode-toggle-row');
    const label = document.getElementById('cluster-mode-label');
    if (!row || !label) return;
    if (enabled) {
        label.textContent = '集群模式 · 点击切换单机';
        row.title = '当前为集群模式，点击切换到单机模式';
        row.classList.add('active');
    } else {
        label.textContent = '单机模式 · 点击切换集群';
        row.title = '当前为单机模式，点击切换到集群模式';
        row.classList.remove('active');
    }
}

function applyClusterMode(enabled) {
    const body = document.body;
    if (body) {
        body.classList.remove(
            'workspace-scope-pending',
            'workspace-scope-single',
            'workspace-scope-cluster'
        );
        body.classList.add(enabled ? 'workspace-scope-cluster' : 'workspace-scope-single');
    }
    document.querySelectorAll('.sidebar-item[data-page="cluster"]').forEach(item => {
        // 集群管理页也是启用集群模式和接入 Worker 的入口，单机模式下也必须可见。
        item.style.display = '';
    });
    const testHostSelect = document.getElementById('cluster-worker');
    if (testHostSelect) {
        const workersLoaded = testHostSelect.dataset.workersLoaded === 'true';
        testHostSelect.disabled = !enabled || !workersLoaded;
        testHostSelect.title = enabled
            ? (workersLoaded
                ? '选择执行测试的 Cluster Worker'
                : '正在加载测试主机列表')
            : '当前为单机模式；切换到集群模式后可选择远端测试主机';
    }
    const suiteWorkerSelect = document.getElementById('suite-worker-select');
    if (suiteWorkerSelect?.closest('label')) suiteWorkerSelect.closest('label').style.display = enabled ? '' : 'none';
    const reportsWorkerSelect = document.getElementById('reports-worker-filter');
    const reportsHostFilter = reportsWorkerSelect?.closest('label');
    if (reportsHostFilter) {
        reportsHostFilter.style.visibility = enabled ? 'visible' : 'hidden';
        reportsHostFilter.style.pointerEvents = enabled ? '' : 'none';
    }
    const terminalControl = document.getElementById('terminal-worker-control');
    if (terminalControl) terminalControl.style.display = enabled ? 'contents' : 'none';
    if (!enabled) {
        syncWorkspaceWorkerSelectors(workspaceLocalWorkerId());
    }
    if (typeof window.applyHostWorkspaceScopeMode === 'function') {
        window.applyHostWorkspaceScopeMode(enabled);
    }
    updateTestHostScopedControls(enabled
        ? (window.GmsWorkspace?.get?.().worker_id || workspaceLocalWorkerId())
        : workspaceLocalWorkerId());
    updateClusterToggleUI(enabled);
}

async function toggleClusterMode() {
    const row = document.getElementById('cluster-mode-toggle-row');
    if (row) { row.style.pointerEvents = 'none'; row.style.opacity = '0.6'; }
    try {
        const infrastructureEnabled = Boolean(state.clusterStatus?.enabled);
        const wasEnabled = infrastructureEnabled && window.GmsWorkspace?.get?.().scope_mode === 'cluster';
        if (!wasEnabled && !infrastructureEnabled) {
            throw new Error('集群基础设施未启用，请先在服务端 configs/cluster.json 启用集群能力');
        }
        const context = window.GmsWorkspace?.update({
            scope_mode: wasEnabled ? 'single' : 'cluster',
            worker_id: wasEnabled ? workspaceLocalWorkerId() : (window.GmsWorkspace?.get?.().worker_id || workspaceLocalWorkerId()),
            device_ids: []
        }, {source: 'cluster-toggle'});
        const enabled = context?.scope_mode === 'cluster';
        applyClusterMode(enabled);
        if (enabled) {
            await loadClusterWorkers().catch(error => debugLog('[Cluster] Worker list unavailable:', error));
            showToast('已切换到集群模式', 'success');
        } else {
            showToast('已切换到单机模式', 'success');
        }
        // 重新加载设备列表以反映模式变化
        await Promise.all([loadDevices(true), loadTestSuites(true)]);
        // 模式切换时同步刷新报告列表的 Worker 过滤器
        if (typeof loadTestReports === 'function') {
            loadTestReports(currentUserFilter).catch(() => {});
        }

    } catch (error) {
        showToast(`切换模式失败: ${error.message}`, 'error');
    } finally {
        if (row) { row.style.pointerEvents = ''; row.style.opacity = ''; }
    }
}

window.toggleClusterMode = toggleClusterMode;

let _initializeClusterModeInFlight = null;
let _clusterModeInitialized = false;
async function initializeClusterMode() {
    if (_clusterModeInitialized) {
        const context = window.GmsWorkspace?.get?.() || {};
        return Boolean(state.clusterStatus?.enabled && context.scope_mode === 'cluster');
    }
    if (_initializeClusterModeInFlight) return _initializeClusterModeInFlight;
    _initializeClusterModeInFlight = (async () => {
        try {
            const [response, context] = await Promise.all([
                fetch('/api/cluster/status', {cache: 'no-store'}),
                window.GmsWorkspace?.ready || Promise.resolve({scope_mode: 'single', worker_id: workspaceLocalWorkerId()})
            ]);
            const status = await response.json();
            state.clusterStatus = status;
            const enabled = Boolean(response.ok && status.enabled && context.scope_mode === 'cluster');
            applyClusterMode(enabled);
            if (!enabled && context.scope_mode === 'cluster') {
                window.GmsWorkspace?.update({scope_mode: 'single', worker_id: workspaceLocalWorkerId(), device_ids: []},
                    {source: 'cluster-unavailable'});
            }
            return enabled;
        } catch (error) {
            debugLog('[Cluster] Status unavailable, preserving single-host UI:', error);
            applyClusterMode(false);
            return false;
        } finally {
            _clusterModeInitialized = true;
            _initializeClusterModeInFlight = null;
        }
    })();
    return _initializeClusterModeInFlight;
}

window.addEventListener('gms:workspace-context', event => {
    const context = event.detail?.context || {};
    const previous = event.detail?.previous || {};
    const enabled = Boolean(state.clusterStatus?.enabled && context.scope_mode === 'cluster');
    const workerId = enabled ? (context.worker_id || workspaceLocalWorkerId()) : workspaceLocalWorkerId();
    applyClusterMode(enabled);
    syncWorkspaceWorkerSelectors(workerId);
    updateTestHostScopedControls(workerId);
    if (previous.scope_mode !== context.scope_mode
            && typeof currentPage !== 'undefined' && currentPage === 'devices'
            && typeof loadDevicesManagement === 'function') {
        // 模式切换后立即刷新对应设备清单。
        setTimeout(() => loadDevicesManagement().catch(error =>
            debugLog('[Devices] Scope refresh failed:', error)), 0);
    }
    const contextJobId = String(context.cluster_job_id || '');
    const contextSource = String(event.detail?.source || '');
    const reportProvenanceOnly = ['reports', 'report-analysis', 'report-download', 'test-suites', 'automation']
        .includes(String(context.origin_page || ''))
        || ['reports', 'report-analysis'].includes(contextSource);
    if (contextJobId && contextJobId !== state.clusterJobId && !reportProvenanceOnly) {
        state.clusterJobId = contextJobId;
        state.clusterEventSequence = -1;
        sessionStorage.setItem('active_cluster_job', contextJobId);
        wakeTestStatusPolling();
    }

    const previousWorker = previous.scope_mode === 'cluster'
        ? (previous.worker_id || workspaceLocalWorkerId())
        : workspaceLocalWorkerId();
    if (previousWorker !== workerId) {
        state.selectedDevices.clear();
        testSuitesCache = [];
        testSuitesWorkerId = '';
        renderTestSuitesDropdown();
    }
});

let testWorkerSwitchGeneration = 0;

async function switchTestWorker() {
    const workerId = document.getElementById('cluster-worker')?.value || workspaceLocalWorkerId();
    const switchGeneration = ++testWorkerSwitchGeneration;
    state.selectedDevices.clear();
    window.GmsWorkspace?.update({
        scope_mode: isLocalWorkspaceWorker(workerId) ? window.GmsWorkspace.get().scope_mode : 'cluster',
        worker_id: workerId,
        device_ids: []
    }, {source: 'test'});
    syncWorkspaceWorkerSelectors(workerId);
    updateTestHostScopedControls(workerId);
    // 立即清除旧主机的测试状态。旧 job 继续在后端跑，但 UI 必须切到空闲状态。
    // refreshTestStatusForWorker 会异步查询新主机状态并在有活跃测试时恢复。
    state.clusterJobId = '';
    state.clusterEventSequence = -1;
    state.testing = false;
    state.testStopping = false;
    updateTestToggleButton(false);
    refreshTestStatusForWorker(workerId);
    try {
        testSuitesCache = [];
        testSuitesWorkerId = '';
        await Promise.all([loadDevices(true), loadTestSuites(true)]);
        if (
            switchGeneration !== testWorkerSwitchGeneration
            || workspaceWorkerId() !== workerId
        ) return;
        showToast(isLocalWorkspaceWorker(workerId)
            ? '已切换到本机测试主机'
            : `已切换到 ${workerId}，发现 ${state.devices.length} 台设备`, 'success');
    } catch (error) {
        showToast(`切换 Worker 失败: ${error.message}`, 'error');
    }
}

window.switchTestWorker = switchTestWorker;

let _refreshTestStatusGeneration = 0;
async function refreshTestStatusForWorker(workerId) {
    const generation = ++_refreshTestStatusGeneration;
    try {
        const status = await apiCall('/api/test/status?logs=false');
        // 丢弃过期响应：用户可能又切换到了另一台主机
        if (generation !== _refreshTestStatusGeneration) return;
        const activeJobs = Array.isArray(status.active_jobs) ? status.active_jobs : [];
        const job = activeJobs.find(j => j.worker_id === workerId);
        if (job) {
            state.testing = true;
            state.clusterJobId = job.id;
            state.clusterEventSequence = -1;
            state.testStopping = job.status === 'stopping';
            sessionStorage.setItem('active_cluster_job', job.id);
            window.GmsWorkspace?.update(
                {cluster_job_id: job.id, attempt_id: job.attempt_id || ''},
                {source: 'worker-switch'}
            );
            updateTestToggleButton(true);
            wakeTestStatusPolling();
        } else {
            // 当前主机没有活跃测试，恢复空闲状态
            state.testing = false;
            state.testStopping = false;
            state.clusterJobId = '';
            state.clusterEventSequence = -1;
            updateTestToggleButton(false);
        }
    } catch (error) {
        debugLog('[Worker Switch] Failed to check test status:', error);
        // 请求失败时保守地保持当前状态不变，避免误清测试状态
    }
}

let deviceRefreshGeneration = 0;
const deviceRefreshFlights = new Map();

function isSelectableTestDevice(device) {
    if (typeof device === 'string') return true;
    const status = device.status || device.state || 'online';
    return !device.locked && ['online', 'available'].includes(status);
}

function isSelectableBootloaderDevice(device) {
    if (typeof device === 'string') return true;
    if (device.transport === 'adb_proxy') return false;
    const status = device.status || device.state || 'online';
    if (status === 'fastboot') {
        return !device.locked && !device.cluster_worker_id;
    }
    return !device.locked && ['online', 'available'].includes(status);
}

function isSelectableWorkspaceDevice(device) {
    if (typeof device === 'string') return true;
    const status = device.status || device.state || 'online';
    if (status === 'fastboot') {
        // 当前 GSI 直刷 Fastbootd 仅由本机 /api/burn/gsi 支持；
        // 远端 Worker 仍需从 available 状态发起。
        return !device.locked && !device.cluster_worker_id;
    }
    return !device.locked && ['online', 'available'].includes(status);
}

function selectedFastbootDeviceIds() {
    return Array.from(state.selectedDevices).filter(deviceId => {
        const device = state.devices.find(item => {
            const id = typeof item === 'string' ? item : item.device_id;
            return id === deviceId;
        });
        return device && (device.status === 'fastboot' || device.state === 'fastboot');
    });
}

function fetchDevicesForWorker(workerId, forceRefresh, source) {
    const requestKey = `${workerId}\n${forceRefresh ? 'force' : 'cached'}`;
    const existing = deviceRefreshFlights.get(requestKey);
    if (existing) return existing;

    const request = (async () => {
        if (isLocalWorkspaceWorker(workerId)) {
            const params = new URLSearchParams();
            if (forceRefresh) params.set('force_refresh', '1');
            if (source) params.set('source', source);
            const query = params.toString();
            const url = `/api/devices/list${query ? '?' + query : ''}`;
            return apiCall(url);
        }
        const response = await fetch(`/api/cluster/devices?worker_id=${encodeURIComponent(workerId)}`, {cache: 'no-store'});
        if (!response.ok) throw new Error(`Worker ${workerId} 设备加载失败 (HTTP ${response.status})`);
        const payload = await response.json();
        return (payload.devices || [])
            .filter(device => !['offline', 'unknown'].includes(device.state))
            .map(device => {
                const lockedStates = ['allocated', 'leased', 'busy', 'external_busy', 'reserved'];
                const isLocked = Boolean(device.claimed || lockedStates.includes(device.state));
                const lockLabels = {
                    'cluster-job': '测试任务',
                    'cluster-device-action': '设备操作',
                    'cluster-firmware': '固件烧写'
                };
                return {
                    ...(device.properties || {}),
                    device_id: device.id,
                    serial: device.serial,
                    worker_id: workerId,
                    cluster_worker_id: workerId,
                    transport: device.transport || 'local_usb',
                    locked: isLocked,
                    locked_by: isLocked
                        ? (lockLabels[device.claim_source_type]
                            || device.claim_source_type
                            || device.state
                            || '平台操作')
                        : '',
                    locked_by_self: false,
                    state: device.state,
                    status: device.state === 'available' ? 'online' : device.state
                };
            });
    })();
    deviceRefreshFlights.set(requestKey, request);
    const clearFlight = () => {
        if (deviceRefreshFlights.get(requestKey) === request) {
            deviceRefreshFlights.delete(requestKey);
        }
    };
    request.then(clearFlight, clearFlight);
    return request;
}

async function loadDevices(forceRefresh = false, options = {}) {
    const workerId = workspaceWorkerId();
    const generation = ++deviceRefreshGeneration;
    const silent = Boolean(options && options.silent);
    const source = (options && options.source) || 'auto';
    state.isRefreshingDevices = true;
    state.deviceRefreshPromise = fetchDevicesForWorker(workerId, forceRefresh, source);

    try {
        const devices = await state.deviceRefreshPromise;
        // 丢弃非当前 Worker 的延迟响应。
        if (generation !== deviceRefreshGeneration || workspaceWorkerId() !== workerId) {
            return state.devices;
        }
        state.devices = devices;
        // 设备列表更新后，清理选中集合里已消失的设备。否则 USB 抖动导致设备断开时，
        // 它仍残留在 state.selectedDevices 中，会被烧写/重启等批量操作一起送进后端，
        // 触发"部分设备失败"（离线那台等 fastboot 超时才暴露）。
        const currentIds = new Set(devices.map(d => typeof d === 'string' ? d : d.device_id).filter(Boolean));
        for (const id of Array.from(state.selectedDevices)) {
            if (!currentIds.has(id)) {
                state.selectedDevices.delete(id);
            }
        }
        for (const id of window.GmsWorkspace?.get?.().device_ids || []) {
            if (currentIds.has(id)) state.selectedDevices.add(id);
        }
        // 先用已有数据渲染设备，让 ADB 区立刻显示，不被分组加载阻塞
        renderDevices();
        // 首次加载时并行拉取分组定义（用于按"关注"筛选），到位后再重渲染一次
        if (!state.deviceGroupsLoaded) {
            state.deviceGroupsLoaded = true;
            loadDeviceGroups()
                .then(() => renderDevices())
                .catch((err) => debugLog('[Devices] loadDeviceGroups failed:', err));
        }

        if (!silent) {
            // 显示设备信息，包含序列号和刷新来源
            const sourceLabel = source === 'manual' ? '[手动刷新] ' : '[自动刷新] ';
            let deviceInfo = `${sourceLabel}已刷新设备列表，找到 ${devices.length} 台设备`;
            if (devices.length > 0) {
                // 支持 device_id 和 serial 两种字段名
                const serials = devices.map(d => d.device_id || d.serial || '未知').filter(s => s).join(' ');
                if (serials) {
                    deviceInfo += ` (${serials})`;
                }
            }
            addLogEntry(deviceInfo, 'info');
        }

        // 不再自动检查 USB/IP 状态，避免覆盖连接状态
        // USB/IP 状态只在连接/断开操作时更新
        return devices;
    } catch (error) {
        if (generation !== deviceRefreshGeneration || workspaceWorkerId() !== workerId) {
            return state.devices;
        }
        if (silent) {
            debugLog('[Devices] Silent refresh failed:', error.message);
        } else {
            addLogEntry('加载设备列表失败: ' + error.message, 'error');
        }
        throw error;
    } finally {
        if (generation === deviceRefreshGeneration) {
            state.isRefreshingDevices = false;
            state.deviceRefreshPromise = null;
        }
    }
}

function startBurnDeviceProtocolRefresh(deviceIds, options = {}) {
    const targets = new Set(
        (deviceIds || []).map(deviceId => String(deviceId || '').trim()).filter(Boolean)
    );
    const workerId = workspaceWorkerId();
    if (!targets.size || !isLocalWorkspaceWorker(workerId)) {
        return () => {};
    }

    const requestedInterval = Number(options.intervalMs);
    const requestedTimeout = Number(options.timeoutMs);
    const refreshDevices = typeof options.refreshDevices === 'function'
        ? options.refreshDevices
        : () => loadDevices(true, {silent: true});
    const intervalMs = Number.isFinite(requestedInterval) && requestedInterval > 0
        ? Math.max(10, requestedInterval)
        : 1500;
    const timeoutMs = Number.isFinite(requestedTimeout) && requestedTimeout > 0
        ? Math.max(intervalMs, requestedTimeout)
        : 45000;
    const deadline = Date.now() + timeoutMs;
    let stopped = false;
    let timer = null;
    let inFlight = false;

    const stop = () => {
        stopped = true;
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
    };
    const hasTargetFastbootDevice = devices => (devices || []).some(device => {
        const deviceId = typeof device === 'string'
            ? device
            : device.device_id || device.serial;
        const protocol = typeof device === 'string'
            ? ''
            : device.protocol || device.status || device.state;
        return targets.has(String(deviceId || ''))
            && String(protocol || '').toLowerCase() === 'fastboot';
    });
    const poll = async () => {
        if (stopped || inFlight) return;
        if (Date.now() >= deadline || workspaceWorkerId() !== workerId) {
            stop();
            return;
        }
        inFlight = true;
        try {
            const devices = await refreshDevices();
            if (hasTargetFastbootDevice(devices)) {
                debugLog('[Burn] Fastboot device became visible; stopping transition refresh');
                stop();
                return;
            }
        } catch (error) {
            debugLog('[Burn] Device transition refresh failed:', error.message);
        } finally {
            inFlight = false;
        }
    };

    timer = setInterval(() => void poll(), intervalMs);
    void poll();
    return stop;
}

// 测试套件管理
let testSuitesCache = [];
let _loadSuitesPromise = null;
let testSuitesWorkerId = '';

async function loadTestSuites(forceRefresh = false) {
    const requestedWorker = workspaceWorkerId();
    // 如果有正在进行的请求，等待它完成
    if (_loadSuitesPromise) {
        await _loadSuitesPromise;
        if (testSuitesWorkerId !== requestedWorker) return loadTestSuites(forceRefresh);
        return testSuitesCache;
    }

    if (!forceRefresh && testSuitesCache.length > 0 && testSuitesWorkerId === requestedWorker) {
        return testSuitesCache;
    }

    _loadSuitesPromise = (async () => {
        try {
            let response;
            if (isLocalWorkspaceWorker(requestedWorker)) {
                const url = forceRefresh ? '/api/test/suites?force_refresh=1' : '/api/test/suites';
                response = await apiCall(url);
            } else {
                const remoteResponse = await fetch(`/api/cluster/suites?worker_id=${encodeURIComponent(requestedWorker)}`,
                    {cache: 'no-store'});
                if (!remoteResponse.ok) {
                    throw new Error(`Worker ${requestedWorker} 套件加载失败 (HTTP ${remoteResponse.status})`);
                }
                const payload = await remoteResponse.json();
                response = {
                    success: true,
                    count: (payload.suites || []).length,
                    suites: (payload.suites || []).filter(suite => suite.available).map(suite => ({
                        tools_path: suite.tools_path,
                        test_type: String(suite.test_type || '').toLowerCase(),
                        version: suite.version,
                        suite_key: suite.suite_key,
                        worker_id: requestedWorker
                    }))
                };
            }

            if (response.suites) {
                if (workspaceWorkerId() !== requestedWorker) return testSuitesCache;
                testSuitesCache = response.suites || [];
                testSuitesWorkerId = requestedWorker;
                renderTestSuitesDropdown();
                if (typeof renderTestSuiteBrowserList === 'function') {
                    renderTestSuiteBrowserList();
                }
                if (response.success === false) {
                    showToast(response.warning || response.error || '测试套件主机不可用', 'warning');
                    return testSuitesCache;
                }
                debugLog('[loadTestSuites] 已加载测试套件:', response.count, '个', response.cached ? '(缓存)' : '(实时)');
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

function normalizeReportTestType(testType) {
    return String(testType || '').trim().toLowerCase().replace(/_/g, '-');
}

function tradefedResultFolderName(value) {
    const normalized = String(value || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    const name = normalized.split('/').filter(Boolean).pop() || '';
    return /^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?$/.test(name)
        ? name
        : '';
}

function findSuitePathForReport(testType, suitePath = '') {
    const normalizedSuitePath = String(suitePath || '').trim();
    if (normalizedSuitePath) {
        return normalizedSuitePath;
    }

    const normalizedType = normalizeReportTestType(testType);
    if (!normalizedType || !Array.isArray(testSuitesCache) || testSuitesCache.length === 0) {
        return '';
    }

    const exact = testSuitesCache.find(suite => normalizeReportTestType(suite.test_type) === normalizedType);
    if (exact) return exact.tools_path || '';

    const pathMatch = testSuitesCache.find(suite => {
        const path = String(suite.tools_path || '').toLowerCase();
        return path.includes(`/android-${normalizedType}-`) || path.includes(`/android-${normalizedType}/`);
    });
    return pathMatch?.tools_path || '';
}

function getReportSuiteVersion(report) {
    if (report?.suite_version) {
        return report.suite_version;
    }
    const suitePath = String(report?.suite_path || '');
    const match = suitePath.match(/android-[^/]*?-(\d+(?:\.\d+)?_r\d+)(?:\/|$)/i);
    if (match) {
        return match[1];
    }
    const versionMatch = suitePath.match(/(\d+(?:\.\d+)?_r\d+)/i);
    return versionMatch ? versionMatch[1] : '-';
}

function getReportSuiteDisplayName(report) {
    const suitePath = String(report?.suite_path || '').replace(/\\/g, '/');
    const pathName = suitePath
        .split('/')
        .find(part => /^android-(?:cts|gts|vts|sts|xts)-/i.test(part));
    if (pathName) return pathName;

    const version = getReportSuiteVersion(report);
    const type = normalizeReportTestType(report?.test_type);
    if (type && version && version !== '-') {
        return `android-${type}-${version}`;
    }
    return report?.suite_key || version || '-';
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
    // 旧实现只有 Controller 本机分享链接会省略 Worker ID，因此缺省值
    // 可以明确归属本机；远端 Worker 分享链接始终包含 worker_id。
    const workerId = params.get('worker_id') || params.get('host') || workspaceLocalWorkerId();

    if (!suitePath) {
        return null;
    }

    return {
        suitePath,
        directoryPath,
        filePath,
        workerId
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
    // 分享链接始终携带明确 Worker ID；本机链接也必须能把其他浏览器
    // 从上次保存的远端 Worker 切回 Controller。
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    const workerId = suite?.worker_id
        || testSuitesWorkerId
        || $('suite-worker-select')?.value
        || workspaceLocalWorkerId();
    params.set('worker_id', workerId);

    // Hash 内的分享参数不发送给服务器。仅恢复路径分隔符以提升可读性，
    // 其余可能改变查询参数边界的字符继续保持 URL 编码。
    const readableQuery = buildReadablePathQuery(params);
    return `${window.location.origin}${window.location.pathname}${window.location.search}#test-suites?${readableQuery}`;
}

function buildReadablePathQuery(params) {
    // Query values still encode characters that could alter parameter
    // boundaries, but path separators remain readable in copied/opened URLs.
    return params.toString().replace(/%2F/gi, '/');
}

async function initTestSuiteBrowserPage() {
    const listEl = $('suite-browser-list');
    if (listEl) {
        listEl.innerHTML = '<div class="suite-empty">正在加载...</div>';
    }

    await loadSuiteWorkerSelector();
    const routeParams = getSuiteBrowserRouteParams();
    // 在首次加载套件前先应用链接指定的 Worker，避免先按浏览器保存的
    // ats-worker-* 加载并短暂显示“测试套件不存在”。
    if (routeParams?.workerId) {
        const workerSelect = $('suite-worker-select');
        const supported = workerSelect
            && Array.from(workerSelect.options).some(opt => opt.value === routeParams.workerId);
        if (workerSelect && supported) {
            workerSelect.value = routeParams.workerId;
        } else {
            debugLog('[Suites] Shared link targets unknown worker:', routeParams.workerId);
        }
    }

    await loadSuitesForBrowserWorker(false);
    renderTestSuiteBrowserList();

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
    resumeSuiteDownloadIfNeeded();
}

let _suiteWorkerSelectorPromise = null;
async function loadSuiteWorkerSelector() {
    const select = $('suite-worker-select');
    if (!select || select.dataset.loaded === '1') return;
    if (_suiteWorkerSelectorPromise) return _suiteWorkerSelectorPromise;
    select.disabled = true;
    _suiteWorkerSelectorPromise = (async () => { try {
        const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        await (window.GmsWorkspace?.ready || Promise.resolve());
        const workspace = window.GmsWorkspace?.get?.() || {};
        const localWorkerId = workspaceLocalWorkerId();
        const saved = workspace.worker_id || localWorkerId;
        const workers = (payload.workers || []).filter(worker => worker.status !== 'offline');
        select.innerHTML = workers.map(worker =>
            `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.id)}</option>`
        ).join('');
        if (!workers.some(worker => worker.id === localWorkerId)) {
            select.insertAdjacentHTML('afterbegin', `<option value="${escapeHtml(localWorkerId)}">${escapeHtml(localWorkerId)}</option>`);
        }
        if (Array.from(select.options).some(option => option.value === saved)) select.value = saved;
        select.dataset.loaded = '1';
        select.disabled = false;
    } catch (error) {
        debugLog('[Suites] Worker selector unavailable:', error);
        select.innerHTML = `<option value="${escapeHtml(workspaceLocalWorkerId())}">${escapeHtml(workspaceLocalWorkerId())}</option>`;
        select.disabled = false;
    } })();
    try {
        await _suiteWorkerSelectorPromise;
    } finally {
        _suiteWorkerSelectorPromise = null;
    }
}

async function loadSuitesForBrowserWorker(force = false) {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    window.GmsWorkspace?.update({
        worker_id: workerId
    }, {source: 'suites'});
    syncWorkspaceWorkerSelectors(workerId);
    if (isLocalWorkspaceWorker(workerId)) {
        testSuitesWorkerId = '';
        return loadTestSuites(force);
    }
    const response = await fetch(`/api/cluster/suites?worker_id=${encodeURIComponent(workerId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('加载 Worker 套件失败');
    const payload = await response.json();
    testSuitesCache = (payload.suites || []).filter(item => item.available).map(item => ({
        tools_path: item.tools_path,
        test_type: String(item.test_type || '').toLowerCase(),
        version: item.version,
        suite_key: item.suite_key || item.tools_path,
        worker_id: workerId
    }));
    testSuitesWorkerId = workerId;
    return testSuitesCache;
}

async function switchSuiteWorker() {
    const workerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
    window.GmsWorkspace?.update({
        scope_mode: isLocalWorkspaceWorker(workerId) ? window.GmsWorkspace.get().scope_mode : 'cluster',
        worker_id: workerId, suite_key: '', suite_path: ''
    }, {source: 'suites'});
    syncWorkspaceWorkerSelectors(workerId);
    clearSuiteBrowserSelection('正在加载 Worker 套件...');
    // 立即清除旧主机的测试状态，再异步查询新主机状态。
    state.clusterJobId = '';
    state.clusterEventSequence = -1;
    state.testing = false;
    state.testStopping = false;
    updateTestToggleButton(false);
    refreshTestStatusForWorker(workerId);
    try {
        testSuitesCache = [];
        await loadSuitesForBrowserWorker(true);
        renderTestSuiteBrowserList();
        clearSuiteBrowserSelection(testSuitesCache.length ? '请选择左侧测试套件' : '此 Worker 暂无套件');
    } catch (error) {
        clearSuiteBrowserSelection(`加载失败: ${error.message}`);
    }
}

window.switchSuiteWorker = switchSuiteWorker;

async function refreshTestSuiteBrowser(preferredSuiteRoot = '') {
    await loadSuitesForBrowserWorker(true);
    renderTestSuiteBrowserList();
    const normalizedPreferredRoot = (preferredSuiteRoot || '').replace(/\/+$/, '');
    if (normalizedPreferredRoot) {
        const preferredSuite = testSuitesCache.find(suite => {
            const toolsPath = (suite.tools_path || '').replace(/\/+$/, '');
            const releasePath = (getSuiteReleasePath(suite) || '').replace(/\/+$/, '');
            return toolsPath === normalizedPreferredRoot
                || releasePath === normalizedPreferredRoot
                || toolsPath.startsWith(`${normalizedPreferredRoot}/`);
        });
        if (preferredSuite) {
            await selectTestSuiteForBrowser(preferredSuite.tools_path, '');
            return;
        }
    }

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

    debugLog('[downloadTestSuite] urlInput:', urlInput);
    debugLog('[downloadTestSuite] downloadBtn:', downloadBtn);

    if (!urlInput || !urlInput.value) {
        showToast('请输入下载地址', 'error');
        return;
    }

    const url = urlInput.value.trim();

    debugLog('[downloadTestSuite] URL:', url);

    if (downloadBtn) {
        downloadBtn.disabled = true;
        downloadBtn.textContent = '⬇️ 下载中...';
    }
    if (extractBtn) extractBtn.disabled = true;
    if (progressDiv) progressDiv.style.display = 'block';
    if (logDiv) {
        logDiv.style.display = 'block';
        logDiv.innerHTML = '';
    }

    let pollingStarted = false;

    const log = (msg) => {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ${msg}\n`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        debugLog('[downloadTestSuite] ' + msg);
    };

    debugLog('[downloadTestSuite] 开始下载：', url);

    try {
        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        if (!isLocalWorkspaceWorker(suiteWorkerId)) {
            const accepted = await apiCall('/api/cluster/suites/download', 'POST', {
                worker_id: suiteWorkerId, url
            });
            if (progressStatus) progressStatus.textContent = `正在由 ${suiteWorkerId} 下载...`;
            if (progressBar) progressBar.style.width = '10%';
            if (progressPercent) progressPercent.textContent = '10%';
            let command;
            while (true) {
                await new Promise(resolve => setTimeout(resolve, 1500));
                const status = await apiCall(`/api/cluster/commands/${encodeURIComponent(accepted.command_id)}`);
                command = status.command;
                if (['completed', 'failed', 'cancelled'].includes(command.status)) break;
            }
            if (command.status !== 'completed') throw new Error(command.error || 'Worker 下载失败');
            const downloaded = command.result || {};
            urlInput.dataset.lastArchivePath = downloaded.archive_path || '';
            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';
            log(`✅ ${suiteWorkerId} 下载完成：${downloaded.archive_path}`);
            log(`📦 文件大小：${((downloaded.file_size || 0) / 1024 / 1024).toFixed(2)} MB`);
            notifyOperationResult('测试套件下载完成', downloaded.message || '下载完成',
                'success', 'suite-download', {worker_id: suiteWorkerId, archive_path: downloaded.archive_path});
            return;
        }
        const result = await apiCall('/api/test/suites/download-url', 'POST', {
            url: url,
            save_dir: getDefaultSuitesPath()
        });
        debugLog('[downloadTestSuite] 响应结果:', result);

        if (result.success && result.task_id) {
            pollingStarted = true;
            sessionStorage.setItem('active_suite_download', JSON.stringify({
                task_id: result.task_id,
                archive_path: result.archive_path || ''
            }));
            await pollDownloadProgress(result.task_id);
        } else if (result.success) {
            log(`✅ 下载完成：${result.archive_path}`);
            log(`📦 文件大小：${(result.file_size / 1024 / 1024).toFixed(2)} MB`);

            if (progressBar) progressBar.style.width = '100%';
            if (progressPercent) progressPercent.textContent = '100%';
            if (progressStatus) progressStatus.textContent = '✅ 下载完成';

            notifyOperationResult(
                '测试套件下载完成',
                result.message || '下载完成',
                'success',
                'suite-download',
                { archive_path: result.archive_path }
            );

            await refreshTestSuiteBrowser();
        } else {
            log(`❌ 下载失败：${result.error}`);
            if (progressStatus) progressStatus.textContent = '❌ 下载失败';
            notifyOperationResult('测试套件下载失败', result.error, 'error', 'suite-download');
        }
    } catch (error) {
        console.error('[downloadTestSuite] 异常:', error);
        log(`❌ 错误：${error.message}`);
        if (progressStatus) progressStatus.textContent = '❌ 错误';
        notifyOperationResult('测试套件下载失败', error.message, 'error', 'suite-download');
    } finally {
        if (!pollingStarted) {
            if (downloadBtn) {
                downloadBtn.disabled = false;
                downloadBtn.textContent = '⬇️ 下载套件';
            }
            if (extractBtn) extractBtn.disabled = false;
        }
    }
};

async function pollTaskProgress({ statusUrl, progressBar, progressPercent, progressStatus, completedLabel, activeLabel }) {
    let lastPercent = -1;
    let lastStatus = '';
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const resp = await fetch(statusUrl);
        const result = await resp.json();
        if (!result.success) {
            throw new Error(result.error || '任务状态查询失败');
        }
        const task = result.task;
        const percent = Math.max(0, Math.min(100, Number(task.progress || 0)));
        if (progressBar && percent !== lastPercent) progressBar.style.width = `${percent}%`;
        if (progressPercent && percent !== lastPercent) progressPercent.textContent = `${percent.toFixed(1)}%`;
        const statusText = task.status === 'completed' ? completedLabel : activeLabel;
        if (progressStatus && statusText !== lastStatus) {
            progressStatus.textContent = statusText;
            lastStatus = statusText;
        }
        lastPercent = percent;
        if (task.status === 'completed') return task;
        if (task.status === 'error') throw new Error(task.error || '任务失败');
    }
}

async function pollDownloadProgress(taskId) {
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');
    const logDiv = $('suite-download-log');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const urlInput = $('suite-download-url');

    if (downloadBtn) { downloadBtn.disabled = true; downloadBtn.textContent = '⬇️ 下载中...'; }
    if (extractBtn) extractBtn.disabled = true;
    if (progressDiv) progressDiv.style.display = 'block';

    try {
        const statusUrl = `/api/test/suites/download-status/${encodeURIComponent(taskId)}`;
        const completedTask = await pollTaskProgress({
            statusUrl,
            progressBar, progressPercent, progressStatus,
            completedLabel: '✅ 下载完成',
            activeLabel: '下载中...'
        });

        const sizeMb = ((completedTask.downloaded_size || 0) / 1024 / 1024).toFixed(2);
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ✅ 下载完成：${completedTask.archive_path}\n`;
            logDiv.innerHTML += `[${time}] 📦 文件大小：${sizeMb} MB\n`;
        }
        notifyOperationResult(
            '测试套件下载完成',
            completedTask.message || '下载完成',
            'success',
            'suite-download',
            { task_id: taskId, archive_path: completedTask.archive_path }
        );
        if (urlInput) urlInput.dataset.lastArchivePath = completedTask.archive_path || '';
        await refreshTestSuiteBrowser();
    } catch (error) {
        notifyOperationResult(
            '测试套件下载失败',
            error.message,
            'error',
            'suite-download',
            { task_id: taskId }
        );
        if (progressStatus) progressStatus.textContent = `❌ ${error.message}`;
    } finally {
        sessionStorage.removeItem('active_suite_download');
        if (downloadBtn) { downloadBtn.disabled = false; downloadBtn.textContent = '⬇️ 下载套件'; }
        if (extractBtn) extractBtn.disabled = false;
    }
}

async function resumeSuiteDownloadIfNeeded() {
    const saved = sessionStorage.getItem('active_suite_download');
    if (!saved) return;
    try {
        const { task_id } = JSON.parse(saved);
        if (!task_id) return;
        const resp = await fetch(`/api/test/suites/download-status/${encodeURIComponent(task_id)}`);
        const result = await resp.json();
        if (!result.success || !result.task) {
            sessionStorage.removeItem('active_suite_download');
            return;
        }
        const task = result.task;
        if (task.status === 'completed' || task.status === 'error') {
            sessionStorage.removeItem('active_suite_download');
            return;
        }
        // Active download found — resume polling
        await pollDownloadProgress(task_id);
    } catch (e) {
        sessionStorage.removeItem('active_suite_download');
    }
}

// 显示添加本地测试套件路径弹框
window.showAddLocalSuiteDialog = function showAddLocalSuiteDialog() {
    const modal = $('add-local-suite-modal');
    if (modal) {
        ModalManager.open('add-local-suite-modal');
        const input = $('local-suite-path-input');
        if (input) {
            input.value = '';
            input.focus();
        }
    }
};

// 关闭弹框
window.closeAddLocalSuiteModal = function closeAddLocalSuiteModal() {
    ModalManager.close('add-local-suite-modal');
};

// 浏览服务器目录，选择本地测试套件目录后回填到输入框
window.browseLocalSuitePath = async function browseLocalSuitePath() {
    state.fileBrowser.mode = 'local-suite';
    state.fileBrowser.targetInputId = 'local-suite-path-input';
    state.fileBrowser.selectedFile = null;
    document.getElementById('file-browser-title').textContent = '选择测试套件目录';
    ModalManager.open('file-browser-modal');

    await loadFileDirectory(getDefaultSuitesPath());
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
    debugLog('[submitAddLocalSuite] 本地路径:', localPath);

    try {
        const result = await apiCall('/api/test/suites/add-local', 'POST', { path: localPath });
        debugLog('[submitAddLocalSuite] 响应结果:', result);

        if (result.success) {
            showToast(`添加成功：${result.message}`, 'success');
            closeAddLocalSuiteModal();
            await refreshTestSuiteBrowser();
        } else {
            showToast(`添加失败：${result.error}`, 'error');
        }
    } catch (error) {
        console.error('[submitAddLocalSuite] 异常:', error);
        showToast(`添加失败：${error.message}`, 'error');
    }
};

function deriveSuiteFolderNameFromArchivePath(archivePath) {
    const filename = (archivePath || '').split('/').pop() || '';
    const extensions = ['.tar.bz2', '.tar.gz', '.tgz', '.zip', '.tar'];
    for (const ext of extensions) {
        if (filename.endsWith(ext)) return filename.slice(0, -ext.length);
    }
    return filename.replace(/\.[^.]+$/, '') || 'test-suite';
}

window.extractTestSuite = async function extractTestSuite() {
    await showExtractSuiteModal();
};

window.showExtractSuiteModal = async function showExtractSuiteModal() {
    const urlInput = $('suite-download-url');
    const modal = $('extract-suite-modal');
    const select = $('extract-suite-archive-select');
    const pathInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    if (!modal || !select || !pathInput || !folderInput) return;

    modal.style.display = '';
    ModalManager.open('extract-suite-modal');
    select.innerHTML = '<option value="">正在加载压缩包...</option>';

    try {
        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const result = await apiCall(
            isLocalWorkspaceWorker(suiteWorkerId)
                ? '/api/test/suites/archives'
                : `/api/cluster/suites/archives?worker_id=${encodeURIComponent(suiteWorkerId)}`,
            'GET'
        );
        const archives = result.success ? (result.archives || []) : [];
        select.innerHTML = '<option value="">手动输入压缩包路径</option>' + archives.map(archive => {
            const sizeMb = ((archive.size || 0) / 1024 / 1024).toFixed(1);
            return `<option value="${escapeHtml(archive.path)}" data-folder="${escapeHtml(archive.default_dir_name || '')}">${escapeHtml(archive.name)} (${sizeMb} MB)</option>`;
        }).join('');

        const lastArchivePath = urlInput?.dataset?.lastArchivePath || '';
        const defaultPath = lastArchivePath || (archives[0]?.path || '');
        if (defaultPath) {
            pathInput.value = defaultPath;
            const option = Array.from(select.options).find(opt => opt.value === defaultPath);
            if (option) select.value = defaultPath;
        } else if (urlInput && urlInput.value) {
            pathInput.value = `${getDefaultSuitesPath()}/${urlInput.value.split('/').pop()}`;
        } else {
            pathInput.value = '';
        }
        folderInput.value = deriveSuiteFolderNameFromArchivePath(pathInput.value);
        folderInput.focus();
        folderInput.select();
    } catch (error) {
        select.innerHTML = '<option value="">手动输入压缩包路径</option>';
        showToast(`加载压缩包列表失败：${error.message}`, 'warning');
    }
};

window.closeExtractSuiteModal = function closeExtractSuiteModal() {
    ModalManager.close('extract-suite-modal');
    const modal = $('extract-suite-modal');
    if (modal) modal.style.display = 'none';
};

window.handleExtractSuiteKeydown = function handleExtractSuiteKeydown(event) {
    if (event.key === 'Escape') closeExtractSuiteModal();
    if (event.key === 'Enter') submitExtractSuite();
};

window.handleExtractArchiveSelectChange = function handleExtractArchiveSelectChange() {
    const select = $('extract-suite-archive-select');
    const pathInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    if (!select || !pathInput || !folderInput || !select.value) return;
    pathInput.value = select.value;
    folderInput.value = select.selectedOptions[0]?.dataset?.folder || deriveSuiteFolderNameFromArchivePath(select.value);
};

window.submitExtractSuite = async function submitExtractSuite() {
    const archiveInput = $('extract-suite-archive-path');
    const folderInput = $('extract-suite-folder-name');
    const downloadBtn = $('btn-download-suite');
    const extractBtn = $('btn-extract-suite');
    const submitBtn = $('btn-submit-extract-suite');
    const logDiv = $('suite-download-log');
    const progressDiv = $('suite-download-progress');
    const progressBar = $('suite-progress-bar');
    const progressPercent = $('suite-progress-percent');
    const progressStatus = $('suite-progress-status');

    try {
        const archivePath = (archiveInput?.value || '').trim();
        const folderName = (folderInput?.value || '').trim();

        if (!archivePath) {
            showToast('请选择或输入压缩包路径', 'error');
            return;
        }
        if (!folderName) {
            showToast('请输入解压后的文件夹名称', 'error');
            return;
        }

        if (extractBtn) {
            extractBtn.disabled = true;
            extractBtn.textContent = '📦 解压中...';
        }
        if (downloadBtn) downloadBtn.disabled = true;
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = '解压中...';
        }

        closeExtractSuiteModal();
        if (progressDiv) progressDiv.style.display = 'block';
        if (progressBar) progressBar.style.width = '0%';
        if (progressPercent) progressPercent.textContent = '0%';
        if (progressStatus) progressStatus.textContent = '正在解压...';

        if (logDiv) {
            logDiv.style.display = 'block';
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] 开始解压：${archivePath}\n`;
        }

        const suiteWorkerId = $('suite-worker-select')?.value || workspaceLocalWorkerId();
        const result2 = await apiCall(
            isLocalWorkspaceWorker(suiteWorkerId)
                ? '/api/test/suites/extract-start'
                : '/api/cluster/suites/extract',
            'POST',
            isLocalWorkspaceWorker(suiteWorkerId) ? {
                archive_path: archivePath,
                extract_dir: getDefaultSuitesPath(),
                target_dir_name: folderName
            } : {
                worker_id: suiteWorkerId,
                archive_path: archivePath,
                target_dir_name: folderName
            }
        );

        if (result2.success && (result2.task_id || result2.command_id)) {
            let completedTask;
            if (result2.command_id) {
                while (true) {
                    await new Promise(resolve => setTimeout(resolve, 1000));
                    const state = await apiCall(`/api/cluster/commands/${encodeURIComponent(result2.command_id)}`);
                    if (['completed', 'failed', 'cancelled'].includes(state.command.status)) {
                        if (state.command.status !== 'completed') throw new Error(state.command.error || 'Worker 解压失败');
                        completedTask = state.command.result || {};
                        break;
                    }
                }
                if (progressBar) progressBar.style.width = '100%';
                if (progressPercent) progressPercent.textContent = '100%';
                if (progressStatus) progressStatus.textContent = '✅ 解压完成';
            } else {
                const statusUrl = `/api/test/suites/extract-status/${encodeURIComponent(result2.task_id)}`;
                completedTask = await pollTaskProgress({statusUrl, progressBar, progressPercent, progressStatus,
                    completedLabel: '✅ 解压完成', activeLabel: '正在解压...'});
            }
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ✅ 解压完成：${completedTask.extracted_path}\n`;
            }
            notifyOperationResult(
                '测试套件解压完成',
                completedTask.message || '解压完成',
                'success',
                'suite-extract',
                { task_id: result2.task_id || result2.command_id, extracted_path: completedTask.extracted_path }
            );

            debugLog('[submitExtractSuite] refreshing suite browser, extracted_path:', completedTask.extracted_path);
            await refreshTestSuiteBrowser(completedTask.extracted_path || '');
        } else {
            if (logDiv) {
                const time = new Date().toLocaleTimeString();
                logDiv.innerHTML += `[${time}] ❌ 解压失败：${result2.error}\n`;
            }
            notifyOperationResult(
                '测试套件解压失败',
                result2.error,
                'error',
                'suite-extract',
                { archive_path: archivePath }
            );
        }
    } catch (error) {
        if (logDiv) {
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `[${time}] ❌ 错误：${error.message}\n`;
        }
        notifyOperationResult(
            '测试套件解压失败',
            error.message,
            'error',
            'suite-extract',
            { archive_path: archivePath }
        );
    } finally {
        if (extractBtn) {
            extractBtn.disabled = false;
            extractBtn.textContent = '📦 解压套件';
        }
        if (downloadBtn) downloadBtn.disabled = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '开始解压';
        }
    }
};

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
    clearSuiteSearchResults();

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
    window.GmsWorkspace?.update({
        worker_id: $('suite-worker-select')?.value || workspaceWorkerId(),
        suite_key: suite.suite_key || suite.tools_path,
        suite_path: suite.tools_path,
        origin_page: 'test-suites'
    }, {source: 'suites'});
    if (!options.preserveHighlight) {
        state.suiteBrowser.highlightPath = '';
    }
    if (!options.preserveSearchResults) {
        clearSuiteSearchResults();
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

function handleSuiteFileSearchKeydown(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        searchSuiteFiles();
    }
    if (event.key === 'Escape') {
        clearSuiteFileSearch();
    }
}

function clearSuiteSearchResults() {
    const resultsEl = $('suite-search-results');
    if (resultsEl) {
        resultsEl.innerHTML = '';
        resultsEl.style.display = 'none';
    }
}

function clearSuiteFileSearch() {
    const input = $('suite-file-search');
    if (input) input.value = '';
    clearSuiteSearchResults();
    state.suiteBrowser.highlightPath = '';
    setSuiteBrowserHighlightedPath('');
}

function renderSuiteSearchResults(items, query) {
    const resultsEl = $('suite-search-results');
    if (!resultsEl) return;

    if (!items.length) {
        resultsEl.innerHTML = `<div class="suite-empty" style="padding: 10px;">未找到: ${escapeHtml(query)}</div>`;
        resultsEl.style.display = 'block';
        return;
    }

    resultsEl.innerHTML = '';
    items.slice(0, 30).forEach(item => {
        const row = document.createElement('div');
        row.className = 'suite-search-result';
        row.title = item.path || item.name || '';
        row.innerHTML = `
            <span>${item.type === 'directory' ? '📁' : (item.is_apk ? '📦' : (item.is_jar ? '🫙' : '📄'))}</span>
            <div class="suite-search-result-main">
                <div class="suite-search-result-name">${escapeHtml(item.name || '-')}</div>
                <div class="suite-search-result-path">${escapeHtml([item.suite_label || '', item.path || ''].filter(Boolean).join(' · '))}</div>
            </div>
        `;
        row.addEventListener('click', () => locateSuiteSearchResult(item));
        resultsEl.appendChild(row);
    });
    resultsEl.style.display = 'block';
}

async function locateSuiteSearchResult(item) {
    if (!item || !item.path) return;
    const targetPath = item.path || '';
    const parentPath = item.type === 'directory' ? getParentSuitePath(targetPath) : getParentSuitePath(targetPath);
    state.suiteBrowser.highlightPath = targetPath;
    await selectTestSuiteForBrowser(
        item.suite_path || state.suiteBrowser.selectedSuitePath,
        parentPath,
        { preserveHighlight: true, preserveSearchResults: true }
    );
}

async function searchSuiteFilesInSuite(suite, query, limit = 30) {
    const params = new URLSearchParams({
        suite_path: suite.tools_path,
        query,
        limit: String(limit)
    });
    if (suite.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/search' : '/api/test/suites/search';
    const result = await apiCall(`${endpoint}?${params.toString()}`);
    const payload = result.data || {};
    const suiteLabel = `${String(suite.test_type || '').toUpperCase()} ${getSuiteDisplayName(suite)}`.trim();
    return (payload.items || []).map(item => ({
        ...item,
        suite_path: suite.tools_path,
        suite_label: suiteLabel
    }));
}

async function searchSuiteFiles() {
    const input = $('suite-file-search');
    const query = (input?.value || '').trim();
    if (!query) {
        showToast('请输入搜索关键词', 'warning');
        return;
    }
    if (!testSuitesCache.length) {
        await loadTestSuites();
    }
    if (!testSuitesCache.length) {
        showToast('未找到可搜索的测试套件', 'warning');
        return;
    }

    const resultsEl = $('suite-search-results');
    if (resultsEl) {
        resultsEl.innerHTML = '<div class="suite-empty" style="padding: 10px;">搜索中...</div>';
        resultsEl.style.display = 'block';
    }

    try {
        const selectedSuite = testSuitesCache.find(suite => suite.tools_path === state.suiteBrowser.selectedSuitePath);
        const orderedSuites = [
            ...(selectedSuite ? [selectedSuite] : []),
            ...testSuitesCache.filter(suite => !selectedSuite || suite.tools_path !== selectedSuite.tools_path)
        ];
        let items = [];
        for (const suite of orderedSuites) {
            items = await searchSuiteFilesInSuite(suite, query, 30);
            if (items.length) break;
        }
        renderSuiteSearchResults(items, query);
        if (items.length) {
            await locateSuiteSearchResult(items[0]);
            showToast(`找到 ${items.length} 个匹配项`, 'success');
        } else {
            showToast('未找到匹配项', 'warning');
        }
    } catch (error) {
        renderSuiteSearchResults([], query);
        showToast('搜索失败: ' + error.message, 'error');
    }
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
        const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
        if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
        const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? '/api/cluster/suites/files' : '/api/test/suites/files';
        const result = await apiCall(`${endpoint}?${params.toString()}`);
        const data = result.data || {};
        state.suiteBrowser.currentPath = data.path || '';
        // 保留解析后的套件根绝对路径，供"报告分析"等需要绝对路径的操作使用。
        state.suiteBrowser.suiteRoot = data.suite_root || '';
        renderSuiteBreadcrumb(state.suiteBrowser.currentPath);
        renderSuiteFiles(data.items || []);
    } catch (error) {
        renderSuiteFileEmpty(`加载失败: ${error.message}`);
    }
}

// Tradefed 测试结果。
window.openTestResultsModal = function openTestResultsModal() {
    if (!state.suiteBrowser.selectedSuitePath) {
        showToast('请先选择一个测试套件', 'warning');
        return;
    }
    ModalManager.open('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
    loadTestResults(true);
};

window.closeTestResultsModal = function closeTestResultsModal() {
    ModalManager.close('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
};

window.minimizeTestResultsModal = function minimizeTestResultsModal() {
    ModalManager.close('test-results-modal');
    const minimized = document.getElementById('test-results-minimized');
    const title = document.getElementById('test-results-minimized-title');
    if (title) {
        const suite = document.getElementById('test-results-modal-suite');
        title.textContent = suite ? suite.textContent.trim() : '';
    }
    if (minimized) minimized.style.display = 'flex';
};

window.restoreTestResultsModal = function restoreTestResultsModal() {
    const minimized = document.getElementById('test-results-minimized');
    if (minimized) minimized.style.display = 'none';
    ModalManager.open('test-results-modal');
};

async function loadTestResults(force = false) {
    const suitePath = state.suiteBrowser.selectedSuitePath;
    const suite = testSuitesCache.find(s => s.tools_path === suitePath);
    const listEl = $('test-results-list');
    const statusEl = $('test-results-modal-status');
    const suiteLabelEl = $('test-results-modal-suite');

    if (suiteLabelEl && suite) {
        let displayType = suite.test_type || '';
        if (displayType === 'cts-verifier') displayType = 'cts-v';
        suiteLabelEl.textContent = `· ${displayType.toUpperCase()} ${getSuiteDisplayName(suite)}`;
    }

    if (!suitePath) {
        if (listEl) listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">请先选择测试套件</div>';
        return;
    }

    if (listEl) listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">查询 tradefed list results 中...</div>';
    if (statusEl) statusEl.textContent = '正在执行 tradefed list results，可能需要数秒...';

    try {
        // 不传 tradefed_bin：让后端 find_tradefed_binary 解析绝对路径。
        // suite.binary 只是裸文件名（如 vts-tradefed），cd 到 tools 后不在
        // PATH 中无法直接执行，会触发系统 "command not found" 建议而失败。
        const payload = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? await apiCall(`/api/cluster/suites/results?${new URLSearchParams({
                worker_id: suite.worker_id, suite_path: suitePath
            })}`, 'POST')
            : await apiCall('/api/test/suites/result', 'POST', {suite_path: suitePath});
        if (!payload || !payload.success) {
            const msg = (payload && (payload.error || payload.message)) || '查询失败';
            if (listEl) listEl.innerHTML = `<div style="padding: 20px; color: var(--danger-color, #e53935); text-align: center;">查询失败: ${escapeHtml(msg)}</div>`;
            if (statusEl) statusEl.textContent = '查询失败';
            return;
        }
        renderTestResults(payload.results || [], payload.columns || []);
        if (statusEl) statusEl.textContent = `共 ${payload.count || 0} 条结果 · 点击行可跳转到对应目录`;
    } catch (error) {
        if (listEl) listEl.innerHTML = `<div style="padding: 20px; color: var(--danger-color, #e53935); text-align: center;">加载失败: ${escapeHtml(error.message || String(error))}</div>`;
        if (statusEl) statusEl.textContent = '加载失败';
    }
}

// 原始列名 → 字段渲染。不同套件列不同（CTS/GTS 多 Warning 列），按后端
// 返回的原始表头 columns 动态渲染，列名与 tradefed 输出完全一致。
const RESULT_COLUMN_RENDERERS = {
    'session': r => ({ text: escapeHtml(String(r.session ?? '-')) }),
    'pass': r => ({ text: escapeHtml(String(r.pass ?? '-')), style: 'text-align: right; color: var(--success-color, #43a047);' }),
    'fail': r => {
        const failNum = Number(r.fail) || 0;
        return { text: escapeHtml(String(r.fail ?? '-')), style: `text-align: right;${failNum > 0 ? ' color: var(--danger-color, #e53935); font-weight: 600;' : ''}` };
    },
    'warning': r => ({ text: escapeHtml(String(r.warning ?? '-')), style: 'text-align: right;' }),
    'modules complete': r => ({
        text: (r.modules || r.modules_total)
            ? `${escapeHtml(String(r.modules ?? '-'))}${r.modules_total ? ` of ${escapeHtml(String(r.modules_total))}` : ''}`
            : '<span style="color: var(--text-secondary);">-</span>',
    }),
    'result directory': r => ({
        text: r.result_directory ? `📁 ${escapeHtml(String(r.result_directory))}` : '<span style="color: var(--text-secondary);">-</span>',
    }),
    'test plan': r => ({ text: escapeHtml(String(r.test_plan ?? '-')) }),
    'device serial(s)': r => ({ text: escapeHtml(String(r.device_serial ?? '-')) }),
    'build id': r => ({ text: escapeHtml(String(r.build_id ?? '-')) }),
    'product': r => ({ text: escapeHtml(String(r.product ?? '-')) }),
};

function renderTestResults(results, columns) {
    const listEl = $('test-results-list');
    if (!listEl) return;

    if (!results.length) {
        listEl.innerHTML = '<div style="padding: 20px; color: var(--text-secondary); text-align: center;">暂无测试结果</div>';
        return;
    }

    // 若后端未返回表头，回退到默认列集（不含 Warning）。
    const cols = (columns && columns.length)
        ? columns
        : ['Session', 'Pass', 'Fail', 'Modules Complete', 'Result Directory', 'Test Plan', 'Device serial(s)', 'Build ID', 'Product'];

    // 数值列（Pass/Fail/Warning）表头右对齐，与数据 text-align:right 保持一致，
    // 否则宽列里表头左对齐、数字右对齐会错位。
    const numericCols = new Set(['pass', 'fail', 'warning']);
    const headerCells = cols.map(name => {
        const align = numericCols.has(name.toLowerCase()) ? 'right' : 'left';
        return `<th style="padding: 8px; text-align: ${align}; white-space: nowrap;">${escapeHtml(name)}</th>`;
    }).join('');

    listEl.innerHTML = `
        <table style="width: 100%; border-collapse: collapse;">
            <thead style="position: sticky; top: 0; z-index: 1;">
                <tr style="background: var(--darker-bg); border-bottom: 1px solid var(--border-color); font-size: 12px;">${headerCells}</tr>
            </thead>
            <tbody id="test-results-tbody"></tbody>
        </table>
    `;

    const tbody = $('test-results-tbody');
    results.forEach(r => {
        const tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom: 1px solid var(--border-color); cursor: pointer; font-size: 12px;';
        tr.onmouseenter = () => { tr.style.background = 'var(--hover-bg, rgba(0,0,0,0.04))'; };
        tr.onmouseleave = () => { tr.style.background = ''; };
        tr.title = r.result_directory ? `跳转到目录 results/${r.result_directory}` : '无结果目录';

        const cells = cols.map(name => {
            const renderer = RESULT_COLUMN_RENDERERS[name.toLowerCase()];
            const cell = renderer ? renderer(r) : { text: '' };
            // nowrap：每列单行显示，避免内容换行造成视觉错位。
            return `<td style="padding: 8px; white-space: nowrap; ${cell.style || ''}">${cell.text}</td>`;
        }).join('');
        tr.innerHTML = cells;

        tr.addEventListener('click', () => jumpToResultDirectory(r));
        tbody.appendChild(tr);
    });
}

async function jumpToResultDirectory(result) {
    const dir = result && result.result_directory;
    if (!dir) {
        showToast('该结果没有结果目录信息', 'warning');
        return;
    }
    // 结果目录位于套件根下的 results/<timestamp>，文件浏览器以套件根为相对根。
    const relPath = `results/${dir}`;
    closeTestResultsModal();
    // 先确保停留在当前选中套件，再跳转到结果目录并高亮。
    state.suiteBrowser.highlightPath = relPath;
    await loadSuiteBrowserDirectory(relPath);
    setSuiteBrowserHighlightedPath(relPath);
    showToast(`已跳转到 ${relPath}`, 'success');
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

    // 当前位于运行文件夹 results/<ts> 或 logs/<ts> 时，在面包屑右侧显示互跳按钮：
    // results 显示「跳到 logs」，logs 显示「跳到 results」。无论在目录内浏览多深，
    // 只要路径前缀是 results/<ts> 或 logs/<ts> 即可互跳（保留 <ts>）。
    const runKind = (parts.length >= 2 && (parts[0].toLowerCase() === 'results' || parts[0].toLowerCase() === 'logs'))
        ? parts[0].toLowerCase()
        : '';
    if (runKind) {
        const sibling = runKind === 'results' ? 'logs' : 'results';
        const sibBtn = document.createElement('button');
        sibBtn.className = 'btn-xs';
        sibBtn.textContent = `跳到 ${sibling}`;
        sibBtn.title = `跳转到 ${sibling}/${parts[1]}`;
        // 面包屑为普通块级布局，float 右靠使按钮固定在右侧。
        sibBtn.style.cssFloat = 'right';
        sibBtn.addEventListener('click', () => {
            const target = `${sibling}/${parts[1]}`;
            state.suiteBrowser.highlightPath = target;
            loadSuiteBrowserDirectory(target).then(() => {
                setSuiteBrowserHighlightedPath(target);
                showToast(`已跳转到 ${target}`, 'success');
            });
        });
        breadcrumb.appendChild(sibBtn);

        // 「retry」：跳到测试页并预填该运行的时间戳/测试类型/套件路径，
        // 与报告管理页 retry 按钮逻辑一致。与互跳按钮同处面包屑右侧。
        const retryBtn = document.createElement('button');
        retryBtn.className = 'btn-xs';
        retryBtn.style.background = 'var(--primary-color)';
        retryBtn.style.cssFloat = 'right';
        retryBtn.textContent = 'retry报告';
        retryBtn.title = '跳到测试页并预填该运行信息';
        retryBtn.addEventListener('click', () => {
            const ts = parts[1] || '';
            // 从套件路径（如 android-gts-14-R1-...）解析测试类型，归一化到
            // #test-type 下拉框的合法 value（CTS/GSI/GTS/...）。
            // 直接用 test_type 字段常因 GTS-root 等变体不匹配而填不进下拉框。
            const suitePath = state.suiteBrowser.selectedSuitePath || '';
            const m = String(suitePath).toLowerCase().match(/android-([a-z]+)/);
            const typeMap = { cts: 'CTS', gsi: 'GSI', gts: 'GTS', sts: 'STS', vts: 'VTS', apts: 'APTS' };
            const testType = (m && typeMap[m[1]]) || '';
            const selectedSuite = testSuitesCache.find(item => item.tools_path === suitePath);
            retryReportWithSuite(ts, testType, suitePath, {
                worker_id: selectedSuite?.worker_id || workspaceLocalWorkerId(),
                source_timestamp: ts
            });
        });
        breadcrumb.appendChild(retryBtn);
    }

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

function isSuiteResultsFolderPath(currentPath) {
    // 当前浏览路径位于某个 .../results 目录内（例如 "android-vts/results" 或
    // "android-vts/results/2026.06.25_10.57.05"）。
    const segs = (currentPath || '').split('/').filter(Boolean);
    return segs.some(seg => seg.toLowerCase() === 'results');
}

// item 是否为一个测试运行文件夹 results/<ts> 或 logs/<ts>——恰好两段、首段为
// results/logs。用 item 自身 path 判断（而非 currentPath），避免在
// logs/2026.06.25_10.57.05 内部对 inv_* 子文件夹也误判为运行文件夹而错误显示
// 下载/互跳按钮，导致跳转到不存在的 logs/.../results/inv_*。
function getSuiteRunFolderKind(itemPath) {
    const segs = (itemPath || '').split('/').filter(Boolean);
    if (segs.length !== 2) return '';
    const head = segs[0].toLowerCase();
    return (head === 'results' || head === 'logs') ? head : '';
}

function isSuiteLogsFolderPath(currentPath) {
    // 当前浏览路径位于某个 .../logs 目录内（例如 "android-vts/logs" 或
    // "android-vts/logs/2026.06.25_10.57.05"）。用路径段判断，避免误匹配
    // 名字里含 "logs" 的目录（如 "catalogs"）。
    const segs = (currentPath || '').split('/').filter(Boolean);
    return segs.some(seg => seg.toLowerCase() === 'logs');
}

async function analyzeSuiteLogDir(relPath) {
    // 复用现有的报告分析页与展示逻辑：切到 report-analysis 页，调用专门的
    // 日志目录分析端点，结果交给 displayReportAnalysis 渲染。
    const suitePath = state.suiteBrowser.selectedSuitePath;
    if (!suitePath) {
        showToast('请先选择测试套件', 'warning');
        return;
    }
    const folderName = (relPath || '').split('/').filter(Boolean).pop() || '日志目录';

    const sidebarItem = document.querySelector('[data-page="report-analysis"]');
    if (sidebarItem) sidebarItem.click();

    setTimeout(async () => {
        showToast(`正在分析 ${folderName} ...`, 'info');
        try {
            const suite = testSuitesCache.find(item => item.tools_path === suitePath);
            let data;
            if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) {
                const transferId = await createRemoteSuiteTransfer(relPath, true, suite);
                data = await apiCall(
                    `/api/cluster/transfers/${encodeURIComponent(transferId)}/report-analysis`,
                    'POST'
                );
            } else {
                const formData = new FormData();
                formData.append('suite_path', suitePath);
                formData.append('path', relPath || '');
                const resp = await fetch('/api/reports/analyze-log-dir', {
                    method: 'POST',
                    body: formData
                });
                data = await resp.json().catch(() => ({ success: false }));
            }
            if (!data.success) {
                notifyOperationResult('报告分析失败', data.message || data.error || '未知错误', 'error', 'report-analysis', { path: relPath });
                return;
            }
            displayReportAnalysis(data.data);
            notifyOperationResult(
                '报告分析完成',
                data.data?.report_name || folderName,
                'success',
                'report-analysis',
                { path: relPath }
            );
        } catch (e) {
            console.error('[Reports] analyzeSuiteLogDir error:', e);
            notifyOperationResult('报告分析失败', e.message, 'error', 'report-analysis', { path: relPath });
        }
    }, 300);
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
        // 下载 + 互跳 只对真正的运行文件夹 results/<ts>、logs/<ts> 显示（按 item 自身
        // path 精确判断），避免在 logs/<ts>/inv_* 这类深层子目录误显示导致跳转到
        // 不存在的 logs/.../results/inv_*。
        const runKind = !item.isParent ? getSuiteRunFolderKind(item.path || '') : '';
        const isRunnableFolder = Boolean(runKind);
        const inResults = runKind === 'results';
        const inLogs = runKind === 'logs';
        // 报告分析适用于 logs 目录树，包括 inv_* 子目录。
        const inLogsTree = !item.isParent && isSuiteLogsFolderPath(state.suiteBrowser.currentPath);

        const openBtn = document.createElement('button');
        openBtn.className = 'btn-xs';
        // results/logs 目录内的时间戳运行文件夹：首按钮为「下载」(打包整个文件夹)。
        // 其余目录（含 .. 返回行）保持「打开」。
        openBtn.textContent = isRunnableFolder ? '下载' : (item.isParent ? '返回' : '打开');
        if (isRunnableFolder) {
            openBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                downloadSuiteDir(item.path || '', item.name);
            });
        } else {
            openBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                if (!item.isParent) {
                    setSuiteBrowserHighlightedPath(item.path || '');
                }
                loadSuiteBrowserDirectory(item.path || '');
            });
        }
        actions.appendChild(openBtn);

        if (!item.isParent) {
            //   - results/<ts>: + 「logs」互跳
            //   - logs/<ts>:   + 「results」互跳 + 保留「报告分析」
            // 行体点击/双击仍可进入子目录，导航能力不丢。
            if (isRunnableFolder) {
                const sibling = inResults ? 'logs' : 'results';
                const sibBtn = document.createElement('button');
                sibBtn.className = 'btn-xs';
                sibBtn.textContent = sibling;
                sibBtn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    jumpSuiteSiblingFolder(item.path || '', sibling);
                });
                actions.appendChild(sibBtn);
            }

            if (inLogsTree) {
                const analyzeLogBtn = document.createElement('button');
                analyzeLogBtn.className = 'btn-xs';
                analyzeLogBtn.textContent = '报告分析';
                analyzeLogBtn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    setSuiteBrowserHighlightedPath(item.path || '');
                    analyzeSuiteLogDir(item.path || '');
                });
                actions.appendChild(analyzeLogBtn);
            }

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

        row.addEventListener('dblclick', () => {
            // HTML 报告双击在浏览器新标签页内联预览；其余文件仍下载。
            if (isSuiteHtmlFile(item.name)) {
                openSuiteFileInline(item.path);
            } else {
                downloadSuiteFile(item.path, item.name);
            }
        });
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

function isSuiteHtmlFile(name) {
    // 是否为可在浏览器内联预览的 HTML 文件（test_result.html 等报告）。
    return /\.(html?|htm)$/i.test(name || '');
}

function openSuiteFileInline(path) {
    // 用 inline=true 让后端返回 Content-Disposition: inline，浏览器新标签页内联渲染。
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    const params = new URLSearchParams({
        suite_path: state.suiteBrowser.selectedSuitePath,
        path,
        inline: 'true'
    });
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/download' : '/api/test/suites/download';
    window.open(`${endpoint}?${buildReadablePathQuery(params)}`, '_blank');
}

async function startRemoteSuiteExport(path, directory = false) {
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (!suite?.worker_id || isLocalWorkspaceWorker(suite.worker_id)) return false;
    const transferId = await createRemoteSuiteTransfer(path, directory, suite);
    const frame = document.getElementById('suite-download-frame') || Object.assign(document.createElement('iframe'), {
        id: 'suite-download-frame', name: 'suite-download-frame'
    });
    frame.style.display = 'none';
    if (!frame.parentNode) document.body.appendChild(frame);
    window.open(`/api/cluster/transfers/${encodeURIComponent(transferId)}/download`, frame.name);
    return true;
}

async function createRemoteSuiteTransfer(path, directory = false, suite = null) {
    suite = suite || testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (!suite?.worker_id || isLocalWorkspaceWorker(suite.worker_id)) {
        throw new Error('未选择远端 Worker 套件');
    }
    showToast(`正在从 ${suite.worker_id} 准备下载...`, 'info');
    const params = new URLSearchParams({worker_id: suite.worker_id,
        suite_path: suite.tools_path, path, directory: String(directory)});
    const created = await apiCall(`/api/cluster/suites/export?${params.toString()}`, 'POST');
    const transferId = created.transfer.id;
    window.GmsWorkspace?.update({
        worker_id: suite.worker_id,
        suite_key: suite.suite_key || suite.tools_path,
        suite_path: suite.tools_path,
        artifact_id: transferId
    }, {source: 'suite-export'});
    while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const status = await apiCall(`/api/cluster/transfers/${encodeURIComponent(transferId)}`);
        if (status.transfer.status === 'completed') break;
        if (status.transfer.status === 'failed') throw new Error(status.transfer.error || '远端导出失败');
    }
    return transferId;
}

async function downloadSuiteFile(path, filename = '') {
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    try {
        if (await startRemoteSuiteExport(path, false)) return;
    } catch (error) {
        showToast(`远端文件下载失败: ${error.message}`, 'error');
        return;
    }
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
    const suite = testSuitesCache.find(item => item.tools_path === state.suiteBrowser.selectedSuitePath);
    if (suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)) params.set('worker_id', suite.worker_id);
    const endpoint = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
        ? '/api/cluster/suites/download' : '/api/test/suites/download';
    link.href = `${endpoint}?${buildReadablePathQuery(params)}`;
    link.download = filename || path.split('/').pop() || 'download';
    link.target = frame.name;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
}

async function downloadSuiteDir(path, name = '') {
    // 后端把整个文件夹打包成 zip 流式回传（保持目录树）。复用 downloadSuiteFile
    // 的隐藏 iframe 模式，避免浏览器把流响应当作页面跳转。
    if (!state.suiteBrowser.selectedSuitePath || !path) return;
    try {
        if (await startRemoteSuiteExport(path, true)) return;
    } catch (error) {
        showToast(`远端目录下载失败: ${error.message}`, 'error');
        return;
    }
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
    link.href = `/api/test/suites/download-dir?${buildReadablePathQuery(params)}`;
    const dirSuffix = getSuiteRunFolderKind(path) ? `-${getSuiteRunFolderKind(path)}` : '';
    link.download = `${name || path.split('/').pop() || 'download'}${dirSuffix}.zip`;
    link.target = frame.name;
    link.style.display = 'none';
    document.body.appendChild(link);
    showToast(`正在打包下载 ${name || path} ...`, 'info');
    link.click();
    link.remove();
}

function jumpSuiteSiblingFolder(itemPath, sibling) {
    // 替换完整相对路径的首段，在 results 和 logs 同名目录间跳转。
    const parts = (itemPath || '').split('/').filter(Boolean);
    if (parts.length < 2) {
        showToast('无法定位同级目录', 'warning');
        return;
    }
    parts[0] = sibling;
    const target = parts.join('/');
    closeTestResultsModal();
    state.suiteBrowser.highlightPath = target;
    loadSuiteBrowserDirectory(target).then(() => {
        setSuiteBrowserHighlightedPath(target);
        showToast(`已跳转到 ${target}`, 'success');
    });
}

async function analyzeSuiteApk(path, options = {}) {
    if (!state.suiteBrowser.selectedSuitePath || !path) return;

    try {
        showToast('正在准备反编译任务...', 'info');
        const suite = testSuitesCache.find(item =>
            item.tools_path === state.suiteBrowser.selectedSuitePath);
        const result = suite?.worker_id && !isLocalWorkspaceWorker(suite.worker_id)
            ? await (async () => {
                const transferId = await createRemoteSuiteTransfer(path, false, suite);
                return apiCall(
                    `/api/cluster/transfers/${encodeURIComponent(transferId)}/apk-analysis`,
                    'POST'
                );
            })()
            : await apiCall('/api/test/suites/apk/analyze', 'POST', {
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
        window.GmsWorkspace?.update({
            worker_id: suite?.worker_id || workspaceLocalWorkerId(),
            suite_key: suite?.suite_key || suite?.tools_path || '',
            suite_path: suite?.tools_path || '',
            artifact_id: task.transfer_id || '',
            origin_page: 'apk-analysis'
        }, {source: 'suite-apk-analysis'});
        setApkUploadEmpty(false);
        const pendingOpenPaths = Array.from(new Set([
            options.openSourcePath,
            options.openFallbackSourcePath
        ].filter(Boolean)));
        window.apkPendingOpenTarget = pendingOpenPaths.length ? {
            filePath: pendingOpenPaths[0],
            fallbackPaths: pendingOpenPaths.slice(1),
            line: Number(options.openSourceLine || 0) || null
        } : null;

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


// 防抖版本的刷新函数
const debouncedRefreshDevices = debounce(() => loadDevices(false), 500);
const debouncedRefreshUsers = debounce(() => loadUsers(false), 500);

function renderDevices() {
    debugLog('[renderDevices] Called, state.devices:', state.devices);
    const leftContainer = document.getElementById('device-list-left');
    const rightContainer = document.getElementById('device-list-right');
    const deviceCanvas = document.getElementById('device-canvas');

    debugLog('[renderDevices] Containers:', { leftContainer: !!leftContainer, rightContainer: !!rightContainer, deviceCanvas: !!deviceCanvas });

    // Early return if containers not ready
    if (!leftContainer || !rightContainer || !deviceCanvas) {
        console.warn('[renderDevices] Early return: containers not ready');
        return;
    }

    if (state.devices.length === 0) {
        // 先加居中 class 再渲染消息，避免分两步布局导致空态提示先出现在
        // 左栏顶部、再被 class 拉到正中间的视觉跳变。
        rightContainer.innerHTML = '';
        deviceCanvas.classList.add('device-canvas-empty');
        leftContainer.innerHTML = '<div class="empty-message">点击刷新按钮获取设备列表...</div>';
        syncLocalUsbActionButtons();
        return;
    }

    deviceCanvas.classList.remove('device-canvas-empty');

    // 设备统一放入响应式网格，由可用宽度自动决定一至三列。
    // ADB 区按"关注"筛选：开启且有关注分组时，只显示属于任一关注分组的设备
    const followedIds = new Set(
        (state.deviceGroups || []).filter(g => g.followed).flatMap(g => g.device_ids || [])
    );
    const visibleDevices = (state.followFilter && followedIds.size > 0)
        ? state.devices.filter(d => {
            const id = typeof d === 'string' ? d : d.device_id;
            return followedIds.has(id);
        })
        : state.devices;

    const deviceInfos = [];
    visibleDevices.forEach(device => {
        // Handle both string device IDs and device objects
        const deviceId = typeof device === 'string' ? device : device.device_id;
        const isLocked = typeof device === 'object' && device.locked;
        const lockedBy = typeof device === 'object' ? device.locked_by : '';
        const status = typeof device === 'object'
            ? (device.status || device.state || 'online')
            : 'online';
        const selectable = isSelectableWorkspaceDevice(device);
        const transport = typeof device === 'object'
            ? (device.transport || 'local_usb')
            : 'local_usb';
        const adbProxySourceWorkerId = typeof device === 'object'
            ? (device.adb_proxy_source_worker_id || '')
            : '';
        const adbProxyTargetWorkerId = typeof device === 'object'
            ? (device.cluster_worker_id || device.worker_id || '')
            : '';
        const isUsbip = typeof device === 'object'
            && (device.is_usbip === true || transport === 'usbip');
        const usbipSourceHost = typeof device === 'object'
            ? (device.usbip_source_host || device.source || '')
            : '';
        const displaySerial = typeof device === 'object'
            ? (device.adb_proxy_source_serial || device.serial || deviceId)
            : deviceId;

        deviceInfos.push({
            deviceId, isLocked, lockedBy, status, selectable,
            transport, adbProxySourceWorkerId, adbProxyTargetWorkerId,
            isUsbip, usbipSourceHost, displaySerial
        });
    });

    // 使用DocumentFragment优化DOM操作
    // 容器统一使用事件委托。
    const renderDeviceItem = (info) => buildDeviceItemEl(info);

    // 旧的左右栏 ID 保持不变以兼容现有页面选择器；主栏承载响应式网格。
    const deviceFragment = document.createDocumentFragment();
    deviceInfos.forEach(deviceInfo => {
        deviceFragment.appendChild(renderDeviceItem(deviceInfo));
    });
    leftContainer.innerHTML = '';
    leftContainer.appendChild(deviceFragment);
    rightContainer.innerHTML = '';

    // 按 data 属性初始化一次事件委托。
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
    syncLocalUsbActionButtons();
}

// 构建单个设备项 DOM（renderDevices 奇偶分栏与分组视图共用）
function buildDeviceItemEl({
    deviceId,
    isLocked,
    lockedBy,
    status = 'online',
    selectable = true,
    transport = 'local_usb',
    adbProxySourceWorkerId = '',
    adbProxyTargetWorkerId = '',
    isUsbip = false,
    usbipSourceHost = '',
    displaySerial = ''
}) {
    const div = document.createElement('div');
    const isSelected = state.selectedDevices.has(deviceId);
    div.className = `device-item ${isSelected ? 'selected' : ''} ${isLocked ? 'locked' : ''}`;
    div.dataset.deviceId = deviceId;
    if (!selectable) div.dataset.locked = 'true';
    const adbProxyTargetHint = adbProxyTargetWorkerId
        ? `；接入：${adbProxyTargetWorkerId}`
        : '';
    const usbipSource = String(usbipSourceHost || '').split('@').pop() || '来源未知';
    const lockHint = isLocked ? `；占用：${lockedBy}` : '';
    div.title = transport === 'adb_proxy'
        ? `ADB Proxy远程设备，来源：${adbProxySourceWorkerId || '未知'}${adbProxyTargetHint}；可执行ADB/测试，不能执行Fastboot、锁定或烧写${lockHint}`
        : isUsbip
        ? `USB/IP远程设备，来源：${usbipSource}${lockHint}`
        : isLocked
        ? `已被 ${lockedBy} 占用`
        : status === 'fastboot'
        ? 'Fastboot/Fastbootd 设备仅可用于 GSI 烧写'
        : selectable ? '点击选择设备' : `设备当前处于 ${status} 状态`;

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'device-checkbox';
    checkbox.checked = isSelected;
    if (!selectable) checkbox.disabled = true;

    const info = document.createElement('div');
    info.className = 'device-info';
    const idDiv = document.createElement('div');
    idDiv.className = 'device-id';
    idDiv.textContent = displaySerial || deviceId;
    info.appendChild(idDiv);
    if (transport === 'adb_proxy') {
        const sourceStatus = document.createElement('div');
        sourceStatus.className = 'device-source';
        const source = adbProxySourceWorkerId || '来源未知';
        sourceStatus.textContent = `ADB · ${source}`;
        info.appendChild(sourceStatus);
    } else if (isUsbip) {
        const sourceStatus = document.createElement('div');
        sourceStatus.className = 'device-source';
        sourceStatus.textContent = `USB/IP · ${usbipSource}`;
        info.appendChild(sourceStatus);
    }
    const statusEl = document.createElement('span');
    statusEl.className = 'device-status';
    const displayStatus = String(status || '').toLowerCase() === 'fastboot'
        ? 'Fastboot'
        : String(status || '').toLowerCase() === 'unauthorized'
        ? '未授权'
        : isLocked ? '已分配' : selectable ? '可用' : status;
    statusEl.textContent = isLocked && displayStatus !== '已分配'
        ? `${displayStatus} · 已占用`
        : displayStatus;

    div.appendChild(checkbox);
    div.appendChild(info);
    div.appendChild(statusEl);
    return div;
}

// 加载分组定义（GET /api/device-groups）
async function loadDeviceGroups() {
    try {
        const res = await apiCall('/api/device-groups', 'GET');
        state.deviceGroups = res?.data?.groups || [];
    } catch (e) {
        debugLog('[loadDeviceGroups] error:', e);
        state.deviceGroups = [];
    }
    syncFollowFilterBtn();
}

// 主页 ADB 区"只看关注"开关
function toggleFollowFilter() {
    state.followFilter = !state.followFilter;
    localStorage.setItem('gms_follow_filter', state.followFilter ? '1' : '0');
    syncFollowFilterBtn();
    renderDevices();
}

function syncFollowFilterBtn() {
    const btn = $('btn-follow-filter');
    if (!btn) return;
    const hasFollowed = (state.deviceGroups || []).some(g => g.followed);
    btn.classList.toggle('active', state.followFilter && hasFollowed);
    btn.disabled = !hasFollowed;
    btn.title = hasFollowed
        ? (state.followFilter ? '当前只显示关注分组的设备，点击显示全部' : '点击只显示关注分组的设备')
        : '请先在设备管理页"关注"一个分组';
}
window.toggleFollowFilter = toggleFollowFilter;

// 设备分组的交互逻辑（视图切换/筛选/弹框/自动分组）由设备管理页面提供，
// 以下函数仅供设备管理页的 allDevices 表格使用。

function toggleDevice(deviceId) {
    const device = state.devices.find(item => {
        const id = typeof item === 'string' ? item : item.device_id;
        return id === deviceId;
    });
    if (device && !isSelectableWorkspaceDevice(device)) {
        showToast(`设备 ${deviceId} 当前不可选择`, 'warning');
        return;
    }
    if (state.selectedDevices.has(deviceId)) {
        state.selectedDevices.delete(deviceId);
    } else {
        state.selectedDevices.add(deviceId);
    }
    window.GmsWorkspace?.update({device_ids: Array.from(state.selectedDevices)}, {source: 'test'});
    renderDevices();
}

async function refreshDevices() {
    // 手动刷新时强制绕过缓存，并标记来源为手动
    await loadDevices(true, {source: 'manual'});
    showToast('正在刷新设备列表...', 'info');
}

function selectAllDevices() {
    const selectableDevices = state.devices.filter(isSelectableTestDevice);
    const selectableIds = selectableDevices.map(
        device => typeof device === 'string' ? device : device.device_id
    );
    if (
        selectableIds.length > 0
        && selectableIds.every(deviceId => state.selectedDevices.has(deviceId))
    ) {
        // Deselect all
        state.selectedDevices.clear();
    } else {
        // Select all - skip locked devices and non-ADB protocol states.
        let selectedCount = 0;
        let skippedUnavailable = 0;

        state.devices.forEach(device => {
            // Extract device_id from object or use string directly
            const deviceId = typeof device === 'string' ? device : device.device_id;
            const deviceObj = typeof device === 'string' ?
                state.devices.find(d => d.device_id === deviceId) : device;

            // 锁定设备以及 Fastboot 等非 ADB 可用状态均不可选。
            if (deviceObj && !isSelectableTestDevice(deviceObj)) {
                skippedUnavailable++;
                debugLog(`[SelectAll] Skipping unavailable device: ${deviceId} (${deviceObj.status || deviceObj.state || deviceObj.locked_by})`);
            } else {
                state.selectedDevices.add(deviceId);
                selectedCount++;
            }
        });

        if (skippedUnavailable > 0) {
            showToast(`跳过 ${skippedUnavailable} 台锁定或非 ADB 可用设备`, 'warning');
            addLogEntry(`全选设备：已选择 ${selectedCount} 台，跳过 ${skippedUnavailable} 台不可用设备`, 'warning');
        }
    }
    window.GmsWorkspace?.update({device_ids: Array.from(state.selectedDevices)}, {source: 'test'});
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
        const workerId = selectedClusterWorker();
        await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/reboot', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'reboot'}
                     : {devices: Array.from(state.selectedDevices)});
        addLogEntry(`正在重启 ${state.selectedDevices.size} 台设备...`, 'info');
        showToast('设备正在重启', 'success');
        if (state.usbipConnected) {
            scheduleUsbipReconnect('USB/IP 设备正在重启');
        }
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
        const workerId = selectedClusterWorker();
        if (workerId) {
            await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'remount'
            });
        } else {
            await callDeviceApi('/api/devices/remount');
        }
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
    // 预填 config.wifi 的默认 SSID/密码（管理员在 /api/config/read 中拿到明文密码）
    const wifi = state.config?.wifi || {};
    const ssidInput = document.getElementById('wifi-ssid');
    const pwdInput = document.getElementById('wifi-password');
    if (ssidInput) ssidInput.value = wifi.ssid || '';
    if (pwdInput) {
        pwdInput.value = wifi.password || '';
        pwdInput.placeholder = '请输入 Wi-Fi 密码';
        pwdInput.onfocus = null;
        delete pwdInput.dataset.savedPassword;
    }
    ModalManager.open('wifi-modal');
}

function closeWifiModal() {
    ModalManager.close('wifi-modal');
}

async function submitWifiConfig() {
    const ssid = document.getElementById('wifi-ssid').value.trim();
    const password = document.getElementById('wifi-password').value.trim();

    if (!ssid) {
        showToast('SSID 不能为空', 'error');
        return;
    }
    if (!password) {
        showToast('密码不能为空', 'error');
        return;
    }

    try {
        // 立即关闭模态框
        closeWifiModal();

        addLogEntry(`正在连接 Wi-Fi (${ssid})...`, 'info');
        showToast('正在连接 Wi-Fi...', 'info');

        const workerId = selectedClusterWorker();
        await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/wifi', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices),
                        action: 'wifi', ssid, password}
                     : {devices: Array.from(state.selectedDevices), ssid, password});

        addLogEntry(`Wi-Fi 连接命令已发送 (${ssid})`, 'success');
    } catch (error) {
        addLogEntry('连接 WiFi 失败: ' + error.message, 'error');
    }
}

async function lockSelectedDevices(action) {
    if (!validateBootloaderDeviceSelection()) return;

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
        const granted = await requestElevatedAccess(`${actionText}设备 Bootloader`);
        if (!granted) return;
        addLogEntry(`正在${actionText}设备...`, 'info');
        const workerId = selectedClusterWorker();
        let result;
        if (workerId) {
            result = await apiCall('/api/cluster/devices/actions', 'POST', {
                worker_id: workerId, devices: Array.from(state.selectedDevices),
                action: action === 'lock' ? 'bootloader_lock' : 'bootloader_unlock'
            });
        } else {
            result = await apiCall(`/api/devices/bootloader-${action}`, 'POST', {
                devices: Array.from(state.selectedDevices)
            });
        }
        const operationResults = result?.data?.results || result?.results || [];
        const failedResults = operationResults.filter(item => !item.success);
        if (result?.success === false || failedResults.length > 0) {
            const detail = failedResults.map(
                item => `${item.device}: ${item.error || item.output || '未知错误'}`
            ).join('; ');
            throw new Error(result?.error || detail || `设备${actionText}失败`);
        }
        addLogEntry(`设备${actionText}完成`, 'info');
        // 解锁/锁定后设备会重启并经历 fastboot→正常启动的状态转换，
        // 轮询刷新直到设备重新上线，避免界面停留在旧状态。
        loadDevices(true).catch(() => {});
        startBurnDeviceProtocolRefresh(Array.from(state.selectedDevices));
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
        const workerId = selectedClusterWorker();
        const result = await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/bootloader-status', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'bootloader_status'}
                     : {devices: Array.from(state.selectedDevices)});
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
        const workerId = selectedClusterWorker();
        const result = await apiCall(workerId ? '/api/cluster/devices/actions' : '/api/devices/info', 'POST',
            workerId ? {worker_id: workerId, devices: Array.from(state.selectedDevices), action: 'get_properties'}
                     : {devices: Array.from(state.selectedDevices)});
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
// 使用会话密码发送 body；401 时提示输入并重试一次。
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
    if (response.status === 401) {
        const message = data.error || '连接远端固件主机认证失败';
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
    tbody.innerHTML = '<tr><td colspan="5" style="padding: 14px; color: var(--text-secondary); text-align: center;">加载中...</td></tr>';
    try {
        const result = await firmwareShareApi('/api/firmware-shares');
        const records = result.data?.records || [];
        if (!records.length) {
            tbody.innerHTML = '<tr><td colspan="5" style="padding: 14px; color: var(--text-secondary); text-align: center;">暂无共享固件</td></tr>';
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
    } catch (error) {
        tbody.innerHTML = `<tr><td colspan="5" style="padding: 14px; color: var(--danger-color); text-align: center;">${escapeHtml(error.message)}</td></tr>`;
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
    try {
        const granted = await requestElevatedAccess('烧写设备固件');
        if (!granted) return;
        closeFirmwareModal();
        showToast('正在烧写固件...', 'info');
        addLogEntry(`开始烧写固件: ${firmwarePath}`, 'info');

        // 立即在UI上标记设备为锁定状态
        lockDevicesInUI(devices);

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
            notifyOperationResult('固件烧写已启动', '固件上传任务已开始', 'info', 'firmware-burn');
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
                        firmware_path: firmwarePath,
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
        } else {
            const formData = new FormData();
            formData.append('firmware_path', firmwarePath);
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
    assignments.textContent = '正在读取接入状态...';
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
        assignments.textContent = '接入状态读取失败';
        message.textContent = `加载ADB接入信息失败：${error.message}`;
        addLogEntry('加载ADB接入信息失败: ' + error.message, 'error');
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
    const capable = hosts.filter(host => (
        host.adb_proxy && host.status === 'online'
    ));
    const localWorkerId = adbProxyStatus.local_worker_id || workspaceLocalWorkerId();
    // Keep an online source selectable while its last device is unplugged, so
    // the open modal can show the device again as soon as a heartbeat reports
    // the hotplug event. Targets that currently aggregate a source remain
    // excluded from becoming sources themselves.
    const sourceHosts = capable.filter(host => (
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
    if (!adbProxyStatus.cluster_enabled && capable.length < 2) {
        message.textContent = (
            '单机模式下本机ADB设备已直接可用，无需再次接入。若设备连接在另一台Ubuntu主机，'
            + '请部署Worker并启用集群模式。'
        );
    }
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
    const host = (adbProxyStatus?.hosts || []).find(item => item.worker_id === sourceId);
    const capable = (adbProxyStatus?.hosts || []).filter(item => (
        item.adb_proxy && item.status === 'online'
    ));
    const previousTarget = selection?.targetWorkerId || targetSelect.value;
    const targetHosts = existingAssignment
        ? capable.filter(item => (
            item.worker_id === existingAssignment.target_worker_id
            && !item.adb_proxy_source_only
        ))
        : capable.filter(item => (
            item.worker_id !== sourceId
            && !activeSources.has(item.worker_id)
            && !item.adb_proxy_source_only
        ));
    targetSelect.replaceChildren();
    targetHosts.forEach(item => targetSelect.append(
        new Option(adbProxyHostLabel(item), item.worker_id)
    ));
    if (!targetHosts.length) {
        targetSelect.append(new Option('没有可用的ADB接入主机', ''));
    } else {
        const preferred = existingAssignment?.target_worker_id
            || (targetHosts.some(item => item.worker_id === previousTarget)
                ? previousTarget
                : workspaceWorkerId());
        if (targetHosts.some(item => item.worker_id === preferred)) {
            targetSelect.value = preferred;
        }
    }
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
    } else if (sourceId && existingAssignment && devices.length) {
        message.textContent = (
            `该来源还有 ${devices.length} 台ADB设备可追加接入 `
            + `${existingAssignment.target_worker_id}。`
        );
    } else if (sourceId && devices.length && targetHosts.length) {
        message.textContent = `请选择要接入的ADB设备，共 ${devices.length} 台可用。`;
    } else if (assignments.length) {
        message.textContent = '当前没有剩余可接入的ADB设备；已有接入可在上方查看或断开。';
    } else if (!capable.length) {
        message.textContent = '没有在线且已安装adbproxy-rs的主机。';
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
    if (container) container.textContent = '正在刷新接入状态...';
    try {
        const selection = adbProxySelectionSnapshot();
        adbProxyStatus = await apiCall('/api/adb-forward/status', 'GET');
        renderAdbProxyAssignments();
        renderAdbProxyHosts(selection);
    } catch (error) {
        if (container) container.textContent = `刷新失败: ${error.message}`;
    }
}

function renderAdbProxyAssignments() {
    const container = document.getElementById('adb-proxy-assignments');
    if (!container) return;
    container.replaceChildren();
    const assignments = adbProxyStatus?.assignments || [];
    if (!assignments.length) {
        container.textContent = '当前没有通过adbproxy-rs接入的设备。';
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
    container.textContent = '正在读取接入状态...';
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
        state.usbipConnected = Boolean(rows.length || status.connected);
        updateUsbipButtonStatus(state.usbipConnected);
        updateUsbipAssignmentOperationButtons();
    } catch (error) {
        container.textContent = `读取USB/IP接入状态失败：${error.message}`;
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

// ==================== 设备主机密码输入 ====================
function showDevicePasswordModal(deviceHost, action = 'usbip', onSaved = null) {
    pendingUsbipDeviceHost = deviceHost || pendingUsbipDeviceHost || '';
    pendingDevicePasswordAction = action || 'usbip';
    pendingDevicePasswordRetry = typeof onSaved === 'function' ? onSaved : null;
    // 显示层用可读的主机地址；后端回退值若是裸 client_id（非 user@ip）则优先
    // 用配置里的 device_host/usbip_device_host，仍无可读值时给友好提示。
    const displayHost = (() => {
        if (deviceHost && deviceHost.includes('@')) return deviceHost;
        const cfg = state.config || {};
        const configured = cfg.usbip_device_host || cfg.device_host;
        if (configured && configured.includes('@')) return configured;
        return deviceHost || configured || '（未配置主机）';
    })();
    const hostInput = document.getElementById('device-host-display');
    hostInput.value = displayHost;
    hostInput.readOnly = pendingDevicePasswordAction === 'terminal';
    const title = document.getElementById('device-password-modal-title');
    if (title) title.textContent = pendingDevicePasswordAction === 'terminal'
        ? '主机终端 SSH 密码'
        : '设备主机 SSH 密码';
    document.getElementById('device-pswd').value = '';
    ModalManager.open('device-password-modal');
    document.getElementById('device-pswd').focus();
}

function closeDevicePasswordModal() {
    ModalManager.close('device-password-modal');
    pendingDevicePasswordRetry = null;
}

// ==================== Username Detection Modal ====================
function showUsernameDetectModal(clientIp) {
    // 登录层显示时不叠加客户端主机识别弹框。
    const authGate = document.getElementById('auth-gate');
    if (authGate && authGate.style.display === 'flex') {
        debugLog('[UsernameDetect] Skipped: platform auth-gate is showing');
        return;
    }
    document.getElementById('username-detect-ip').value = clientIp;
    document.getElementById('username-detect-username').value = '';
    document.getElementById('username-detect-password').value = '';
    ModalManager.open('username-detect-modal');
    document.getElementById('username-detect-username').focus();
}

function closeUsernameDetectModal() {
    ModalManager.close('username-detect-modal');
}

// 敏感操作管理员二次认证。

let _elevationExpiryTimer = null;

function _clearElevationExpiryTimer() {
    if (_elevationExpiryTimer) {
        clearTimeout(_elevationExpiryTimer);
        _elevationExpiryTimer = null;
    }
}

function _markElevated(elevatedUntilIso) {
    state.elevated = true;
    state.elevatedUntil = elevatedUntilIso || null;
    _clearElevationExpiryTimer();
    if (elevatedUntilIso) {
        const ms = new Date(elevatedUntilIso).getTime() - Date.now();
        if (ms > 0) {
            _elevationExpiryTimer = setTimeout(() => {
                state.elevated = false;
                state.elevatedUntil = null;
                debugLog('[Elevation] expired, admin elevation cleared');
            }, ms);
        }
    }
}

/**
 * Ensure the current session has temporary admin elevation before running a
 * sensitive action. Resolves true if already elevated or after successful
 * re-authentication; false if the user cancels or fails.
 *
 * @param {string} actionLabel - what the elevation is for (shown in the modal)
 * @returns {Promise<boolean>}
 */
let _elevationRequestPromise = null;
async function requestElevatedAccess(actionLabel = '需要管理员权限', options = {}) {
    if (!state.authRequired) {
        if (options.allowAnonymousDev) return true;
    }
    if (state.elevated) {
        try {
            const status = await fetchAuthStatus();
            if (status.elevated) {
                _markElevated(status.elevated_until);
                debugLog('[Elevation] server confirmed the active elevation');
                return true;
            }
            state.elevated = false;
            state.elevatedUntil = null;
        } catch (error) {
            debugLog('[Elevation] could not verify the active elevation', error);
            return true;
        }
    }
    if (!state.currentUser && state.authSetupRequired) {
        showAuthGate(true);
        return false;
    }
    if (_elevationRequestPromise) {
        debugLog('[Elevation] sharing the active credential prompt');
        return _elevationRequestPromise;
    }
    const modal = document.getElementById('elevate-modal');
    if (!modal) {
        showToast('提权弹框未加载，无法执行此操作', 'error');
        return false;
    }
    const labelEl = document.getElementById('elevate-action-label');
    if (labelEl) labelEl.textContent = actionLabel;
    const userEl = document.getElementById('elevate-username');
    const pwdEl = document.getElementById('elevate-password');
    const msgEl = document.getElementById('elevate-message');
    if (userEl) userEl.value = '';
    if (pwdEl) pwdEl.value = '';
    if (msgEl) msgEl.textContent = '';
    // Prefill the current username for convenience.
    if (userEl && state.currentUser?.role === 'admin' && state.currentUser.username) {
        userEl.value = state.currentUser.username;
    }

    _elevationRequestPromise = new Promise(resolve => {
        const onGranted = (elevatedUntilIso) => {
            _markElevated(elevatedUntilIso);
            ModalManager.close('elevate-modal');
            resolve(true);
        };
        const onCancel = () => {
            ModalManager.close('elevate-modal');
            resolve(false);
        };
        // Wire one-shot handlers via onClose + submit; store for cleanup.
        window._elevateResolve = { onGranted, onCancel };
        ModalManager.onClose('elevate-modal', () => {
            if (window._elevateResolve) {
                window._elevateResolve.onCancel();
                window._elevateResolve = null;
            }
        });
        ModalManager.open('elevate-modal');
        setTimeout(() => pwdEl && pwdEl.focus(), 0);
    });
    try {
        return await _elevationRequestPromise;
    } finally {
        _elevationRequestPromise = null;
    }
}

async function submitElevateForm() {
    const username = document.getElementById('elevate-username')?.value.trim() || '';
    const password = document.getElementById('elevate-password')?.value || '';
    const msgEl = document.getElementById('elevate-message');
    const submitBtn = document.querySelector('#elevate-modal .btn-primary');
    if (msgEl) msgEl.textContent = '';
    if (!username || !password) {
        if (msgEl) msgEl.textContent = '请输入管理员账号和密码';
        return;
    }
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = '验证中...'; }
    try {
        const response = await fetch('/api/auth/elevate', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        const result = await response.json().catch(() => ({ success: false, error: '响应解析失败' }));
        if (!response.ok || result.success === false) {
            if (msgEl) msgEl.textContent = result.error || result.message || '管理员凭证无效';
            return;
        }
        if (result.user) {
            state.currentUser = result.user;
            state.clientId = result.client_id || result.user.id || state.clientId;
            applyRoleBasedUiAccess();
        }
        const resolve = window._elevateResolve;
        if (resolve) {
            const until = result.elevated_until || null;
            window._elevateResolve = null;
            resolve.onGranted(until);
        }
    } catch (error) {
        if (msgEl) msgEl.textContent = error.message || '提权失败';
    } finally {
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = '提权'; }
    }
}

function cancelElevate() {
    const resolve = window._elevateResolve;
    if (resolve) {
        window._elevateResolve = null;
        resolve.onCancel();
    } else {
        ModalManager.close('elevate-modal');
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
    const localServerInput = document.getElementById('local-server');
    if (localServerInput) {
        localServerInput.value = display;
    }
}

async function saveUsernameManually(clientIp, username) {
    const response = await postJson('/api/users/set-username', {
        ip: clientIp,
        username
    });

    const savedUsername = response.username || username;
    const clientId = response.client_id || `${savedUsername}@${clientIp}`;
    state.clientDisplayId = response.display_client_id || `${savedUsername}@${clientIp}`;
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
    const deviceHost = document.getElementById('device-host-display').value.trim() || pendingUsbipDeviceHost;
    if (!password) {
        showToast('请输入密码', 'warning');
        return;
    }

    if (
        pendingDevicePasswordAction === 'sshd'
        || pendingDevicePasswordAction === 'terminal'
        || pendingDevicePasswordAction === 'usbip-list'
    ) {
        const action = pendingDevicePasswordAction;
        const retry = pendingDevicePasswordRetry;
        try {
            await apiCall('/api/config/client-ssh-credentials', 'POST', {
                device_host: deviceHost,
                password
            });
            closeDevicePasswordModal();
            showToast('SSH 凭据已保存', 'success');
            addLogEntry(`已保存 ${deviceHost} 的 SSH 凭据`, 'success');
            pendingDevicePasswordAction = 'usbip';
            if (action === 'terminal' || action === 'usbip-list') {
                if (retry) await retry();
            } else {
                addLogEntry('正在重新检查 SSHD...', 'info');
                await checkSshd();
            }
        } catch (error) {
            addLogEntry('保存 SSH 凭据失败: ' + error.message, 'error');
            showToast('保存失败: ' + error.message, 'error');
        }
        return;
    }

    try {
        // 显示正在连接的提示
        addLogEntry('正在连接 USB/IP...', 'info');
        showToast('正在连接...', 'info');

        // 立即关闭模态框
        closeDevicePasswordModal();

        const result = await apiCall('/api/usbip/connect', 'POST', {
            device_host: deviceHost,
            worker_id: activeUsbipSelection?.worker_id || '',
            busids: activeUsbipSelection?.busids || [],
            device_password: password,
            manual_connect: true
        });
        if (!isUsbipAdbReady(result)) {
            throw new Error(result.error || result.message || 'USB/IP 连接后尚未识别到 ADB 设备');
        }

        // 状态由主按钮处理。
        addLogEntry(result.message || 'USB/IP 连接已启动', 'success');
        showToast('USB/IP 连接成功', 'success');

        // 刷新设备列表（使用防抖版本）
        setTimeout(() => debouncedRefreshDevices(), 3500);

        // 主函数返回后更新按钮状态。
        const btn = $('usbip-btn');
        if (btn) {
            state.usbipConnected = true;
            pendingUsbipDeviceHost = result.device_host || deviceHost || pendingUsbipDeviceHost;
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
    if (!requireControllerHostAction('SSHD 检查')) return;
    try {
        const result = await apiCall('/api/ssh/sshd', 'GET');

        if (result.success === true && result.installed === false && result.install_guide) {
            showSshdInstallGuide(result.install_guide);
        } else if (result.running) {
            addLogEntry(`SSHD 状态: 运行中`, 'success');
        } else if (result.installed === null || result.installed === undefined) {
            addLogEntry(`SSHD 状态: 无法连接设备主机，无法判断 SSHD 是否安装`, 'warning');
        } else if (!result.installed) {
            addLogEntry(`SSHD 状态: 无法确认是否已安装`, 'warning');
        } else {
            addLogEntry(`SSHD 状态: 已安装但未运行`, 'warning');
        }

        if (result.error) {
            addLogEntry(`⚠️ ${result.error}`, 'warning');
            if (result.need_password && result.device_host) {
                showDevicePasswordModal(result.device_host, 'sshd');
            }
        }
    } catch (error) {
        if (error.needPassword && error.deviceHost) {
            addLogEntry('需要输入SSH密码以检查 ' + error.deviceHost + ' 的 SSHD 状态', 'warning');
            showDevicePasswordModal(error.deviceHost, 'sshd');
            return;
        }
        addLogEntry('检查 SSHD 失败: ' + error.message, 'error');
        try {
            const result = await apiCall('/api/ssh/sshd', 'GET');
            if (result.install_guide) {
                showSshdInstallGuide(result.install_guide);
            } else {
                addLogEntry('无法加载安装指南', 'error');
            }
        } catch (guideError) {
            addLogEntry('无法加载安装指南', 'error');
        }
    }
}

async function checkRouting() {
    if (!requireControllerHostAction('路由检查')) return;
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
                // 命令需在测试主机执行。
                // 需要通过测试主机的网关来访问客户端网段
                const testGateway = testNetwork.split('.').slice(0, 3).join('.') + '.1';

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
    if (!requireControllerHostAction('VPN 连接')) return;
    if (state.vpnConnected) {
        await checkVpnStatus();
        return;
    }

    // 直接弹出 VPN 选择框让用户选择连接哪个
    showVpnCredentialModal();
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

// ==================== VPN Credential Modal ====================
async function showVpnCredentialModal() {
    const select = document.getElementById('vpn-credential-name');

    try {
        const result = await apiCall('/api/vpn/connections', 'GET');
        select.innerHTML = '';
        (result.connections || []).forEach(name => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            select.appendChild(opt);
        });
    } catch (e) {
        select.innerHTML = '<option value="">加载失败</option>';
    }

    const modal = document.getElementById('vpn-credential-modal');
    ModalManager.open('vpn-credential-modal');
}

function closeVpnCredentialModal() {
    ModalManager.close('vpn-credential-modal');
}

function handleVpnCredentialKeyPress(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        submitVpnCredential();
    }
}

async function submitVpnCredential() {
    const vpnName = document.getElementById('vpn-credential-name').value;
    if (!vpnName) {
        showToast('请选择 VPN 连接', 'error');
        return;
    }

    const submitBtn = document.querySelector('#vpn-credential-modal .btn-primary');
    const originalText = submitBtn.textContent;
    try {
        submitBtn.textContent = '连接中...';
        submitBtn.disabled = true;

        const result = await apiCall('/api/vpn/connect', 'POST', {
            vpn_name: vpnName
        });

        if (result.connected) {
            updateVpnStatus(true);
            addLogEntry(result.message || 'VPN 已连接', 'success');
            closeVpnCredentialModal();
        } else {
            updateVpnStatus(false);
            addLogEntry(result.message || 'VPN 连接失败', 'error');
        }
    } catch (error) {
        updateVpnStatus(false);
        addLogEntry('连接 VPN 失败: ' + error.message, 'error');
    } finally {
        submitBtn.textContent = originalText;
        submitBtn.disabled = false;
    }
}

// ==================== USB/IP Status Check ====================
async function checkUsbipStatus() {
    try {
        const result = await apiCall('/api/usbip/status', 'GET');
        if (result.device_host) {
            pendingUsbipDeviceHost = result.device_host;
        }
        if (result.cluster_selection) {
            activeUsbipSelection = result.cluster_selection;
        }
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
        usbipReconnectWaiting = false;
    } else {
        btn.textContent = usbipReconnectWaiting ? '📱 等待重连...' : '📱 本地设备';
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
function getFirmwareUploadId(file) {
    const raw = `${state.clientId || 'client'}:${file.name}:${file.size}:${file.lastModified || 0}`;
    return 'fw-' + btoa(unescape(encodeURIComponent(raw))).replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 96);
}

function getReusableFirmwareUploadId(file) {
    const savedName = sessionStorage.getItem('firmwareUploadFileName');
    const savedSize = parseInt(sessionStorage.getItem('firmwareUploadFileSize') || '0');
    const savedLastModified = parseInt(sessionStorage.getItem('firmwareUploadLastModified') || '-1');
    const savedId = sessionStorage.getItem('firmwareUploadId');
    if (savedId && savedName === file.name && savedSize === file.size && savedLastModified === (file.lastModified || 0)) {
        return savedId;
    }
    return getFirmwareUploadId(file);
}

function saveFirmwareUploadState(fileName, fileSize, startTime, progress = 0, uploadedSize = 0, totalSize = 0, uploadId = '', lastModified = 0) {
    sessionStorage.setItem('firmwareUploadInProgress', 'true');
    sessionStorage.setItem('firmwareUploadFileName', fileName);
    sessionStorage.setItem('firmwareUploadFileSize', fileSize);
    sessionStorage.setItem('firmwareUploadLastModified', String(lastModified || 0));
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
    list.innerHTML = `
        <div class="file-browser-item" style="cursor: default; flex-direction: column; align-items: flex-start; gap: 6px;">
            <div style="color: var(--danger-color);">⚠️ 无法加载远端固件目录</div>
            <div style="color: var(--text-secondary); font-size: 12px;">主机 ${escapeHtml(host || '')}：${escapeHtml(message || '')}</div>
            <div style="color: var(--text-muted); font-size: 11px;">请确认主机可达且 SSH 凭据正确；若仍失败可关闭后重试。</div>
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

function selectFile(name, type, sourceEvent) {
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

        const eventSource = sourceEvent || window.event;
        if (eventSource && eventSource.currentTarget) {
            eventSource.currentTarget.classList.add('selected');
        }
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

    if (!validateDeviceSelection()) return;

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
        if ('Notification' in window && Notification.permission === 'default' && !state.browserNotificationsEnabled) {
            void requestBrowserNotificationPermission();
        }

        // 清空两个日志容器并重置计数
        const systemOut = getLogContainer('system');
        const moduleOut = getLogContainer('module');
        if (systemOut) systemOut.innerHTML = '';
        if (moduleOut) moduleOut.innerHTML = '';
        state.lastLogCount = 0;
        state.wsLogStallTicks = 0;

        const startResult = await apiCall('/api/test/start', 'POST', {
            worker_id: workspaceWorkerId(),
            devices: Array.from(state.selectedDevices),
            test_type: testType,
            test_module: testModule,
            test_case: testCase,
            retry_dir: retryResult,
            test_suite: suitePath,
            local_server: state.config?.local_server || state.clientDisplayId || state.clientId || ''
        });
        const clusterJobId = startResult?.data?.cluster_job_id || startResult?.cluster_job_id || '';
        if (clusterJobId) {
            state.clusterJobId = clusterJobId;
            state.clusterEventSequence = -1;
            sessionStorage.setItem('active_cluster_job', clusterJobId);
            window.GmsWorkspace?.update({
                worker_id: workspaceWorkerId(),
                device_ids: Array.from(state.selectedDevices),
                suite_key: suitePath,
                suite_path: suitePath,
                cluster_job_id: clusterJobId,
                attempt_id: startResult?.data?.attempt_id || startResult?.attempt_id || '',
                origin_page: 'test'
            }, {source: 'test-start'});
            addLogEntry(`分布式任务 ${clusterJobId} 已排队`, 'info');
        }

        debugLog('[startTest] API call successful, setting testing = true');
        state.testStopping = false;
        state.testing = true;
        updateTestToggleButton(true);
        addLogEntry('测试已启动', 'success');
        showToast('测试已启动', 'success');
        switchLogTab('module');
        wakeTestStatusPolling();

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

        if (state.clusterJobId) {
            await apiCall(`/api/cluster/jobs/${encodeURIComponent(state.clusterJobId)}/cancel`, 'POST');
            state.testStopping = true;
            updateTestToggleButton(true);
            addLogEntry('停止请求已发送，正在等待 Worker 结束任务...', 'warning');
            showToast('停止请求已发送', 'warning');
            wakeTestStatusPolling();
            return;
        } else {
            // 使用新的 stop 接口（支持多用户隔离）
            await apiCall('/api/test/stop', 'POST');
        }

        // Update test state
        state.testing = false;
        state.testStopping = false;
        state.clusterJobId = '';
        state.clusterEventSequence = -1;
        sessionStorage.removeItem('active_cluster_job');
        window.GmsWorkspace?.update({cluster_job_id: '', attempt_id: ''}, {source: 'test-stop'});
        updateTestToggleButton(false);

        addLogEntry('测试已停止', 'warning');
        showToast('测试已停止', 'warning');

        // Refresh devices (强制刷新以获取最新状态)
        await loadDevices(true);
    } catch (error) {
        state.testStopping = false;
        updateTestToggleButton(state.testing);
        addLogEntry('停止测试失败: ' + error.message, 'error');
    }
}

function updateTestToggleButton(isTesting) {
    const btn = $('test-toggle-btn');
    if (!btn) return;

    btn.disabled = Boolean(state.testStopping);
    if (state.testStopping) {
        btn.textContent = '⏳ 停止中';
        btn.className = 'btn-danger btn-lg';
    } else if (isTesting) {
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

    // 测试主机下拉框在测试期间也保持可用：切换主机不会中断正在运行的测试
    // （测试在后端按 clusterJobId 运行，停止操作也通过 clusterJobId 执行）。
    const workerSelect = document.getElementById('cluster-worker');
    if (workerSelect) {
        const clusterEnabled = Boolean(
            state.clusterStatus?.enabled
            && window.GmsWorkspace?.get?.().scope_mode === 'cluster'
        );
        const workersLoaded = workerSelect.dataset.workersLoaded === 'true';
        workerSelect.disabled = !clusterEnabled || !workersLoaded;
        workerSelect.title = clusterEnabled
            ? (workersLoaded
                ? '选择执行测试的 Cluster Worker'
                : '正在加载测试主机列表')
            : '当前为单机模式；切换到集群模式后可选择远端测试主机';
    }

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
        if (isLocalWorkspaceWorker(workspaceWorkerId())) {
            await apiCall('/api/test/clean', 'POST');
        }
        const systemOut = getLogContainer('system');
        const moduleOut = getLogContainer('module');
        if (systemOut) systemOut.innerHTML = '';
        if (moduleOut) moduleOut.innerHTML = '';
        addLogEntry('测试日志已清除', 'info');
        state.lastLogCount = 0;
    } catch (error) {
        addLogEntry('清除日志失败: ' + error.message, 'error');
    }
}

async function downloadTestLog() {
    try {
        addLogEntry('正在保存日志...', 'info');

        // 拼接两个日志容器的实际内容（系统日志 + 测试日志）
        const systemOut = getLogContainer('system');
        const moduleOut = getLogContainer('module');
        const systemContent = systemOut ? systemOut.innerText : '';
        const moduleContent = moduleOut ? moduleOut.innerText : '';
        const logContent = [
            systemContent.trim() ? `===== 系统日志 =====\n${systemContent.trim()}` : '',
            moduleContent.trim() ? `===== 测试日志 =====\n${moduleContent.trim()}` : '',
        ].filter(Boolean).join('\n\n');

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

        <div style="margin-top:14px;border-top:1px solid var(--border-color);padding-top:10px;">
            <div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:8px;">日志显示设置</div>
            <div class="modal-form-row">
                <label>历史日志条数:</label>
                <input type="number" id="config-log-history-limit" min="10" max="2000"
                       value="${localStorage.getItem('gms-log-history-limit') || 100}" />
            </div>
            <div class="modal-form-row">
                <label>实时日志上限:</label>
                <input type="number" id="config-log-max-entries" min="50" max="10000"
                       value="${localStorage.getItem('gms-log-max-entries') || 1000}" />
            </div>
            <small style="color:var(--text-secondary);">历史日志：首次加载状态时显示的条数；实时日志：页面每个Tab最多保留的条数。保存后即时生效。</small>
        </div>
        </form>

        <!-- 客户端 SSH 凭据管理（增删独立于上方静态配置，即时生效） -->
        <div class="modal-form-row" style="flex-direction:column;align-items:stretch;margin-top:14px;">
            <label style="margin-bottom:6px;">客户端 SSH 凭据:</label>
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <thead>
                    <tr style="background: var(--darker-bg);">
                        <th style="padding:5px 6px;text-align:left;">设备主机</th>
                        <th style="padding:5px 6px;text-align:left;">用户名</th>
                        <th style="padding:5px 6px;text-align:left;">密码</th>
                        <th style="padding:5px 6px;text-align:center;">操作</th>
                    </tr>
                </thead>
                <tbody id="client-creds-table-body">
                    <tr><td colspan="4" style="padding:20px;text-align:center;">加载中...</td></tr>
                </tbody>
            </table>
            <div class="modal-form-row" style="margin-top:8px;gap:6px;">
                <input type="text" id="client-cred-host" placeholder="user@ip，例如 gms@192.168.1.100" autocomplete="off" style="flex:2;" />
                <input type="password" id="client-cred-password" placeholder="SSH 密码" autocomplete="new-password" style="flex:1.5;" />
                <button class="btn-xxs btn-primary" onclick="addClientCredential()">添加/更新</button>
            </div>
            <small style="color:var(--text-secondary);margin-top:4px;">填入已有主机的地址+新密码可覆盖更新；密码不会回显明文。</small>
        </div>
    `;

    ModalManager.open('config-modal');
    const footer = document.getElementById('config-modal-footer');
    if (footer) footer.style.display = '';
    loadClientCredentials();
}

async function showGmsAssistantConfig() {
    const input = document.getElementById('gms-assistant-url');
    if (!input) return;
    try {
        const result = await apiCall('/api/config/external-services', 'GET');
        const data = result?.data || result || {};
        input.value = String(data.gms_assistant_url || '').trim();
        ModalManager.open('gms-assistant-config-modal');
    } catch (error) {
        showToast('读取 GMS助手配置失败: ' + error.message, 'error');
    }
}

async function saveGmsAssistantConfig() {
    const input = document.getElementById('gms-assistant-url');
    const url = String(input?.value || '').trim();
    if (url && !/^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(url)) {
        showToast('请输入完整的 http(s) 地址', 'warning');
        return;
    }
    try {
        await apiCall('/api/config/external-services', 'POST', {gms_assistant_url: url});
        ModalManager.close('gms-assistant-config-modal');
        showToast('GMS助手配置已保存', 'success');
        const frame = document.getElementById('gms-assistant-frame');
        const dataSrc = frame?.getAttribute('data-src');
        if (frame && dataSrc) {
            frame.removeAttribute('src');
            setTimeout(() => frame.setAttribute('src', dataSrc), 0);
        }
    } catch (error) {
        showToast('保存 GMS助手配置失败: ' + error.message, 'error');
    }
}

// 客户端 SSH 凭据管理。
async function loadClientCredentials() {
    const tbody = document.getElementById('client-creds-table-body');
    if (!tbody) return;
    let credentials = [];
    try {
        const result = await apiCall('/api/config/client-ssh-credentials', 'GET');
        credentials = (result && (result.credentials || result.data?.credentials)) || [];
    } catch (error) {
        renderClientCredentialsMessage(tbody, `加载失败: ${error.message}`, 'var(--error-color)');
        return;
    }

    if (!credentials.length) {
        renderClientCredentialsMessage(tbody, '暂无凭据。在下方添加设备主机 SSH 凭据。', 'var(--text-secondary)');
        return;
    }

    tbody.replaceChildren();
    credentials.forEach(cred => {
        const host = cred.device_host || `${cred.username || ''}@${cred.host || ''}`.replace(/^@$/, '');
        const masked = cred.has_password ? '••••••' : '未设置';
        const row = document.createElement('tr');
        row.style.borderBottom = '1px solid var(--border-color)';

        appendClientCredentialCell(row, host || '-');
        appendClientCredentialCell(row, cred.username || '-');
        appendClientCredentialCell(row, masked, 'var(--text-secondary)');

        const actionCell = appendClientCredentialCell(row, '');
        actionCell.style.textAlign = 'center';
        const deleteButton = document.createElement('button');
        deleteButton.className = 'btn-xxs';
        deleteButton.type = 'button';
        deleteButton.textContent = '删除';
        deleteButton.addEventListener('click', () => deleteClientCredential(host));
        actionCell.appendChild(deleteButton);

        tbody.appendChild(row);
    });
}

function renderClientCredentialsMessage(tbody, message, color) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.style.padding = '20px';
    cell.style.textAlign = 'center';
    if (color) cell.style.color = color;
    cell.textContent = message;
    row.appendChild(cell);
    tbody.replaceChildren(row);
}

function appendClientCredentialCell(row, text, color) {
    const cell = document.createElement('td');
    cell.style.padding = '4px 6px';
    if (color) cell.style.color = color;
    cell.textContent = text;
    row.appendChild(cell);
    return cell;
}

async function addClientCredential() {
    const hostInput = document.getElementById('client-cred-host');
    const pwdInput = document.getElementById('client-cred-password');
    const deviceHost = (hostInput?.value || '').trim();
    const password = pwdInput?.value || '';

    if (!/^[^@\s<>"'`]+@[^@\s<>"'`]+$/.test(deviceHost)) {
        showToast('请输入 user@ip 格式的设备主机', 'warning');
        return;
    }
    if (!password) {
        showToast('请输入密码', 'warning');
        return;
    }

    try {
        await apiCall('/api/config/client-ssh-credentials', 'POST', { device_host: deviceHost, password });
        showToast('凭据已保存', 'success');
        if (hostInput) hostInput.value = '';
        if (pwdInput) pwdInput.value = '';
        await loadClientCredentials();
    } catch (error) {
        showToast('保存凭据失败: ' + error.message, 'error');
    }
}

async function deleteClientCredential(deviceHost) {
    if (!deviceHost) return;
    if (!await showConfirmDialog(
        '删除 SSH 凭据',
        `确认删除凭据：${deviceHost}？`
    )) return;
    try {
        await apiCall('/api/config/client-ssh-credentials', 'DELETE', { device_host: deviceHost });
        showToast('凭据已删除', 'success');
        await loadClientCredentials();
    } catch (error) {
        showToast('删除凭据失败: ' + error.message, 'error');
    }
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

    // Save UI log settings to localStorage (frontend-only)
    const historyLimit = parseInt(document.getElementById('config-log-history-limit')?.value) || 100;
    const maxEntries = parseInt(document.getElementById('config-log-max-entries')?.value) || 1000;
    localStorage.setItem('gms-log-history-limit', String(historyLimit));
    localStorage.setItem('gms-log-max-entries', String(maxEntries));

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
// 批量合并日志，减少 DOM 更新。
const _logQueue = [];
let _logFlushScheduled = false;

// 返回系统或模块日志容器。
function getLogContainer(source = 'system') {
    return document.getElementById(`${source === 'module' ? 'module' : 'system'}-log-output`);
}

const MODULE_LOG_PATTERNS = [
    /\b(?:CTS|VTS|GTS|STS|Tradefed|TradeFed|Compatibility Console|Invocation)\b/,
    /\b(?:ModuleListener|PrettyTestEventLogger|TestRunner|TestInvocation|ITestInvocationListener)\b/,
    /\b(?:testRunStarted|testRunEnded|testStarted|testEnded|testFailed|testIgnored|IGNORED|ASSUMPTION_FAILURE)\b/,
    /\b(?:PASSED|FAILED)\b/,
    /\[[0-9]+\/[0-9]+\]\s+\S+\s+\S+#\S+/
];

function inferLogSource(message, explicitSource) {
    if (explicitSource === 'module' || explicitSource === 'system') {
        return explicitSource;
    }

    const text = String(message || '');
    return MODULE_LOG_PATTERNS.some(pattern => pattern.test(text)) ? 'module' : 'system';
}

function normalizeLogEntry(log) {
    const isObject = log && typeof log === 'object';
    const message = isObject
        ? (log.msg || log.message || log.log || '')
        : String(log || '');
    const cleanedMessage = String(message).replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '');

    return {
        message: cleanedMessage,
        type: isObject ? (log.type || log.log_type || 'info') : 'info',
        source: inferLogSource(cleanedMessage, isObject ? log.source : undefined)
    };
}

function addNormalizedLogEntry(log) {
    const entry = normalizeLogEntry(log);
    addLogEntry(entry.message, entry.type, true, entry.source);
}

function getLogDisplayLimit() {
    return parseInt(localStorage.getItem('gms-log-history-limit')) || 100;
}
function getLogMaxEntries() {
    return parseInt(localStorage.getItem('gms-log-max-entries')) || 1000;
}

function addLogEntry(message, type = 'info', showTimestamp = true, source = 'system') {
    // Queue the log entry
    _logQueue.push({
        message,
        type,
        showTimestamp,
        source: source === 'module' ? 'module' : 'system',
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false })
    });

    // 限制队列大小，避免 WebSocket 突发日志占满内存。
    if (_logQueue.length > 500) _logQueue.splice(0, _logQueue.length - 500);

    // Schedule a flush if not already scheduled
    if (!_logFlushScheduled) {
        _logFlushScheduled = true;
        requestAnimationFrame(flushLogQueue);
    }
}

function flushLogQueue() {
    _logFlushScheduled = false;

    // Take all queued entries
    const entries = _logQueue.splice(0, _logQueue.length);
    if (entries.length === 0) return;

    // Route each entry to its log container by source
    const maxLogs = getLogMaxEntries();
    const buckets = { system: [], module: [] };
    entries.forEach(entry => (buckets[entry.source] || buckets.system).push(entry));

    for (const src of ['system', 'module']) {
        const bucket = buckets[src];
        if (!bucket.length) continue;
        const logOutput = getLogContainer(src);
        if (!logOutput) continue;
        const shouldFollow = isLogScrolledNearBottom(logOutput);

        // Use DocumentFragment for batch DOM insertion
        const fragment = document.createDocumentFragment();
        bucket.forEach(({ message, type, timestamp, showTimestamp }) => {
            const logEntry = document.createElement('div');
            logEntry.className = `log-entry log-${type}`;
            logEntry.textContent = showTimestamp ? `[${timestamp}] ${message}` : message;
            fragment.appendChild(logEntry);
        });

        logOutput.appendChild(fragment);

        // Batch trim old log entries (keep max 500 per container)
        if (logOutput.children.length > maxLogs) {
            const removeCount = logOutput.children.length - maxLogs;
            const range = document.createRange();
            range.setStartBefore(logOutput.firstChild);
            range.setEndBefore(logOutput.children[removeCount]);
            range.deleteContents();
        }

        if (shouldFollow) {
            logOutput.scrollTop = logOutput.scrollHeight;
        }
    }
}

function isLogScrolledNearBottom(logOutput) {
    if (!logOutput) return true;
    const distance = logOutput.scrollHeight - logOutput.clientHeight - logOutput.scrollTop;
    return distance <= 24;
}

// 切换系统操作和模块测试日志。
function switchLogTab(tabName) {
    const target = tabName === 'module' ? 'module' : 'system';
    state.currentLogTab = target;

    document.querySelectorAll('.log-tab-btn').forEach(btn => {
        const selected = btn.dataset.logTab === target;
        btn.classList.toggle('active', selected);
        btn.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    document.querySelectorAll('.log-tab-content').forEach(panel => {
        panel.classList.toggle('active', panel.id === `log-tab-${target}`);
    });

    const out = getLogContainer(target);
    if (out) out.scrollTop = out.scrollHeight;
}

// 用户主动发起设备、烧写、VNC、VPN 或上传操作时显示系统日志。
// 只绑定操作按钮点击，避免后台系统消息在测试运行时抢走“测试日志”页签。
document.addEventListener('click', (event) => {
    const button = event.target?.closest?.(
        '#page-test [data-operation-log-tab="system"] button'
    );
    if (!button || button.disabled) return;
    switchLogTab('system');
});

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
    stopTestStatusPolling();
    // 轮询状态和日志
    let shownPyudevWarning = false;  // 标记是否已显示过 pyudev 警告
    let pollInterval = 2000;  // 初始轮询间隔：2秒
    const maxPollInterval = 30000;  // 最大轮询间隔：30秒
    let pollTimer = null;
    let pollRunning = false;
    let pollRequested = false;
    let stopped = false;
    // WebSocket 是实时日志主通道，但 client_id 不一致或推送丢失时它会静默丢日志。
    // wsLogStallTicks 检测"服务端日志在涨、本地却没收到"的停滞后回退到增量拉取。
    // 必须使用全局 state.wsLogStallTicks，WebSocket onmessage 才能正确重置计数。
    let lastSeenServerLogCount = 0; // 最近一次观测到的服务端日志总数

    const schedulePoll = delay => {
        if (stopped) return;
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(() => {
            pollTimer = null;
            void pollStatus();
        }, delay);
    };

    const pollStatus = async () => {
        if (stopped) return;
        if (pollRunning) {
            pollRequested = true;
            return;
        }
        pollRunning = true;
        try {
            if (state.clusterJobId) {
                const jobId = encodeURIComponent(state.clusterJobId);
                const [jobResponse, eventResponse] = await Promise.all([
                    apiCall(`/api/cluster/jobs/${jobId}`),
                    apiCall(`/api/cluster/jobs/${jobId}/events?after=${encodeURIComponent(String(state.clusterEventSequence ?? -1))}&limit=1000`)
                ]);
                const job = jobResponse.job;
                // 轮询只更新 job/attempt 元数据，不覆盖用户手动选择的 worker。
                // 否则正在运行的旧任务会反复把 worker_id 刷回它分配的主机，
                // 导致用户切换主机后立刻被还原。
                window.GmsWorkspace?.update({
                    cluster_job_id: job.id || state.clusterJobId,
                    attempt_id: job.current_attempt_id || ''
                }, {source: 'test-poll'});
                const currentSequence = Number(state.clusterEventSequence ?? -1);
                const events = (eventResponse.events || []).filter(
                    event => Number(event.sequence) > currentSequence
                );
                events.forEach(event => addNormalizedLogEntry({message: event.message,
                    type: event.level === 'error' ? 'error' : 'info',
                    source: ['stdout', 'stderr'].includes(event.source) ? 'module' : undefined}));
                if (events.length) state.clusterEventSequence = Math.max(...events.map(event => Number(event.sequence)));
                const active = ['created', 'queued', 'leasing', 'assigned', 'dispatching', 'running', 'stopping', 'collecting', 'worker_lost'].includes(job.status);

                // 只在 job 属于当前选中主机时才更新测试状态。
                // 用户可能已切换到另一台主机：旧 job 继续在后端跑，但 UI
                // 不应把当前主机显示为"测试中"。
                const jobBelongsToCurrentWorker = !job.assigned_worker_id
                    || job.assigned_worker_id === workspaceWorkerId();
                if (jobBelongsToCurrentWorker) {
                    state.testStopping = job.status === 'stopping';
                    state.testing = active;
                    updateTestToggleButton(active);
                }
                if (!active) {
                    const level = job.status === 'completed' ? 'success' : 'error';
                    addLogEntry(`分布式测试 ${job.status}${job.error ? `: ${job.error}` : ''}`, level);
                    showToast(`分布式测试${job.status === 'completed' ? '完成' : '结束'}: ${job.status}`, level);
                    state.clusterJobId = '';
                    state.testStopping = false;
                    state.clusterEventSequence = -1;
                    sessionStorage.removeItem('active_cluster_job');
                    window.GmsWorkspace?.update({
                        cluster_job_id: '',
                        attempt_id: '',
                        report_id: `cluster-${job.id}`,
                        report_timestamp: `cluster-${job.id}`,
                        origin_page: 'test'
                    }, {source: 'test-complete'});
                    loadDevices(true).catch(() => {});
                }
                pollInterval = active ? 1000 : 3000;
                return;
            }
            // 检查是否有 WebSocket 连接
            const hasRealtimeConnection = state.websocket && state.websocket.readyState === WebSocket.OPEN;

            // WebSocket 是实时主通道：连接正常时绝不拉增量日志，否则会与 WebSocket
            // 推送的同一批日志重复显示（两者共用 state.lastLogCount，竞态必现重复）。
            // 但若服务端日志总数持续增长而本地 lastLogCount 不动（WebSocket 推送丢失或
            // client_id 不一致），则回退到 since 增量兜底，避免"测试在跑却看不到日志"。
            let shouldFetchLogs = !hasRealtimeConnection;
            if (hasRealtimeConnection && state.testing && state.wsLogStallTicks >= 2) {
                shouldFetchLogs = true;
            }
            const statusUrl = shouldFetchLogs
                ? `/api/test/status?since=${encodeURIComponent(String(state.lastLogCount || 0))}`
                : '/api/test/status?logs=false';
            const status = await apiCall(statusUrl);

            // Durable jobs survive a Controller restart and do not depend on
            // sessionStorage.  Recover the newest active job for the selected
            // Worker when a tab or workspace has lost its current job id.
            const activeJobs = Array.isArray(status.active_jobs) ? status.active_jobs : [];
            if (!state.clusterJobId && activeJobs.length) {
                // 只恢复属于当前选中主机的活跃任务，不把用户切到别的 worker。
                const recoveredJob = activeJobs.find(job => job.worker_id === workspaceWorkerId());
                if (recoveredJob) {
                    state.clusterJobId = recoveredJob.id;
                    state.clusterEventSequence = -1;
                    state.testStopping = recoveredJob.status === 'stopping';
                    sessionStorage.setItem('active_cluster_job', recoveredJob.id);
                    window.GmsWorkspace?.update({
                        cluster_job_id: recoveredJob.id,
                        attempt_id: recoveredJob.attempt_id || ''
                    }, {source: 'test-durable-recovery'});
                    pollRequested = true;
                    return;
                }
            }

            // 检测 WebSocket 日志停滞：服务端 log_count 在涨、本地却没有跟进时累计计数。
            if (typeof status.log_count === 'number' && hasRealtimeConnection && state.testing) {
                if (status.log_count > (state.lastLogCount || 0)) {
                    state.wsLogStallTicks += 1;
                } else {
                    state.wsLogStallTicks = 0;
                }
                lastSeenServerLogCount = status.log_count;
            }

            // 检查 USB 监控器状态并提示（仅显示一次）
            if (!shownPyudevWarning && status.usb_monitor) {
                const { mode, running, pyudev_available } = status.usb_monitor;
                if (running && mode === 'polling' && !pyudev_available) {
                    shownPyudevWarning = true;
                    const message = '💡 提示：安装 pyudev 可获得更好的USB监控性能（实时响应，低CPU占用）\n' +
                                   '安装方式：重新运行一键安装脚本即可自动安装\n' +
                                   '或手动执行：cd /opt/gms-remote-test/web_app && .venv/bin/pip install pyudev\n' +
                                   '安装后需重启服务：sudo systemctl restart gms-web-app';
                    addLogEntry(message, 'warning');

                    // 也可以在页面显示一次提示
                    if (!localStorage.getItem('pyudev_warning_shown')) {
                        showToast('建议安装 pyudev 以提升性能', 'info');
                        localStorage.setItem('pyudev_warning_shown', 'true');
                    }
                }
            }

            // 更新测试状态按钮
            // status.running 和 active_jobs 是所有主机的全局状态。
            // 只根据当前选中主机的活跃 job 来决定测试状态，避免 A 主机的
            // 测试导致切换到 B 主机后仍显示"测试中"。
            const currentWorkerActiveJobs = activeJobs.filter(j => j.worker_id === workspaceWorkerId());
            const currentWorkerRunning = currentWorkerActiveJobs.length > 0;
            if (currentWorkerRunning && !state.testing) {
                state.testing = true;
                updateTestToggleButton(true);
            } else if (!currentWorkerRunning && state.testing) {
                state.testing = false;
                updateTestToggleButton(false);
            }

            // Update VPN status
            if (status.vpn_connected !== undefined) {
                updateVpnStatus(status.vpn_connected);
            }

            if (status.logs && status.logs.length > 0) {
                status.logs.forEach(addNormalizedLogEntry);
                state.lastLogCount = status.log_count || (state.lastLogCount + status.logs.length);
                // 增量拉取补回日志后重置停滞计数。
                state.wsLogStallTicks = 0;
            } else if (typeof status.log_count === 'number' && shouldFetchLogs) {
                state.lastLogCount = Math.max(state.lastLogCount || 0, status.log_count);
            }

            // 动态调整轮询间隔：如果测试正在运行，使用快速轮询；否则退避
            // Use exponential backoff when no changes detected
            if (currentWorkerRunning) {
                pollInterval = 2000;  // 测试运行时：2秒
            } else {
                // If nothing changed since last poll, increase backoff faster
                const stateChanged = (currentWorkerRunning !== state.testing) ||
                                     (status.vpn_connected !== undefined && status.vpn_connected !== state.vpnConnected);
                if (stateChanged) {
                    pollInterval = 2000;  // Reset to fast polling on state change
                } else {
                    pollInterval = Math.min(pollInterval * 1.5, maxPollInterval);  // 测试未运行时：逐渐增加到30秒
                }
            }

        } catch (error) {
            console.error('Status polling error:', error);
        } finally {
            pollRunning = false;
            const nextDelay = pollRequested ? 0 : pollInterval;
            pollRequested = false;
            schedulePoll(nextDelay);
        }
    };

    stopTestStatusPolling = () => {
        stopped = true;
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
    };
    wakeTestStatusPolling = () => {
        if (stopped) return;
        pollInterval = 250;
        if (pollRunning) {
            pollRequested = true;
            return;
        }
        schedulePoll(0);
    };

    wakeTestStatusPolling();
}

async function checkInitialTestStatus() {
    try {
        const workspace = await (window.GmsWorkspace?.ready || Promise.resolve({}));
        const savedClusterJob = sessionStorage.getItem('active_cluster_job') || workspace.cluster_job_id || '';
        if (savedClusterJob) {
            if (state.clusterJobId !== savedClusterJob) {
                state.clusterJobId = savedClusterJob;
                state.clusterEventSequence = -1;
            }
            let response;
            try {
                response = await apiCall(`/api/cluster/jobs/${encodeURIComponent(savedClusterJob)}`);
            } catch (fetchError) {
                // 网络错误或 job 不存在：清理残留状态，不阻塞页面初始化。
                debugLog('[Init] Failed to fetch cluster job, clearing stale state:', fetchError);
                sessionStorage.removeItem('active_cluster_job');
                state.clusterJobId = '';
                state.testing = false;
                state.testStopping = false;
                updateTestToggleButton(false);
                window.GmsWorkspace?.update(
                    {cluster_job_id: '', attempt_id: ''},
                    {source: 'test-recovery-failed'}
                );
                return;
            }
            const jobStatus = response?.job?.status;
            if (!jobStatus) {
                // Job 不存在或响应异常：清理残留状态。
                sessionStorage.removeItem('active_cluster_job');
                state.clusterJobId = '';
                state.testing = false;
                state.testStopping = false;
                updateTestToggleButton(false);
                window.GmsWorkspace?.update(
                    {cluster_job_id: '', attempt_id: ''},
                    {source: 'test-recovery-missing'}
                );
                return;
            }
            const active = ['created', 'queued', 'leasing', 'assigned', 'dispatching', 'running', 'stopping', 'collecting', 'worker_lost'].includes(jobStatus);
            // 只在 job 属于当前选中主机时才显示测试中状态。
            const jobWorkerId = response?.job?.assigned_worker_id || '';
            const jobBelongsToCurrentWorker = !jobWorkerId
                || jobWorkerId === workspaceWorkerId();
            state.testStopping = jobStatus === 'stopping' && jobBelongsToCurrentWorker;
            state.testing = active && jobBelongsToCurrentWorker;
            updateTestToggleButton(state.testing);
            if (active) {
                wakeTestStatusPolling();
                return;
            }
            sessionStorage.removeItem('active_cluster_job');
            state.clusterJobId = '';
            window.GmsWorkspace?.update(
                {cluster_job_id: '', attempt_id: ''},
                {source: 'test-recovery-terminal'}
            );
        }
        const status = await apiCall('/api/test/status');
        const activeJobs = Array.isArray(status.active_jobs) ? status.active_jobs : [];
        if (activeJobs.length) {
            // 只恢复属于当前选中主机的活跃任务，不把用户切到别的 worker。
            const recoveredJob = activeJobs.find(job => job.worker_id === workspaceWorkerId());
            if (recoveredJob) {
                state.clusterJobId = recoveredJob.id;
                state.clusterEventSequence = -1;
                state.testStopping = recoveredJob.status === 'stopping';
                state.testing = true;
                sessionStorage.setItem('active_cluster_job', recoveredJob.id);
                window.GmsWorkspace?.update({
                    cluster_job_id: recoveredJob.id,
                    attempt_id: recoveredJob.attempt_id || ''
                }, {source: 'test-initial-durable-recovery'});
                updateTestToggleButton(true);
                wakeTestStatusPolling();
                return;
            }
        }
        // 只根据当前选中主机的活跃 job 来判断测试状态。
        const initialWorkerActiveJobs = activeJobs.filter(j => j.worker_id === workspaceWorkerId());
        const initialWorkerRunning = initialWorkerActiveJobs.length > 0;
        state.testing = initialWorkerRunning;
        state.testStopping = false;
        updateTestToggleButton(initialWorkerRunning);
        if (initialWorkerRunning) wakeTestStatusPolling();
        // 重置停滞计数：页面刚加载，WebSocket 可能尚未就绪或尚未投递日志。
        state.wsLogStallTicks = 0;

        // 页面刷新时加载历史日志（限制最近100条，避免卡顿）
        if (status.logs && status.logs.length > 0) {
            const systemOut = getLogContainer('system');
            const moduleOut = getLogContainer('module');
            if (systemOut) systemOut.innerHTML = '';
            if (moduleOut) moduleOut.innerHTML = '';

            // 只显示最近100条历史日志，避免卡顿（按 source 路由到对应 Tab）
            const recentLogs = status.logs.slice(-getLogDisplayLimit());
            recentLogs.forEach(addNormalizedLogEntry);

            const activeOut = getLogContainer(state.currentLogTab || 'system');
            if (activeOut) activeOut.scrollTop = activeOut.scrollHeight;

            state.lastLogCount = status.log_count || status.logs.length;
        } else {
            state.lastLogCount = typeof status.log_count === 'number' ? status.log_count : 0;
        }
    } catch (error) {
        console.error('Failed to check initial test status:', error);
        state.lastLogCount = 0;
    }
}

// ==================== UI Helpers ====================
function updateConnectionStatus(connected) {
    state.connected = connected;
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

        let settled = false;

        // 确定按钮事件
        const handleOk = () => {
            if (settled) return;
            settled = true;
            ModalManager.close('confirm-modal');
            cleanup();
            resolve(true);
            if (onConfirm) onConfirm();
        };

        // 取消按钮事件
        const handleCancel = () => {
            if (settled) return;
            settled = true;
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
        ModalManager.onClose('confirm-modal', () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(false);
            if (onCancel) onCancel();
        });

        // 显示模态框
        ModalManager.open('confirm-modal');
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
    debugLog('[showSnackbar] 被调用:', { title, message, level });

    const container = document.getElementById('snackbar-container');
    debugLog('[showSnackbar] container:', container);

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

    debugLog('[showSnackbar] 创建 snackbar 元素:', snackbar);
    container.appendChild(snackbar);
    debugLog('[showSnackbar] 已添加到容器');

    // 自动关闭
    setTimeout(() => {
        if (snackbar.parentElement) {
            snackbar.classList.add('snackbar-exit');
            setTimeout(() => {
                if (snackbar.parentElement) {
                    snackbar.remove();
                    debugLog('[showSnackbar] 已移除 snackbar');
                }
            }, 300);
        }
    }, duration);
};

// 点击弹框外部时关闭，并复用弹框映射。
const _modalCloseHandlers = {
    'config-modal': closeModal,
    'firmware-modal': closeFirmwareModal,
    'firmware-share-modal': closeFirmwareShareModal,
    'firmware-share-password-modal': closeFirmwareSharePasswordModal,
    'file-browser-modal': closeFileBrowserModal,
    'gsi-modal': closeGsiModal,
    'sn-modal': closeSnModal,
    'ui-control-modal': closeUiControl
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
let reportsWorkersLoaded = false;

async function loadReportWorkers() {
    if (reportsWorkersLoaded) return;
    const select = document.getElementById('reports-worker-filter');
    if (!select) return;
    try {
        const response = await fetch('/api/cluster/workers', {cache: 'no-store'});
        const payload = await response.json();
        await (window.GmsWorkspace?.ready || Promise.resolve());
        const workspace = window.GmsWorkspace?.get?.() || {};
        const previous = workspace.worker_id || '';
        const localWorkerId = workspaceLocalWorkerId();
        const workers = [...(payload.workers || [])].sort((left, right) =>
            Number(right.id === localWorkerId) - Number(left.id === localWorkerId)
        );
        select.innerHTML = workers.map(worker =>
            `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.id)}</option>`
        ).join('') + '<option value="">全部 Worker</option>';
        if (Array.from(select.options).some(option => option.value === previous)) select.value = previous;
        reportsWorkersLoaded = true;
    } catch (error) {
        debugLog('[Reports] Worker filter unavailable:', error);
    }
}

async function switchReportsWorker() {
    const workerId = document.getElementById('reports-worker-filter')?.value || '';
    if (workerId) {
        window.GmsWorkspace?.update({
            scope_mode: isLocalWorkspaceWorker(workerId) ? window.GmsWorkspace.get().scope_mode : 'cluster',
            worker_id: workerId,
            origin_page: 'reports'
        }, {source: 'reports'});
        syncWorkspaceWorkerSelectors(workerId);
    }
    await loadTestReports(currentUserFilter);
}

window.switchReportsWorker = switchReportsWorker;

function reportsListUrl(userOnly) {
    const params = new URLSearchParams();
    if (userOnly) params.set('user_only', 'true');
    const workerId = document.getElementById('reports-worker-filter')?.value || '';
    if (workerId) params.set('worker_id', workerId);
    const query = params.toString();
    return `/api/reports/list${query ? `?${query}` : ''}`;
}

// 离开页面时清理报告轮询定时器。
function cleanupReportsPolling() {
    if (reportsRefreshInterval) {
        clearInterval(reportsRefreshInterval);
        reportsRefreshInterval = null;
    }
}

async function loadTestReports(userOnly = false) {
    try {
        await loadReportWorkers();
        const url = reportsListUrl(userOnly);
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
                        const url = reportsListUrl(currentUserFilter);
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
                    <td colspan="10" style="padding: 40px; text-align: center; color: var(--text-secondary);">
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
                <td colspan="10" style="padding: 60px 40px; text-align: center; color: var(--text-secondary);">
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
        const displayClient = report.display_client_id || report.client_name || report.user || report.client_id || '-';
        const passCount = report.pass !== undefined ? report.pass : '-';
        const failCount = report.fail !== undefined ? report.fail : '-';
        const totalCount = report.total !== undefined ? report.total : '-';
        const passRate = report.total > 0 ? ((report.pass / report.total) * 100).toFixed(1) + '%' : '-';
        const suiteName = getReportSuiteDisplayName(report);
        const workerId = report.worker_id || workspaceLocalWorkerId() || '-';

        const passRateStyle = report.total > 0 ? (report.pass / report.total >= 0.9 ? 'color: var(--success-color);' : 'color: var(--warning-color);') : '';

        const typeColor = typeColors[testType] || 'var(--text-secondary)';

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-color)';
        tr.dataset.timestamp = report.timestamp;
        tr.dataset.testType = report.test_type || '';
        tr.dataset.suitePath = report.suite_path || '';
        tr.dataset.workerId = report.worker_id || workspaceLocalWorkerId();
        tr.dataset.clusterJobId = report.cluster_job_id || '';
        tr.dataset.attemptId = report.attempt_id || '';
        tr.dataset.automationRunId = report.automation_run_id || '';
        tr.dataset.reportId = report.report_id || report.timestamp || '';
        tr.dataset.artifactId = report.artifact_id || '';
        tr.dataset.sourceTimestamp = report.source_timestamp || '';
        tr.dataset.reportName = report.report_name || '';

        tr.innerHTML = `
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">${escapeHtml(displayClient)}</td>
            <td style="padding: 4px 6px; text-align: center; font-weight: 700; font-size: 13px; color: ${typeColor};">${testType}</td>
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px; color: var(--text-primary);">${escapeHtml(suiteName)}</td>
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;">${escapeHtml(workerId)}</td>
            <td style="padding: 4px 6px; text-align: center; font-family: monospace; font-size: 12px;" title="${escapeIconAttr(report.timestamp || '')}">${escapeHtml(report.report_name || report.timestamp || '-')}</td>
            <td style="padding: 4px 6px; text-align: center; color: var(--success-color); font-weight: 600; font-size: 13px;">${passCount}</td>
            <td style="padding: 4px 6px; text-align: center; color: var(--danger-color); font-weight: 600; font-size: 13px;">${failCount}</td>
            <td style="padding: 4px 6px; text-align: center; font-weight: 600; font-size: 13px;">${totalCount}</td>
            <td style="padding: 4px 6px; text-align: center; font-weight: 600; font-size: 13px; ${passRateStyle}">${passRate}</td>
            <td style="padding: 4px 6px; text-align: center;">
                <button class="btn-xxs" data-action="analyze" style="margin: 2px; font-size: 12px;">📈 分析</button>
                <button class="btn-xxs" data-action="retry" style="background: var(--primary-color); margin: 2px; font-size: 12px;">🔄 retry</button>
                <button class="btn-xxs" data-action="download" style="background: var(--success-color); margin: 2px; font-size: 12px;">⬇️ 下载</button>
                <button class="btn-xxs" data-action="delete" style="background: var(--danger-color); margin: 2px; font-size: 12px;">🗑️ 删除</button>
                <button class="btn-xxs" data-action="results" style="background: var(--info-color); margin: 2px; font-size: 12px;">results</button>
                <button class="btn-xxs" data-action="logs" style="background: var(--warning-color); margin: 2px; font-size: 12px;">logs</button>
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
    const reportContext = {
        worker_id: tr.dataset.workerId || workspaceLocalWorkerId(),
        cluster_job_id: tr.dataset.clusterJobId || '',
        attempt_id: tr.dataset.attemptId || '',
        automation_run_id: tr.dataset.automationRunId || '',
        report_id: tr.dataset.reportId || timestamp,
        report_timestamp: timestamp,
        artifact_id: tr.dataset.artifactId || '',
        source_timestamp: tr.dataset.sourceTimestamp || '',
        report_name: tr.dataset.reportName || '',
        suite_path: suitePath || '',
        origin_page: 'reports'
    };
    window.GmsWorkspace?.update(reportContext, {source: 'reports'});

    event.stopPropagation();

    switch (action) {
        case 'analyze':
            analyzeReport(timestamp, reportContext.report_id);
            break;
        case 'retry':
            retryReportWithSuite(reportContext.report_name || timestamp, testType, suitePath, reportContext);
            break;
        case 'download':
            downloadReport(timestamp, reportContext.report_id, reportContext.report_name);
            break;
        case 'results':
            openReportSuiteDirectory(timestamp, suitePath, testType, 'results', reportContext);
            break;
        case 'logs':
            openReportSuiteDirectory(timestamp, suitePath, testType, 'logs', reportContext);
            break;
        case 'delete':
            deleteReport(timestamp, reportContext.report_id, reportContext.report_name);
            break;
    }
}

async function openReportSuiteDirectory(timestamp, suitePath, testType, kind, reportContext = {}, targetFile = '') {
    if (!timestamp || !['results', 'logs'].includes(kind)) {
        showToast('报告目录参数无效', 'error');
        return;
    }

    const workerId = reportContext.worker_id || workspaceLocalWorkerId();
    window.GmsWorkspace?.update({...reportContext, worker_id: workerId}, {source: 'reports'});
    const suiteWorker = document.getElementById('suite-worker-select');
    if (suiteWorker) {
        await loadSuiteWorkerSelector();
        if (Array.from(suiteWorker.options).some(option => option.value === workerId)) suiteWorker.value = workerId;
        await loadSuitesForBrowserWorker(false);
    } else if (!testSuitesCache.length || testSuitesWorkerId !== workerId) {
        await loadTestSuites();
    }

    const resolvedSuitePath = findSuitePathForReport(testType, suitePath);
    if (!resolvedSuitePath) {
        showToast('未找到该报告对应的测试套件路径', 'warning');
        return;
    }

    // 旧集群报告可能把 start_display（"Fri Jul 31 ..."）误存到
    // source_timestamp。只接受 Tradefed 目录格式，并优先使用报告名恢复。
    const folderName = tradefedResultFolderName(reportContext.report_name)
        || tradefedResultFolderName(reportContext.source_timestamp)
        || tradefedResultFolderName(timestamp);
    if (!folderName) {
        showToast('报告缺少有效的 Tradefed 结果目录，请刷新报告列表后重试', 'error');
        return;
    }
    const targetPath = `${kind}/${folderName}`;
    switchPage('test-suites', null);
    const filePath = targetFile ? `${targetPath}/${targetFile}` : '';
    if (filePath) state.suiteBrowser.highlightPath = filePath;
    await selectTestSuiteForBrowser(resolvedSuitePath, targetPath, {
        preserveHighlight: Boolean(filePath)
    });
    if (filePath) {
        setSuiteBrowserHighlightedPath(filePath);
        showToast(`已定位到 ${filePath}`, 'success');
    }
}

async function deleteReport(timestamp, reportId = '', reportName = '') {
    const displayName = reportName || timestamp;
    const confirmed = await showConfirmDialog(
        '删除报告',
        `确定要删除报告 ${displayName} 吗？此操作不可恢复。`
    );

    if (!confirmed) return;

    try {
        const identity = reportId
            ? `report_id=${encodeURIComponent(reportId)}`
            : `timestamp=${encodeURIComponent(timestamp)}`;
        const response = await fetch(`/api/reports/delete?${identity}`, {
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

            // 互斥：填入报告时清空模块和用例
            enforceFieldExclusion('retry');

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

async function retryReportWithSuite(timestamp, testType, suitePath, reportContext = {}) {
    try {
        const workerId = reportContext.worker_id || workspaceLocalWorkerId();
        const workerSelect = document.getElementById('cluster-worker');
        if (workerSelect) {
            await loadClusterWorkers();
            if (Array.from(workerSelect.options).some(option => option.value === workerId)) {
                workerSelect.value = workerId;
                await switchTestWorker();
            }
        }
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

            // 互斥：填入报告时清空模块和用例
            enforceFieldExclusion('retry');

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

async function downloadReport(timestamp, reportId = '', reportName = '') {
    try {
        debugLog('[downloadReport] Starting download for timestamp:', timestamp);
        await downloadReportAsZip(timestamp, reportId, reportName);
    } catch (error) {
        console.error('Download report error:', error);
        notifyOperationResult('报告下载失败', error.message, 'error', 'report-download', { timestamp });
    }
}

// 回退方案：下载为 ZIP
async function downloadReportAsZip(timestamp, reportId = '', reportName = '') {
    try {
        const identity = reportId
            ? `report_id=${encodeURIComponent(reportId)}`
            : `report_timestamp=${encodeURIComponent(timestamp)}`;
        const response = await fetch(`/api/reports/download?${identity}&download=true`);

        if (!response.ok) {
            let errorMsg = `HTTP ${response.status}`;
            try {
                const errorData = await response.json();
                errorMsg = errorData.error || errorMsg;
            } catch (e) {
                // 如果无法解析 JSON，使用默认错误消息
            }
            console.error('Download failed:', response.status, errorMsg);
            notifyOperationResult('报告下载失败', errorMsg, 'error', 'report-download', { timestamp });
            return;
        }

        // 检查 Content-Type
        const contentType = response.headers.get('Content-Type');
        debugLog('Response Content-Type:', contentType);

        if (contentType && contentType.includes('application/json')) {
            // 如果返回的是 JSON 而不是文件，说明有错误
            const errorData = await response.json();
            console.error('Server returned error:', errorData);
            notifyOperationResult('报告下载失败', errorData.error || '服务器错误', 'error', 'report-download', { timestamp });
            return;
        }

        // 获取文件名
        const contentDisposition = response.headers.get('Content-Disposition');
        let filename = `${reportName || timestamp}.zip`;

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
            notifyOperationResult('报告下载失败', '文件为空', 'error', 'report-download', { timestamp });
            return;
        }

        const url = window.URL.createObjectURL(blob);
        triggerDownload(url, filename, true);

        notifyOperationResult('报告下载完成', `ZIP 下载成功：${filename}`, 'success', 'report-download', { timestamp, filename });
    } catch (error) {
        console.error('Download report as ZIP error:', error);
        notifyOperationResult('报告下载失败', error.message, 'error', 'report-download', { timestamp });
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

async function analyzeReport(timestamp, reportId = '') {
    try {
        // 从报告列表行中提前回写 Worker 上下文，确保分析结果跳转和后续操作
        // 能正确继承来源 Worker / Cluster Job 信息。
        const reportRow = document.querySelector(`tr[data-timestamp="${timestamp}"]`);
        if (reportRow) {
            const reportContext = {
                worker_id: reportRow.dataset.workerId || workspaceWorkerId(),
                cluster_job_id: reportRow.dataset.clusterJobId || '',
                attempt_id: reportRow.dataset.attemptId || '',
                automation_run_id: reportRow.dataset.automationRunId || '',
                report_id: reportRow.dataset.reportId || timestamp,
                report_timestamp: timestamp,
                artifact_id: reportRow.dataset.artifactId || '',
                suite_path: reportRow.dataset.suitePath || '',
                origin_page: 'report-analysis'
            };
            window.GmsWorkspace?.update(reportContext, {source: 'report-analysis'});
        } else {
            window.GmsWorkspace?.update({report_timestamp: timestamp, origin_page: 'report-analysis'},
                {source: 'report-analysis'});
        }

        // 切换到报告分析页面
        const sidebarItem = document.querySelector('[data-page="report-analysis"]');
        if (sidebarItem) {
            sidebarItem.click();
        }

        // 等待页面切换完成后，自动加载并分析报告
        setTimeout(async () => {
            showToast('正在分析报告...', 'info');

            const formData = createFormData(AnalysisMode.SAVED, {
                report_timestamp: timestamp,
                report_id: reportId || reportRow?.dataset.reportId || ''
            });
            const resp = await fetch('/api/reports/analyze', {
                method: 'POST',
                body: formData
            });
            const data = await resp.json();

            if (!data.success) {
                notifyOperationResult('报告分析失败', data.error || '未知错误', 'error', 'report-analysis', { timestamp });
                return;
            }

            // 使用与手动上传相同的显示函数，保持布局一致
            displayReportAnalysis(data.data);
            notifyOperationResult(
                '报告分析完成',
                data.data?.report_name || data.data?.test_result?.test_name || '报告分析完成',
                'success',
                'report-analysis',
                { timestamp }
            );
        }, 300);
    } catch (e) {
        console.error('[Reports] Error analyzing report:', e);
        notifyOperationResult('报告分析失败', e.message, 'error', 'report-analysis', { timestamp });
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
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content modal-xs">
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
                        支持 .xml, .zip, .rar, .tar.gz
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

async function handleReportDataTransfer(dataTransfer) {
    if (!dataTransfer) return;

    // 检查是否有 URL（从网页拖拽，如 Redmine 附件）
    const url = dataTransfer.getData('URL') || dataTransfer.getData('text/uri-list');
    if (url) {
        debugLog('[Report Analysis] Detected URL drop:', url);
        const dropContext = extractRedmineDropContext(dataTransfer, url);
        await handleRedmineAttachment(url, dropContext);
        return;
    }

    const items = dataTransfer.items;

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
                    while (true) {
                        const batch = await new Promise((resolve) => {
                            reader.readEntries(resolve);
                        });
                        if (batch.length === 0) break;
                        allEntries.push(...batch);
                    }
                    await readFileEntries(allEntries);
                }
            }
        };

        // 处理所有 items
        const itemEntries = [];
        for (let i = 0; i < items.length; i++) {
            const item = items[i];
            if (item.kind === 'file') {
                const entry = item.webkitGetAsEntry?.();
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

    // 不支持目录条目 API 时使用 files 属性。
    const files = dataTransfer.files;
    if (files.length > 0) {
        if (files.length === 1) {
            handleReportFile(files[0]);
        } else {
            handleReportFolder(files);
        }
    }
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
        await handleReportDataTransfer(e.dataTransfer);
    });

    // 文件选择事件
    fileInput.addEventListener('change', async (e) => {
        if (e.target.files.length > 0) {
            await handleReportFile(e.target.files[0]);
            e.target.value = '';
        }
    });

    // 文件夹选择事件
    folderInput.addEventListener('change', async (e) => {
        if (e.target.files.length > 0) {
            await handleReportFolder(e.target.files);
            e.target.value = '';
        }
    });
}

// 用于取消正在进行的请求
let currentRedmineRequest = null;

function extractRedmineDropContext(dataTransfer, url) {
    const candidates = [
        url,
        dataTransfer?.getData('text/plain') || '',
        dataTransfer?.getData('text/html') || '',
        dataTransfer?.getData('text/uri-list') || ''
    ];
    const issueMatch = candidates.join('\n').match(/\/issues\/(\d+)/);
    if (!issueMatch) return {};
    return {
        source_issue_id: issueMatch[1],
        source_issue_url: candidates.find(value => value.includes(`/issues/${issueMatch[1]}`)) || ''
    };
}

async function handleRedmineAttachment(url, context = {}) {
    const originalUrl = url;
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
        let redmineBaseUrl;

        try {
            const redmineConfig = await getRedmineConfig();
            redmineDomain = redmineConfig.domain;
            redmineBaseUrl = redmineConfig.base_url || `https://${redmineDomain}`;
        } catch (configError) {
            console.error('[Redmine] 配置获取失败:', configError);
            showToast('❌ Redmine 配置错误，请联系管理员', 'error');
            return; // 终止处理
        }

        const redminePathUrl = /\/(?:issues|attachments)(?:\/|$)/.test(url);
        const isConfiguredRedmineUrl = url.includes(redmineDomain);
        if (redminePathUrl && !isConfiguredRedmineUrl) {
            const publicUrl = url.replace(/^https?:\/\/[^/]+/, redmineBaseUrl.replace(/\/$/, ''));
            notifyOperationResult('报告分析失败', `请使用公网 Redmine 地址：${publicUrl}`, 'warning', 'report-analysis', {
                source: 'url'
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
            return;
        }

        // 检测是否为配置中的公网 Redmine URL
        const isRedmineUrl = isConfiguredRedmineUrl;
        if (isRedmineUrl) {
            // 检查是否是直接的附件 URL (如 /attachments/2604033)
            const attachmentMatch = url.match(/\/attachments\/(\d+)/);
            const issueMatch = url.match(/\/issues\/(\d+)/);

            if (attachmentMatch && !issueMatch) {
                // 直接的附件 URL，跳过提取步骤，直接使用 analyze-url
                showToast('📎 检测到 Redmine 附件 URL，直接分析...', 'info');
                // 直接跳到 analyze-url 调用，不执行下面的 issue 提取逻辑
            } else if (issueMatch) {
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
                        url = extractResult.attachment_url;
                        context.source_issue_id = context.source_issue_id || issueMatch[1];
                        context.source_issue_url = context.source_issue_url || originalUrl;
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
                    source_issue_id: context.source_issue_id || '',
                    source_issue_url: context.source_issue_url || '',
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
                    notifyOperationResult(
                        '报告分析完成',
                        result.filename || '附件分析完成',
                        'success',
                        'report-analysis',
                        { source: 'url', filename: result.filename || '' }
                    );
                }, 300);
            } else {
                currentRedmineRequest = null;  // 重置请求控制器
                // 如果需要凭证，显示凭证输入框
                if (result.requires_auth) {
                    showRedmineAuthDialog(url, uploadZone, content, progress, progressFill, context);
                } else {
                    notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                        source: 'url'
                    });
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
                notifyOperationResult(
                    '报告分析完成',
                    result.filename || '附件分析完成',
                    'success',
                    'report-analysis',
                    { source: 'url', filename: result.filename || '' }
                );
            }, 300);
        } else {
            currentRedmineRequest = null;  // 重置请求控制器
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                source: 'url'
            });
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
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { source: 'url' });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function showRedmineAuthDialog(url, uploadZone, content, progress, progressFill, context = {}) {
    window._pendingRedmineDropContext = context || {};
    // 显示 Redmine 凭证输入对话框
    const modal = document.createElement('div');
    modal.id = 'redmine-auth-modal';
    modal.className = 'modal show';
    modal.style.cssText = 'z-index: 10000;';
    modal.innerHTML = `
        <div class="modal-content modal-xs">
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
                source_issue_id: window._pendingRedmineDropContext?.source_issue_id || '',
                source_issue_url: window._pendingRedmineDropContext?.source_issue_url || '',
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
                notifyOperationResult(
                    '报告分析完成',
                    result.filename || '附件分析完成',
                    'success',
                    'report-analysis',
                    { source: 'redmine', filename: result.filename || '' }
                );
            }, 300);
        } else {
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                source: 'redmine'
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 2000);
        }
    } catch (error) {
        console.error('Redmine auth error:', error);
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { source: 'redmine' });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

async function handleReportFile(file) {
    const fileName = file?.name || '测试报告';
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
                notifyOperationResult(
                    '报告分析完成',
                    `成功分析 ${fileName}`,
                    'success',
                    'report-analysis',
                    { filename: fileName }
                );
            }, 300);
        } else {
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                filename: fileName
            });
            setTimeout(() => {
                if (progress) progress.style.opacity = '0';
                if (content) content.style.opacity = '1';
            }, 1000);
        }
    } catch (error) {
        console.error('Report analysis error:', error);
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { filename: fileName });
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

            reject(new Error(result.message || result.error || result.detail || `HTTP ${xhr.status}`));
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
                notifyOperationResult(
                    '报告分析完成',
                    `成功分析 ${fileCount} 个文件`,
                    'success',
                    'report-analysis',
                    { file_count: fileCount }
                );
            }, 300);
        } else {
            notifyOperationResult('报告分析失败', result.error || '未知错误', 'error', 'report-analysis', {
                file_count: fileCount
            });
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
        notifyOperationResult('报告分析失败', error.message, 'error', 'report-analysis', { file_count: fileCount });
        if (progress) progress.style.opacity = '0';
        if (content) content.style.opacity = '1';
    }
}

function ensureReportAnalysisResultStructure() {
    const resultDiv = $('report-analysis-result');
    if (!resultDiv) return null;

    if (!$('report-summary') || !$('report-details') || !$('report-failures') || !$('report-failure-list')) {
        resultDiv.innerHTML = `
            <div style="background: var(--light-bg); border-radius: 8px; border: 1px solid var(--border-color); padding: 20px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div style="font-size: 16px; font-weight: 600;">📊 分析结果</div>
                    <button class="btn-xs" onclick="resetReportAnalysis()">清除</button>
                </div>
                <div id="report-summary" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 20px;"></div>
                <div id="report-details" style="font-size: 12px; color: var(--text-primary);"></div>
            </div>
            <div id="report-failures" style="background: var(--light-bg); border-radius: 8px; border: 1px solid var(--border-color); padding: 20px; display: none;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                    <div style="font-size: 12px; font-weight: 600;">❌ 失败用例</div>
                </div>
                <div id="report-failure-list" style="max-height: 580px; overflow-y: auto;"></div>
            </div>
        `;
    }

    return resultDiv;
}

function displayReportAnalysis(data) {
    if (DEBUG) debugLog('[displayReportAnalysis] Called with data:', data);

    // 保存当前报告名称到全局变量，供失败用例卡片使用（使用一次性状态）
    window.currentReportName = data.report_name || '';
    window.currentReportAnalysisData = data;
    const provenance = data.provenance || {};
    if (Object.keys(provenance).length) {
        window.GmsWorkspace?.update({
            worker_id: provenance.worker_id || workspaceWorkerId(),
            cluster_job_id: provenance.cluster_job_id || '',
            attempt_id: provenance.attempt_id || '',
            automation_run_id: provenance.automation_run_id || '',
            report_id: data.report_id || provenance.report_id || '',
            report_timestamp: data.report_timestamp || provenance.timestamp || '',
            artifact_id: provenance.artifact_id || '',
            gerrit_change_id: provenance.gerrit_change_id || '',
            gerrit_patchset: provenance.gerrit_patchset || '',
            redmine_issue_id: provenance.redmine_issue_id || '',
            suite_path: provenance.suite_path || '',
            origin_page: 'report-analysis'
        }, {source: 'report-analysis'});
    }

    if (DEBUG) debugLog('[displayReportAnalysis] Current report name:', window.currentReportName);

    const resultDiv = ensureReportAnalysisResultStructure();
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
            ${data.details && data.details.soc_platform ? `
                <div>
                    <span class="summary-label">SOC平台：</span>
                    <span class="summary-value">${data.details.soc_platform}</span>
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

    if (detailsDiv) {
        const fields = [
            ['Worker', provenance.worker_id], ['Job', provenance.cluster_job_id],
            ['Attempt', provenance.attempt_id], ['ATS Run', provenance.automation_run_id],
            ['Artifact', provenance.artifact_id], ['Gerrit', provenance.gerrit_change_id],
            ['Redmine', provenance.redmine_issue_id]
        ].filter(([, value]) => value);
        detailsDiv.innerHTML = fields.length ? `
            <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;border-top:1px solid var(--border-color);padding-top:10px;">
                ${fields.map(([label, value]) => `<span style="padding:3px 7px;border:1px solid var(--border-color);border-radius:10px;"><b>${escapeHtml(label)}</b> ${escapeHtml(value)}</span>`).join('')}
                ${provenance.cluster_job_id ? '<button class="btn-xs" data-provenance-page="cluster">打开集群任务</button>' : ''}
                ${provenance.automation_run_id ? '<button class="btn-xs" data-provenance-page="automation">打开 ATS</button>' : ''}
                ${provenance.gerrit_change_id ? '<button class="btn-xs" data-provenance-page="gerrit-dashboard">打开 Gerrit</button>' : ''}
                ${provenance.redmine_issue_id ? '<button class="btn-xs" data-provenance-page="redmine-agent">打开 Redmine</button>' : ''}
            </div>` : '';
        detailsDiv.querySelectorAll('[data-provenance-page]').forEach(button => {
            button.addEventListener('click', () => window.GmsWorkspace?.navigate(button.dataset.provenancePage));
        });
    }

    // 显示失败用例
    if (failuresDiv && failureList && data.failures && data.failures.length > 0) {
        failuresDiv.style.display = 'block';

        // 测试类型在循环外提取（每份报告固定不变）
        const reportTestType = escapeJsAttr((data.details && data.details.test_type) || '');

        const failuresHTML = data.failures.map((failure, idx) => {
            // 解析失败信息
            const reasonText = failure.reason || '无失败原因';

            // 使用后端返回的模块名，如果没有则使用默认值
            const moduleName = failure.module || '未知模块';

            // 使用后端返回的测试用例名
            const testCaseName = failure.name || '未知用例';

            // 格式化完整堆栈信息，保留换行和缩进
            const formattedStackTrace = (reasonText || '无失败原因')
                .split('\n')
                .map(line => '&nbsp;&nbsp;&nbsp;&nbsp;' + line
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                )
                .join('<br>');

            // 从 report_name 中提取 Redmine issue ID（使用预编译的正则表达式）
            const reportName = window.currentReportName || '';
            const redmineIssueMatch = reportName.match(/^Redmine-(\d+)-/);
            const issueIdFromReport = redmineIssueMatch ? redmineIssueMatch[1] : '';

            // 转义用于 onclick 属性的参数
            const escModuleName = escapeJsAttr(moduleName);
            const escTestCaseName = escapeJsAttr(testCaseName);

            return `
                <div class="report-failure-card">
                    <div class="report-failure-card-head">
                        <div class="report-failure-title">
                            <div>测试模块: <span>${escapeHtml(moduleName)}</span></div>
                            <div>测试用例: <code>${escapeHtml(testCaseName)}</code></div>
                        </div>
                        <div class="report-failure-actions">
                            ${issueIdFromReport ? `<button class="report-failure-action reply" onclick="openRedmineReplyModal('${escModuleName}', '${escTestCaseName}', '${idx}', '${issueIdFromReport}')" data-reason="${encodeURIComponent(reasonText)}">Redmine回复</button>` : ''}
                            <button class="report-failure-action test" onclick="goToTestCase('${reportTestType}', '${escModuleName}', '${escTestCaseName}')">单测用例</button>
                            <button class="report-failure-action diagnose" onclick="openReportDiagnosisModal(${idx})">报错诊断</button>
                        </div>
                    </div>
                    <div>
                        <div class="report-failure-reason-label">报错信息</div>
                        <div class="failure-reason" id="failure-reason-${idx}" style="font-size: 11px; font-family: 'Courier New', monospace; white-space: pre-wrap; word-wrap: break-word;">${formattedStackTrace}</div>
                        <div class="failure-reason-raw" id="failure-reason-raw-${idx}" style="display: none;">${escapeHtml(reasonText)}</div>
                    </div>
                </div>
            `;
        }).join('');

        failureList.innerHTML = failuresHTML;
    } else if (failuresDiv) {
        failuresDiv.style.display = 'block';
        if (failureList) {
            failureList.innerHTML = `
                <div class="report-empty-success">
                    <b>未发现失败用例</b>
                    <span>这份报告没有可诊断的失败项，可以清除后继续分析下一份报告。</span>
                </div>
            `;
        }
    }
}

function getReportFailureByIndex(failureIndex) {
    const report = window.currentReportAnalysisData;
    if (!report || !Array.isArray(report.failures)) return null;
    return report.failures[failureIndex] || null;
}

function openReportAnalysisRedmineAgent(issueId = '') {
    const frame = document.getElementById('redmine-agent-frame');
    const query = new URLSearchParams();
    query.set('tab', 'issues');
    if (issueId) query.set('issue', issueId);
    if (frame) frame.src = '/redmine-agent?' + query.toString();
    minimizeReportDiagnosisWorkbench();
    if (typeof switchPage === 'function') switchPage('redmine-agent', null);
}

function getReportDiagnosisKey(failureIndex = 0) {
    const report = window.currentReportAnalysisData || {};
    const failure = getReportFailureByIndex(failureIndex) || {};
    return [
        report.report_name || '',
        failureIndex,
        failure.name || failure.test_name || '',
        failure.module || ''
    ].join('|');
}

function openReportDiagnosisModal(failureIndex = 0) {
    const modal = $('report-diagnosis-modal');
    if (!modal) {
        showToast('诊断弹框未加载', 'error');
        return;
    }
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';
    modal.dataset.failureIndex = String(failureIndex);
    ModalManager.open('report-diagnosis-modal');

    const diagnosisKey = getReportDiagnosisKey(failureIndex);
    const diag = window.reportDiagnosis || {};
    if (diag.key === diagnosisKey && diag.data) {
        return;
    }
    window.reportDiagnosis = window.reportDiagnosis || {};
    window.reportDiagnosis.key = diagnosisKey;
    runReportDiagnosis(failureIndex);
}

function closeReportDiagnosisWorkbench() {
    ModalManager.close('report-diagnosis-modal');
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';
}

function minimizeReportDiagnosisWorkbench() {
    const modal = $('report-diagnosis-modal');
    if (!modal) return;
    ModalManager.close('report-diagnosis-modal');
    const minimized = $('report-diagnosis-minimized');
    const title = $('report-diagnosis-minimized-title');
    if (title) {
        const data = (window.reportDiagnosis || {}).data || {};
        title.textContent = data.test_name || data.report_name || '诊断工作台';
    }
    if (minimized) minimized.style.display = 'flex';
}

function restoreReportDiagnosisWorkbench() {
    const minimized = $('report-diagnosis-minimized');
    if (minimized) minimized.style.display = 'none';
    ModalManager.open('report-diagnosis-modal');
}

function rerunReportDiagnosis() {
    const modal = $('report-diagnosis-modal');
    const currentIndex = Number(
        modal?.dataset?.failureIndex ||
        (window.reportDiagnosis || {}).failureIndex ||
        0
    ) || 0;
    window.reportDiagnosis = {
        ...(window.reportDiagnosis || {}),
        key: null,
        data: null,
    };
    runReportDiagnosis(currentIndex);
}

function renderReportDiagnosisLoading(failure, classNames, errorMessage) {
    const diagnosticSummary = $('report-diagnostic-summary');
    const diagnosticResult = $('report-diagnostic-result');
    if (diagnosticSummary) {
        diagnosticSummary.innerHTML = `
            <div class="dx-hero">
                <div class="dx-title-row">
                    <div class="dx-title-main">${escapeHtml(failure.name || failure.test_name || '未知用例')}</div>
                    <span class="dx-status-pill">诊断中</span>
                </div>
                <div class="dx-compact-line">${escapeHtml((errorMessage || '').split('\n').slice(0, 2).join('\n') || '正在提取失败上下文...')}</div>
            </div>
        `;
    }
    if (diagnosticResult) {
        diagnosticResult.innerHTML = `
            <div class="dx-loading-grid">
                <div class="dx-loading-card"><span>1</span>提取失败堆栈</div>
                <div class="dx-loading-card"><span>2</span>定位套件构件</div>
                <div class="dx-loading-card"><span>3</span>OpenGrok 源码搜索</div>
                <div class="dx-loading-card"><span>4</span>AI 诊断和建议</div>
            </div>
        `;
    }
}

async function runReportDiagnosis(failureIndex = 0) {
    const report = window.currentReportAnalysisData;
    if (!report) {
        showToast('请先加载一份报告', 'warning');
        return;
    }

    const failure = getReportFailureByIndex(failureIndex);
    if (!failure) {
        showToast('当前报告没有可诊断的失败用例', 'warning');
        return;
    }

    const testName = failure.name || failure.test_name || report.report_name || '未知用例';
    const errorMessage = failure.reason || failure.stack_trace || '';
    const moduleName = failure.module || '';
    const classNames = extractClassNames(testName, errorMessage);
    renderReportDiagnosisLoading(failure, classNames, errorMessage);

    try {
        const result = await apiCall('/api/reports/diagnose', 'POST', {
            test_name: testName,
            error_message: errorMessage,
            stack_trace: errorMessage,
            module: moduleName,
            class_names: classNames,
            report_name: report.report_name || '',
            failure_index: failureIndex,
            test_type: report.details?.test_type || '',
            suite_version: report.details?.suite_version || '',
            source_path: failure.source_path || failure.file_path || report.source_path || ''
        });
        if (!result.success) {
            throw new Error(result.error || result.message || '诊断失败');
        }
        renderReportDiagnosis(result.data || {});
        const aiFallback = result.data?.ai_result?.ai_enabled === false;
        const providerFallback = Boolean(result.data?.ai_result?.ai_fallback_used);
        notifyOperationResult(
            aiFallback
                ? '报告诊断已降级'
                : providerFallback ? '报告诊断使用备用模型' : '报告诊断完成',
            aiFallback
                ? `${testName} 本地 AI 不可用，当前显示规则分析`
                : providerFallback
                ? `${testName} 本地 AI 不可用，已由备用模型完成`
                : `${testName} 诊断已完成`,
            (aiFallback || providerFallback) ? 'warning' : 'success', 'report-diagnosis', {
            report_name: report.report_name || '',
            failure_index: failureIndex
        });
    } catch (error) {
        debugLog('[Report Diagnosis] Error:', error);
        const diagnosticResult = $('report-diagnostic-result');
        if (diagnosticResult) {
            diagnosticResult.innerHTML = `<div class="dx-error">诊断失败: ${escapeHtml(error.message)}</div>`;
        }
        notifyOperationResult('报告诊断失败', error.message, 'error', 'report-diagnosis');
    }
}

function switchReportDiagnosisPanel(panelName) {
    document.querySelectorAll('[data-dx-tab]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.dxTab === panelName);
    });
    document.querySelectorAll('[data-dx-panel]').forEach(panel => {
        panel.classList.toggle('active', panel.dataset.dxPanel === panelName);
    });
}

function renderDxMetric(label, value) {
    return `
        <div class="dx-metric">
            <span class="dx-metric-label">${escapeHtml(label)}</span>
            <span class="dx-metric-value">${escapeHtml(value || '无')}</span>
        </div>
    `;
}

function renderDxLocatorRow(label, value) {
    return `
        <div class="dx-locator-row">
            <span class="dx-locator-label">${escapeHtml(label)}</span>
            <span class="dx-locator-value">${escapeHtml(value || '无')}</span>
        </div>
    `;
}

function renderDxEmpty(text) {
    return `<div class="dx-empty">${escapeHtml(text)}</div>`;
}

function joinSuiteArtifactPath(rootPath, artifactPath) {
    const artifact = String(artifactPath || '').trim();
    if (!artifact) return String(rootPath || '').trim();
    if (artifact.startsWith('/')) return artifact;
    const root = String(rootPath || '').trim().replace(/\/+$/, '');
    return root ? `${root}/${artifact.replace(/^\/+/, '')}` : artifact;
}

function normalizeDiagnosisSourceDisplayPath(path) {
    const text = String(path || '').trim().replace(/\\/g, '/');
    if (!text) return '';
    const srcIndex = text.lastIndexOf('/src/');
    if (srcIndex >= 0) return text.slice(srcIndex + 5);
    const packageMatch = text.match(/(?:^|\/)(com|android|org|libcore)\//);
    if (packageMatch && typeof packageMatch.index === 'number') {
        return text.slice(text[packageMatch.index] === '/' ? packageMatch.index + 1 : packageMatch.index);
    }
    return text;
}

function getDiagnosisDisplaySourcePath(sourceResults, sourceGuess, sourcePath) {
    const results = Array.isArray(sourceResults) ? sourceResults : [];
    const exact = results.find(item => item && item.is_exact_location && (item.path || item.display_path));
    const first = exact || results.find(item => item && (item.path || item.display_path));
    return normalizeDiagnosisSourceDisplayPath(
        (first && (first.path || first.display_path)) ||
        sourceGuess?.source_path ||
        sourcePath ||
        ''
    );
}

function getReportIssueIdFromName() {
    const reportName = window.currentReportName || window.currentReportAnalysisData?.report_name || '';
    const match = String(reportName || '').match(/^Redmine-(\d+)-/);
    return match ? match[1] : '';
}

function buildReportDiagnosisReplyText(data, patchDraft) {
    const aiResult = data.ai_result || {};
    const lines = [];
    const rootVerified = aiResult.root_cause_status === 'verified';
    const exemptions = Array.isArray(data.mainline_exemptions) ? data.mainline_exemptions : [];
    if (exemptions.length) {
        const ids = exemptions.map(item => item.exemption_id).filter(Boolean).join(', ');
        lines.push(
            `**Mainline 已知豁免**: 该用例命中 Google Mainline 已知豁免（exemption ${ids || '未知'}，${exemptions[0].test_module || data.module || ''}），通常无需本地修复。`,
            '',
        );
    }
    lines.push(
        `**测试模块**: ${data.module || '-'}`,
        '',
        `**测试用例**: ${data.test_name || '-'}`,
        '',
        '**已观察到的失败**:',
        aiResult.observed_failure || data.error_message || '-',
        '',
        rootVerified ? '**已验证根因**:' : '**初步判断（待验证）**:',
        aiResult.root_cause || data.summary || aiResult.analysis || '-',
    );
    if (aiResult.root_cause_note) lines.push('', `> ${aiResult.root_cause_note}`);
    const suggestions = aiResult.suggestions || [];
    if (suggestions.length) {
        lines.push('', '**处理建议**:', suggestions.map((item, idx) => `${idx + 1}. ${item}`).join('\n'));
    }
    if (patchDraft) {
        lines.push('', rootVerified ? '**补丁方向**:' : '**排查方向**:', '<pre>', patchDraft, '</pre>');
    }
    return lines.join('\n');
}

function renderReportDiagnosis(data) {
    const diagnosticSummary = $('report-diagnostic-summary');
    const diagnosticResult = $('report-diagnostic-result');
    if (!diagnosticResult) return;

    const aiResult = data.ai_result || {};
    const kbResults = data.knowledge_base_results || [];
    const sourceResults = data.source_search_results || [];
    const exemptions = Array.isArray(data.mainline_exemptions) ? data.mainline_exemptions : [];
    const patchDraft = data.patch_draft || '';
    const stackTrace = data.stack_trace || '';
    const suiteTarget = data.suite_target || {};
    const suiteArtifact = suiteTarget.artifact || null;
    const artifactCandidates = suiteTarget.artifact_candidates || [];
    const sourceGuess = suiteTarget.source_guess || {};
    const sourcePath = data.source_path || sourceGuess.source_path || '';
    const suiteArtifactPath = joinSuiteArtifactPath(suiteTarget.suite_root, suiteArtifact ? suiteArtifact.path : '');
    const displaySourcePath = getDiagnosisDisplaySourcePath(sourceResults, sourceGuess, sourcePath);
    const currentFailureIndex = Number(data.failure_index || 0) || 0;
    const suggestions = aiResult.suggestions || [];
    const issueIdFromReport = getReportIssueIdFromName();
    const currentFailure = getReportFailureByIndex(currentFailureIndex) || {};
    const aiFallback = aiResult.ai_enabled === false && aiResult.ai_attempted;
    const providerFallback = Boolean(aiResult.ai_fallback_used);
    const rootCauseStatus = aiResult.root_cause_status || 'hypothesis';
    const rootCauseVerified = rootCauseStatus === 'verified';
    const rootCauseLabel = rootCauseVerified ? '已验证根因' : '初步判断';
    const rootCauseTag = rootCauseVerified ? 'Verified root cause' : 'Hypothesis';
    const confidenceLabels = {high: '高置信度', medium: '中置信度', low: '低置信度'};
    const rootConfidence = confidenceLabels[aiResult.root_cause_confidence] || '低置信度';
    const observedFailure = aiResult.observed_failure || data.error_message || stackTrace.split('\n').find(Boolean) || '未提取到明确失败信息';
    const patchDraftTitle = rootCauseVerified ? '补丁草案' : '排查草案';
    const aiStatusLabel = aiFallback
        ? '规则分析（AI 不可用）'
        : `${aiResult.ai_model || 'AI'}${providerFallback ? '（备用）' : ''}`;
    const aiFallbackNotice = aiFallback
        ? `<div class="dx-error">
            <b>本地 AI 未完成分析，当前结果来自规则降级</b>
            <div>${escapeHtml(String(aiResult.ai_error || '模型调用失败').slice(0, 260))}</div>
        </div>`
        : '';
    const providerFallbackNotice = providerFallback
        ? `<div class="dx-error">
            <b>本地 AI 未完成分析，本次已由 ${escapeHtml(aiResult.ai_model || aiResult.ai_provider || '备用模型')} 完成</b>
            <div>${escapeHtml(String((aiResult.ai_provider_errors || []).join('; ') || '本地模型调用失败').slice(0, 260))}</div>
        </div>`
        : '';
    const reportTestType = (window.currentReportAnalysisData?.details && window.currentReportAnalysisData.details.test_type) || data.test_type || suiteTarget.test_type || '';
    const replyDraft = buildReportDiagnosisReplyText(data, patchDraft);
    const hasExactArtifact = Boolean(suiteArtifact && (
        (suiteArtifact.reasons || []).includes('exact-module-binary') ||
        (suiteArtifact.path || '').toLowerCase().endsWith('/' + String(data.module || currentFailure.module || '').toLowerCase() + '.apk') ||
        (suiteArtifact.path || '').toLowerCase().endsWith('/' + String(data.module || currentFailure.module || '').toLowerCase() + '.jar')
    ));

    window.reportDiagnosis = {
        data,
        target: suiteTarget,
        failureIndex: currentFailureIndex,
        key: (window.reportDiagnosis || {}).key,
        displaySourcePath,
        suiteArtifactPath,
        text: [
            `报告: ${data.report_name || ''}`,
            `用例: ${data.test_name || ''}`,
            `模块: ${data.module || ''}`,
            `测试类型: ${suiteTarget.test_type || data.test_type || ''}`,
            `套件版本: ${suiteTarget.suite_version || data.suite_version || ''}`,
            `套件: ${suiteTarget.suite_name || suiteTarget.suite_path || ''}`,
            `构件: ${suiteArtifact ? suiteArtifact.path : ''}`,
            `源码路径: ${displaySourcePath || sourcePath}`,
            `Mainline 豁免: ${exemptions.length ? exemptions.map(i => `${i.exemption_id}(${i.issue_type || ''})`).join(', ') : '无'}`,
            `失败现象: ${observedFailure}`,
            `${rootCauseLabel}: ${aiResult.root_cause || data.summary || ''}`,
            `结论置信度: ${rootConfidence}`,
            `分析: ${aiResult.analysis || ''}`,
            `建议: ${(aiResult.suggestions || []).join('\n')}`,
            `${patchDraftTitle}:\n${patchDraft}`,
            `堆栈:\n${stackTrace || '无'}`
        ].join('\n\n'),
        replyDraft,
        patchDraft
    };

    if (diagnosticSummary) {
        diagnosticSummary.innerHTML = `
            <div class="dx-hero">
                <div class="dx-title-row">
                    <div class="dx-title-main">${escapeHtml(data.test_name || data.report_name || '诊断工作台')}</div>
                    <div class="dx-pill-row">
                        <span class="dx-status-pill">${escapeHtml(aiStatusLabel)}</span>
                        <span class="dx-status-pill">${escapeHtml(suiteTarget.test_type || data.test_type || '未知类型')}</span>
                        <span class="dx-status-pill">${escapeHtml(suiteTarget.suite_version || data.suite_version || '未知版本')}</span>
                    </div>
                </div>
                <div class="dx-compact-line">${escapeHtml([data.module || currentFailure.module || '', suiteArtifactPath, displaySourcePath].filter(Boolean).join(' | ') || '当前失败项诊断')}</div>
            </div>
        `;
    }

    const sourceCards = sourceResults.length > 0
        ? sourceResults.map(item => `
            <div class="dx-list-item${item.url ? ' dx-clickable' : ''}" ${item.url ? `onclick="window.open('${escapeJsAttr(item.url)}', '_blank')"` : ''}>
                <div class="dx-list-head">
                    <div class="dx-list-title">${escapeHtml(item.type || 'source')}</div>
                    ${item.url ? `<a class="dx-link" href="${escapeHtml(item.url)}" target="_blank" onclick="event.stopPropagation()">打开 OpenGrok</a>` : ''}
                </div>
                <div class="dx-list-path dx-list-path-inline">${escapeHtml(item.path || item.display_path || '')}${item.line ? `<span>:${escapeHtml(String(item.line))}</span>` : ''}</div>
            </div>
        `).join('')
        : renderDxEmpty('未检索到 OpenGrok 源码结果');

    const kbCards = kbResults.length > 0
        ? kbResults.map(item => `
            <div class="dx-list-item">
                <div class="dx-list-title">#${escapeHtml(String(item.id || ''))} ${escapeHtml(item.subject || '')}</div>
                <div class="dx-list-meta">${escapeHtml(item.status_name || '')} | ${escapeHtml(item.updated_on || '')}</div>
                <div class="dx-list-text">${escapeHtml((item.solution_summary || item.description || '').slice(0, 260))}</div>
            </div>
        `).join('')
        : renderDxEmpty('未命中知识库');

    const candidateCards = !hasExactArtifact && artifactCandidates.length > 0
        ? `<details class="dx-details"><summary>候选构件 (${artifactCandidates.length})</summary><div class="dx-list">${
            artifactCandidates.slice(0, 5).map((item, idx) => `
                <button class="dx-candidate" onclick="openReportDiagnosisArtifactCandidate(${idx})">
                    <span>${escapeHtml(item.path || item.name || '未知构件')}</span>
                    <b>${escapeHtml(String(item.score || 0))}</b>
                </button>
            `).join('')
        }</div></details>`
        : '';

    const suggestionCards = suggestions.length > 0
        ? suggestions.map((s, idx) => `
            <div class="dx-suggestion">
                <span>${idx + 1}</span>
                <div>${escapeHtml(s)}</div>
            </div>
        `).join('')
        : renderDxEmpty('暂无解决建议');

    const exemptionBanner = exemptions.length
        ? `<section class="dx-section dx-exempt-banner">
            <div class="dx-exempt-head">
                <span class="dx-exempt-badge">✓ 已豁免</span>
                <span class="dx-exempt-title">命中 Google Mainline 已知豁免（通常无需本地修复）</span>
            </div>
            <div class="dx-exempt-list">
                ${exemptions.map(item => `
                    <div class="dx-exempt-item">
                        <div class="dx-exempt-item-head">
                            <b class="dx-exempt-id">exemption ${escapeHtml(String(item.exemption_id || ''))}</b>
                            <span class="dx-exempt-meta">${escapeHtml([item.issue_type, item.test_module].filter(Boolean).join(' · '))}</span>
                            ${item.match_kind === 'fuzzy' ? '<span class="dx-exempt-kind">模糊匹配</span>' : ''}
                            ${item.source_url ? `<a class="dx-link" href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener">来源</a>` : ''}
                        </div>
                        <div class="dx-exempt-case">${escapeHtml(item.test_case || '')}</div>
                        ${item.issue_text ? `<div class="dx-exempt-text">${escapeHtml(String(item.issue_text).slice(0, 280))}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        </section>`
        : '';

    const actionPanel = `
        <section class="dx-section dx-action-section">
            <div class="dx-section-title">下一步动作</div>
            <button type="button" class="dx-action-card" onclick="openReportDiagnosisTestCase('${escapeJsAttr(reportTestType)}', '${escapeJsAttr(data.module || currentFailure.module || '')}', '${escapeJsAttr(data.test_name || currentFailure.name || '')}')">
                <b>执行单测复现</b>
                <span>跳到测试页并填入模块/用例</span>
            </button>
            ${suiteArtifact ? `<button type="button" class="dx-action-card" onclick="openReportDiagnosisSuiteBrowser()"><b>打开测试套件</b><span>${escapeHtml(suiteArtifact.path || '')}</span></button>` : ''}
            ${issueIdFromReport ? `<button type="button" class="dx-action-card" onclick="openReportDiagnosisRedmineReply()"><b>Redmine 回复</b><span>基于诊断结论生成回复草稿</span></button>` : ''}
            ${issueIdFromReport ? `<button type="button" class="dx-action-card" onclick="openReportAnalysisRedmineAgent('${escapeJsAttr(issueIdFromReport)}')"><b>Redmine 工作台</b><span>查看工单历史、附件证据和相似案例</span></button>` : ''}
            <button type="button" class="dx-action-card" onclick="saveDiagnosisToWiki()"><b>📥 存为Wiki</b><span>把诊断结论沉淀到知识库</span></button>
        </section>
    `;

    diagnosticResult.innerHTML = `
        <div class="dx-workbench-vertical">
            ${aiFallbackNotice}
            ${providerFallbackNotice}
            ${exemptionBanner}
            ${actionPanel}
            <div class="dx-workflow">
                <section class="dx-workflow-step dx-workflow-step-analysis">
                <div class="dx-step-label">
                    <span>1</span>
                    <div>
                        <b>详细分析</b>
                        <em>先区分失败现象，再验证上游原因</em>
                    </div>
                </div>
                <div class="dx-two-col">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-title">${rootCauseLabel}</div>
                        <div class="dx-observed-failure">
                            <span>已观察到的失败</span>
                            <div>${escapeHtml(observedFailure)}</div>
                        </div>
                        <div class="dx-root-cause">
                            <span>${rootCauseTag} · ${rootConfidence}</span>
                            <div>${escapeHtml(aiResult.root_cause || data.summary || '待分析')}</div>
                        </div>
                        ${aiResult.root_cause_note ? `<div class="dx-root-note">${escapeHtml(aiResult.root_cause_note)}</div>` : ''}
                        <div class="dx-preline">${escapeHtml(aiResult.analysis || '无')}</div>
                    </div>
                    <div class="dx-section dx-context-section">
                        <div class="dx-section-title">失败上下文</div>
                        <div class="dx-stack">${escapeHtml(stackTrace || data.error_message || '无')}</div>
                    </div>
                </div>
                </section>

                <section class="dx-workflow-step dx-workflow-step-source">
                <div class="dx-step-label">
                    <span>2</span>
                    <div>
                        <b>OpenGrok 源码或测试套件反编译</b>
                        <em>把定位依据、候选构件和源码搜索放在一起</em>
                    </div>
                </div>
                <div class="dx-source-layout">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-head">
                            <div class="dx-section-title">套件源码定位</div>
                            ${suiteArtifact ? `<button class="btn-xxs btn-primary" onclick="openReportDiagnosisSourcePreview()">反编译并预览源码</button>` : ''}
                        </div>
                        <div class="dx-locator-list">
                            ${renderDxLocatorRow('测试套件', suiteArtifactPath || '未定位')}
                            ${renderDxLocatorRow('源码路径猜测', displaySourcePath || '未推断')}
                        </div>
                        ${candidateCards}
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">OpenGrok 源码搜索 <span>${sourceResults.length} 结果</span></div>
                        <div class="dx-list">${sourceCards}</div>
                    </div>
                </div>
                </section>

                <section class="dx-workflow-step dx-workflow-step-solution">
                <div class="dx-step-label">
                    <span>3</span>
                    <div>
                        <b>解决建议</b>
                        <em>建议、补丁草案和知识库证据集中收口</em>
                    </div>
                </div>
                <div class="dx-solution-layout">
                    <div class="dx-section dx-section-large">
                        <div class="dx-section-title">建议动作</div>
                        <div class="dx-list">${suggestionCards}</div>
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">${patchDraftTitle}</div>
                        <pre class="dx-code">${escapeHtml(patchDraft || '无')}</pre>
                    </div>
                    <div class="dx-section">
                        <div class="dx-section-title">GMS 认证知识库</div>
                        <div class="dx-list">${kbCards}</div>
                    </div>
                </div>
                </section>
            </div>
        </div>
    `;
}

function openReportDiagnosisRedmineReply() {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    const failureIndex = Number(data.failure_index || diag.failureIndex || 0) || 0;
    const issueId = getReportIssueIdFromName();
    if (!issueId) {
        showToast('当前报告名称未关联 Redmine Issue ID', 'warning');
        return;
    }
    const failure = getReportFailureByIndex(failureIndex) || {};
    const moduleName = data.module || failure.module || '未知模块';
    const testName = data.test_name || failure.name || failure.test_name || '未知用例';
    const modalId = openRedmineReplyModal(moduleName, testName, failureIndex, issueId);
    const modal = modalId ? document.getElementById(modalId) : null;
    const area = modal?.querySelector('[data-redmine-reply-text]');
    if (area && diag.replyDraft) area.value = diag.replyDraft;
}

function openReportDiagnosisTestCase(testType, moduleName, testCaseName) {
    minimizeReportDiagnosisWorkbench();
    goToTestCase(testType, moduleName, testCaseName);
}

async function copyReportDiagnosis() {
    const text = (window.reportDiagnosis || {}).text || '';
    if (!text) {
        showToast('暂无可复制的诊断结果', 'warning');
        return;
    }
    try {
        await navigator.clipboard.writeText(text);
        showToast('诊断结果已复制', 'success');
    } catch (error) {
        showToast('复制失败', 'error');
    }
}

function getCurrentReportDiagnosisTarget() {
    return (window.reportDiagnosis || {}).target || null;
}

async function saveDiagnosisToWiki() {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    if (!data || Object.keys(data).length === 0) {
        showToast('暂无诊断结果可保存', 'warning');
        return;
    }
    const moduleName = data.module || '';
    const testName = data.test_name || '';
    const aiResult = data.ai_result || {};
    const reportName = data.report_name || (window.currentReportAnalysisData && window.currentReportAnalysisData.timestamp) || '';
    const reportTimestamp = (window.currentReportAnalysisData && window.currentReportAnalysisData.timestamp) || reportName;
    const issueId = getReportIssueIdFromName();
    const kbHit = (data.knowledge_base_results || []).map(k => `- ${k.subject || k.error_signature || ''}: ${k.solution_summary || k.root_cause || ''}`).join('\n');

    const content = [
        `# ${moduleName ? moduleName + (testName ? '#' + testName : '') : '测试诊断'}`,
        '',
        `**测试用例:** ${testName || '未知'}`,
        `**模块:** ${moduleName || '未知'}`,
        `**报告:** ${reportName || '未知'}`,
        '',
        '## 报错信息',
        '```',
        (data.error_message || '').slice(0, 4000),
        '```',
        aiResult.observed_failure ? `\n## 已观察到的失败\n${aiResult.observed_failure}` : '',
        aiResult.root_cause ? `\n## ${aiResult.root_cause_status === 'verified' ? '已验证根因' : '初步判断（待验证）'}\n${aiResult.root_cause}` : '',
        aiResult.root_cause_note ? `\n> ${aiResult.root_cause_note}` : '',
        aiResult.analysis ? `\n## 分析\n${aiResult.analysis}` : '',
        aiResult.suggestions && aiResult.suggestions.length ? `\n## 建议\n${aiResult.suggestions.map(s => '- ' + s).join('\n')}` : '',
        kbHit ? `\n## 知识库命中\n${kbHit}` : '',
    ].filter(Boolean).join('\n');

    const links = [];
    if (reportTimestamp) links.push({target_type:'test_report', target_id:String(reportTimestamp), title:String(reportTimestamp)});
    if (issueId) links.push({target_type:'redmine_issue', target_id:String(issueId), title:'#' + String(issueId)});
    if (moduleName) links.push({target_type:'test_case', target_id:moduleName + (testName ? '::' + testName : ''), title:moduleName});

    try {
        await window.saveToWiki({
            content,
            notebook: '测试问题库',
            links
        });
        showToast('已存入知识库「测试问题库」', 'success');
    } catch (e) {
        showToast('存为Wiki失败: ' + e.message, 'error');
    }
}

function buildReportDiagnosisSourcePath(target) {
    const guess = target?.source_guess || {};
    return guess.source_path || '';
}

function getReportDiagnosisSourceLocation() {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    const target = getCurrentReportDiagnosisTarget();
    const sourcePath = diag.displaySourcePath || buildReportDiagnosisSourcePath(target);
    const fallbackSourcePath = buildReportDiagnosisSourcePath(target);
    const lineNumber = Number(
        data?.failure_location?.line_number ||
        target?.source_guess?.line_number ||
        0
    ) || null;
    return { sourcePath, fallbackSourcePath, lineNumber };
}

function _requireDiagnosisArtifact(msg) {
    const target = getCurrentReportDiagnosisTarget();
    if (!target || !target.artifact) {
        showToast(msg || '未找到可反编译的构件', 'warning');
        return null;
    }
    return target;
}

async function openReportDiagnosisSourcePreview() {
    if (!_requireDiagnosisArtifact()) return;
    minimizeReportDiagnosisWorkbench();
    const { sourcePath, fallbackSourcePath, lineNumber } = getReportDiagnosisSourceLocation();
    await openReportDiagnosisApkAnalysis({ sourcePath, fallbackSourcePath, lineNumber });
}

async function openReportDiagnosisArtifactCandidate(index = 0) {
    const target = getCurrentReportDiagnosisTarget();
    const candidate = target?.artifact_candidates?.[index];
    if (!target || !candidate) {
        showToast('候选构件不存在', 'warning');
        return;
    }
    window.reportDiagnosis.target = {
        ...target,
        artifact: candidate,
        artifact_confidence: candidate.score || 0
    };
    minimizeReportDiagnosisWorkbench();
    const { sourcePath, fallbackSourcePath, lineNumber } = getReportDiagnosisSourceLocation();
    await openReportDiagnosisApkAnalysis({ sourcePath, fallbackSourcePath, lineNumber });
}

async function openReportDiagnosisSuiteBrowser() {
    const target = getCurrentReportDiagnosisTarget();
    if (!target || !target.suite_path || !target.artifact) {
        showToast('未找到可打开的套件构件', 'warning');
        return;
    }
    minimizeReportDiagnosisWorkbench();
    const artifactPath = target.artifact.path || '';
    const directoryPath = getParentSuitePath(artifactPath);
    if (typeof switchPage === 'function') {
        switchPage('test-suites', null);
    }
    await initTestSuiteBrowserPage();
    setSuiteBrowserHighlightedPath(artifactPath);
    await selectTestSuiteForBrowser(target.suite_path, directoryPath || '', { preserveHighlight: true });
}

async function openReportDiagnosisSourceFile() {
    const target = getCurrentReportDiagnosisTarget();
    if (!target) {
        showToast('未推断出源码路径', 'warning');
        return;
    }
    const { sourcePath, fallbackSourcePath, lineNumber } = getReportDiagnosisSourceLocation();
    if (!sourcePath) {
        showToast('未推断出源码路径', 'warning');
        return;
    }
    minimizeReportDiagnosisWorkbench();
    await openReportDiagnosisApkAnalysis({ sourcePath, fallbackSourcePath, lineNumber });
}

async function openReportDiagnosisApkAnalysis(options = {}) {
    const target = _requireDiagnosisArtifact();
    if (!target) return;

    const data = (window.reportDiagnosis || {}).data || {};
    const sourcePath = options.sourcePath || buildReportDiagnosisSourcePath(target);
    const fallbackSourcePath = options.fallbackSourcePath || buildReportDiagnosisSourcePath(target);
    const lineNumber = Number(options.lineNumber || data?.failure_location?.line_number || target?.source_guess?.line_number || 0) || null;
    state.suiteBrowser.selectedSuitePath = target.suite_path || state.suiteBrowser.selectedSuitePath;
    await analyzeSuiteApk(target.artifact.path, {
        openSourcePath: sourcePath,
        openFallbackSourcePath: fallbackSourcePath,
        openSourceLine: lineNumber,
        diagnosisTarget: target
    });
}

async function enhanceReportDiagnosisWithSource(filePath, sourceCode) {
    const diag = window.reportDiagnosis || {};
    const data = diag.data || {};
    if (!data.test_name || !sourceCode || diag.enhanceInFlight) return;

    window.reportDiagnosis.enhanceInFlight = true;
    try {
        const result = await apiCall('/api/reports/diagnose', 'POST', {
            test_name: data.test_name || '',
            error_message: data.error_message || '',
            stack_trace: data.stack_trace || data.error_message || '',
            module: data.module || '',
            class_names: data.class_names || [],
            report_name: data.report_name || '',
            failure_index: data.failure_index || 0,
            test_type: data.suite_target?.test_type || '',
            suite_version: data.suite_target?.suite_version || '',
            source_path: filePath,
            source_code: sourceCode
        });
        if (result.success && result.data) {
            renderReportDiagnosis({
                ...result.data,
                suite_target: result.data.suite_target || data.suite_target
            });
            showToast('已结合反编译源码刷新 AI 诊断', 'success');
        }
    } catch (error) {
        debugLog('[Report Diagnosis] Source enhanced diagnosis failed:', error);
    } finally {
        window.reportDiagnosis.enhanceInFlight = false;
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
                    line_number: lineNumber
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

// ==================== 对话Agent ====================
let agentSessionId = localStorage.getItem('gms_agent_session_id') || '';
let agentPollTimer = null;
let agentInitialized = false;
let agentInputHistory = JSON.parse(localStorage.getItem('gms_agent_input_history') || '[]');
let agentHistoryIndex = -1;  // -1 = not browsing history
const agentHandledAutoOpenMessageIds = new Set();

function getAgentWorkspaceContext() {
    const context = window.GmsWorkspace?.get?.() || {};
    return {
        scope_mode: context.scope_mode || 'single',
        worker_id: context.worker_id || workspaceLocalWorkerId(),
        device_ids: Array.isArray(context.device_ids) ? context.device_ids : [],
        suite_key: context.suite_key || '',
        suite_path: context.suite_path || '',
        cluster_job_id: context.cluster_job_id || '',
        attempt_id: context.attempt_id || '',
        automation_run_id: context.automation_run_id || '',
        report_id: context.report_id || '',
        report_timestamp: context.report_timestamp || '',
        artifact_id: context.artifact_id || '',
        gerrit_change_id: context.gerrit_change_id || '',
        gerrit_patchset: context.gerrit_patchset || '',
        redmine_issue_id: context.redmine_issue_id || '',
        origin_page: 'agent'
    };
}

function applyAgentSessionWorkspace(session) {
    const context = session?.workspace_context;
    if (!context || typeof context !== 'object') return;
    const current = getAgentWorkspaceContext();
    // 空闲会话仅供展示，不覆盖当前工作区选择。
    const authoritative = ['planning', 'running', 'monitoring', 'analyzing'].includes(session?.status)
        || Boolean(context.cluster_job_id || context.automation_run_id);
    if (authoritative) {
        const changed = Object.keys(context).some(key => JSON.stringify(current[key]) !== JSON.stringify(context[key]));
        if (changed) window.GmsWorkspace?.update(context, {source: 'agent'});
    }
}

function getAgentStatusLabel(status) {
    const labels = {
        idle: '空闲',
        planning: '待确认',
        running: '测试中',
        monitoring: '监控中',
        analyzing: '分析中',
        done: '完成',
        error: '异常',
        cancelled: '已取消'
    };
    return labels[status] || status || '空闲';
}

function getCurrentPageName() {
    const active = document.querySelector('.page-content.active');
    return active?.id?.replace(/^page-/, '') || '';
}

function formatAgentTime(value) {
    if (!value) return '';
    const raw = String(value);
    const match = raw.match(/T(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : raw.replace('T', ' ');
}

function getAgentMessageTone(message) {
    const content = message?.content || '';
    if (message?.kind === 'plan' || /生成执行计划|需要确认|确认后开始/.test(content)) return 'plan';
    if (/失败|异常|没有可用|不能执行|error/i.test(content)) return 'error';
    if (/完成|已启动|已取消|成功/.test(content)) return 'success';
    return '';
}

function renderAgentPlanContent(content) {
    const lines = String(content || '').split('\n').map(line => line.trim()).filter(Boolean);
    const intro = [];
    const fields = [];
    const notes = [];

    lines.forEach(line => {
        const item = line.replace(/^- /, '');
        const idx = item.indexOf(':');
        if (line.startsWith('- ') && idx > 0) {
            fields.push([item.slice(0, idx).trim(), item.slice(idx + 1).trim()]);
        } else if (/输入|当前没有|不能执行/.test(line)) {
            notes.push(line);
        } else {
            intro.push(line);
        }
    });

    const introHtml = intro.length ? `<div style="margin-bottom: 10px;">${escapeHtml(intro.join('\n'))}</div>` : '';
    const gridHtml = fields.length ? `
        <div class="agent-plan-grid">
            ${fields.map(([key, value]) => `
                <div class="agent-plan-key">${escapeHtml(key)}</div>
                <div class="agent-plan-value">${escapeHtml(value || '-')}</div>
            `).join('')}
        </div>
    ` : '';
    const noteHtml = notes.length ? `<div class="agent-plan-note">${escapeHtml(notes.join('\n'))}</div>` : '';
    return introHtml + gridHtml + noteHtml;
}

function renderAgentMessages(session) {
    const container = $('agent-chat-messages');
    if (!container) return;

    const messages = session?.messages || [];
    if (!messages.length) {
        container.innerHTML = '<div class="agent-chat-empty">可以问：每个页面功能、rk3572设备、最近报告、测试套件、VPN状态；也可以说：跑 CtsWifiTestCases，失败 retry 2 次并分析报告</div>';
        return;
    }

    container.innerHTML = messages.map(message => {
        const isUser = message.role === 'user';
        const data = message.data || {};
        const plan = data.plan || null;
        const kind = message.kind || 'text';
        const reportTimestamp = data.report_timestamp || '';
        const apkTaskId = data.analysis?.apk_source_analysis?.task_id || '';
        const targetPage = data.page || '';
        const quickActions = data.quick_actions || [];
        const tone = getAgentMessageTone(message);
        const roleLabel = isUser ? '你' : 'Agent';
        const isPlanLike = kind === 'plan' || plan;
        let actions = '';

        // Plan confirmation buttons
        if (plan && session.status === 'planning') {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" onclick="confirmAgentPlan()">执行计划</button>
                    <button class="btn-xs" onclick="sendAgentMessage(false, '重新规划')">重新规划</button>
                </div>
            `;
        }

        // Report analysis buttons
        if (reportTimestamp) {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" onclick="openAgentReportAnalysis('${escapeJsAttr(reportTimestamp)}')">打开报告分析</button>
                    <button class="btn-xs" onclick="switchPage('reports', null)">报告管理</button>
                    ${apkTaskId ? `<button class="btn-xs" onclick="openAgentApkAnalysis('${escapeJsAttr(apkTaskId)}')">打开APK分析</button>` : ''}
                </div>
            `;
        }

        // Quick actions from response generator
        if (quickActions.length > 0) {
            const actionBtns = quickActions.map(a => {
                if (a.page) {
                    return `<button class="btn-xs" onclick="openAgentPageAction('${escapeJsAttr(a.page)}', '${escapeJsAttr(JSON.stringify(a.params || {}))}')">${escapeHtml(a.label)}</button>`;
                } else if (a.action) {
                    return `<button class="btn-xs" onclick="sendAgentAction('${escapeJsAttr(a.action)}', '${escapeJsAttr(JSON.stringify(a.params || {}))}', '${escapeJsAttr(a.label || a.action)}')">${escapeHtml(a.label)}</button>`;
                }
                return '';
            }).filter(Boolean).join('');
            if (actionBtns) {
                actions += `<div class="agent-actions">${actionBtns}</div>`;
            }
        }

        // Page navigation button (fallback when no other actions)
        if (!data.auto_open && !reportTimestamp && !quickActions.length && targetPage && targetPage !== 'agent') {
            actions += `
                <div class="agent-actions">
                    <button class="btn-xs" onclick="switchPage('${escapeJsAttr(targetPage)}', null)">打开页面</button>
                </div>
            `;
        }

        const escapedContent = escapeHtml(message.content || '').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        const contentHtml = isPlanLike ? renderAgentPlanContent(message.content || '') : escapedContent;
        const toneClass = tone ? ` ${tone}` : '';
        const bodyClass = isPlanLike ? 'agent-message-body plan-body' : 'agent-message-body';

        return `
            <div class="agent-message-row ${isUser ? 'user' : 'assistant'}">
                <div class="agent-message ${isUser ? 'user' : 'assistant'}${toneClass}">
                    <div class="agent-message-header">
                        <span class="agent-role">${roleLabel}</span>
                        <span class="agent-time">${escapeHtml(formatAgentTime(message.created_at))}</span>
                    </div>
                    <div class="${bodyClass}">${contentHtml}${actions}</div>
                </div>
            </div>
        `;
    }).join('');
    container.scrollTop = container.scrollHeight;

    const lastAutoOpen = [...messages].reverse().find(message =>
        message.role !== 'user'
        && message.data?.auto_open
        && message.data?.page
        && !agentHandledAutoOpenMessageIds.has(message.id)
    );
    if (lastAutoOpen) {
        agentHandledAutoOpenMessageIds.add(lastAutoOpen.id);
        if (lastAutoOpen.data.page !== getCurrentPageName()) {
            setTimeout(() => switchPage(lastAutoOpen.data.page, null), 0);
        }
    }
}

function renderAgentSteps(session) {
    const stepsEl = $('agent-steps');
    const statusEl = $('agent-status');
    if (statusEl) statusEl.textContent = getAgentStatusLabel(session?.status);
    if (!stepsEl) return;

    const steps = session?.steps || [];
    if (!steps.length) {
        stepsEl.innerHTML = '<div class="suite-empty">等待任务</div>';
        return;
    }

    const colorByStatus = {
        done: 'var(--success-color)',
        running: 'var(--primary-color)',
        warning: 'var(--warning-color)',
        error: 'var(--danger-color)'
    };
    const iconByStatus = {
        done: '✓',
        running: '…',
        warning: '!',
        error: '×'
    };

    stepsEl.innerHTML = steps.map(step => {
        const color = colorByStatus[step.status] || 'var(--text-secondary)';
        const icon = iconByStatus[step.status] || '•';
        return `
            <div style="border: 1px solid var(--border-color); border-radius: 6px; padding: 9px; background: var(--darker-bg);">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 5px;">
                    <span style="width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; background: ${color}; color: #fff; font-size: 11px; flex-shrink: 0;">${icon}</span>
                    <span style="font-size: 13px; font-weight: 600; color: var(--text-primary);">${escapeHtml(step.title || '')}</span>
                </div>
                <div style="font-size: 12px; color: var(--text-secondary); line-height: 1.45; overflow-wrap: anywhere;">${escapeHtml(step.detail || '')}</div>
            </div>
        `;
    }).join('');
}

function renderAgentSession(session) {
    if (!session) return;
    agentSessionId = session.session_id || agentSessionId;
    if (agentSessionId) {
        localStorage.setItem('gms_agent_session_id', agentSessionId);
    }
    renderAgentMessages(session);
    renderAgentSteps(session);
    applyAgentSessionWorkspace(session);

    if (['running', 'monitoring'].includes(session.status)) {
        startAgentPolling();
    } else if (agentPollTimer) {
        stopAgentPolling();
    }
}

async function fetchAgentSession() {
    if (!agentSessionId) return;
    try {
        const response = await fetch(`/api/agent/sessions/${encodeURIComponent(agentSessionId)}`);
        if (!response.ok) {
            newAgentSession();
            return;
        }
        const result = await response.json();
        if (result.data?.expired) {
            newAgentSession();
            return;
        }
        if (result.success && result.data?.session) {
            renderAgentSession(result.data.session);
        }
    } catch (error) {
        debugLog('[Agent] session fetch failed:', error);
    }
}

function startAgentPolling() {
    if (agentPollTimer) return;
    agentPollTimer = setInterval(fetchAgentSession, 3000);
}

function stopAgentPolling() {
    if (agentPollTimer) {
        clearInterval(agentPollTimer);
        agentPollTimer = null;
    }
}

async function sendAgentMessage(execute = false, overrideMessage = '') {
    const input = $('agent-input');
    const message = (overrideMessage || input?.value || '').trim();
    if (!message && !execute) {
        showToast('请输入 Agent 指令', 'warning');
        return;
    }

    if (input && !overrideMessage) {
        // Save to history before clearing
        if (message && message !== agentInputHistory[0]) {
            agentInputHistory.unshift(message);
            if (agentInputHistory.length > 50) agentInputHistory.length = 50;
            localStorage.setItem('gms_agent_input_history', JSON.stringify(agentInputHistory));
        }
        input.value = '';
        agentHistoryIndex = -1;
        // Reset height
        input.style.height = 'auto';
        input.style.height = '100px';
    }

    try {
        // Show typing indicator
        const container = $('agent-chat-messages');
        const typingEl = document.createElement('div');
        typingEl.id = 'agent-typing';
        typingEl.className = 'agent-typing';
        typingEl.textContent = '思考中...';
        if (container) { container.appendChild(typingEl); container.scrollTop = container.scrollHeight; }

        const response = await fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: agentSessionId || null,
                message: message || '确认执行',
                execute,
                workspace_context: getAgentWorkspaceContext()
            })
        });
        if (!response.ok) {
            let errorText = `HTTP ${response.status}`;
            try {
                const errorResult = await response.json();
                errorText = errorResult.error || errorResult.message || errorText;
            } catch (_) {
                // Keep the HTTP status when the server did not return JSON.
            }
            throw new Error(errorText);
        }
        const result = await response.json();

        // Remove typing indicator
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();

        if (!result.success) {
            showToast(result.error || 'Agent 请求失败', 'error');
            return;
        }
        renderAgentSession(result.data.session);
    } catch (error) {
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();
        showToast(`Agent 请求失败: ${error.message}`, 'error');
    }
}

async function sendAgentAction(action, paramsJson = '{}', label = '') {
    let params = {};
    try {
        params = JSON.parse(paramsJson || '{}');
    } catch (error) {
        showToast(`Agent 参数解析失败: ${error.message}`, 'error');
        return;
    }

    const display = label || action;
    try {
        const container = $('agent-chat-messages');
        const typingEl = document.createElement('div');
        typingEl.id = 'agent-typing';
        typingEl.className = 'agent-typing';
        typingEl.textContent = '执行中...';
        if (container) { container.appendChild(typingEl); container.scrollTop = container.scrollHeight; }

        const response = await fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: agentSessionId || null,
                message: display,
                action,
                params,
                execute: false,
                workspace_context: getAgentWorkspaceContext()
            })
        });
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        if (!result.success) {
            showToast(result.error || 'Agent 操作失败', 'error');
            return;
        }
        renderAgentSession(result.data.session);
    } catch (error) {
        const indicator = document.getElementById('agent-typing');
        if (indicator) indicator.remove();
        showToast(`Agent 操作失败: ${error.message}`, 'error');
    }
}

function openAgentPageAction(page, paramsJson = '{}') {
    let params = {};
    try {
        params = JSON.parse(paramsJson || '{}');
    } catch (_) {
        params = {};
    }
    if (page === 'redmine-agent') {
        const frame = document.getElementById('redmine-agent-frame');
        const query = new URLSearchParams();
        query.set('tab', params.tab || 'stats');
        if (params.name) query.set('name', params.name);
        if (frame) frame.src = '/redmine-agent?' + query.toString();
    }
    if (page === 'gerrit-dashboard') {
        const frame = document.getElementById('gerrit-dashboard-frame');
        if (frame) frame.src = '/gerrit-dashboard';
    }
    const contextPatch = {};
    if (params.worker_id) Object.assign(contextPatch, {worker_id: params.worker_id});
    if (params.devices) contextPatch.device_ids = params.devices;
    if (params.report_timestamp || params.timestamp) contextPatch.report_timestamp = params.report_timestamp || params.timestamp;
    if (params.issue_id) contextPatch.redmine_issue_id = String(params.issue_id);
    if (params.change_id) contextPatch.gerrit_change_id = String(params.change_id);
    if (Object.keys(contextPatch).length) window.GmsWorkspace?.update(contextPatch, {source: 'agent-navigation'});
    switchPage(page, null);
}

function confirmAgentPlan() {
    sendAgentMessage(true, '确认执行');
}

function newAgentSession() {
    agentSessionId = '';
    localStorage.removeItem('gms_agent_session_id');
    stopAgentPolling();
    renderAgentSession({ status: 'idle', messages: [], steps: [] });
}

async function cancelAgentSession() {
    if (!agentSessionId) {
        newAgentSession();
        return;
    }

    try {
        const response = await fetch(`/api/agent/sessions/${encodeURIComponent(agentSessionId)}/cancel`, {
            method: 'POST'
        });
        const result = await response.json();
        if (result.success && result.data?.session) {
            renderAgentSession(result.data.session);
        } else {
            showToast(result.error || '取消失败', 'error');
        }
    } catch (error) {
        showToast(`取消失败: ${error.message}`, 'error');
    }
}

function openAgentReportAnalysis(timestamp) {
    window.GmsWorkspace?.update({report_timestamp: timestamp || '', origin_page: 'agent'}, {source: 'agent-report'});
    switchPage('report-analysis', null);
    if (typeof analyzeReport === 'function' && timestamp) {
        analyzeReport(timestamp);
    }
}

function openAgentApkAnalysis(taskId) {
    if (!taskId) return;
    switchPage('apk-analysis', null);
    if (typeof initApkAnalysisPage === 'function') {
        initApkAnalysisPage();
    }
    if (typeof stopApkPolling === 'function') {
        stopApkPolling();
    }
    window.apkCurrentTaskId = taskId;
    window.apkNotifiedTaskId = taskId;
    setApkUploadEmpty(false);
    if ($('apk-analysis-status')) $('apk-analysis-status').style.display = 'block';
    if ($('apk-analysis-result')) $('apk-analysis-result').style.display = 'block';
    if ($('apk-analysis-state')) $('apk-analysis-state').textContent = '正在加载 Agent 反编译任务...';
    pollApkStatus();
}

function initAgentPage() {
    if (!agentInitialized) {
        agentInitialized = true;
        const input = $('agent-input');
        if (input) {
            // Track current draft when user starts browsing history
            let agentDraftInput = '';

            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    sendAgentMessage(false);
                } else if (event.key === 'ArrowUp') {
                    // 光标在开头或输入为空时才浏览历史命令。
                    if (agentInputHistory.length === 0) return;
                    // Save draft on first history navigation
                    if (agentHistoryIndex === -1) {
                        agentDraftInput = input.value;
                        agentHistoryIndex = 0;
                    } else if (agentHistoryIndex < agentInputHistory.length - 1) {
                        agentHistoryIndex++;
                    }
                    event.preventDefault();
                    input.value = agentInputHistory[agentHistoryIndex];
                    // Move cursor to end
                    setTimeout(() => { input.selectionStart = input.selectionEnd = input.value.length; }, 0);
                } else if (event.key === 'ArrowDown') {
                    if (agentHistoryIndex === -1) return;
                    event.preventDefault();
                    if (agentHistoryIndex > 0) {
                        agentHistoryIndex--;
                        input.value = agentInputHistory[agentHistoryIndex];
                    } else {
                        // Restore draft
                        agentHistoryIndex = -1;
                        input.value = agentDraftInput;
                    }
                    setTimeout(() => { input.selectionStart = input.selectionEnd = input.value.length; }, 0);
                }
            });

            // Auto-resize textarea based on content
            input.addEventListener('input', () => {
                // Reset history browsing on manual input
                agentHistoryIndex = -1;
                input.style.height = 'auto';
                input.style.height = Math.max(100, Math.min(input.scrollHeight, 300)) + 'px';
            });
        }
    }

    if (agentSessionId) {
        fetchAgentSession();
    } else {
        renderAgentSession({ status: 'idle', messages: [], steps: [] });
    }
}


// ==================== 全局函数暴露 ====================
// 将 HTML onclick 需要的函数暴露到 window 对象
window.refreshDevices = refreshDevices;
window.selectAllDevices = selectAllDevices;
window.rebootDevices = rebootDevices;
window.remountDevices = remountDevices;
window.connectWifi = connectWifi;
window.setupUsbipForward = setupUsbipForward;
window.closeUsbipAttachModal = closeUsbipAttachModal;
window.submitUsbipAttach = submitUsbipAttach;
window.loadUsbipSourceDevices = loadUsbipSourceDevices;
window.checkSshd = checkSshd;
window.checkRouting = checkRouting;
window.connectVpn = connectVpn;
window.checkVpnStatus = checkVpnStatus;
window.closeVpnCredentialModal = closeVpnCredentialModal;
window.handleVpnCredentialKeyPress = handleVpnCredentialKeyPress;
window.submitVpnCredential = submitVpnCredential;
window.startTest = startTest;
window.wakeTestStatusPolling = () => wakeTestStatusPolling();
window.stopTest = stopTest;
window.selectReportSource = selectReportSource;
window.deleteReport = deleteReport;
window.downloadReport = downloadReport;
window.retryReportWithSuite = retryReportWithSuite;
window.analyzeReport = analyzeReport;
window.handleReportDataTransfer = handleReportDataTransfer;
window.loadTestReports = loadTestReports;
window.showSshdInstallGuide = showSshdInstallGuide;
window.closeSshdInstallGuide = closeSshdInstallGuide;
window.autoInstallUsbipd = autoInstallUsbipd;
window.resetReportAnalysis = resetReportAnalysis;
window.sendReportAnalysisEmail = sendReportAnalysisEmail;
window.openReportDiagnosisModal = openReportDiagnosisModal;
window.closeReportDiagnosisWorkbench = closeReportDiagnosisWorkbench;
window.minimizeReportDiagnosisWorkbench = minimizeReportDiagnosisWorkbench;
window.restoreReportDiagnosisWorkbench = restoreReportDiagnosisWorkbench;
window.requestElevatedAccess = requestElevatedAccess;
window.submitElevateForm = submitElevateForm;
window.cancelElevate = cancelElevate;
window.rerunReportDiagnosis = rerunReportDiagnosis;
window.copyReportDiagnosis = copyReportDiagnosis;
window.saveDiagnosisToWiki = saveDiagnosisToWiki;
window.openReportDiagnosisSourcePreview = openReportDiagnosisSourcePreview;
window.openReportDiagnosisSuiteBrowser = openReportDiagnosisSuiteBrowser;
window.openReportDiagnosisArtifactCandidate = openReportDiagnosisArtifactCandidate;
window.openReportDiagnosisSourceFile = openReportDiagnosisSourceFile;
window.openReportDiagnosisApkAnalysis = openReportDiagnosisApkAnalysis;
window.openRedmineReplyModal = openRedmineReplyModal;
window.initAgentPage = initAgentPage;
window.sendAgentMessage = sendAgentMessage;
window.sendAgentAction = sendAgentAction;
window.confirmAgentPlan = confirmAgentPlan;
window.newAgentSession = newAgentSession;
window.cancelAgentSession = cancelAgentSession;
window.openAgentReportAnalysis = openAgentReportAnalysis;
window.openAgentApkAnalysis = openAgentApkAnalysis;
window.showTailscaleInfoModal = showTailscaleInfoModal;
window.copyDeployCommand = copyDeployCommand;
window.copyTailscaleAccessUrl = copyTailscaleAccessUrl;

// ==================== Tailscale 内网地址 ====================

/**
 * 复制部署脚本命令
 */
function copyDeployCommand() {
    // 发布包由 install.sh package 生成并签名。用通配符匹配最新的包，避免用户
    // 手填 <VERSION>（bash 会把尖括号当成重定向报错，且版本号默认是时间戳）。
    const deployCommand =
        'PKG=$(ls -t gms-web-app-*.tar.gz | head -1) && ' +
        'sha256sum -c "${PKG}.sha256" && ' +
        'gpg --verify "${PKG}.sig" "${PKG}" && ' +
        'tar -xzf "${PKG}" && cd gms-web-app && sudo ./install.sh';

    const clipboardWrite = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(deployCommand)
        : Promise.reject(new Error('Clipboard API unavailable'));

    clipboardWrite.then(() => {
        showToast('✓ 已复制签名发布包部署命令', 'success');
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
            showToast('✓ 已复制签名发布包部署命令', 'success');
        } catch (e) {
            showToast('复制失败', 'error');
        }
        document.body.removeChild(textArea);
    });
}

/**
 * 显示 Tailscale 信息弹框，自动检测并启动 Tailscale
 * 缓存结果 5 分钟，避免每次打开弹框都调用 API
 */
let _tailscaleCache = { url: null, ts: 0 };
const TAILSCALE_CACHE_TTL = 5 * 60 * 1000;

async function showTailscaleInfoModal() {
    const display = document.getElementById('tailscale-url-display');
    ModalManager.open('tailscale-info-modal');

    if (_tailscaleCache.url && Date.now() - _tailscaleCache.ts < TAILSCALE_CACHE_TTL) {
        display.value = _tailscaleCache.url;
        return;
    }

    display.value = '正在检查 Tailscale...';
    try {
        const granted = await requestElevatedAccess('启动或检查 Tailscale');
        if (!granted) {
            display.value = '已取消管理员提权';
            return;
        }
        const data = await apiCall('/api/tailscale/ensure', 'POST');

        if (!data.success || !data.public_url) {
            throw new Error(data.error || 'Tailscale 不可用');
        }

        window.tailscaleUrl = data.public_url;
        _tailscaleCache = { url: data.public_url, ts: Date.now() };
        display.value = data.public_url;
    } catch (error) {
        _tailscaleCache = { url: null, ts: 0 };
        display.value = '未连接';
        showToast('Tailscale 未连接，请在终端执行 sudo tailscale up 授权登录', 'warning');
    }
}

function closeTailscaleInfoModal() {
    ModalManager.close('tailscale-info-modal');
}

function copyTailscaleAccessUrl() {
    if (window.tailscaleUrl) {
        copyText(window.tailscaleUrl, { successMsg: '✓ Tailscale 地址已复制' });
    } else {
        showToast('暂无可用地址', 'error');
    }
}


// 跳转到测试界面，自动填入测试类型、测试模块、测试用例，并匹配测试套件
function goToTestCase(testType, moduleName, testCaseName) {
    try {
        // 切换到测试界面
        switchPage('test');

        // 等待页面切换完成后填充数据
        setTimeout(() => {
            debugLog(`[goToTestCase] 填充数据: testType=${testType}, module=${moduleName}, testCase=${testCaseName}`);

            // 设置测试类型
            const testTypeSelect = document.getElementById('test-type');
            if (testTypeSelect && testType) {
                testTypeSelect.value = testType;
                debugLog(`[goToTestCase] 已设置测试类型: ${testType}`);
            }

            // 填入测试模块
            const testModuleInput = document.getElementById('test-module');
            if (testModuleInput && moduleName && moduleName !== '未知模块') {
                testModuleInput.value = moduleName;
                debugLog(`[goToTestCase] 已设置测试模块: ${moduleName}`);
            }

            // 填入测试用例
            const testCaseInput = document.getElementById('test-case');
            if (testCaseInput && testCaseName && testCaseName !== '未知用例') {
                testCaseInput.value = testCaseName;
                debugLog(`[goToTestCase] 已设置测试用例: ${testCaseName}`);
            }

            // 互斥：填入模块/用例时清空测试报告
            enforceFieldExclusion('module_case');

            // 根据测试类型自动选择测试套件
            if (testType && typeof autoSelectTestSuite === 'function') {
                autoSelectTestSuite(testType);
                debugLog(`[goToTestCase] 已自动匹配测试套件: ${testType}`);
            }

            showToast(`已跳转到测试界面，请选择设备后开始测试`, 'success');
        }, 200);
    } catch (error) {
        console.error('[goToTestCase] Error:', error);
        showToast('跳转失败: ' + error.message, 'error');
    }
}

// Redmine 回复对话框
function openRedmineReplyModal(moduleName, testCaseName, failureIndex, issueIdFromReport) {
    const modalId = 'redmine-reply-modal-' + Date.now();
    const issueInputId = `${modalId}-issue-id`;
    const replyTextId = `${modalId}-reply-text`;
    const fileInputId = `${modalId}-files`;
    const fileListId = `${modalId}-file-list`;
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
                    <input type="text" id="${issueInputId}" data-redmine-issue-input value="${issueIdFromReport}" placeholder="输入 Redmine Issue ID"
                           style="width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--darker-bg); color: var(--text-primary); font-size: 14px; font-family: 'Courier New', monospace;">
                </div>
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">回复内容</label>
                    <textarea id="${replyTextId}" data-redmine-reply-text rows="10" placeholder="输入回复内容..."
                              style="width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--darker-bg); color: var(--text-primary); font-size: 13px; font-family: 'Courier New', monospace; white-space: pre-wrap; resize: vertical;">${defaultReply}</textarea>
                </div>
                <div style="margin-bottom: 16px;">
                    <label style="display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--text-primary);">📎 附件</label>
                    <input type="file" id="${fileInputId}" data-redmine-files multiple
                           style="display: none;"
                           onchange="updateRedmineFileList('${fileInputId}', '${fileListId}')">
                    <div id="${fileInputId}-drop" class="redmine-drop-zone" data-redmine-drop
                         onclick="document.getElementById('${fileInputId}').click()"
                         style="padding: 20px 14px; background: var(--secondary-bg); color: var(--text-muted); border: 2px dashed var(--border-color); border-radius: 6px; cursor: pointer; font-size: 12px; width: 100%; text-align: center; transition: all 0.2s; user-select: none;">
                        📎 拖拽文件到此处，或点击选择文件
                    </div>
                    <div id="${fileListId}" style="margin-top: 8px;"></div>
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

    // 绑定拖拽事件
    const dropZone = document.getElementById(`${fileInputId}-drop`);
    if (dropZone) {
        dropZone.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.add('drag-over'); });
        dropZone.addEventListener('dragleave', (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.remove('drag-over'); });
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault(); e.stopPropagation();
            dropZone.classList.remove('drag-over');
            if (!e.dataTransfer?.files?.length) return;
            const input = document.getElementById(fileInputId);
            const dt = new DataTransfer();
            if (input.files) { for (const f of input.files) dt.items.add(f); }
            for (const f of e.dataTransfer.files) dt.items.add(f);
            input.files = dt.files;
            updateRedmineFileList(fileInputId, fileListId);
        });
    }
    return modalId;
}

function updateRedmineFileList(fileInputId, fileListId) {
    const input = document.getElementById(fileInputId);
    const container = document.getElementById(fileListId);
    if (!input || !container) return;
    const files = input.files;
    if (!files || !files.length) { container.innerHTML = ''; return; }
    container.innerHTML = Array.from(files).map((f, i) => {
        const size = f.size >= 1048576 ? (f.size / 1048576).toFixed(1) + ' MB' : (f.size / 1024).toFixed(0) + ' KB';
        return `<div class="redmine-file-item">
            <span class="redmine-file-name">📎 ${escapeHtml(f.name)} <span class="redmine-file-size">(${size})</span></span>
            <span class="redmine-file-remove" onclick="removeRedmineFile('${fileInputId}', '${fileListId}', ${i})">✕</span>
        </div>`;
    }).join('');
}

function removeRedmineFile(fileInputId, fileListId, index) {
    const input = document.getElementById(fileInputId);
    if (!input) return;
    const dt = new DataTransfer();
    const files = input.files;
    for (let i = 0; i < files.length; i++) {
        if (i !== index) dt.items.add(files[i]);
    }
    input.files = dt.files;
    updateRedmineFileList(fileInputId, fileListId);
}

// 确认并发送 Redmine 回复
async function confirmAndSendRedmineReply(modalId) {
    const modal = document.getElementById(modalId);
    const issueId = modal?.querySelector('[data-redmine-issue-input]')?.value?.trim();
    const replyText = modal?.querySelector('[data-redmine-reply-text]')?.value?.trim();
    const fileInput = modal?.querySelector('[data-redmine-files]');

    if (!issueId) {
        showToast('❌ 请输入 Redmine Issue ID', 'error');
        return;
    }

    if (!replyText) {
        showToast('❌ 回复内容不能为空', 'error');
        return;
    }

    const files = fileInput?.files;
    const hasFiles = files && files.length > 0;

    // 立即关闭弹窗，提升响应速度
    ModalManager.close(modalId);
    const attachHint = hasFiles ? `（含 ${files.length} 个附件）` : '';
    showToast('📤 正在发送回复' + attachHint + '...', 'info');

    // 构建 FormData
    const formData = new FormData();
    formData.append('issue_id', issueId);
    formData.append('reply_text', replyText);
    if (hasFiles) {
        for (const f of files) {
            formData.append('files', f);
        }
    }

    // 异步发送请求，不阻塞 UI
    fetch('/api/redmine/reply', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(result => {
        if (result.success) {
            const replyData = result.data || {};
            const attachMsg = replyData.attachments ? `，携带 ${replyData.attachments} 个附件` : '';
            showToast(`✅ 回复已成功发送到 Redmine #${issueId}${attachMsg}`, 'success');
            if (replyData.issue_url) {
                setTimeout(() => window.open(replyData.issue_url, '_blank', 'noopener'), 800);
            }
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
    const resultDiv = ensureReportAnalysisResultStructure();
    const uploadZone = $('report-upload-zone');
    const summaryDiv = $('report-summary');
    const detailsDiv = $('report-details');
    const failuresDiv = $('report-failures');
    const failureList = $('report-failure-list');

    // 清空分析结果但保留容器结构。
    if (resultDiv) resultDiv.style.display = 'none';
    if (summaryDiv) summaryDiv.innerHTML = '';
    if (detailsDiv) detailsDiv.innerHTML = '';
    if (failuresDiv) failuresDiv.style.display = 'none';
    if (failureList) failureList.innerHTML = '';

    // Reset upload zone to empty state
    if (uploadZone) {
        uploadZone.classList.add('upload-empty');
        const content = uploadZone.querySelector('.report-upload-content');
        if (content) content.style.opacity = '1';
    }

    debugLog('[resetReportAnalysis] Report analysis reset complete');
}

/**
 * 将当前报告分析结果作为 HTML 邮件发送。
 * 复用 POST /api/email/send（SMTP 配置来自 Redmine 看板设置）。
 */
async function sendReportAnalysisEmail() {
    const data = window.currentReportAnalysisData;
    if (!data || !data.summary) {
        showToast('请先生成报告分析结果', 'warning');
        return;
    }
    const to = prompt('收件人邮箱（多个用逗号或分号分隔）：', '');
    if (!to || !to.trim()) return;
    const cc = (prompt('抄送（可留空，多个用逗号或分号分隔）：', '') || '').trim();

    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');

    const s = data.summary || {};
    const d = data.details || {};
    const reportName = data.report_name || data.test_result?.test_name || '测试报告';
    const rows = [
        ['测试类型', d.test_type],
        ['套件版本', d.suite_version],
        ['Android版本', d.android_version],
        ['SOC平台', d.soc_platform],
        ['总用例数', s.total],
        ['通过', s.pass],
        ['失败', s.fail],
        ['通过率', s.pass_rate],
    ].filter(([, v]) => v !== undefined && v !== null && v !== '');

    const summaryHtml = rows.map(([k, v]) =>
        `<tr><td style="padding:4px 12px 4px 0;color:#666;">${esc(k)}</td><td style="padding:4px 0;"><b>${esc(v)}</b></td></tr>`
    ).join('');

    const failures = Array.isArray(data.failures) ? data.failures : [];
    const failureHtml = failures.length ? `
        <h3 style="margin:18px 0 8px;">❌ 失败用例（${failures.length}）</h3>
        ${failures.map((f, i) => `
            <div style="border:1px solid #eee;border-radius:6px;padding:10px;margin-bottom:8px;">
                <div><b>${i + 1}. ${esc(f.name || '未知用例')}</b> <span style="color:#888;">[${esc(f.module || '未知模块')}]</span></div>
                <pre style="white-space:pre-wrap;background:#fafafa;padding:8px;margin-top:6px;font-size:12px;border-radius:4px;">${esc(f.reason || '无失败原因')}</pre>
            </div>
        `).join('')}
    ` : '<p style="color:#888;">无失败用例 🎉</p>';

    const body = `
        <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222;max-width:760px;">
            <h2 style="margin:0 0 12px;">📊 测试报告分析：${esc(reportName)}</h2>
            <table style="border-collapse:collapse;font-size:13px;">${summaryHtml}</table>
            ${failureHtml}
        </div>`;

    const subject = `测试报告分析 - ${reportName}（通过率 ${s.pass_rate || 'N/A'}）`;
    try {
        const resp = await fetch('/api/email/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                to: to.trim(),
                cc: cc || undefined,
                subject,
                body,
                is_html: true,
                sender_name: '报告分析',
            }),
        });
        const result = await resp.json().catch(() => ({ success: false }));
        if (result.success) {
            showToast(`邮件已发送至 ${result.data.to.length} 位收件人`, 'success');
        } else {
            showToast('邮件发送失败：' + (result.error || '未知错误'), 'error');
        }
    } catch (err) {
        showToast('邮件发送失败：' + (err.message || err), 'error');
    }
}

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

// API 文档常量。
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

/**
 * Generate curl command for an API endpoint
 * Moved to module level to avoid recreating on every render
 */
function generateCurlCommand(api, details) {
    const apiPath = api.path || '';
    if (api.method === 'GET') {
        if (apiPath === '/api/system/skills/install.sh') {
            const command = buildSkillInstallCommand();
            return {display: command, full: command};
        }
        // 特殊处理stream端点：使用 -N 而不是 -s
        const isStreamEndpoint = apiPath.includes('/api/test/logs/stream');
        // ZIP 离线包使用 -OJ；安装脚本在上方生成可直接执行的管道命令。
        const isDownloadEndpoint = apiPath === '/api/system/skills';

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
        // 包含文件参数时使用 FormData。
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

    // 批量拼接 HTML。
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
 * 生成与当前 Controller 地址绑定的一键安装命令。
 */
function buildSkillInstallCommand() {
    const insecureOption = window.location.protocol === 'https:' ? '-k ' : '';
    return `curl ${insecureOption}-fsSL "${window.location.origin}/api/system/skills/install.sh" | bash`;
}

/**
 * 复制一键安装/更新命令。浏览器不能代替目标 Linux 主机执行该命令。
 */
function copySkillInstallCommand() {
    copyText(buildSkillInstallCommand(), {
        successMsg: '✓ Skill 一键安装/更新命令已复制',
    });
}

/**
 * 下载 Skill ZIP 离线包（不执行安装和命令链接迁移）。
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
        showToast('离线包已下载；在线安装请使用“安装/更新命令”', 'success');
    } catch (e) {
        console.error('[downloadSkillsZip] Error:', e);
        showToast('下载失败：' + e.message, 'error');
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
window.apkPendingOpenTarget = null;
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

    if (uploadZone.dataset.initialized === 'true') return;
    uploadZone.dataset.initialized = 'true';

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

    if ('Notification' in window && Notification.permission === 'default' && !state.browserNotificationsEnabled) {
        void requestBrowserNotificationPermission();
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
            if (window.apkPendingOpenTarget?.filePath) {
                const target = window.apkPendingOpenTarget;
                window.apkPendingOpenTarget = null;
                setTimeout(() => {
                    openApkPendingSourceTarget(target)
                        .then(file => enhanceReportDiagnosisWithSource(file?.path || target.filePath, file?.content || ''))
                        .catch(() => {});
                }, 200);
            }
            if (window.apkNotifiedTaskId !== window.apkCurrentTaskId) {
                window.apkNotifiedTaskId = window.apkCurrentTaskId;
                notifyOperationResult(
                    'APK反编译已完成',
                    status.filename || '反编译完成，可查看结果',
                    'success',
                    'apk',
                    { task_id: window.apkCurrentTaskId }
                );
            }
        } else if (status.status === 'error') {
            stopApkPolling();
            window.apkPendingOpenTarget = null;

            showToast(`分析失败: ${status.error}`, 'error');
            if (window.apkNotifiedTaskId !== window.apkCurrentTaskId) {
                window.apkNotifiedTaskId = window.apkCurrentTaskId;
                notifyOperationResult(
                    'APK分析失败',
                    status.error || '反编译失败',
                    'error',
                    'apk',
                    {
                        task_id: window.apkCurrentTaskId
                    }
                );
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
    const fragment = document.createDocumentFragment();
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

        fragment.appendChild(itemDiv);
    });
    container.appendChild(fragment);
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

    viewer.style.display = window.apkOpenFiles.size ? 'flex' : 'none';
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

// Java 语法高亮常量。
const JAVA_KEYWORDS = new Set([
    'abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch', 'char', 'class',
    'const', 'continue', 'default', 'do', 'double', 'else', 'enum', 'extends', 'final',
    'finally', 'float', 'for', 'goto', 'if', 'implements', 'import', 'instanceof', 'int',
    'interface', 'long', 'native', 'new', 'package', 'private', 'protected', 'public',
    'return', 'short', 'static', 'strictfp', 'super', 'switch', 'synchronized', 'this',
    'throw', 'throws', 'transient', 'try', 'void', 'volatile', 'while', 'true', 'false',
    'null'
]);
const JAVA_IDENTIFIER_RE = /[A-Za-z_$][A-Za-z0-9_$]*/g;

function renderApkCodeContent(content, filePath) {
    const source = String(content || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const lightMode = source.length > 300000;
    const lines = source.split('\n');

    return lines.map((line, index) => {
        const lineNo = index + 1;
        let html;
        if (lightMode) {
            html = escapeHtml(line);
        } else {
            html = '';
            let lastIndex = 0;
            JAVA_IDENTIFIER_RE.lastIndex = 0;
            let match;
            while ((match = JAVA_IDENTIFIER_RE.exec(line)) !== null) {
                html += escapeHtml(line.slice(lastIndex, match.index));
                const token = match[0];
                if (JAVA_KEYWORDS.has(token)) {
                    html += `<span class="apk-code-keyword">${escapeHtml(token)}</span>`;
                } else {
                    html += `<span class="apk-code-symbol" data-symbol="${escapeHtml(token)}">${escapeHtml(token)}</span>`;
                }
                lastIndex = match.index + token.length;
            }
            html += escapeHtml(line.slice(lastIndex));
        }

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
        return existingFile;
    }

    window.apkOpenFiles.set(filePath, { loading: true });
    activateApkFileTab(filePath);

    try {
        const data = await apiCall(`/api/apk/source/${window.apkCurrentTaskId}?path=${encodeURIComponent(filePath)}&view=true`);

        if (data.success) {
            window.apkOpenFiles.set(filePath, {
                loading: false,
                content: data.data.content,
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
    return window.apkOpenFiles.get(filePath);
}

async function openApkPendingSourceTarget(target) {
    const paths = Array.from(new Set([
        target?.filePath,
        ...(Array.isArray(target?.fallbackPaths) ? target.fallbackPaths : [])
    ].filter(Boolean)));
    if (paths.length) {
        switchApkTab('source');
    }
    let lastFile = null;
    for (const filePath of paths) {
        const file = await viewApkFileAt(filePath, target?.line || null);
        lastFile = file ? { ...file, path: filePath } : null;
        if (file && !file.error) return lastFile;
        if (paths.length > 1 && window.apkOpenFiles?.has(filePath)) {
            window.apkOpenFiles.delete(filePath);
        }
    }
    if (lastFile?.path) {
        window.apkOpenFiles.set(lastFile.path, lastFile);
        activateApkFileTab(lastFile.path, target?.line || null);
    }
    if (paths.length) {
        showToast(`未能自动打开源码: ${paths[0]}`, 'warning');
    }
    return lastFile;
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
    if (!window.apkOpenFiles || typeof window.apkOpenFiles.clear !== 'function') {
        window.apkOpenFiles = new Map();
    } else {
        window.apkOpenFiles.clear();
    }
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
    window.apkPendingOpenTarget = null;
    window.apkLastSearchMatches = [];

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

let securityAuditState = {
    offset: 0,
    limit: 100,
    loading: false,
    hasMore: false,
    currentFilterParams: null,
    recordsCache: []
};

function getSecurityAuditFilterParams() {
    const params = new URLSearchParams();
    params.set('limit', String(securityAuditState.limit));
    params.set('offset', String(securityAuditState.offset));

    const source = $('audit-source-filter')?.value || '';
    const actionType = $('audit-type-filter')?.value || '';
    const query = $('audit-search-input')?.value?.trim() || '';

    if (source) params.set('source', source);
    if (actionType) params.set('action_type', actionType);
    if (query) params.set('q', query);
    return params;
}

async function loadSecurityAudit(reset = false) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (securityAuditState.loading) return;
    securityAuditState.loading = true;

    if (reset) {
        securityAuditState.offset = 0;
        securityAuditState.recordsCache = [];
        securityAuditState.hasMore = false;
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                    加载中...
                </td>
            </tr>
        `;
    }

    try {
        const params = getSecurityAuditFilterParams();
        securityAuditState.currentFilterParams = params.toString();
        const result = await apiCall(`/api/security-audit/logs?${params.toString()}`);
        const payload = result.data || {};
        const fetchedRecords = payload.records || [];
        securityAuditState.hasMore = payload.has_more || false;

        if (reset) {
            securityAuditState.recordsCache = fetchedRecords;
            renderSecurityAuditRows(securityAuditState.recordsCache);
        } else {
            securityAuditState.recordsCache.push(...fetchedRecords);
            appendSecurityAuditRows(fetchedRecords);
        }
        updateSecurityAuditStats(payload.stats || {});
    } catch (error) {
        const needElevation = error.status === 403 && !state.elevated;
        if (reset) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="padding: 40px; text-align: center; color: var(--text-secondary);">
                        ${needElevation
                            ? '🔒 此页面需要管理员权限，请点击右上角提权后查看。'
                            : `加载失败: ${escapeHtml(error.message)}`}
                    </td>
                </tr>
            `;
        } else {
            if (!needElevation) showToast('加载更多审计记录失败: ' + error.message, 'error');
        }
    } finally {
        securityAuditState.loading = false;
        updateSecurityAuditLoadMoreButton();
    }
}

function updateSecurityAuditLoadMoreButton() {
    const wrapper = $('audit-load-more-wrapper');
    if (!wrapper) return;
    if (securityAuditState.hasMore) {
        wrapper.innerHTML = `
            <button class="btn-xs" id="audit-load-more-btn" onclick="loadMoreSecurityAudit()">
                加载更多
            </button>
        `;
    } else {
        wrapper.innerHTML = '';
    }
}

async function loadMoreSecurityAudit() {
    if (!securityAuditState.hasMore || securityAuditState.loading) return;
    securityAuditState.offset += securityAuditState.limit;
    await loadSecurityAudit(false);
}

function appendSecurityAuditRows(records) {
    const tbody = $('security-audit-table-body');
    if (!tbody) return;

    if (!records.length) return;

    const html = buildSecurityAuditRowsHtml(records);
    const temp = document.createElement('tbody');
    temp.innerHTML = html;
    while (temp.firstChild) {
        tbody.appendChild(temp.firstChild);
    }

    tbody.querySelectorAll('[data-audit-id]').forEach(row => {
        row.addEventListener('click', () => showSecurityAuditDetail(row.dataset.auditId));
    });
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

    tbody.innerHTML = buildSecurityAuditRowsHtml(records);

    tbody.querySelectorAll('[data-audit-id]').forEach(row => {
        row.addEventListener('click', () => showSecurityAuditDetail(row.dataset.auditId));
    });
}

function buildSecurityAuditRowsHtml(records) {
    return records.map(record => {
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
        <div class="modal-content" style="width: min(980px, 92vw); max-width: min(980px, 92vw); max-height: 88vh; overflow: hidden; display: flex; flex-direction: column;">
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
    return modal;
}

function closeSecurityAuditDetailModal() {
    ModalManager.close('security-audit-detail-modal');
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
    ModalManager.open('security-audit-detail-modal');
    body.innerHTML = '加载中...';

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

async function exportSecurityAudit() {
    const granted = await requestElevatedAccess('导出安全审计日志');
    if (!granted) return;
    try {
        await apiCall('/api/security-audit/verify');
    } catch (_error) {
        return;
    }
    window.open('/api/security-audit/export', '_blank');
}

window.recordSecurityPageView = recordSecurityPageView;
window.loadSecurityAudit = loadSecurityAudit;
window.showSecurityAuditDetail = showSecurityAuditDetail;
window.closeSecurityAuditDetailModal = closeSecurityAuditDetailModal;
window.exportSecurityAudit = exportSecurityAudit;

// ==================== APK 文件搜索功能 ====================

let apkSearchDebounceTimer = null;

async function filterApkFiles() {
    const query = $('apk-file-search')?.value?.toLowerCase() || '';
    const resultsEl = $('apk-search-results');

    if (!query || query.length < 2) {
        if (resultsEl) resultsEl.style.display = 'none';
        return;
    }

    let matches = [];
    if (window.apkCurrentTaskId) {
        try {
            const data = await apiCall(`/api/apk/search/${window.apkCurrentTaskId}?q=${encodeURIComponent(query)}&limit=20`);
            matches = data.success ? (data.data.items || []) : [];
        } catch (e) {
            debugLog('[APK Search] backend search failed:', e.message);
        }
    }
    window.apkLastSearchMatches = matches;

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
        item.innerHTML = `<span class="apk-search-result-name">${escapeHtml(file.name)}</span><span class="apk-search-result-path">${escapeHtml(file.path)}</span>`;
        resultsEl.appendChild(item);
    }
    resultsEl.style.display = 'block';

    // 定位搜索结果到搜索框下方，宽度与输入框一致
    const searchEl = $('apk-file-search');
    if (searchEl && resultsEl) {
        const rect = searchEl.getBoundingClientRect();
        resultsEl.style.position = 'absolute';
        resultsEl.style.top = (rect.bottom + window.scrollY) + 'px';
        resultsEl.style.left = (rect.left + window.scrollX) + 'px';
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
        const matches = window.apkLastSearchMatches || [];
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
