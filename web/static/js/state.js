// Shared application state and lightweight global helpers.

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
    clientId: null,
    clientDisplayId: null,
    currentUser: null,
    authReady: false,
    usernameDetectShown: false,
    // Temporary admin elevation for sensitive operations (remove user/device).
    // Populated from /api/auth/status (elevated/elevated_until).
    elevated: false,
    elevatedUntil: null,
    config: null,
    fileBrowser: { currentPath: '', selectedFile: null, targetInputId: null, mode: null },
    gsiVendorFile: null,
    suiteBrowser: { selectedSuitePath: '', currentPath: '', highlightPath: '', suiteRoot: '' },
    // 设备分组：groups 是分组定义数组；groupView 开关分组视图；groupFilter 筛选的组 id（''=全部）
    deviceGroups: [],
    deviceGroupsLoaded: false,
    groupView: localStorage.getItem('gms_group_view') === '1',
    groupFilter: '',
    // 主页 ADB 区：是否只显示"关注"分组里的设备（无关注分组时显示全部）
    followFilter: localStorage.getItem('gms_follow_filter') !== '0',
    collapsedGroups: new Set(JSON.parse(localStorage.getItem('gms_collapsed_groups') || '[]')),
    domCache: {},
    lastLogCount: 0,
    currentLogTab: 'system',
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
