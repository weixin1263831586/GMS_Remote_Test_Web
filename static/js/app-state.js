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
    usernameDetectShown: false,
    config: null,
    fileBrowser: { currentPath: '', selectedFile: null, targetInputId: null, mode: null },
    suiteBrowser: { selectedSuitePath: '', currentPath: '', highlightPath: '' },
    domCache: {},
    lastLogCount: 0,
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
