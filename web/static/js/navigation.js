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
// APK 分析任务状态轮询间隔（毫秒）
const STATUS_POLL_INTERVAL = 5000;
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
                if (userData.user) state.currentUser = userData.user;
                debugLog('[Init] ✅ Set state.clientId from /api/users/current:', state.clientId);

                // 检查是否是 unknown 用户（apiCall 中会统一处理弹框）
                if (userData.client_id.startsWith('unknown@')) {
                    debugLog('[Init] Detected unknown client, will show username modal via apiCall');
                } else {
                    loadNotifications();
                    // 已获取到正确的用户名，延迟检查 USB/IP 和 VPN 状态（避免阻塞关键请求）
                    setTimeout(() => {
                        const statusChecks = [checkUsbipStatus(), checkVpnStatus()];
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

                    case 'event':
                        handleServerEvent(data.event, data.payload);
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

// ==================== Server Event Handler ====================
// Dispatches resource events pushed by the backend EventBus through WebSocket.
// Each handler refreshes the relevant UI state without a full polling cycle.

function handleServerEvent(eventType, payload) {
    debugLog('[EventBus] Received:', eventType, payload);
    switch (eventType) {
        case 'worker.updated':
            // Refresh the cluster worker list instead of polling.
            // loadClusterWorkers already has in-flight de-duplication.
            if (typeof loadClusterWorkers === 'function') {
                loadClusterWorkers().catch(() => {});
            }
            break;
        case 'job.transition':
            // Wake the test status poller so it picks up the new job state
            // immediately rather than waiting for the next interval.
            if (payload && payload.job_id && state.clusterJobId === payload.job_id) {
                wakeTestStatusPolling();
            }
            // Refresh devices to reflect allocation changes.
            if (typeof loadDevices === 'function') {
                loadDevices(true, {silent: true}).catch(() => {});
            }
            break;
        case 'device_lock.changed':
            // The existing device_lock_update WS message already handles
            // per-device lock rendering; this is a supplementary trigger
            // for pages that don't show lock_update messages.
            break;
        default:
            debugLog('[EventBus] Unknown event type:', eventType);
    }
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
