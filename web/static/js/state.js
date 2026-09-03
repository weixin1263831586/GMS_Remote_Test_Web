// Shared application state and lightweight global helpers.

const state = {
    connected: false,
    testing: false,
    testStopping: false,
    devices: [],
    selectedDevices: new Set(),
    socket: null,
    sshConnected: false,
    vpnConnected: null,
    adbForwardRunning: false,
    usbipConnected: false,
    clientId: null,
    clientDisplayId: null,
    currentUser: null,
    authRequired: false,
    authSetupRequired: false,
    // null 表示后端版本尚未声明；首次初始化时按“需要令牌”安全降级。
    authBootstrapTokenRequired: null,
    authReady: false,
    // 用户手动关闭登录层后置 true：后台轮询/初始化中的 401 不再自动
    // 弹回登录层（否则刚关闭就被重新拉起）；用户主动操作触发 401 时
    // 会清除此标记并重新弹出。
    authGateDismissed: false,
    usernameDetectShown: false,
    // 敏感操作使用当前会话的管理员二次认证状态。
    // Populated from /api/auth/status (elevated/elevated_until).
    elevated: false,
    elevatedUntil: null,
    config: null,
    fileBrowser: { currentPath: '', selectedFile: null, targetInputId: null, mode: null },
    gsiVendorFile: null,
    suiteBrowser: { selectedSuitePath: '', currentPath: '', highlightPath: '', suiteRoot: '' },
    // 设备分组定义、视图开关和当前筛选。
    deviceGroups: [],
    deviceGroupsLoaded: false,
    groupView: localStorage.getItem('gms_group_view') === '1',
    groupFilter: '',
    // 主页 ADB 区：是否只显示"关注"分组里的设备（无关注分组时显示全部）
    followFilter: localStorage.getItem('gms_follow_filter') !== '0',
    collapsedGroups: new Set(JSON.parse(localStorage.getItem('gms_collapsed_groups') || '[]')),
    domCache: {},
    lastLogCount: 0,
    wsLogStallTicks: 0,
    currentLogTab: sessionStorage.getItem('gms_test_log_tab') === 'module'
        ? 'module'
        : 'system',
    pendingDeviceRefresh: null,
    deviceRefreshPromise: null,
    isRefreshingDevices: false,
    notifications: [],
    unreadNotifications: 0,
    browserNotificationsEnabled:
        (typeof Notification !== 'undefined' && Notification.permission === 'granted') ||
        localStorage.getItem('gms_browser_notifications') === 'true'
};

const DEBUG = false;

// ADB Proxy 操作互斥标志。
// 注意：workspace-devices.js / firmware-burn.js 在 navigation.js 之前加载并
// 会读写该变量，声明必须放在更早加载的 state.js，否则页面切换时引用
// 尚未初始化的全局词法绑定会抛 ReferenceError。
let adbProxyOperationRunning = false;

function $(id) {
    const cached = state.domCache[id];
    if (cached) {
        if (cached.isConnected) return cached;
        delete state.domCache[id];
    }
    const el = document.getElementById(id);
    if (el) state.domCache[id] = el;
    return el;
}

function clearDomCache() {
    state.domCache = {};
}

function debugLog(...args) {
    if (DEBUG) {
        console.log(...args);
    }
}

window.state = state;
window.$ = $;
window.clearDomCache = clearDomCache;
window.debugLog = debugLog;
