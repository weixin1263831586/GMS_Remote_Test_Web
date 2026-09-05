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
const usbipSourceOsByHost = new Map();
const usbipAssignedBusidsBySource = new Map();
let pendingDevicePasswordAction = 'usbip';
let pendingDevicePasswordRetry = null;
let usbipReconnectTimer = null;
let usbipReconnectAttempts = 0;
let usbipManualDisconnectUntil = 0;
let usbipReconnectWaiting = false;
let usbipOperationGeneration = 0;
let adbProxyStatus = null;
// adbProxyOperationRunning 已迁移到 state.js（先于本文件加载）声明。
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

function validateRebootDeviceSelection() {
    if (state.selectedDevices.size === 0) {
        showToast('请先选择设备', 'warning');
        return false;
    }
    const unavailable = Array.from(state.selectedDevices).filter(deviceId => {
        const device = state.devices.find(item => {
            const id = typeof item === 'string' ? item : item.device_id;
            return id === deviceId;
        });
        return device && !isSelectableRebootDevice(device);
    });
    if (unavailable.length > 0) {
        showToast(`所选设备当前不可执行重启操作: ${unavailable.join(', ')}`, 'warning');
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

function scheduleDeferredPagePreload(initialPage) {
    const preload = () => {
        if (initialPage !== 'terminal' && typeof loadXTermScripts === 'function') {
            loadXTermScripts().catch(error =>
                debugLog('[Preload] xterm assets unavailable:', error));
        }
        if (typeof window.loadClusterHostDirectory === 'function') {
            window.loadClusterHostDirectory().catch(error =>
                debugLog('[Preload] host directory unavailable:', error));
        }
        if (initialPage !== 'reports' && typeof window.preloadTestReports === 'function') {
            window.preloadTestReports(false).catch(error =>
                debugLog('[Preload] reports unavailable:', error));
        }
    };
    setTimeout(() => {
        if (typeof window.requestIdleCallback === 'function') {
            window.requestIdleCallback(preload, {timeout: 3000});
        } else {
            preload();
        }
    }, 1000);
}

async function continueAppInitialization() {
    if (_appInitStarted) return;
    _appInitStarted = true;
    const initialPage = window.__targetPage || 'test';
    const needsTestWorkspace = initialPage === 'test';
    const latencySensitivePage = ['test', 'desktop', 'terminal', 'reports'].includes(initialPage);
    // 文件浏览和传输功能依赖服务端路径配置。
    const configReady = loadConfig();
    // 首屏敏感页面只依赖模板中的主机配置，可与完整配置读取并行初始化。
    if (!latencySensitivePage) await configReady;
    // 通知页面恢复逻辑认证状态已就绪。
    window.dispatchEvent(new CustomEvent('gms:auth-ready'));

    initEventListeners();
    initDragDrop();
    renderNotificationList();

    // 非阻塞加载OpenGrok配置（不等待，让它在后台加载）
    OPENGROK_CONFIG.init();

    const clusterModeReady = initializeClusterMode();
    if (needsTestWorkspace) {
        void window.GmsWorkspace.loadInitialTestData(clusterModeReady);
    }

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

    await Promise.all([clusterModeReady, configReady]);

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
    scheduleDeferredPagePreload(initialPage);

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

    // 用户列表仅在用户管理页按需加载（shell.html loadUsersList）；
    // /api/users/list 需要管理员提权，启动时后台预取只会在每个新
    // 标签页触发一次 403 和提权弹框，因此不做启动预取。
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
        // 初始化链路读取配置：未登录时静默失败，不弹登录层。
        const config = await apiCall('/api/config/read', 'GET', null, {background: true});
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

// WebSocket lifecycle lives in shell/websocket-manager.js.
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

    // Device host 输入框为 readonly（配置在 config.json），无需确认处理；
    // local server 仍支持 Enter 确认更新。
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
    // 特殊处理：GSI使用CTS的测试套件，GTS-ROOT和APTS使用GTS的测试套件
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
    } else if (testTypeLower === 'apts') {
        // APTS使用GTS套件（APTS测试通过gts-tradefed执行）
        matchingSuites = testSuitesCache.filter(suite =>
            suite.test_type.toLowerCase() === 'gts'
        );
        addLogEntry('APTS使用GTS测试套件', 'info');
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

function renderUserRemoveCell(user, normalizedStatus) {
    const removable = user.removable !== undefined
        ? Boolean(user.removable)
        : Boolean(user.configured && normalizedStatus !== 'testing');
    const reason = user.removal_reason || (normalizedStatus === 'testing'
        ? '用户正在测试中，结束测试后才能移除'
        : (user.source === 'cluster'
            ? '集群任务所有者不是客户端配置，不能在此移除'
            : '临时在线会话没有持久配置，断开后会自动清理'));
    if (removable) {
        return `<button class="btn-xxs user-remove-button" data-remove-user="${escapeHtml(user.ip || '')}">移除</button>`;
    }
    return `<button class="btn-xxs user-remove-button" type="button" disabled aria-disabled="true" title="${escapeHtml(reason)}">移除</button>`;
}

// Cluster workspace/device inventory lives in shell/workspace-devices.js.
// ==================== 全局函数暴露 ====================
// 将 HTML onclick 需要的函数暴露到 window 对象
window.refreshDevices = refreshDevices;
window.refreshTestSuites = refreshTestSuites;
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

// Set only after the complete navigation bundle has evaluated successfully.
window.GmsNavigationReady = true;
